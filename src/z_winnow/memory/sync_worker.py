"""T-W10-E-c: MemOS async sync worker.

Background worker loop that polls memos_sync_queue for pending jobs,
dispatches them to MemOS via sync_ops.dispatch_op(), and handles
retry/backlog logic.

P016: Lazy Import + Single-point integration — worker started/stopped
in FastAPI lifespan via asyncio.create_task + stop_event.  MemOS adapter
created lazily when the first batch arrives.

Design:
- at-least-once semantics: mark_processing → dispatch → mark_done
- retry: up to 3 attempts, then mark_failed
- backlog detection: > 5000 pending → WARNING; > 10000 → ERROR
- graceful shutdown: stop_event → finish current batch → exit
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import Any

import aiosqlite

from z_winnow.config import get_settings
from z_winnow.pipeline.database import (
    fetch_pending_jobs,
    mark_done,
    mark_failed,
    mark_processing,
)

logger = logging.getLogger(__name__)

# Default config
DEFAULT_POLL_INTERVAL_S: float = 5.0
DEFAULT_BATCH_SIZE: int = 20
DEFAULT_MAX_RETRIES: int = 3


async def start_worker(
    stop_event: asyncio.Event,
    db_path: str | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    """Start the memos sync worker loop.

    P016: Single-point integration — called via asyncio.create_task in
    FastAPI lifespan.  Gracefully stops when stop_event is set.

    MemOS adapter is created lazily on first batch to avoid import-time
    side effects and allow the worker to start even when MemOS is
    unavailable (queue still accepts writes).

    Args:
        stop_event: asyncio.Event — set to signal graceful shutdown.
        db_path: SQLite database path (defaults to Settings.db_path when None).
        poll_interval_s: Seconds between polls when queue is empty.
        batch_size: Max jobs to fetch per batch.
    """
    if db_path is None:
        # A013: read default from Settings inside the function body so env /
        # WINNOW_DB_PATH overrides take effect at call time (not import time).
        db_path = get_settings().db_path

    logger.info(
        "memos_sync_worker: starting (db=%s, poll=%.1fs, batch=%d)",
        db_path,
        poll_interval_s,
        batch_size,
    )

    # P016: Lazy adapter — created on first batch
    _adapter: Any = None
    _db: aiosqlite.Connection | None = None

    async def _get_adapter() -> Any:
        """Lazily create MemOS adapter."""
        nonlocal _adapter
        if _adapter is None:
            from z_winnow.memory.factory import create_memos_adapter

            _adapter = create_memos_adapter()
            # P016: Log health on startup
            try:
                health = await _adapter.health_check()
                logger.info(
                    "memos_sync_worker: adapter health=%s",
                    health.get("status", "unknown"),
                )
            except Exception:
                logger.warning("memos_sync_worker: adapter health check failed")
        return _adapter

    while not stop_event.is_set():
        try:
            # Open/verify DB connection each poll cycle (auto-closes on error)
            if _db is None:
                _db = await aiosqlite.connect(db_path)
                _db.row_factory = aiosqlite.Row

            rows = await fetch_pending_jobs(_db, limit=batch_size)

            if not rows:
                # Queue empty → sleep and retry
                await asyncio.sleep(poll_interval_s)
                continue

            logger.debug("memos_sync_worker: fetched %d pending jobs", len(rows))

            adapter = await _get_adapter()

            for row in rows:
                if stop_event.is_set():
                    logger.info("memos_sync_worker: stop signal received, finishing batch")
                    break

                await process_one(_db, adapter, row)

        except aiosqlite.Error as exc:
            logger.warning("memos_sync_worker: database error — %s", exc)
            # Close and reopen on next cycle
            if _db is not None:
                with suppress(Exception):
                    await _db.close()
                _db = None
            await asyncio.sleep(poll_interval_s)
        except Exception:
            logger.exception("memos_sync_worker: unexpected error in main loop")
            await asyncio.sleep(poll_interval_s)

    # Graceful shutdown
    if _db is not None:
        with suppress(Exception):
            await _db.close()
        _db = None

    logger.info("memos_sync_worker: stopped")


async def process_one(
    db: aiosqlite.Connection,
    adapter: Any,
    row: dict[str, Any],
) -> None:
    """Process a single sync queue row: mark processing → dispatch → mark done/failed.

    Implements at-least-once semantics:
      1. mark_processing (transition: pending → processing)
      2. dispatch_op (call MemOS adapter)
      3. mark_done (transition: processing → done)
      On failure:
        - retry_count < 3 → mark_failed → reset to 'pending'
        - retry_count >= 3 → mark_failed → set 'failed'

    Args:
        db: aiosqlite database connection.
        adapter: MemOS adapter instance.
        row: Sync queue row dict.

    Raises:
        Does not raise — all exceptions are caught and handled via mark_failed.
    """
    queue_id: int = row["queue_id"]
    retry_count: int = row.get("retry_count", 0)

    # Step 1: mark as processing
    await mark_processing(db, queue_id)

    try:
        # Step 2: dispatch to MemOS adapter
        from z_winnow.memory.sync_ops import dispatch_op

        await dispatch_op(adapter, row)

        # Step 3: mark as done
        await mark_done(db, queue_id)
        logger.debug("memos_sync_worker: job %d done (op=%s)", queue_id, row.get("op_type", "?"))

    except Exception as exc:
        error_msg: str = str(exc)[:500]
        # Step 4: handle failure with retry logic
        new_status = await mark_failed(db, queue_id, error_msg, retry_count)
        if new_status == "pending":
            logger.debug(
                "memos_sync_worker: job %d retry scheduled (attempt %d)",
                queue_id,
                retry_count + 1,
            )
        else:
            logger.error(
                "memos_sync_worker: job %d permanently failed after %d retries — %s",
                queue_id,
                retry_count + 1,
                error_msg,
            )


async def stop_worker(stop_event: asyncio.Event, timeout_s: float = 30.0) -> None:
    """Signal the worker to stop and wait for graceful shutdown.

    Args:
        stop_event: The asyncio.Event used by start_worker.
        timeout_s: Maximum seconds to wait for the worker to finish.
    """
    logger.info("memos_sync_worker: signalling stop...")
    stop_event.set()

    # Brief wait for worker to notice the event and finish current batch
    t0: float = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        await asyncio.sleep(0.5)
    logger.info("memos_sync_worker: stop timeout reached (%.1fs)", timeout_s)
