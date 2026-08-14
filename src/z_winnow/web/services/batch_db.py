"""Batch generation database operations.

CRUD functions for batch_jobs and batch_job_items tables.

Patterns:
  P050: All SQL uses parameterized ? placeholders
  A008: All data variables initialized before try blocks
  P067: SQLite-backed async operations with aiosqlite
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


# ============================================================
# batch_jobs CRUD
# ============================================================


async def create_batch_job(
    db: aiosqlite.Connection,
    *,
    batch_id: str | None = None,
    total_groups: int = 0,
    total_days: int = 0,
    total_items: int = 0,
    max_parallel: int = 3,
    created_by: str | None = None,
) -> str:
    """Create a new batch job record.

    Args:
        db: aiosqlite database connection.
        batch_id: Optional batch ID (UUID generated if not provided).
        total_groups: Number of groups in this batch.
        total_days: Total days across all groups.
        total_items: Total items (groups × days).
        max_parallel: Maximum parallel groups.
        created_by: Creator identifier.

    Returns:
        The batch_id of the created job.
    """
    _batch_id = batch_id or str(uuid.uuid4())
    await db.execute(
        """INSERT INTO batch_jobs
           (batch_id, total_groups, total_days, total_items, max_parallel, created_by, status)
           VALUES (?, ?, ?, ?, ?, ?, 'queued')""",
        (_batch_id, total_groups, total_days, total_items, max_parallel, created_by),
    )
    await db.commit()
    logger.debug(
        "create_batch_job: batch_id=%s groups=%d items=%d",
        _batch_id,
        total_groups,
        total_items,
    )
    return _batch_id


async def get_batch_job(
    db: aiosqlite.Connection,
    batch_id: str,
) -> dict[str, Any] | None:
    """Get a batch job by ID.

    Args:
        db: aiosqlite database connection.
        batch_id: The batch ID to look up.

    Returns:
        Batch job dict if found, None otherwise.
    """
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM batch_jobs WHERE batch_id = ?", (batch_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_batch_job(
    db: aiosqlite.Connection,
    batch_id: str,
    **kwargs: Any,
) -> bool:
    """Update a batch job with arbitrary fields.

    Args:
        db: aiosqlite database connection.
        batch_id: The batch ID to update.
        **kwargs: Fields to update (status, started_at, completed_at, etc.).

    Returns:
        True if at least one row was updated.
    """
    allowed = frozenset(
        {
            "status",
            "started_at",
            "completed_at",
            "completed",
            "failed",
            "skipped_empty",
            "error_message",
            "max_parallel",
        }
    )
    filtered = {k: v for k, v in kwargs.items() if k in allowed}
    if not filtered:
        return False

    set_clauses = ", ".join(f"{k} = ?" for k in filtered)
    values = [*filtered.values(), batch_id]
    cursor = await db.execute(
        f"UPDATE batch_jobs SET {set_clauses} WHERE batch_id = ?",
        values,
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_batch_jobs(
    db: aiosqlite.Connection,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List batch jobs with optional filter.

    Args:
        db: aiosqlite database connection.
        status: Optional status filter.
        limit: Maximum number of rows to return.

    Returns:
        List of batch job dicts ordered by created_at DESC.
    """
    db.row_factory = aiosqlite.Row
    if status:
        cursor = await db.execute(
            "SELECT * FROM batch_jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM batch_jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def list_active_batch_jobs(
    db: aiosqlite.Connection,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List active batch jobs (queued/running) for frontend refresh recovery.

    Args:
        db: aiosqlite database connection.
        limit: Maximum number of rows to return.

    Returns:
        List of active batch job dicts ordered by created_at DESC.
    """
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM batch_jobs WHERE status IN ('queued','running') "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ============================================================
# batch_job_items CRUD
# ============================================================


async def create_batch_items(
    db: aiosqlite.Connection,
    batch_id: str,
    items: list[dict[str, Any]],
) -> int:
    """Create multiple batch job items.

    Args:
        db: aiosqlite database connection.
        batch_id: The batch ID these items belong to.
        items: List of dicts with keys: group_id, date.

    Returns:
        Number of items successfully created.
    """
    count = 0
    now = datetime.utcnow().isoformat()
    for item in items:
        item_id = str(uuid.uuid4())
        try:
            await db.execute(
                """INSERT OR IGNORE INTO batch_job_items
                   (item_id, batch_id, group_id, date, created_at, status)
                   VALUES (?, ?, ?, ?, ?, 'pending')""",
                (item_id, batch_id, item["group_id"], item["date"], now),
            )
            count += 1
        except Exception as exc:
            logger.warning(
                "create_batch_items: failed for %s/%s — %s", item["group_id"], item["date"], exc
            )
    await db.commit()
    logger.debug("create_batch_items: batch_id=%s created %d items", batch_id, count)
    return count


async def get_batch_items(
    db: aiosqlite.Connection,
    batch_id: str,
    *,
    group_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Get batch job items for a batch.

    Args:
        db: aiosqlite database connection.
        batch_id: The batch ID to query.
        group_id: Optional group filter.
        status: Optional status filter.

    Returns:
        List of item dicts.
    """
    db.row_factory = aiosqlite.Row
    conditions = ["batch_id = ?"]
    params: list[Any] = [batch_id]

    if group_id:
        conditions.append("group_id = ?")
        params.append(group_id)
    if status:
        conditions.append("status = ?")
        params.append(status)

    where = " AND ".join(conditions)
    cursor = await db.execute(
        f"SELECT * FROM batch_job_items WHERE {where} ORDER BY date, group_id",
        params,
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_batch_item(
    db: aiosqlite.Connection,
    item_id: str,
) -> dict[str, Any] | None:
    """Get a single batch job item by ID.

    Args:
        db: aiosqlite database connection.
        item_id: The item ID to look up.

    Returns:
        Item dict if found, None otherwise.
    """
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM batch_job_items WHERE item_id = ?", (item_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_batch_item(
    db: aiosqlite.Connection,
    item_id: str,
    **kwargs: Any,
) -> bool:
    """Update a batch job item with arbitrary fields.

    Args:
        db: aiosqlite database connection.
        item_id: The item ID to update.
        **kwargs: Fields to update.

    Returns:
        True if at least one row was updated.
    """
    allowed = frozenset(
        {
            "status",
            "run_id",
            "progress_pct",
            "error_message",
            "started_at",
            "completed_at",
        }
    )
    filtered = {k: v for k, v in kwargs.items() if k in allowed}
    if not filtered:
        return False

    set_clauses = ", ".join(f"{k} = ?" for k in filtered)
    values = [*filtered.values(), item_id]
    cursor = await db.execute(
        f"UPDATE batch_job_items SET {set_clauses} WHERE item_id = ?",
        values,
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_batch_group_summary(
    db: aiosqlite.Connection,
    batch_id: str,
) -> list[dict[str, Any]]:
    """Get per-group summary for a batch.

    Args:
        db: aiosqlite database connection.
        batch_id: The batch ID to summarize.

    Returns:
        List of per-group summary dicts with counts by status.
    """
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """SELECT
             group_id,
             COUNT(*) as total,
             SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
             SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
             SUM(CASE WHEN status = 'skipped_empty' THEN 1 ELSE 0 END) as skipped_empty,
             SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running,
             SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
             AVG(progress_pct) as avg_progress_pct
           FROM batch_job_items
           WHERE batch_id = ?
           GROUP BY group_id
           ORDER BY group_id""",
        (batch_id,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_batch_progress_stats(
    db: aiosqlite.Connection,
    batch_id: str,
) -> dict[str, int]:
    """Get overall progress stats for a batch.

    Args:
        db: aiosqlite database connection.
        batch_id: The batch ID to get stats for.

    Returns:
        Dict with keys: total, completed, failed, skipped_empty, running, pending.
    """
    cursor = await db.execute(
        """SELECT
             COUNT(*) as total,
             SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
             SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
             SUM(CASE WHEN status = 'skipped_empty' THEN 1 ELSE 0 END) as skipped_empty,
             SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running,
             SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending
           FROM batch_job_items
           WHERE batch_id = ?""",
        (batch_id,),
    )
    row = await cursor.fetchone()
    if row:
        return {
            "total": row[0] or 0,
            "completed": row[1] or 0,
            "failed": row[2] or 0,
            "skipped_empty": row[3] or 0,
            "running": row[4] or 0,
            "pending": row[5] or 0,
        }
    return {"total": 0, "completed": 0, "failed": 0, "skipped_empty": 0, "running": 0, "pending": 0}


async def cancel_batch_items(
    db: aiosqlite.Connection,
    batch_id: str,
) -> int:
    """Cancel all pending/running items in a batch.

    Args:
        db: aiosqlite database connection.
        batch_id: The batch ID to cancel.

    Returns:
        Number of items cancelled.
    """
    cursor = await db.execute(
        """UPDATE batch_job_items
           SET status = 'cancelled', completed_at = datetime('now')
           WHERE batch_id = ? AND status IN ('pending', 'running')""",
        (batch_id,),
    )
    await db.commit()
    count = cursor.rowcount
    logger.debug("cancel_batch_items: batch_id=%s cancelled %d items", batch_id, count)
    return count


__all__ = [
    "cancel_batch_items",
    "create_batch_items",
    "create_batch_job",
    "get_batch_group_summary",
    "get_batch_item",
    "get_batch_items",
    "get_batch_job",
    "get_batch_progress_stats",
    "list_active_batch_jobs",
    "list_batch_jobs",
    "update_batch_item",
    "update_batch_job",
]
