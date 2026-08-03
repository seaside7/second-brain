"""Manual adapter: ingest a markdown/text file as a meeting record."""
import os
import re
from datetime import datetime, timedelta, timezone

from .base import MeetingRecord, MeetingSourceAdapter

WIB = timezone(timedelta(hours=7))


class ManualAdapter(MeetingSourceAdapter):
    """Ingest manually provided meeting notes into a MeetingRecord."""

    def from_file(self, workspace_ctx, file_path, title, project='', attendees_str=''):
        """Create a MeetingRecord from a markdown/text file.

        Args:
            workspace_ctx: WorkspaceContext
            file_path: path to the notes file
            title: meeting title
            project: project name
            attendees_str: comma-separated attendee names
        """
        if not os.path.exists(file_path):
            return None

        with open(file_path, encoding='utf-8') as f:
            content = f.read()

        attendees = []
        if attendees_str:
            for name in attendees_str.split(','):
                name = name.strip()
                if name:
                    attendees.append({'name': name, 'email': '', 'is_owner': False})

        now = datetime.now(WIB)
        date_str = now.strftime('%Y-%m-%d')

        # Try to extract date from filename (YYYY-MM-DD pattern)
        basename = os.path.basename(file_path)
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', basename)
        if date_match:
            date_str = date_match.group(1)

        # Build transcript as a single block (manual notes don't have speaker labels)
        transcript = [{'speaker': 'Notes', 'text': content, 'timestamp': ''}]

        # Use content as both transcript and summary for manual notes
        # The extractor will process it
        summary = content[:2000] if len(content) > 2000 else content

        slug = re.sub(r'[^a-z0-9]+', '-', title.lower().strip()).strip('-')[:50]
        record_id = f"manual:{workspace_ctx.name}:{date_str}_{slug}"

        return MeetingRecord({
            'id': record_id,
            'workspace': workspace_ctx.name,
            'source': 'manual',
            'source_id': f"{date_str}_{slug}",
            'source_url': '',
            'title': title,
            'date': date_str,
            'start_time': '',
            'end_time': '',
            'duration_minutes': 0,
            'attendees': attendees,
            'transcript': transcript,
            'summary': summary,
            'action_items': [],  # Will be LLM-extracted by the extractor
            'fetched_at': now.isoformat(timespec='seconds'),
            'stored_path': '',
        })

    def fetch_recent(self, workspace_ctx, since=None, limit=10):
        """Manual adapter doesn't auto-fetch. Use from_file() directly."""
        return []

    def fetch_one(self, workspace_ctx, meeting_id):
        """Not applicable for manual adapter."""
        return None
