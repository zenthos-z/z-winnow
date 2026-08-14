"""T-W14-4: Pipeline run management service.

Wraps ``pipeline_runs`` table CRUD plus an SSE async generator for
real-time run status streaming.

  - ``insert_run`` / ``update_run`` open their own connection via db_path.
  - ``list_runs`` accepts an injected ``aiosqlite.Connection``.
  - ``stream_runs`` is an ``AsyncGenerator[str, None]`` that polls the DB
    and yields ``text/event-stream`` formatted payloads.

Patterns applied:
  P067: SQLite-backed async task queue — aiosqlite patterns for pipeline_runs
  A008: All data variables initialized before try blocks
  A013: No module-level env var reads; db_path passed as parameter
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# SQL for creating the pipeline_runs table (used by stream_runs)
_PIPELINE_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    started_at TEXT,
    completed_at TEXT,
    message_count INTEGER DEFAULT 0,
    error_message TEXT,
    current_node TEXT,
    progress_pct INTEGER,
    node_history TEXT,
    group_id TEXT,
    date TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
"""


async def resolve_group_name(group_id: str, db_path: str) -> str:
    """Resolve a group_id (g_xxx) to display_name for orchestrate().

    Prefers groups.display_name, then chatroom_id, then returns group_id as-is.
    orchestrate()/data_fetch resolve group_name back to group_id internally, so
    any identifier works — display_name matches run_pipeline_range.py.

    # P050: parameterized SQL.
    # P054: lives in the service layer — routes must stay thin (no SQL).

    Args:
        group_id: Group identifier to resolve.
        db_path: SQLite database path.

    Returns:
        display_name / chatroom_id / group_id (in order of preference).
    """
    if not group_id:
        return group_id
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT display_name, chatroom_id FROM groups WHERE group_id = ?",
                (group_id,),
            )
            row = await cursor.fetchone()
            if row:
                return row["display_name"] or row["chatroom_id"] or group_id
    except Exception:
        pass
    return group_id


async def insert_run(
    run_id: str,
    *,
    group_id: str = "",
    date: str = "",
    message_count: int = 0,
) -> bool:
    """Insert a new pipeline run record.

    Opens its own DB connection (does not use injected connection).

    Args:
        run_id: Unique run identifier (UUID).
        group_id: Group identifier.
        date: Date string.
        message_count: Number of messages in the run.

    Returns:
        True if insert succeeded.
    """
    # A008
    success: bool = False
    try:
        # A013: db_path read from Settings at call site (function body) —
        # canonical file path, no sqlite:/// prefix to strip.
        from z_winnow.config import get_settings

        settings = get_settings()
        db_path = settings.db_path
    except Exception:
        db_path = "data/winnow.db"

    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO pipeline_runs
                   (run_id, component, status, group_id, date, message_count)
                   VALUES (?, 'pipeline', 'queued', ?, ?, ?)""",
                (run_id, group_id, date, message_count),
            )
            await db.commit()
            success = True
    except Exception:
        logger.exception("run_service.insert_run failed for run_id=%s", run_id)
    return success


async def update_run(
    run_id: str,
    **kwargs: Any,
) -> bool:
    """Update a pipeline run record with arbitrary fields.

    Opens its own DB connection.  Supported kwargs: status, completed_at,
    error_message, current_node, progress_pct, node_history, message_count.

    Args:
        run_id: Run identifier to update.
        **kwargs: Fields to update.

    Returns:
        True if at least one row was updated.
    """
    # A008
    success: bool = False

    # Whitelist of allowed columns (P050)
    allowed = frozenset(
        {
            "status",
            "completed_at",
            "error_message",
            "current_node",
            "progress_pct",
            "node_history",
            "message_count",
            "started_at",
        }
    )
    filtered = {k: v for k, v in kwargs.items() if k in allowed}
    if not filtered:
        return False

    try:
        from z_winnow.config import get_settings

        settings = get_settings()
        db_path = settings.db_path
    except Exception:
        db_path = "data/winnow.db"

    try:
        set_clauses = ", ".join(f"{k} = ?" for k in filtered)
        values = [*filtered.values(), run_id]
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                f"UPDATE pipeline_runs SET {set_clauses} WHERE run_id = ?",
                values,
            )
            await db.commit()
            success = cursor.rowcount > 0
    except Exception:
        logger.exception("run_service.update_run failed for run_id=%s", run_id)
    return success


async def get_run(
    db: aiosqlite.Connection,
    run_id: str,
) -> dict[str, Any] | None:
    """Get a single pipeline run by ID.

    Args:
        db: Async SQLite connection (injected).
        run_id: Run identifier to look up.

    Returns:
        Run dict if found, None otherwise.
    """
    try:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    except Exception:
        logger.exception("run_service.get_run failed for run_id=%s", run_id)
        return None


async def list_runs(
    db: aiosqlite.Connection,
    *,
    group_id: str | None = None,
    date: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List pipeline runs with optional filters.

    Args:
        db: Async SQLite connection (injected).
        group_id: Optional group filter.
        date: Optional date filter.
        limit: Maximum number of rows to return.

    Returns:
        List of run dicts ordered by created_at DESC.
    """
    # A008
    results: list[dict[str, Any]] = []
    try:
        db.row_factory = aiosqlite.Row
        conditions: list[str] = []
        params: list[Any] = []

        if group_id is not None:
            conditions.append("group_id = ?")
            params.append(group_id)
        if date is not None:
            conditions.append("date = ?")
            params.append(date)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM pipeline_runs {where} ORDER BY created_at DESC LIMIT ?"
        cursor = await db.execute(sql, [*params, limit])
        results = [dict(r) for r in await cursor.fetchall()]
    except Exception:
        logger.exception("run_service.list_runs failed")
    return results


async def stream_runs(
    db_path: str,
    *,
    poll_interval_s: float = 2.0,
    max_iterations: int = 300,
) -> AsyncGenerator[str, None]:
    """SSE async generator that polls pipeline_runs and yields events.

    Yields ``data: {...}\\n\\n`` formatted strings suitable for
    ``text/event-stream`` responses.

    Args:
        db_path: Path to the SQLite database (or ``:memory:``).
        poll_interval_s: Seconds between polls.
        max_iterations: Maximum poll iterations before stopping.

    Yields:
        SSE-formatted strings.
    """
    iteration = 0
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            while iteration < max_iterations:
                cursor = await db.execute(
                    "SELECT * FROM pipeline_runs ORDER BY created_at DESC LIMIT 50"
                )
                rows = [dict(r) for r in await cursor.fetchall()]
                payload = json.dumps({"runs": rows, "iteration": iteration}, default=str)
                yield f"data: {payload}\n\n"

                iteration += 1
                await asyncio.sleep(poll_interval_s)
    except Exception:
        logger.exception("run_service.stream_runs error")
        payload = json.dumps({"error": "stream interrupted", "iteration": iteration})
        yield f"data: {payload}\n\n"


async def batch_create_runs(
    items: list[dict[str, Any]],
    *,
    db_path: str,
) -> dict[str, Any]:
    """Create multiple pipeline runs in batch with per-item error isolation.

    P057: Uses asyncio.Semaphore(50) to cap concurrent insert_run calls,
    preventing connection pool exhaustion.

    L037: Iterates items with per-item try/except. A single invalid
    group_id or malformed item does NOT abort the entire batch;
    errors are reported per-item in the response.

    P050: All SQL uses parameterized ? placeholders.

    Args:
        items: List of dicts with keys: component, group_id, date.
        db_path: Path to the SQLite database.

    Returns:
        Dict with keys: batch_id, total, status, results (list of per-item dicts).
    """
    import uuid

    batch_id = str(uuid.uuid4())
    total = len(items)
    results: list[dict[str, Any]] = []

    # P057: Semaphore to cap concurrent insert_run calls
    semaphore = asyncio.Semaphore(50)

    async def _insert_one(index: int, item: dict[str, Any]) -> None:
        # L037: Per-item try/except — single failure does not abort batch
        async with semaphore:
            try:
                run_id = str(uuid.uuid4())
                component = item.get("component", "pipeline")
                group_id = item.get("group_id", "")
                date = item.get("date", "")

                success = await insert_run(
                    run_id,
                    group_id=group_id,
                    date=date,
                )
                results.append(
                    {
                        "index": index,
                        "run_id": run_id,
                        "component": component,
                        "group_id": group_id,
                        "date": date,
                        "status": "created" if success else "failed",
                    }
                )
            except (
                Exception
            ) as exc:  # L037: per-item isolation — single failure does not abort batch
                logger.exception("batch_create_runs item %d failed: %s", index, exc)
                results.append(
                    {
                        "index": index,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    # Process all items concurrently (limited by semaphore)
    tasks = [_insert_one(i, item) for i, item in enumerate(items)]
    await asyncio.gather(*tasks)

    # Sort results by index for deterministic ordering
    results.sort(key=lambda r: r.get("index", 0))

    ok_count = sum(1 for r in results if "run_id" in r)
    fail_count = sum(1 for r in results if "error" in r)

    return {
        "batch_id": batch_id,
        "total": total,
        "status": f"{ok_count} created, {fail_count} failed",
        "results": results,
    }


async def cancel_run(
    run_id: str,
    *,
    db_path: str,
) -> dict[str, Any]:
    """Cancel a pipeline run and its associated async task(s).

    Dual-table consistency: Updates BOTH pipeline_runs.status='cancelled'
    AND async_tasks entries (via task_queue.cancel_task) with matching
    resource_id.

    P050: All SQL uses parameterized ? placeholders.

    Args:
        run_id: The pipeline run identifier to cancel.
        db_path: Path to the SQLite database.

    Returns:
        Dict with keys: success (bool), status (str), detail (str).
        success=True when cancellation was applied.
        success=False with status='not_found' for missing run.
        success=False with status='terminal' for already-terminal runs (409).
    """
    from z_winnow.web.services.task_queue import cancel_task

    # P050: Parameterized query
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # 1. Check pipeline_runs exists
        cursor = await db.execute(
            "SELECT run_id, status FROM pipeline_runs WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"success": False, "status": "not_found", "detail": f"Run {run_id} not found"}

        current_status = row["status"]

    # 2. Check terminal state -> 409 Conflict
    terminal_states = {"cancelled", "completed", "failed", "done"}
    if current_status in terminal_states:
        return {
            "success": False,
            "status": "terminal",
            "detail": f"Run {run_id} is already in terminal state '{current_status}'",
        }

    # 3. Cancel associated async_tasks entries
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT task_id FROM async_tasks WHERE resource_id = ? AND task_type = 'pipeline_run'",
            (run_id,),
        )
        task_rows = await cursor.fetchall()

    for task_row in task_rows:
        await cancel_task(task_row["task_id"], db_path=db_path)

    # 4. Update pipeline_runs status to 'cancelled'
    await update_run(run_id, status="cancelled")

    return {"success": True, "status": "cancelled", "detail": f"Run {run_id} cancelled"}


__all__ = [
    "batch_create_runs",
    "cancel_run",
    "get_run",
    "insert_run",
    "list_runs",
    "stream_runs",
    "update_run",
]
