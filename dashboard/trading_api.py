"""Trading Brain API routes for the dashboard server.

Mirrors the transactions_api delegate pattern: exposes ``route_get(handler)``
and ``route_post(handler)`` called from server.py dispatch.

Routes are personal-only (reject samudera like transactions). Reads are served
straight from the locally learned state file (no live spreadsheet call per page
load); the learn route runs a fresh diff + summarization on demand.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

_SKILL_DIR = (Path(__file__).resolve().parent.parent /
              '.agent' / 'skills' / 'trading-sheets' / 'scripts')
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

try:
    import trading_brain as tb
    _IMPORTS_OK = True
except ImportError as exc:
    _IMPORTS_OK = False
    _IMPORT_ERR = str(exc)

_WS_DIR = Path(__file__).resolve().parent.parent / '.agent' / 'workspaces' / 'personal'
_STATE_DIR = _WS_DIR / 'state' / 'trading_sheets'
_STATE_FILE = _STATE_DIR / 'trading_brain.json'


def _request_ws(handler) -> str | None:
    return getattr(handler, 'ws', None)


def _send_json(handler, status: int, body: str) -> None:
    handler._send_json(status, body)


def _ok(handler, data) -> None:
    _send_json(handler, 200, json.dumps({'ok': True, **data}, ensure_ascii=False))


def _err(handler, status: int, message: str) -> None:
    _send_json(handler, status, json.dumps({'ok': False, 'error': message}))


def _route_post(handler, fn) -> None:
    try:
        fn()
    except Exception as e:
        traceback.print_exc()
        _err(handler, 500, f'Trading Brain error: {e}')


def route_get(handler) -> None:
    """GET /api/trading/overview - serve the latest learned summary + engine state."""
    def handle():
        if _request_ws(handler) == 'samudera':
            _err(handler, 403, 'Not available in samudera mode')
            return
        if not _IMPORTS_OK:
            _err(handler, 500, f'Trading Brain module not available: {_IMPORT_ERR}')
            return
        state = tb._load_state()
        if state is None:
            _ok(handler, {
                'state': None,
                'path': str(_STATE_FILE),
                'hint': 'No learning yet - click "Learn now" or run '
                        '`trading_brain.py learn` from the skill scripts.',
            })
            return
        _ok(handler, {
            'state': state,
            'path': str(_STATE_FILE),
            'spreadsheet_id': tb.SPREADSHEET_ID,
            'strategy_keys': list(tb.STRATEGY_KEYS),
        })

    _route_post(handler, handle)


def route_post(handler) -> None:
    """POST /api/trading/learn - run a fresh diff + summarization now."""
    def handle():
        if _request_ws(handler) == 'samudera':
            _err(handler, 403, 'Not available in samudera mode')
            return
        if not _IMPORTS_OK:
            _err(handler, 500, f'Trading Brain module not available: {_IMPORT_ERR}')
            return
        result = tb.learn(force=False)
        _ok(handler, result)

    _route_post(handler, handle)