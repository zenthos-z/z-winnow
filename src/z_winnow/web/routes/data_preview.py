"""数据预检路由 — GET /api/v1/data/preview + GET /api/v1/data/source-check

preview: 查询本地 SQLite raw_messages 表（已抓取过的数据快照）。
source-check: 直接查询 CipherTalk API 确认数据源是否有消息。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import aiosqlite
from fastapi import APIRouter, Query, Request

from z_winnow.web.schemas.batch import DataPreviewItem, DataPreviewResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["data"])

# source-check 并发上限：避免打爆 CipherTalk API
_SOURCE_CHECK_CONCURRENCY = 5


@router.get("/data/preview", response_model=DataPreviewResponse)
async def get_data_preview(
    request: Request,
    group_ids: str = Query(..., description="群组 ID 列表（逗号分隔）"),
    date_from: str = Query(..., description="起始日期 YYYY-MM-DD"),
    date_to: str = Query(..., description="结束日期 YYYY-MM-DD"),
) -> DataPreviewResponse:
    """数据预检：查询本地 SQLite 中各群各日期已抓取的数据。

    【批量生成】生成前快速查看本地数据状态。

    什么时候用：批量生成面板展开后，快速查看本地已有哪些数据。
    - 注意：首次运行的日期 raw_messages 中无数据，显示"无数据"属正常。
    - 如需确认数据源是否有消息，请使用 /data/source-check。
    """
    db_path: str = request.app.state.db_path

    # 解析 group_ids
    gid_list = [g.strip() for g in group_ids.split(",") if g.strip()]
    if not gid_list:
        return DataPreviewResponse(items=[])

    # 解析日期范围
    try:
        start_date = datetime.strptime(date_from, "%Y-%m-%d")
        end_date = datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError as exc:
        logger.warning("get_data_preview: invalid date format — %s", exc)
        return DataPreviewResponse(items=[])

    # 查询数据库
    items = await _query_preview_data(db_path, gid_list, start_date, end_date)

    return DataPreviewResponse(items=items)


@router.get("/data/source-check", response_model=DataPreviewResponse)
async def get_source_check(
    request: Request,
    group_ids: str = Query(..., description="群组 ID 列表（逗号分隔）"),
    date_from: str = Query(..., description="起始日期 YYYY-MM-DD"),
    date_to: str = Query(..., description="结束日期 YYYY-MM-DD"),
) -> DataPreviewResponse:
    """数据源检查：直接查询 CipherTalk API 确认各群各日期是否有消息。

    【批量生成】确认数据源中哪些天有消息，准确判断是否该生成日报。
    不走本地 SQLite 缓存——专用于首次抓取前的存在性验证。

    什么时候用：批量生成面板中点击「数据预检」时调用。
    - 返回：各群各日期的 has_data 和 message_count（直接从远程 API 获取）
    - 注意：比 /data/preview 慢（N×M 次 API 调用），但准确反映数据源状态。
    """
    from z_winnow.config.settings import get_settings
    from z_winnow.pipeline.cipher_talk_client import create_data_client
    from z_winnow.pipeline.group_config import resolve_chatroom_id

    db_path: str = request.app.state.db_path

    # 解析 group_ids
    gid_list = [g.strip() for g in group_ids.split(",") if g.strip()]
    if not gid_list:
        return DataPreviewResponse(items=[])

    # 解析日期范围
    try:
        start_date = datetime.strptime(date_from, "%Y-%m-%d")
        end_date = datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError as exc:
        logger.warning("get_source_check: invalid date format — %s", exc)
        return DataPreviewResponse(items=[])

    # 展开所有 (group_id, date) 组合
    tasks_input: list[tuple[str, str, str]] = []  # (group_id, chatroom_id, date_ymd)
    settings = get_settings()

    for gid in gid_list:
        try:
            chatroom_id = await resolve_chatroom_id(gid, db_path)
        except Exception as exc:
            logger.warning("get_source_check: resolve_chatroom_id failed for %s — %s", gid, exc)
            continue

        current = start_date
        while current <= end_date:
            date_ymd = current.strftime("%Y%m%d")
            tasks_input.append((gid, chatroom_id, date_ymd))
            current += timedelta(days=1)

    if not tasks_input:
        return DataPreviewResponse(items=[])

    # 并发查询（Semaphore 控速）
    sem = asyncio.Semaphore(_SOURCE_CHECK_CONCURRENCY)
    results: list[DataPreviewItem | None] = [None] * len(tasks_input)

    async def _check_one(idx: int, gid: str, cid: str, d: str) -> None:
        async with sem:
            try:
                async with create_data_client(
                    base_url=settings.effective_data_base_url,
                    token=settings.effective_data_token,
                ) as client:
                    has_data, count = await client.check_messages_count(
                        chatroom_id=cid,
                        date=d,
                        limit=1000,  # 翻页统计真实总数
                    )
                results[idx] = DataPreviewItem(
                    group_id=gid,
                    date=f"{d[:4]}-{d[4:6]}-{d[6:]}",
                    has_data=has_data,
                    message_count=count,
                )
            except Exception as exc:
                logger.warning(
                    "get_source_check: API check failed for %s/%s — %s", gid, d, exc
                )
                results[idx] = DataPreviewItem(
                    group_id=gid,
                    date=f"{d[:4]}-{d[4:6]}-{d[6:]}",
                    has_data=False,
                    message_count=0,
                )

    await asyncio.gather(*[
        _check_one(i, gid, cid, d)
        for i, (gid, cid, d) in enumerate(tasks_input)
    ])

    items = [r for r in results if r is not None]
    return DataPreviewResponse(items=items)


async def _query_preview_data(
    db_path: str,
    group_ids: list[str],
    start_date: datetime,
    end_date: datetime,
) -> list[DataPreviewItem]:
    """查询各群各日期的消息数量（本地 SQLite）。

    Args:
        db_path: 数据库路径。
        group_ids: 群组 ID 列表。
        start_date: 起始日期。
        end_date: 结束日期。

    Returns:
        预检结果列表。
    """
    items: list[DataPreviewItem] = []

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        for gid in group_ids:
            current = start_date
            while current <= end_date:
                date_str = current.strftime("%Y%m%d")
                date_display = current.strftime("%Y-%m-%d")

                # 查询消息数量
                cursor = await db.execute(
                    "SELECT COUNT(*) as cnt FROM raw_messages WHERE group_id = ? AND date = ?",
                    (gid, date_str),
                )
                row = await cursor.fetchone()
                count = row[0] if row else 0

                items.append(
                    DataPreviewItem(
                        group_id=gid,
                        date=date_display,
                        has_data=count > 0,
                        message_count=count,
                    )
                )

                current += timedelta(days=1)

    return items


__all__ = ["router"]
