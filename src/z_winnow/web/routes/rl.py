"""RL route — POST /api/v1/rl/export (202), GET /api/v1/rl/export/{task_id}.

# P054: Parse-validate-delegate. Zero business logic.
# P067: POST /rl/export uses start_task for 202 async pattern.
# P022: GET /rl/export/{task_id} is pure data retrieval from async_tasks table.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from z_winnow.web.schemas.common import AsyncTaskResponse, TaskStatusResponse
from z_winnow.web.schemas.export import RLExportRequest

router = APIRouter(tags=["rl"])


@router.post("/rl/export", response_model=AsyncTaskResponse, status_code=202)
async def export_rl_dataset_endpoint(
    request: Request,
    body: RLExportRequest,
) -> AsyncTaskResponse:
    """导出强化学习（RL）训练数据集（后台执行，立即返回任务号）。

    【训练数据】把指定日期范围内的报告整理成 RL 训练用的 JSONL 数据集。

    什么时候用：要拿历史报告训练/微调模型时。
    - 入参：group_id、start_date、end_date（日期格式 YYYY-MM-DD）
    - 返回：202 + 任务号；用 GET /rl/export/{task_id} 查导出进度
    """
    # A008: explicit initialization
    from z_winnow.web.services.export_service import run_rl_date_range_export

    db_path: str = request.app.state.db_path

    # P054: RLExportRequest already validated by Pydantic —
    # date normalization + end_date >= start_date enforced in schema.
    task_id = await run_rl_date_range_export(
        group_id=body.group_id,
        start_date=body.start_date,
        end_date=body.end_date,
        db_path=db_path,
    )

    return AsyncTaskResponse(
        task_id=task_id,
        status_url=f"/api/v1/rl/export/{task_id}",
    )


@router.get("/rl/export/{task_id}", response_model=TaskStatusResponse)
async def get_rl_export_status(
    request: Request,
    task_id: str,
) -> TaskStatusResponse:
    """查询 RL 数据导出任务的进度。

    【训练数据】配合 POST /rl/export 轮询，看导出跑完没、结果在哪。

    什么时候用：发起导出后查结果。
    - 返回：任务状态 + 导出结果
    - 出错：404 = 任务号不存在
    """
    # A008: pre-initialize result before try
    from z_winnow.web.services.task_queue import get_task_status

    db_path: str = request.app.state.db_path

    result: Any = None  # A008
    row = await get_task_status(task_id, db_path=db_path)

    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")

    # Parse result_json (stored as JSON string in async_tasks.result column)
    if row.get("result_json"):
        try:
            result = json.loads(row["result_json"])
        except (json.JSONDecodeError, TypeError):
            result = None

    return TaskStatusResponse(
        task_id=task_id,
        status=row["status"],
        progress=None,
        result=result,
        error=row.get("error_message"),
    )
