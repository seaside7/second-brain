"""Fathom adapter: fetches meetings from the Fathom API and normalizes to MeetingRecord."""
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

from .base import MeetingRecord, MeetingSourceAdapter

WIB = timezone(timedelta(hours=7))
FATHOM_BASE_URL = "https://api.fathom.ai/external/v1"
DEFAULT_TIMEOUT = 60


class FathomAdapter(MeetingSourceAdapter):
    """Fetch meetings from the Fathom API using workspace-specific credentials."""

    def _load_api_key(self, workspace_ctx):
        """Load FATHOM_API_KEY from workspace fathom.env."""
        env = workspace_ctx.load_env('fathom')
        key = env.get('FATHOM_API_KEY', '')
        if not key:
            # Fallback: old location
            old_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                '..', '..', '..', '..', 'fathom-connector', 'token.env')
            old_path = os.path.normpath(old_path)
            if os.path.exists(old_path):
                with open(old_path, encoding='utf-8') as f:
                    for line in f:
                        if line.strip().startswith('FATHOM_API_KEY='):
                            key = line.split('=', 1)[1].strip()
                            break
        if not key:
            key = os.environ.get('FATHOM_API_KEY', '')
        return key

    def _request(self, api_key, endpoint, params=None):
        """Make a GET request to the Fathom API."""
        url = f"{FATHOM_BASE_URL}{endpoint}"
        if params:
            from urllib.parse import urlencode
            url += '?' + urlencode({k: v for k, v in params.items() if v is not None})

        headers = {
            'X-Api-Key': api_key,
            'Accept': 'application/json',
        }
        req = urllib.request.Request(url, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8')[:300]
            except Exception:
                pass
            print(f"[ERROR] Fathom API {e.code}: {detail}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"[ERROR] Fathom request failed: {e}", file=sys.stderr)
            return None

    def _normalize_meeting(self, raw, workspace_name):
        """Convert a raw Fathom meeting response to a MeetingRecord."""
        recording_id = str(raw.get('recording_id', ''))
        title = raw.get('title') or raw.get('meeting_title') or '(Untitled Meeting)'
        date_str = ''
        start_time = raw.get('recording_start_time') or raw.get('scheduled_start_time') or ''
        end_time = raw.get('recording_end_time') or raw.get('scheduled_end_time') or ''

        if start_time:
            try:
                dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                date_str = dt.astimezone(WIB).strftime('%Y-%m-%d')
            except Exception:
                date_str = start_time[:10] if len(start_time) >= 10 else ''

        # Duration
        duration = 0
        if start_time and end_time:
            try:
                s = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                e = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                duration = int((e - s).total_seconds() / 60)
            except Exception:
                pass

        # Attendees
        attendees = []
        for inv in raw.get('calendar_invitees', []):
            attendees.append({
                'name': inv.get('name', ''),
                'email': inv.get('email', ''),
                'is_owner': False,
            })

        # Transcript
        transcript = []
        for entry in raw.get('transcript', []):
            speaker = entry.get('speaker', {})
            transcript.append({
                'speaker': speaker.get('display_name', 'Unknown'),
                'text': entry.get('text', ''),
                'timestamp': entry.get('timestamp', ''),
            })

        # Summary
        summary_data = raw.get('default_summary', {})
        summary = ''
        if isinstance(summary_data, dict):
            summary = summary_data.get('markdown_formatted', '') or summary_data.get('text', '')
        elif isinstance(summary_data, str):
            summary = summary_data

        # Action items (structured, high confidence)
        action_items = []
        for item in raw.get('action_items', []):
            assignee = item.get('assignee', {}) or {}
            action_items.append({
                'description': item.get('description', ''),
                'assignee_name': assignee.get('name', ''),
                'assignee_email': assignee.get('email', ''),
                'completed': item.get('completed', False),
                'timestamp': item.get('recording_timestamp', ''),
                'playback_url': item.get('recording_playback_url', ''),
            })

        return MeetingRecord({
            'id': f"fathom:{workspace_name}:{recording_id}",
            'workspace': workspace_name,
            'source': 'fathom',
            'source_id': recording_id,
            'source_url': raw.get('url') or raw.get('share_url') or '',
            'title': title,
            'date': date_str,
            'start_time': start_time,
            'end_time': end_time,
            'duration_minutes': duration,
            'attendees': attendees,
            'transcript': transcript,
            'summary': summary,
            'action_items': action_items,
            'fetched_at': datetime.now(WIB).isoformat(timespec='seconds'),
            'stored_path': '',
        })

    def fetch_recent(self, workspace_ctx, since=None, limit=10):
        """Fetch recent meetings from Fathom."""
        api_key = self._load_api_key(workspace_ctx)
        if not api_key:
            print(f"[ERROR] No FATHOM_API_KEY for workspace '{workspace_ctx.name}'.",
                  file=sys.stderr)
            print(f"Add it to: .agent/workspaces/{workspace_ctx.name}/fathom.env",
                  file=sys.stderr)
            return []

        params = {
            'limit': limit,
            'include_transcript': 'true',
            'include_summary': 'true',
            'include_action_items': 'true',
        }
        if since:
            params['created_after'] = since

        data = self._request(api_key, '/meetings', params=params)
        if not data:
            return []

        items = data.get('items', [])
        records = []
        for raw in items:
            record = self._normalize_meeting(raw, workspace_ctx.name)
            records.append(record)

        return records

    def fetch_one(self, workspace_ctx, meeting_id):
        """Fetch a specific meeting by recording ID."""
        api_key = self._load_api_key(workspace_ctx)
        if not api_key:
            return None

        # Try to get from the list endpoint with the recording_id
        params = {
            'limit': 1,
            'include_transcript': 'true',
            'include_summary': 'true',
            'include_action_items': 'true',
        }
        data = self._request(api_key, '/meetings', params=params)
        if not data:
            return None

        for raw in data.get('items', []):
            if str(raw.get('recording_id', '')) == str(meeting_id):
                return self._normalize_meeting(raw, workspace_ctx.name)

        return None
