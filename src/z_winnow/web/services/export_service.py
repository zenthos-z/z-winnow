"""Export service -- wraps rl/exporter and Storage.export_jsonl for the API route layer.

Responsibility: orchestrate sync and async export functions via the task queue.
Sync functions (export_dataset) are called via asyncio.to_thread to avoid
blocking the event loop. Async functions (Storage.export_jsonl) are called
directly within an async context.

A019: At least one test must use a real temporary SQLite database rather
than mocking Storage.export_jsonl entirely (L100).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from z_winnow.web.services.task_queue import start_task

logger = logging.getLogger(__name__)


async def _export_jsonl_coro(
    group_id: str,
    start_date: str,
    end_date: str,
    db_path: str,
) -> dict[str, Any]:
    """Background coroutine: call Storage.export_jsonl (async).

    Uses Storage context manager to export topic summaries as JSONL.
    """
    from z_winnow.storage import Storage

    output_dir = Path(db_path).parent / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = str(output_dir / f"{group_id}_{start_date}_{end_date}.jsonl")

    async with Storage(db_path) as store:
        row_count = await store.export_jsonl(
            start_date=start_date,
            end_date=end_date,
            output_path=output_path,
        )

    return {"output_path": output_path, "row_count": row_count}


async def _export_rl_dataset_coro(
    group_id: str,
    days: int,
    db_path: str,
) -> list[dict[str, Any]]:
    """Background coroutine: call export_dataset (sync) via asyncio.to_thread.

    export_dataset uses sqlite3 (sync) -- must not block the event loop.
    """
    from z_winnow.rl.exporter import export_dataset

    result = await asyncio.to_thread(
        export_dataset,
        group_id=group_id,
        days=days,
        db_path=db_path,
    )
    return result


async def run_export(
    group_id: str,
    start_date: str,
    end_date: str,
    db_path: str | None = None,
) -> str:
    """Spawn a background JSONL export task. Returns task_id (UUID).

    Wraps Storage.export_jsonl to export topic summaries for a date range.

    Args:
        group_id: Group identifier.
        start_date: Start date YYYYMMDD.
        end_date: End date YYYYMMDD.
        db_path: SQLite database path.

    Returns:
        task_id: UUID string for tracking.
    """
    from z_winnow.config.settings import get_settings

    resolved_db = db_path or get_settings().db_path
    resource_id = f"{group_id}:{start_date}:{end_date}"

    async def coro_factory() -> dict[str, Any]:
        return await _export_jsonl_coro(group_id, start_date, end_date, resolved_db)

    task_id = await start_task(
        task_type="export",
        resource_id=resource_id,
        coro_factory=coro_factory,
        db_path=resolved_db,
    )
    return task_id


async def run_rl_dataset_export(
    group_id: str,
    days: int = 30,
    db_path: str | None = None,
) -> str:
    """Spawn a background RL dataset export task. Returns task_id (UUID).

    Wraps export_dataset (sync) via asyncio.to_thread.

    Args:
        group_id: Group identifier.
        days: Lookback window in days.
        db_path: SQLite database path.

    Returns:
        task_id: UUID string for tracking.
    """
    from z_winnow.config.settings import get_settings

    resolved_db = db_path or get_settings().db_path
    resource_id = f"{group_id}:rl:{days}d"

    async def coro_factory() -> list[dict[str, Any]]:
        return await _export_rl_dataset_coro(group_id, days, resolved_db)

    task_id = await start_task(
        task_type="rl_export",
        resource_id=resource_id,
        coro_factory=coro_factory,
        db_path=resolved_db,
    )
    return task_id


__all__ = [
    "run_export",
    "run_rl_dataset_export",
    "run_rl_date_range_export",
]


# ============================================================
# W15-P2-RL: Date-range export wrapping export_rl_dataset
# ============================================================


async def run_rl_date_range_export(
    group_id: str,
    start_date: str,
    end_date: str,
    db_path: str | None = None,
) -> str:
    """Spawn a background RL dataset export by date range. Returns task_id (UUID).

    P094: Service function — wraps rl.exporter.export_rl_dataset (sync) via
    asyncio.to_thread.  export_rl_dataset uses sqlite3 (sync); must not block
    the event loop.

    Accepts already-normalized YYYYMMDD dates (RLExportRequest handles
    normalization in the schema layer).

    Args:
        group_id: Group identifier (used for resource_id tagging only).
        start_date: Start date YYYYMMDD.
        end_date: End date YYYYMMDD.
        db_path: SQLite database path.

    Returns:
        task_id: UUID string for tracking.
    """
    from z_winnow.config.settings import get_settings

    resolved_db = db_path or get_settings().db_path
    resource_id = f"{group_id}:{start_date}:{end_date}"

    async def coro_factory() -> dict[str, Any]:
        from z_winnow.rl.exporter import export_rl_dataset

        # P094: export_rl_dataset is sync → asyncio.to_thread
        result = await asyncio.to_thread(
            export_rl_dataset,
            start_date=start_date,
            end_date=end_date,
            db_path=resolved_db,
        )
        # A008: unpack tuple safely
        output_path: str = ""
        record_count: int = 0
        quality_report: dict[str, Any] = {}
        if isinstance(result, tuple) and len(result) == 3:
            output_path, record_count, quality_report = result
        return {
            "output_path": output_path,
            "record_count": record_count,
            "quality_report": quality_report,
        }

    task_id = await start_task(
        task_type="rl_export",
        resource_id=resource_id,
        coro_factory=coro_factory,
        db_path=resolved_db,
    )
    return task_id
