"""Judge service -- wraps rl/llm_judge.judge_report for the API route layer.

Responsibility: parameter validation, ReportVersion construction,
result normalization for API response. Does NOT re-implement scoring logic.

The judge_report function is already async -- no asyncio.to_thread needed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from z_winnow.rl.llm_judge import ReportVersion, judge_report
from z_winnow.web.services.task_queue import start_task

logger = logging.getLogger(__name__)


async def _judge_coro(
    group_id: str,
    date: str,
    version_id: str,
) -> dict[str, Any]:
    """Background coroutine: fetch L3 data and run judge_report.

    Constructs a ReportVersion with full Markdown content loaded from
    L3 JSON via the report rendering pipeline, then calls judge_report.
    """
    import aiosqlite

    from z_winnow.config.settings import get_settings
    from z_winnow.templates.renderer import render_report
    from z_winnow.web.services.report_service import get_report_content

    settings = get_settings()
    db_path = settings.db_path

    # 加载 L3 数据并渲染为 Markdown
    content = ""
    try:
        async with aiosqlite.connect(db_path) as db:
            l3 = await get_report_content(
                db, group_id, date, report_type="daily", output_dir=settings.layer3_output_dir,
            )
            if l3 and l3.data:
                content = render_report("daily_report_v1", l3.data)
    except Exception:
        logger.exception("_judge_coro: failed to load report content for %s/%s", group_id, date)

    report = ReportVersion(
        content=content,
        version_id=version_id,
        group_id=group_id,
        date=date,
    )
    result = await judge_report(report)

    # Normalize to JSON-safe dict
    result_dict = result.model_dump(mode="json")

    # Persist judge result to report_versions so it survives page refresh.
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE report_versions SET judge_result = ? "
                "WHERE group_id = ? AND date = ?",
                (json.dumps(result_dict, ensure_ascii=False), group_id, date),
            )
            await db.commit()
    except Exception:
        logger.exception(
            "_judge_coro: failed to persist judge_result for %s/%s", group_id, date
        )

    return result_dict


async def run_judge(
    group_id: str,
    date: str,
    version_id: str = "v1",
    db_path: str | None = None,
) -> str:
    """Spawn a background judge task. Returns task_id (UUID).

    Args:
        group_id: Group identifier.
        date: Report date (YYYYMMDD).
        version_id: Report version identifier.
        db_path: SQLite path for task queue.

    Returns:
        task_id: UUID string for tracking.
    """
    resource_id = f"{group_id}:{date}:{version_id}"

    async def coro_factory() -> dict[str, Any]:
        return await _judge_coro(group_id, date, version_id)

    task_id = await start_task(
        task_type="judge",
        resource_id=resource_id,
        coro_factory=coro_factory,
        db_path=db_path,
    )
    return task_id


async def get_judge_result(
    task_id: str,
    db_path: str | None = None,
) -> dict[str, Any] | None:
    """Get judge task result. Delegates to task_queue.get_task_status.

    Args:
        task_id: Task UUID from run_judge.
        db_path: SQLite path for task queue.

    Returns:
        Task status dict or None if not found.
    """
    from z_winnow.web.services.task_queue import get_task_status

    status = await get_task_status(task_id, db_path=db_path)
    if status is None:
        return None

    # If done, parse result_json for convenience
    if status.get("status") == "done" and status.get("result_json"):
        import contextlib

        with contextlib.suppress(json.JSONDecodeError, TypeError):
            status["parsed_result"] = json.loads(status["result_json"])

    return status


__all__ = [
    "get_judge_result",
    "run_judge",
]
