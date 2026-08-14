"""P067: SQLite-backed in-process async task queue for long operations.

Five-layer architecture:
  1. DDL: ``async_tasks`` table schema (extends T-W14-8 base DDL)
  2. CRUD helpers: insert_task, update_task_status, query helpers
  3. Background executor: ``asyncio.create_task``-based runner
  4. API functions: start_task, get_task_status, cancel_task, list_tasks
  5. Best-effort side effects: status writes tolerate failures

Each background task gets an independent aiosqlite connection (A024:
shared state via SQLite, not in-memory dicts).

A031/A032: Background tasks MUST write status to SQLite before coroutine
exits. Tests verify via get_task_status, not by assuming the coroutine ran.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# ============================================================
# Layer 1: DDL
# ============================================================
# T-W14-8 (run_merge.py) creates async_tasks with columns:
#   task_id, task_type, status, result, error, created_at, updated_at
# T-W14-5 adds: resource_id, started_at, finished_at
# We use ALTER TABLE ADD COLUMN for the extras (idempotent).

_MIGRATION_SQL = (
    "ALTER TABLE async_tasks ADD COLUMN resource_id TEXT NOT NULL DEFAULT '';"
    "ALTER TABLE async_tasks ADD COLUMN started_at TEXT;"
    "ALTER TABLE async_tasks ADD COLUMN finished_at TEXT;"
)

# Base DDL — matches T-W14-8 run_merge.py create_async_tasks_table.
# Included here so task_queue works standalone (without init_database).
_BASE_DDL = """
CREATE TABLE IF NOT EXISTS async_tasks (
    task_id    TEXT PRIMARY KEY,
    task_type  TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    result     TEXT,
    error      TEXT,
    created_at TEXT,
    updated_at TEXT
);
"""

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_async_tasks_type_status
    ON async_tasks(task_type, status);
CREATE INDEX IF NOT EXISTS idx_async_tasks_status
    ON async_tasks(status);
"""


# ============================================================
# Layer 2: Table initialization + migration
# ============================================================


async def _ensure_async_tasks_table(conn: aiosqlite.Connection) -> None:
    """P067 Layer 2: Ensure async_tasks table exists with all columns.

    The base table is created by pipeline/database.py init_database_in_conn
    (which calls create_async_tasks_table from run_merge migration).
    We create it if missing (standalone mode), then add extra columns
    via ALTER TABLE (idempotent, ignores errors if columns already exist).

    P078: DDL must be tested with real SQLite (PRAGMA table_info + INSERT
    round-trip), not mocked.
    """
    # Ensure base table exists (no-op if already created by init_database)
    await conn.executescript(_BASE_DDL)

    # Run migrations for extra columns (best-effort — column may exist)
    for stmt in _MIGRATION_SQL.strip().split(";"):
        stmt = stmt.strip()
        if not stmt or stmt.startswith("--"):
            continue
        with contextlib.suppress(Exception):
            await conn.execute(stmt)

    await conn.executescript(_INDEX_SQL)
    await conn.commit()


# ============================================================
# Layer 2: CRUD helpers
# ============================================================


async def _insert_task(
    conn: aiosqlite.Connection,
    *,
    task_id: str,
    task_type: str,
    resource_id: str,
) -> None:
    """Insert a new task record with status='queued'."""
    now = datetime.now(UTC).isoformat()
    await conn.execute(
        """INSERT INTO async_tasks
           (task_id, task_type, resource_id, status, created_at, updated_at)
           VALUES (?, ?, ?, 'queued', ?, ?)""",
        (task_id, task_type, resource_id, now, now),
    )
    await conn.commit()


async def _update_status(
    conn: aiosqlite.Connection,
    *,
    task_id: str,
    status: str,
    result_json: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update task status and optional result/error fields.

    Column mapping: result_json -> result, error_message -> error
    (for compatibility with T-W14-8 base DDL which uses 'result' and 'error').
    """
    now = datetime.now(UTC).isoformat()
    started_at: str | None = None
    finished_at: str | None = None

    if status == "running":
        started_at = now
    elif status in ("done", "failed", "cancelled"):
        finished_at = now

    sets = ["status = ?", "updated_at = ?"]
    vals: list[Any] = [status, now]

    if started_at is not None:
        sets.append("started_at = ?")
        vals.append(started_at)
    if finished_at is not None:
        sets.append("finished_at = ?")
        vals.append(finished_at)
    if result_json is not None:
        sets.append("result = ?")  # T-W14-8 column name
        vals.append(result_json)
    if error_message is not None:
        sets.append("error = ?")  # T-W14-8 column name
        vals.append(error_message)

    vals.append(task_id)
    await conn.execute(
        f"UPDATE async_tasks SET {', '.join(sets)} WHERE task_id = ?",
        tuple(vals),
    )
    await conn.commit()


async def _get_task_row(conn: aiosqlite.Connection, task_id: str) -> dict[str, Any] | None:
    """Fetch a single task row as dict."""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM async_tasks WHERE task_id = ?",
        (task_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    d = dict(row)
    # Normalize column names for API consistency
    d["result_json"] = d.get("result")
    d["error_message"] = d.get("error")
    return d


# ============================================================
# Layer 3: Background executor
# ============================================================


async def _spawn_background(
    task_id: str,
    coro_factory: Callable[[], Any],
    db_path: str,
) -> None:
    """P067 Layer 3: Run a coroutine in the background, persisting status.

    Opens an independent aiosqlite connection (A024: shared state via
    SQLite file, not isolated dicts).

    A031/A032: Status is written to SQLite on success AND failure.
    error_message is guaranteed non-empty on failure.
    """
    try:
        # Independent connection per background task
        async with aiosqlite.connect(db_path) as conn:
            await _ensure_async_tasks_table(conn)
            # Mark as running
            await _update_status(conn, task_id=task_id, status="running")

            try:
                # Execute the actual coroutine
                result = await coro_factory()

                # A031: Write result before exit
                result_json = json.dumps(result, default=str)
                await _update_status(
                    conn,
                    task_id=task_id,
                    status="done",
                    result_json=result_json,
                )
            except Exception as e:
                # A032: error_message must be non-empty and truthful
                error_msg = f"{type(e).__name__}: {e}"
                await _update_status(
                    conn,
                    task_id=task_id,
                    status="failed",
                    error_message=error_msg,
                )
                logger.error("Background task %s failed: %s", task_id, e)
    except Exception as outer_e:
        # Outer connection/setup failure -- best-effort log
        logger.error("Background task %s executor failed: %s", task_id, outer_e)


# ============================================================
# Layer 4: API functions (service layer)
# ============================================================


async def start_task(
    task_type: str,
    resource_id: str,
    coro_factory: Callable[[], Any] | None = None,
    db_path: str | None = None,
) -> str:
    """Start a new background task. Returns task_id (UUID).

    Args:
        task_type: Categorization (e.g. "judge", "export", "pipeline").
        resource_id: Identifier for the resource being processed.
        coro_factory: Zero-arg async callable. If None, task stays "queued"
            forever (caller manages lifecycle manually).
        db_path: SQLite path. Defaults to Settings.db_path.

    Returns:
        task_id: UUID string for tracking.
    """
    from z_winnow.config.settings import get_settings

    resolved_db = db_path or get_settings().db_path
    task_id = str(uuid.uuid4())

    async with aiosqlite.connect(resolved_db) as conn:
        await _ensure_async_tasks_table(conn)
        await _insert_task(
            conn,
            task_id=task_id,
            task_type=task_type,
            resource_id=resource_id,
        )

    # Spawn background if coro_factory provided
    if coro_factory is not None:
        _bg_task = asyncio.create_task(_spawn_background(task_id, coro_factory, resolved_db))  # noqa: RUF006 — fire-and-forget by design

    return task_id


async def get_task_status(
    task_id: str,
    db_path: str | None = None,
) -> dict[str, Any] | None:
    """Get current status of a task.

    Returns dict with keys: task_id, task_type, resource_id, status,
    result_json, error_message, created_at, updated_at, started_at, finished_at.
    None if task_id not found.
    """
    from z_winnow.config.settings import get_settings

    resolved_db = db_path or get_settings().db_path

    async with aiosqlite.connect(resolved_db) as conn:
        await _ensure_async_tasks_table(conn)
        return await _get_task_row(conn, task_id)


async def cancel_task(
    task_id: str,
    db_path: str | None = None,
) -> bool:
    """Cancel a task (best-effort status update).

    Returns True if task was found and updated, False otherwise.
    """
    from z_winnow.config.settings import get_settings

    resolved_db = db_path or get_settings().db_path

    async with aiosqlite.connect(resolved_db) as conn:
        await _ensure_async_tasks_table(conn)
        row = await _get_task_row(conn, task_id)
        if row is None:
            return False
        if row["status"] in ("done", "failed", "cancelled"):
            return False
        await _update_status(conn, task_id=task_id, status="cancelled")
        return True


async def list_tasks(
    task_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """List tasks with optional filters.

    Args:
        task_type: Filter by task type.
        status: Filter by status.
        limit: Maximum rows to return.
        db_path: SQLite path.

    Returns:
        List of task dicts.
    """
    from z_winnow.config.settings import get_settings

    resolved_db = db_path or get_settings().db_path

    conditions: list[str] = []
    params: list[Any] = []

    if task_type is not None:
        conditions.append("task_type = ?")
        params.append(task_type)
    if status is not None:
        conditions.append("status = ?")
        params.append(status)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    async with aiosqlite.connect(resolved_db) as conn:
        await _ensure_async_tasks_table(conn)
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"SELECT * FROM async_tasks {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


__all__ = [
    "_ensure_async_tasks_table",
    "_spawn_background",
    "cancel_task",
    "get_task_status",
    "list_tasks",
    "start_task",
]
