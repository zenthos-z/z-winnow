"""批量日报生成路由 — POST /api/v1/runs/batch-v2, GET /api/v1/runs/batch/{id}, POST /api/v1/runs/batch/{id}/cancel

批量任务调度入口，支持群选择、日期范围、实时进度追踪。

设计模式:
  - P054: Parse-validate-delegate — 路由层保持 thin
  - P067: SQLite-backed 异步任务 — start_task 后台执行
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from z_winnow.config.settings import get_settings
from z_winnow.web.schemas.batch import (
    ActiveBatchSummary,
    BatchCancelResponse,
    BatchGroupSummary,
    BatchItemSummary,
    BatchJobDetail,
    BatchRunV2Request,
    BatchRunV2Response,
)
from z_winnow.web.services import batch_db
from z_winnow.web.services.batch_scheduler import (
    BatchScheduler,
    stream_batch_progress,
)
from z_winnow.web.services.task_queue import start_task

logger = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])


@router.post("/runs/batch-v2", response_model=BatchRunV2Response, status_code=202)
async def create_batch_v2(
    request: Request,
    body: BatchRunV2Request,
) -> BatchRunV2Response:
    """发起批量日报生成任务（后台执行，立即返回批次号）。

    【批量生成】选择多个群+日期范围后，批量生成日报。

    什么时候用：批量生成面板点击"确认生成"后调用。
    - 入参：groups 列表，每项含 group_id + date_from + date_to
    - 返回：202 + batch_id；用 GET /runs/batch/{batch_id} 或 SSE 查进度
    - 出错：422 = groups 为空或日期范围无效
    """
    if not body.groups:
        raise HTTPException(status_code=422, detail="groups list is empty")

    db_path: str = request.app.state.db_path
    settings = get_settings()

    # 1. 展开日期范围，计算总任务数
    all_items: list[dict[str, str]] = []
    group_day_counts: dict[str, int] = {}

    for grp in body.groups:
        try:
            start_date = datetime.strptime(grp.date_from, "%Y-%m-%d")
            end_date = datetime.strptime(grp.date_to, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid date format for group {grp.group_id}: {exc}",
            ) from None

        if start_date > end_date:
            raise HTTPException(
                status_code=422,
                detail=f"date_from ({grp.date_from}) must be <= date_to ({grp.date_to})",
            )

        current = start_date
        day_count = 0
        while current <= end_date:
            all_items.append(
                {
                    "group_id": grp.group_id,
                    "date": current.strftime("%Y-%m-%d"),
                }
            )
            day_count += 1
            current += timedelta(days=1)

        group_day_counts[grp.group_id] = day_count

    if not all_items:
        raise HTTPException(status_code=422, detail="No items to generate (empty date range)")

    batch_id = str(uuid.uuid4())
    total_groups = len(body.groups)
    total_days = sum(group_day_counts.values())
    total_items = len(all_items)
    max_parallel = body.max_parallel or settings.max_parallel_groups

    # 2. 创建数据库记录
    async def _create_batch_coro() -> dict[str, Any]:
        import aiosqlite

        async with aiosqlite.connect(db_path) as db:
            # 创建批次主记录
            await batch_db.create_batch_job(
                db,
                batch_id=batch_id,
                total_groups=total_groups,
                total_days=total_days,
                total_items=total_items,
                max_parallel=max_parallel,
            )

            # 创建明细记录
            await batch_db.create_batch_items(db, batch_id, all_items)

        return {"batch_id": batch_id, "total_items": total_items}

    # 3. 启动后台调度任务
    async def _run_batch_coro() -> dict[str, Any]:
        # 先创建数据库记录
        await _create_batch_coro()

        # 执行调度
        scheduler = BatchScheduler(db_path, max_parallel_groups=max_parallel)
        return await scheduler.run_batch(batch_id)

    task_id = await start_task(
        task_type="batch_run_v2",
        resource_id=batch_id,
        coro_factory=_run_batch_coro,
    )

    logger.info(
        "create_batch_v2: batch_id=%s groups=%d days=%d items=%d task_id=%s",
        batch_id,
        total_groups,
        total_days,
        total_items,
        task_id,
    )

    return BatchRunV2Response(
        batch_id=batch_id,
        status_url=f"/api/v1/runs/batch/{batch_id}",
        total_groups=total_groups,
        total_days=total_days,
        total_items=total_items,
    )


@router.get("/runs/batch/active", response_model=list[ActiveBatchSummary])
async def list_active_batches(request: Request) -> list[ActiveBatchSummary]:
    """列出所有活跃批量任务（queued/running），前端刷新后恢复进度。

    【批量生成】刷新浏览器后，前端调此端点判断是否有正在跑的批次。
    - 返回：按 created_at DESC 排序的活跃批次摘要列表（无活跃时为 []）
    - 用途：取 [0] 恢复最近一个批次的按钮状态；详情/进度走 GET /runs/batch/{id}
    """
    import aiosqlite

    db_path: str = request.app.state.db_path
    async with aiosqlite.connect(db_path) as db:
        rows = await batch_db.list_active_batch_jobs(db)

    out: list[ActiveBatchSummary] = []
    for r in rows:
        total = r.get("total_items") or 1
        done = (r.get("completed") or 0) + (r.get("failed") or 0) + (r.get("skipped_empty") or 0)
        out.append(
            ActiveBatchSummary(
                batch_id=r["batch_id"],
                status=r["status"],
                total_items=r.get("total_items") or 0,
                completed=r.get("completed") or 0,
                failed=r.get("failed") or 0,
                skipped_empty=r.get("skipped_empty") or 0,
                progress_pct=int((done / total) * 100),
                started_at=r.get("started_at"),
            )
        )
    return out


@router.get("/runs/batch/{batch_id}", response_model=BatchJobDetail)
async def get_batch_detail(
    request: Request,
    batch_id: str,
) -> BatchJobDetail:
    """查询批量任务详情。

    【批量生成】查看批量生成进度、各群各日期状态。

    什么时候用：批量生成后轮询进度。
    - 返回：批次状态 + 群级汇总 + 日期级明细
    - 出错：404 = 批次不存在
    """
    import aiosqlite

    db_path: str = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # 1. 查询批次主记录
        batch = await batch_db.get_batch_job(db, batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found")

        # 2. 查询进度统计
        stats = await batch_db.get_batch_progress_stats(db, batch_id)

        # 3. 查询群级汇总
        group_summaries_raw = await batch_db.get_batch_group_summary(db, batch_id)

        # 4. 构建群级汇总（含日期明细）
        groups: list[BatchGroupSummary] = []
        for gs in group_summaries_raw:
            # 查询该群的日期明细
            items_raw = await batch_db.get_batch_items(db, batch_id, group_id=gs["group_id"])

            items = [
                BatchItemSummary(
                    item_id=item["item_id"],
                    date=item["date"],
                    status=item["status"],
                    progress_pct=item["progress_pct"] or 0,
                    run_id=item.get("run_id"),
                    error_message=item.get("error_message"),
                )
                for item in items_raw
            ]

            # 解析 display_name（从 groups 表）
            display_name = await _resolve_display_name(db, gs["group_id"])

            groups.append(
                BatchGroupSummary(
                    group_id=gs["group_id"],
                    display_name=display_name,
                    total=gs["total"],
                    completed=gs["completed"] or 0,
                    failed=gs["failed"] or 0,
                    skipped_empty=gs["skipped_empty"] or 0,
                    progress_pct=int(gs["avg_progress_pct"] or 0),
                    items=items,
                )
            )

        # 5. 计算综合进度
        total = stats["total"] or 1
        done = stats["completed"] + stats["failed"] + stats["skipped_empty"]
        progress_pct = int((done / total) * 100)

    return BatchJobDetail(
        batch_id=batch["batch_id"],
        status=batch["status"],
        total_groups=batch["total_groups"],
        total_days=batch["total_days"],
        total_items=batch["total_items"],
        completed=stats["completed"],
        failed=stats["failed"],
        skipped_empty=stats["skipped_empty"],
        max_parallel=batch["max_parallel"],
        progress_pct=progress_pct,
        started_at=batch.get("started_at"),
        completed_at=batch.get("completed_at"),
        error_message=batch.get("error_message"),
        groups=groups,
    )


@router.post("/runs/batch/{batch_id}/cancel", response_model=BatchCancelResponse)
async def cancel_batch(
    request: Request,
    batch_id: str,
) -> BatchCancelResponse:
    """取消批量任务。

    【批量生成】中途取消正在运行的批量生成。

    什么时候用：发现发错批次、想停止生成。
    - 返回：200 取消成功
    - 出错：404 = 批次不存在；409 = 已完成无法取消
    """
    import aiosqlite

    db_path: str = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        batch = await batch_db.get_batch_job(db, batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Batch not found")

        # 检查是否已完成
        if batch["status"] in ("completed", "cancelled", "partial_failed"):
            raise HTTPException(
                status_code=409,
                detail=f"Batch is already in terminal state '{batch['status']}'",
            )

    # 调用调度器取消
    settings = get_settings()
    scheduler = BatchScheduler(db_path, max_parallel_groups=settings.max_parallel_groups)
    await scheduler.cancel_batch(batch_id)

    return BatchCancelResponse(
        success=True,
        status="cancelled",
        detail=f"Batch {batch_id} cancelled",
    )


@router.get("/runs/batch/{batch_id}/stream")
async def stream_batch(
    request: Request,
    batch_id: str,
) -> StreamingResponse:
    """实时推送批量任务进度（SSE 事件流）。

    【批量生成】批量生成后实时查看进度，不用轮询。

    什么时候用：批量生成面板打开时订阅进度。
    - 返回：text/event-stream 流，每条是批次进度事件
    """
    db_path: str = request.app.state.db_path

    return StreamingResponse(
        stream_batch_progress(db_path, batch_id, poll_interval_s=1.0, max_iterations=3600),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


async def _resolve_display_name(db, group_id: str) -> str:
    """解析群显示名称。"""

    try:
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


__all__ = ["router"]
