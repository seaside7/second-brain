"""Base adapter contract and MeetingRecord schema."""


class MeetingRecord:
    """Normalized meeting record. Every adapter produces this."""

    def __init__(self, data):
        self.id = data.get('id', '')
        self.workspace = data.get('workspace', '')
        self.source = data.get('source', '')
        self.source_id = data.get('source_id', '')
        self.source_url = data.get('source_url', '')
        self.title = data.get('title', '')
        self.date = data.get('date', '')
        self.start_time = data.get('start_time', '')
        self.end_time = data.get('end_time', '')
        self.duration_minutes = data.get('duration_minutes', 0)
        self.attendees = data.get('attendees', [])
        self.transcript = data.get('transcript', [])
        self.summary = data.get('summary', '')
        self.action_items = data.get('action_items', [])
        self.fetched_at = data.get('fetched_at', '')
        self.stored_path = data.get('stored_path', '')

    def to_dict(self):
        return {
            'id': self.id,
            'workspace': self.workspace,
            'source': self.source,
            'source_id': self.source_id,
            'source_url': self.source_url,
            'title': self.title,
            'date': self.date,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'duration_minutes': self.duration_minutes,
            'attendees': self.attendees,
            'transcript': self.transcript,
            'summary': self.summary,
            'action_items': self.action_items,
            'fetched_at': self.fetched_at,
            'stored_path': self.stored_path,
        }


class MeetingSourceAdapter:
    """Base contract for meeting source adapters.

    Every adapter must implement fetch_recent and optionally fetch_one.
    """

    def fetch_recent(self, workspace_ctx, since=None, limit=10):
        """Fetch recent meetings from the source.

        Args:
            workspace_ctx: WorkspaceContext from workspace_resolver
            since: ISO date string (only fetch meetings after this date)
            limit: max meetings to return

        Returns:
            list[MeetingRecord]
        """
        raise NotImplementedError

    def fetch_one(self, workspace_ctx, meeting_id):
        """Fetch a specific meeting by source ID.

        Returns:
            MeetingRecord or None
        """
        raise NotImplementedError
