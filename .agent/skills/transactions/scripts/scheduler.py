"""Scheduler for automatic Gmail sync.

Runs at 23:59 WIB (Asia/Jakarta) daily via a daemon thread in the
dashboard server. Also performs a catch-up sync on server start if
the last sync was >25h ago.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

logger = logging.getLogger('transactions.scheduler')

WIB = ZoneInfo('Asia/Jakarta')
DEFAULT_SYNC_HOUR = 23
DEFAULT_SYNC_MINUTE = 59
SYNC_INTERVAL_SECS = 24 * 60 * 60  # 24h
CATCHUP_THRESHOLD_SECS = 25 * 60 * 60  # 25h


class TransactionScheduler:
    """Daemon scheduler for daily Gmail transaction sync."""

    def __init__(self, sync_fn: Callable[[], dict],
                 sync_hour: int = DEFAULT_SYNC_HOUR,
                 sync_minute: int = DEFAULT_SYNC_MINUTE):
        """
        Parameters
        ----------
        sync_fn : callable
            Function to call for sync (no args, returns dict with 'ok' key).
        sync_hour, sync_minute : int
            Time of day to run (WIB). Default 23:59.
        """
        self._sync_fn = sync_fn
        self._sync_hour = sync_hour
        self._sync_minute = sync_minute
        self._timer: Optional[threading.Timer] = None
        self._last_sync_at: Optional[float] = None
        self._running = False

    def start(self) -> None:
        """Start the scheduler. Performs catch-up if needed, then schedules next run."""
        if self._running:
            return
        self._running = True
        logger.info('Transaction scheduler starting (sync at %02d:%02d WIB)',
                     self._sync_hour, self._sync_minute)

        # Check if catch-up is needed
        self._maybe_catchup()

        # Schedule next run
        self._schedule_next()

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        logger.info('Transaction scheduler stopped')

    def force_sync(self) -> dict:
        """Trigger an immediate sync (manual button)."""
        return self._do_sync()

    def _maybe_catchup(self) -> None:
        """Run a sync immediately if the last sync was >25h ago."""
        if self._last_sync_at is None:
            # No sync yet this session — trigger catch-up
            logger.info('No previous sync recorded; running catch-up sync')
            self._do_sync()
            return

        age = time.time() - self._last_sync_at
        if age > CATCHUP_THRESHOLD_SECS:
            logger.info('Last sync was %.1fh ago; running catch-up sync', age / 3600)
            self._do_sync()

    def _schedule_next(self) -> None:
        """Schedule the next sync at the configured time WIB."""
        if not self._running:
            return

        now_wib = datetime.now(WIB)
        target_wib = now_wib.replace(
            hour=self._sync_hour,
            minute=self._sync_minute,
            second=0, microsecond=0
        )

        # If target already passed today, schedule for tomorrow
        if target_wib <= now_wib:
            target_wib += timedelta(days=1)

        delay_secs = (target_wib - now_wib).total_seconds()
        logger.info('Next sync at %s WIB (%.0fs from now)',
                     target_wib.strftime('%Y-%m-%d %H:%M'), delay_secs)

        self._timer = threading.Timer(delay_secs, self._run_scheduled)
        self._timer.daemon = True
        self._timer.start()

    def _run_scheduled(self) -> None:
        """Called by timer; runs sync and reschedules."""
        if not self._running:
            return
        self._do_sync()
        self._schedule_next()

    def _do_sync(self) -> dict:
        """Execute the sync function."""
        self._running = True
        try:
            result = self._sync_fn()
            self._last_sync_at = time.time()
            logger.info('Sync completed: %s', result)
            return result
        except Exception as e:
            logger.error('Sync failed: %s', e)
            return {'ok': False, 'error': str(e)}


def create_scheduler(sync_fn: Callable[[], dict],
                     sync_hour: int = DEFAULT_SYNC_HOUR,
                     sync_minute: int = DEFAULT_SYNC_MINUTE) -> TransactionScheduler:
    """Create and return a TransactionScheduler instance."""
    return TransactionScheduler(sync_fn, sync_hour, sync_minute)
