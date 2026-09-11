"""Scheduler for automatic Gmail sync.

Runs on a fixed interval (default every 6 hours) via a daemon thread in the
dashboard server. Sync is idempotent (already-processed message ids are
skipped), so frequent runs are safe and cheap. The scheduler also performs a
catch-up sync immediately on start, then on that same interval.
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
DEFAULT_SYNC_INTERVAL_SECS = 6 * 60 * 60  # every 6 hours
MIN_INTERVAL_SECS = 60 * 60  # never sync more often than hourly


class TransactionScheduler:
    """Daemon scheduler for Gmail transaction sync on a fixed interval."""

    def __init__(self, sync_fn: Callable[[], dict],
                 interval_secs: int = DEFAULT_SYNC_INTERVAL_SECS):
        """
        Parameters
        ----------
        sync_fn : callable
            Function to call for sync (no args, returns dict with 'ok' key).
        interval_secs : int
            Seconds between syncs. Clamped to >= MIN_INTERVAL_SECS.
        """
        self._sync_fn = sync_fn
        self._interval_secs = max(int(interval_secs), MIN_INTERVAL_SECS)
        self._timer: Optional[threading.Timer] = None
        self._last_sync_at: Optional[float] = None
        self._running = False

    def start(self) -> None:
        """Start the scheduler. Runs an immediate catch-up, then schedules the next."""
        if self._running:
            return
        self._running = True
        self._last_sync_at = None
        logger.info('Transaction scheduler starting (sync every %ds)',
                     self._interval_secs)

        # Immediate catch-up sync, then schedule the next run.
        self._do_sync()
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

    def _schedule_next(self) -> None:
        """Schedule the next sync `interval` seconds from now."""
        if not self._running:
            return

        self._timer = threading.Timer(self._interval_secs, self._run_scheduled)
        self._timer.daemon = True
        self._timer.start()
        next_utc = datetime.now() + timedelta(seconds=self._interval_secs)
        logger.info('Next sync at %s (%.0fs from now)',
                     next_utc.strftime('%Y-%m-%d %H:%M:%S'), self._interval_secs)

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
                     interval_secs: int = DEFAULT_SYNC_INTERVAL_SECS) -> TransactionScheduler:
    """Create and return a TransactionScheduler instance."""
    return TransactionScheduler(sync_fn, interval_secs)
