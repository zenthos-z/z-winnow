"""Judge route -- POST /api/v1/judge (202), GET /api/v1/judge/{task_id}.

# P054: Parse-validate-delegate. Zero business logic.
# P067: POST /judge uses task_queue for background LLM-as-judge execution.
# A031: Background task tracked in registry, not fire-and-forget.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from z_winnow.web.schemas.common import AsyncTaskResponse, TaskStatusResponse
from z_winnow.web.schemas.judge import JudgeRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["judge"])


@router.post("/judge", response_model=AsyncTaskResponse, status_code=202)
async def create_judge(request: Request, body: JudgeRequest) -> AsyncTaskResponse:
    """用 AI（LLM-as-judge）给一份报告打分（后台执行，立即返回任务号）。

    【质量评估】报告生成后，让另一个 AI 从完整度/准确度/简洁度/可执行性 4 个维度评分。

    什么时候用：想量化某份报告的质量。
    - 入参：report_id（要评的报告）
    - 返回：202 + 任务号；用 GET /judge/{task_id} 查评分结果
    """
    # A008: explicit initialization
    from z_winnow.web.services.judge_service import run_judge
    from z_winnow.web.services.report_service import (
        get_report_version,
        get_report_versions,
    )

    db: object = request.app.state.db_conn

    # B9: resolve report_id → real (group_id, date, version_id).
    # body.report_id may be a version_id (e.g. "g-d-v1") or a report_id
    # (e.g. "g-d"); try version lookup first, then latest version by report_id.
    version = await get_report_version(db, body.report_id)
    if version is None:
        versions = await get_report_versions(db, body.report_id)
        # get_report_versions returns ASC by version_number → last is latest.
        version = versions[-1] if versions else None

    if version is not None:
        group_id = version.group_id
        date = version.date
        version_id = version.version_id
    else:
        # A008: report not found — explicit fallback (never pass None.* down).
        # L032: keeps the 202 contract so existing clients polling
        # /judge/{task_id} still get a task_id; non-empty defaults (no ""
        # literals). The background judge fails fast on missing L3 data and
        # surfaces that via task status='failed'.
        logger.warning(
            "judge route: report_id=%s unresolved in DB; falling back to request-scoped defaults",
            body.report_id,
        )
        group_id = body.report_id
        date = body.report_id
        version_id = body.report_id

    # dimensions: run_judge has no dimensions parameter (judge_service.py:43),
    # so we cannot fabricate one. Surface the requested dimensions via logging
    # instead of silently dropping them. body.model is intentionally NOT
    # forwarded (out of scope / would bypass model config).
    if body.dimensions:
        logger.info(
            "judge route: dimensions requested for report_id=%s: %s",
            body.report_id,
            body.dimensions,
        )

    task_id = await run_judge(
        group_id=group_id,
        date=date,
        version_id=version_id,
    )

    return AsyncTaskResponse(
        task_id=task_id,
        status_url=f"/api/v1/judge/{task_id}",
    )


@router.get("/judge/{task_id}", response_model=TaskStatusResponse)
async def get_judge_status(request: Request, task_id: str) -> TaskStatusResponse:
    """查询评分任务的进度和结果。

    【质量评估】配合 POST /judge 轮询，看评分跑完没、4 维度分数是多少。

    什么时候用：发起评分后查结果。
    - 返回：任务状态 + 4 维度评分结果
    - 出错：404 = 任务号不存在
    """
    from z_winnow.web.services.judge_service import get_judge_result

    result = await get_judge_result(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Judge task not found")

    return TaskStatusResponse(
        task_id=task_id,
        status=result.get("status", "unknown"),
        progress=None,
        result=result.get("parsed_result") or result.get("result_json"),
        error=result.get("error_message"),
    )
