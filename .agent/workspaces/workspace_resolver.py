#!/usr/bin/env python3
"""
workspace_resolver.py — Single source of truth for multi-workspace credential resolution.

Every connector imports this module instead of hardcoding credential paths.
It reads workspaces.json, resolves the active (or requested) workspace, and
returns absolute paths to credentials, tokens, and env files.

Usage:
    import workspace_resolver as ws

    # Get active workspace context
    ctx = ws.get()

    # Get a specific workspace
    ctx = ws.get('samudera')

    # Access paths
    ctx.name              → 'catalyze'
    ctx.display_name      → 'Catalyze Communications'
    ctx.owner_email       → 'said@catalyze.id'
    ctx.domain            → 'catalyze.id'
    ctx.workspace_type    → 'freelance'
    ctx.credentials       → absolute path to credentials.json
    ctx.token('gmail')    → absolute path to token_gmail.json
    ctx.token('drive')    → absolute path to token_drive.json
    ctx.token('calendar') → absolute path to token_calendar.json
    ctx.env_file('gitlab')     → absolute path to gitlab.env
    ctx.env_file('mattermost') → absolute path to mattermost.env
    ctx.workspace_md      → absolute path to workspace.md
    ctx.dir               → absolute path to workspace folder
    ctx.load_env('gitlab')     → dict of key=value from gitlab.env
    ctx.config('timesheet')    → parsed timesheet.json dict
    ctx.has_tool('gmail')      → True/False

    # Switch active workspace
    ws.set_active('samudera')

    # List all
    ws.list_all() → ['catalyze', 'samudera', 'personal']
"""
import json
import os
from pathlib import Path

# Resolve paths relative to this file's location
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent
_WORKSPACES_JSON = _THIS_DIR / 'workspaces.json'


def _load_registry():
    """Load and return the workspaces.json registry."""
    with open(_WORKSPACES_JSON, encoding='utf-8') as f:
        return json.load(f)


def _save_registry(registry):
    """Write the registry back to disk (atomic)."""
    tmp = str(_WORKSPACES_JSON) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(_WORKSPACES_JSON))


class WorkspaceContext:
    """Resolved workspace context with paths to all credentials and config."""

    def __init__(self, name, meta, workspace_dir):
        self.name = name
        self.display_name = meta.get('display_name', name)
        self.owner_email = meta.get('owner_email', '')
        self.domain = meta.get('domain', '')
        self.workspace_type = meta.get('type', '')
        self.tools = meta.get('tools', {})
        self.dir = str(workspace_dir)
        self._dir = workspace_dir
        self._meta = meta

        # Persona / operating mode
        persona = meta.get('persona', {})
        self.role = persona.get('role', '')
        self.mode = persona.get('mode', '')
        self.behaviors = persona.get('behaviors', [])
        self.style = persona.get('style', '')
        self.default_actions = persona.get('default_actions', [])

    @property
    def credentials(self):
        """Path to the Google OAuth client credentials.json for this workspace."""
        return str(self._dir / 'credentials.json')

    @property
    def workspace_md(self):
        """Path to workspace.md context file."""
        return str(self._dir / 'workspace.md')

    @property
    def env_path(self):
        """Path to the workspace-level .env file."""
        return str(self._dir / '.env')

    def token(self, service):
        """Path to an OAuth token file for a given service (gmail, drive, calendar)."""
        return str(self._dir / f'token_{service}.json')

    def env_file(self, service):
        """Path to a service-specific env file (gitlab.env, mattermost.env, etc.)."""
        return str(self._dir / f'{service}.env')

    def load_env(self, service):
        """Parse a service env file and return as a dict. Returns {} if missing."""
        path = self.env_file(service)
        result = {}
        if not os.path.exists(path):
            return result
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                result[k.strip()] = v.strip().strip('"').strip("'")
        return result

    def config(self, name):
        """Load a JSON config file (e.g. timesheet.json) from the workspace folder."""
        path = self._dir / f'{name}.json'
        if not path.exists():
            return {}
        with open(path, encoding='utf-8') as f:
            return json.load(f)

    def has_tool(self, tool_name):
        """Check if this workspace has a specific tool configured."""
        return bool(self.tools.get(tool_name))

    def persona_prompt(self):
        """Return a formatted persona string for injection into Claude's context."""
        lines = []
        lines.append(f"Active workspace: {self.display_name} ({self.name})")
        lines.append(f"Your role: {self.role}")
        lines.append(f"Operating mode: {self.mode}")
        lines.append(f"Style: {self.style}")
        if self.default_actions:
            lines.append("Default actions:")
            for a in self.default_actions:
                lines.append(f"  - {a}")
        return '\n'.join(lines)

    def is_developer_mode(self):
        """True when the workspace expects coding/engineering behavior."""
        return self.mode == 'developer'

    def is_executive_mode(self):
        """True when the workspace expects strategic/executive behavior."""
        return self.mode == 'executive'

    def __repr__(self):
        return f"WorkspaceContext(name={self.name!r}, mode={self.mode!r}, email={self.owner_email!r})"


def get(workspace_name=None):
    """Resolve a workspace context.

    If workspace_name is None, returns the active_workspace.
    Raises ValueError if the workspace doesn't exist in the registry.
    """
    registry = _load_registry()
    if workspace_name is None:
        workspace_name = registry.get('active_workspace', 'catalyze')

    workspaces = registry.get('workspaces', {})
    if workspace_name not in workspaces:
        available = ', '.join(workspaces.keys())
        raise ValueError(
            f"Workspace '{workspace_name}' not found. Available: {available}")

    meta = workspaces[workspace_name]
    workspace_dir = _THIS_DIR / workspace_name
    return WorkspaceContext(workspace_name, meta, workspace_dir)


def get_active_name():
    """Return the name of the currently active workspace."""
    registry = _load_registry()
    return registry.get('active_workspace', 'catalyze')


def set_active(workspace_name):
    """Switch the active workspace. Persists to workspaces.json."""
    registry = _load_registry()
    workspaces = registry.get('workspaces', {})
    if workspace_name not in workspaces:
        available = ', '.join(workspaces.keys())
        raise ValueError(
            f"Workspace '{workspace_name}' not found. Available: {available}")
    registry['active_workspace'] = workspace_name
    _save_registry(registry)
    return get(workspace_name)


def list_all():
    """Return a list of all workspace names."""
    registry = _load_registry()
    return list(registry.get('workspaces', {}).keys())


def repo_root():
    """Return the absolute path to the repository root."""
    return str(_REPO_ROOT)
