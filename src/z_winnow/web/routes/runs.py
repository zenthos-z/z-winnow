"""Runs route -- POST /api/v1/runs (202), GET /api/v1/runs,
GET /api/v1/runs/stream (SSE), GET /api/v1/runs/{run_id}.

# P054: Parse-validate-delegate. Zero business logic.
# P067: 202 endpoints use task_queue.start_task for background execution.
# A031: Background task tracked in registry, not fire-and-forget.
# Tech Constraint 4: SSE uses StreamingResponse with X-Accel-Buffering: no.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from z_winnow.web.schemas.common import AsyncTaskResponse, TaskStatusResponse
from z_winnow.web.schemas.runs import BatchRunRequest, RunCreate, RunStatusOut

router = APIRouter(tags=["runs"])

logger = logging.getLogger(__name__)


@router.post("/runs", response_model=AsyncTaskResponse, status_code=202)
async def create_run(request: Request, body: RunCreate) -> AsyncTaskResponse:
    """发起一次日报生成（跑完整条流水线，后台执行，立即返回任务号）。

    【数据抓取】这是"生成日报"的主入口：抓取群消息 → 解析增强 → AI 生成日报/资源/工程问题/议题 → 落库。

    什么时候用：在「运行」页点"生成日报"，或定时触发某群某天的日报。
    - 入参：group_id（必填，哪个群）、date（必填，哪一天）
    - 返回：202 + run_id；用 GET /runs/{run_id} 或 /runs/stream 查进度
    - 出错：422 = 缺 group_id 或 date
    """
    import uuid

    # 契约：group_id + date 必填 — pipeline 需要明确目标群与日期
    if not body.group_id:
        raise HTTPException(status_code=422, detail="group_id is required")
    if not body.date:
        raise HTTPException(status_code=422, detail="date is required")

    from z_winnow.web.services.run_service import insert_run, resolve_group_name
    from z_winnow.web.services.task_queue import start_task

    run_id = str(uuid.uuid4())
    db_path: str = request.app.state.db_path

    async def _run_coro() -> dict[str, Any]:
        # 占位行（status=queued）；orchestrate 内部 insert_pipeline_run 会
        # 用 INSERT OR REPLACE 覆盖为 running，最终 finally 置 completed/failed
        await insert_run(run_id, group_id=body.group_id or "", date=body.date or "")

        from z_winnow.config.settings import get_settings
        from z_winnow.orchestrator import orchestrate

        settings = get_settings()
        # P054: group name resolution (groups 表 SQL) 在 service 层，路由保持 thin
        group_name = await resolve_group_name(body.group_id or "", db_path)
        await orchestrate(
            group_name=group_name,
            date=(body.date or "").replace("-", ""),
            report_types=["daily"],
            api_base_url=settings.effective_data_base_url,
            api_token=settings.effective_data_token,
            run_id=run_id,
        )
        # 自动推送飞书：群开启 feishu_enabled 时，fire-and-forget 排队一个后台上传任务。
        # auto_push_after_run 自身毫秒级返回（真正上传在后台 asyncio.create_task 跑），
        # 且永不抛异常，故绝不影响 run 终态。
        try:
            from z_winnow.web.services.report_service import auto_push_after_run

            await auto_push_after_run(
                group_id=body.group_id or "",
                date=body.date or "",
                db_path=db_path,
                run_id=run_id,
            )
        except Exception:
            logger.exception("runs._run_coro: auto_push_after_run failed")
        return {"run_id": run_id, "component": body.component, "status": "completed"}

    task_id = await start_task(
        task_type="pipeline_run",
        resource_id=run_id,
        coro_factory=_run_coro,
    )

    return AsyncTaskResponse(
        task_id=task_id,
        status_url=f"/api/v1/runs/{run_id}",
    )


@router.get("/runs", response_model=list[RunStatusOut])
async def list_runs(
    request: Request,
    group_id: str | None = Query(default=None),
    date: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> Any:
    """列出历次日报生成任务（运行记录）。

    【数据抓取】看之前跑过哪些生成任务、各自什么状态。

    什么时候用：在「运行记录」页查看历史任务。
    - 筛选：group_id、date
    - 返回：运行记录列表（含状态、群组、日期）
    """
    from z_winnow.web.services.run_service import list_runs as svc

    db: object = request.app.state.db_conn
    rows = await svc(db, group_id=group_id, date=date, limit=limit)
    return [RunStatusOut(**r) for r in rows]


@router.get("/runs/stream")
async def stream_runs(request: Request) -> StreamingResponse:
    """实时推送运行状态变化（SSE 事件流，服务器主动推）。

    【数据抓取】发起日报生成后，用这个接口实时看进度，不用反复轮询。

    什么时候用：运行记录页开着时，持续接收任务状态更新。
    - 返回：text/event-stream 流，每条是一个运行状态事件
    """
    from z_winnow.web.services.run_service import stream_runs as svc_stream

    db_path: str = request.app.state.db_path

    return StreamingResponse(
        svc_stream(db_path, poll_interval_s=2.0, max_iterations=300),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}", response_model=RunStatusOut)
async def get_run(request: Request, run_id: str) -> RunStatusOut:
    """按 run_id 查看某次生成任务的详情/状态。

    【数据抓取】看一次日报生成跑到哪了、成功还是失败。

    什么时候用：点某条运行记录查看详情，或轮询 POST /runs 的结果。
    - 出错：404 = 该 run_id 不存在
    """
    from z_winnow.web.services.run_service import get_run as svc

    db: object = request.app.state.db_conn
    row = await svc(db, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunStatusOut(**row)


@router.get("/runs/{run_id}/log")
async def get_run_log(request: Request, run_id: str) -> dict:
    """获取某次 pipeline 运行的专用日志文件内容。

    【调试】查看一次日报生成的完整运行日志，定位失败原因。

    什么时候用：运行记录显示失败或异常时，查看详细日志排查。
    - 返回：{run_id, log_text, log_path}
    - log_text 为空时表示日志文件不存在或已被清理

    日志文件位于 logs/runs/{date}/{run_id}.log，包含该次运行的完整输出。
    """
    import glob
    import os

    # 搜索日志文件（按 run_id 文件名匹配）
    pattern = os.path.join("logs", "runs", "*", f"{run_id}.log")
    matches = glob.glob(pattern)

    if not matches:
        return {"run_id": run_id, "log_text": "", "log_path": ""}

    log_path = matches[0]
    try:
        with open(log_path, encoding="utf-8") as f:
            log_text = f.read()
    except Exception:
        log_text = ""

    return {"run_id": run_id, "log_text": log_text, "log_path": log_path}


# ============================================================
# W15-P0-RUNS: Batch run creation + run cancellation
# ============================================================


@router.post("/runs/batch", response_model=AsyncTaskResponse, status_code=202)
async def batch_create_runs(request: Request, body: BatchRunRequest) -> AsyncTaskResponse:
    """一次性提交多个日报生成任务（批量跑，后台执行）。

    【数据抓取】同时给多个群/多天生成日报，单条失败不影响其他。

    什么时候用：要批量补跑若干群/若干天的日报。
    - 入参：items 列表，每条含 group_id + date
    - 返回：202 + 批处理任务号（各条独立成败）
    - 出错：422 = items 为空
    """
    from z_winnow.web.services.run_service import batch_create_runs as svc_batch
    from z_winnow.web.services.task_queue import start_task

    db_path: str = request.app.state.db_path

    # P054: Convert Pydantic models to dicts at the boundary
    items_data = [
        {
            "component": item.component,
            "group_id": item.group_id,
            "date": item.date,
        }
        for item in body.items
    ]

    async def _batch_coro() -> dict[str, Any]:
        return await svc_batch(items_data, db_path=db_path)

    task_id = await start_task(
        task_type="batch_run",
        resource_id="batch",
        coro_factory=_batch_coro,
    )

    return AsyncTaskResponse(
        task_id=task_id,
        status_url=f"/api/v1/runs/batch/{task_id}",
    )


@router.post("/runs/{run_id}/cancel", response_model=TaskStatusResponse)
async def cancel_run(request: Request, run_id: str) -> TaskStatusResponse:
    """取消一个还在排队、尚未开始跑的生成任务。

    【数据抓取】任务还没真正开跑时，可以撤销它。

    什么时候用：发现发错任务、且它还在队列里。
    - 返回：200 取消成功
    - 出错：404 = 任务不存在；409 = 任务已结束（跑完/失败），无法取消
    """
    from z_winnow.web.services.run_service import cancel_run as svc_cancel

    db_path: str = request.app.state.db_path
    result = await svc_cancel(run_id, db_path=db_path)

    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=result["detail"])
    if result["status"] == "terminal":
        raise HTTPException(status_code=409, detail=result["detail"])

    return TaskStatusResponse(
        task_id=run_id,
        status=result["status"],
        progress=None,
        result=None,
        error=None,
    )
