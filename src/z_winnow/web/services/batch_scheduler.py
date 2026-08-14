"""Batch Scheduler — 批量日报生成调度器。

分群并行，群内串行执行。支持空数据快速跳过 + MemOS 空信号登记。

核心模式:
  - P057: Semaphore 并发控制
  - L037: 单条失败不影响批次
  - P067: SQLite-backed 异步任务
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import aiosqlite

from z_winnow.config.settings import get_settings
from z_winnow.web.services import batch_db

logger = logging.getLogger(__name__)


class BatchScheduler:
    """批量日报生成调度器。

    调度模型：分群并行 + 群内串行。
    - 不同群并发执行（通过 Semaphore 控制并发上限）
    - 同一群内逐天串行执行

    Attributes:
        db_path: SQLite 数据库路径。
        max_parallel_groups: 最大并行群数。
        _group_semaphore: 群级并发控制信号量。
        _cancel_flags: 批次取消信号字典 {batch_id: Event}。
    """

    def __init__(self, db_path: str, max_parallel_groups: int = 3) -> None:
        """初始化调度器。

        Args:
            db_path: SQLite 数据库路径。
            max_parallel_groups: 最大并行群数（默认 3）。
        """
        self.db_path = db_path
        self.max_parallel_groups = max_parallel_groups
        self._group_semaphore = asyncio.Semaphore(max_parallel_groups)
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}

    async def run_batch(self, batch_id: str) -> dict[str, Any]:
        """执行一个批次任务。

        主调度循环：
        1. 读取所有 items，按 group_id 分组
        2. 更新 batch_jobs.status = 'running'
        3. 对每群（用 Semaphore 控制并发）：
           - 群内逐天串行执行
           - 检查取消信号
           - 预检空数据并跳过
        4. 所有群完成后更新 batch_jobs 状态

        Args:
            batch_id: 批次 ID。

        Returns:
            执行结果摘要。
        """
        # 初始化取消信号
        cancel_flag = asyncio.Event()
        self._cancel_flags[batch_id] = cancel_flag

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # 1. 读取批次信息
            batch = await batch_db.get_batch_job(db, batch_id)
            if not batch:
                logger.error("run_batch: batch_id=%s not found", batch_id)
                return {"batch_id": batch_id, "status": "not_found"}

            # 2. 更新状态为 running
            await batch_db.update_batch_job(
                db,
                batch_id,
                status="running",
                started_at=datetime.utcnow().isoformat(),
            )

            # 3. 读取所有 items
            items = await batch_db.get_batch_items(db, batch_id)

            # 4. 按 group_id 分组
            groups: dict[str, list[dict]] = defaultdict(list)
            for item in items:
                groups[item["group_id"]].append(item)

            # 5. 并行执行各群
            async def run_group(group_id: str, group_items: list[dict]) -> None:
                async with self._group_semaphore:
                    for item in group_items:
                        if cancel_flag.is_set():
                            await batch_db.update_batch_item(
                                db,
                                item["item_id"],
                                status="cancelled",
                                completed_at=datetime.utcnow().isoformat(),
                            )
                            continue

                        await self._run_single_item(db, item, cancel_flag)

            # 启动所有群的并行任务
            tasks = [asyncio.create_task(run_group(g, items)) for g, items in groups.items()]
            self._running_tasks[batch_id] = asyncio.gather(*tasks)

            try:
                await self._running_tasks[batch_id]
            except asyncio.CancelledError:
                logger.info("run_batch: batch_id=%s cancelled", batch_id)
            finally:
                self._running_tasks.pop(batch_id, None)

            # 6. 计算最终状态
            stats = await batch_db.get_batch_progress_stats(db, batch_id)

            final_status = "completed"
            if cancel_flag.is_set():
                final_status = "cancelled"
            elif stats["failed"] > 0:
                final_status = "partial_failed"

            await batch_db.update_batch_job(
                db,
                batch_id,
                status=final_status,
                completed=stats["completed"],
                failed=stats["failed"],
                skipped_empty=stats["skipped_empty"],
                completed_at=datetime.utcnow().isoformat(),
            )

        # 清理取消信号
        self._cancel_flags.pop(batch_id, None)

        logger.info(
            "run_batch: batch_id=%s completed status=%s total=%d done=%d fail=%d skip=%d",
            batch_id,
            final_status,
            stats["total"],
            stats["completed"],
            stats["failed"],
            stats["skipped_empty"],
        )

        return {
            "batch_id": batch_id,
            "status": final_status,
            "total": stats["total"],
            "completed": stats["completed"],
            "failed": stats["failed"],
            "skipped_empty": stats["skipped_empty"],
        }

    async def _run_single_item(
        self,
        db: aiosqlite.Connection,
        item: dict[str, Any],
        cancel_flag: asyncio.Event,
    ) -> str:
        """执行单条任务（群×日期）。

        流程：
        1. 预检：通过 _check_data() 查询 CipherTalk API 判断数据源是否有消息
        2. 若无数据 → skipped_empty + 登记 MemOS 空信号
        3. 若有数据 → 执行 orchestrate() pipeline
        4. 更新 item 状态

        _check_data 直接查 CipherTalk API（非本地 raw_messages），
        首次运行的日期也能正确判断是否有数据。

        Args:
            db: 数据库连接。
            item: 任务项字典。
            cancel_flag: 取消信号。

        Returns:
            最终状态字符串。
        """
        item_id = item["item_id"]
        group_id = item["group_id"]
        date = item["date"]

        # 更新状态为 running
        await batch_db.update_batch_item(
            db,
            item_id,
            status="running",
            started_at=datetime.utcnow().isoformat(),
        )

        try:
            # 1. 预检：查询数据源 API 判断是否有数据
            has_data, _message_count = await self._check_data(db, group_id, date)

            if not has_data:
                # 2. API 确认无数据 → 跳过 + 登记空信号
                await self._register_empty(db, group_id, date)

                await batch_db.update_batch_item(
                    db,
                    item_id,
                    status="skipped_empty",
                    completed_at=datetime.utcnow().isoformat(),
                )

                logger.info(
                    "_run_single_item: item_id=%s group=%s date=%s skipped_empty (API: no messages)",
                    item_id,
                    group_id,
                    date,
                )

                return "skipped_empty"

            # 3. API 确认有数据（或 API 异常保守放行）→ 执行 pipeline
            run_id = str(uuid.uuid4())

            # 更新 run_id
            await batch_db.update_batch_item(db, item_id, run_id=run_id)

            # 执行 orchestrate
            from z_winnow.orchestrator import orchestrate
            from z_winnow.web.services.run_service import resolve_group_name

            group_name = await resolve_group_name(group_id, self.db_path)
            settings = get_settings()

            await orchestrate(
                group_name=group_name,
                date=date.replace("-", ""),
                report_types=["daily"],
                api_base_url=settings.effective_data_base_url,
                api_token=settings.effective_data_token,
                run_id=run_id,
            )

            # 自动推送飞书（fire-and-forget）：群开启 feishu_enabled 时排队一个后台上传任务。
            # 后台跑、毫秒级返回，绝不阻塞本群下一个日期的生成；失败不影响 item 终态。
            try:
                from z_winnow.web.services.report_service import auto_push_after_run

                await auto_push_after_run(
                    group_id=group_id, date=date, db_path=self.db_path, run_id=run_id
                )
            except Exception:
                logger.exception(
                    "_run_single_item: auto_push_after_run failed group=%s date=%s",
                    group_id,
                    date,
                )

            # 4. 成功
            await batch_db.update_batch_item(
                db,
                item_id,
                status="completed",
                progress_pct=100,
                completed_at=datetime.utcnow().isoformat(),
            )

            logger.info(
                "_run_single_item: item_id=%s group=%s date=%s completed run_id=%s",
                item_id,
                group_id,
                date,
                run_id,
            )

            return "completed"

        except Exception as exc:
            # 异常处理
            error_msg = f"{type(exc).__name__}: {exc}"
            await batch_db.update_batch_item(
                db,
                item_id,
                status="failed",
                error_message=error_msg,
                completed_at=datetime.utcnow().isoformat(),
            )

            logger.exception(
                "_run_single_item: item_id=%s group=%s date=%s failed",
                item_id,
                group_id,
                date,
            )

            return "failed"

    async def _check_data(
        self,
        db: aiosqlite.Connection,
        group_id: str,
        date: str,
    ) -> tuple[bool, int]:
        """预检指定群日期在数据源（CipherTalk API）中是否有消息。

        直接查询远程 API 而非本地 raw_messages 表。
        raw_messages 是 pipeline 的 data_fetch 节点写入的——首次运行的日期
        raw_messages 永远是空的，用本地 DB 预检会错误地跳过首次抓取。

        若 API 调用失败，保守返回 (True, -1) → 继续执行 pipeline，
        宁可多跑一次也不跳过可能有数据的日期。

        Args:
            db: 数据库连接（用于解析 chatroom_id）。
            group_id: 群组 ID。
            date: 日期字符串（YYYY-MM-DD 或 YYYYMMDD）。

        Returns:
            (has_data, message_count) 元组。count=-1 表示 API 错误（保守放行）。
        """
        normalized_date = date.replace("-", "") if "-" in date else date

        try:
            from z_winnow.pipeline.cipher_talk_client import create_data_client
            from z_winnow.pipeline.group_config import resolve_chatroom_id

            settings = get_settings()

            # 解析 chatroom_id
            chatroom_id = await resolve_chatroom_id(group_id, self.db_path)

            # 创建客户端并查询
            async with create_data_client(
                base_url=settings.effective_data_base_url,
                token=settings.effective_data_token,
            ) as client:
                has_data, count = await client.check_messages_count(
                    chatroom_id=chatroom_id,
                    date=normalized_date,
                    limit=1,
                )
                return (has_data, count)

        except Exception as exc:
            logger.warning(
                "_check_data: API check failed for group=%s date=%s — %s, will run pipeline",
                group_id,
                normalized_date,
                exc,
            )
            # 保守放行：API 调用失败时不跳过，让 pipeline 自己判断
            return (True, -1)

    async def _register_empty(
        self,
        db: aiosqlite.Connection,
        group_id: str,
        date: str,
    ) -> None:
        """登记空数据信号到 MemOS。

        Args:
            db: 数据库连接。
            group_id: 群组 ID。
            date: 日期字符串。
        """
        from z_winnow.web.services.empty_day_signal import register_empty_day_signal

        try:
            await register_empty_day_signal(db, group_id, date)
        except Exception as exc:
            logger.warning(
                "_register_empty: failed to register empty signal for group=%s date=%s — %s",
                group_id,
                date,
                exc,
            )

    async def cancel_batch(self, batch_id: str) -> bool:
        """取消一个批次。

        Args:
            batch_id: 批次 ID。

        Returns:
            是否成功取消。
        """
        if batch_id in self._cancel_flags:
            self._cancel_flags[batch_id].set()

            # 取消运行中的任务
            if batch_id in self._running_tasks:
                self._running_tasks[batch_id].cancel()

            logger.info("cancel_batch: batch_id=%s cancel signal set", batch_id)
            return True

        # 批次不在运行中，直接更新数据库状态
        async with aiosqlite.connect(self.db_path) as db:
            await batch_db.cancel_batch_items(db, batch_id)
            await batch_db.update_batch_job(
                db,
                batch_id,
                status="cancelled",
                completed_at=datetime.utcnow().isoformat(),
            )

        logger.info("cancel_batch: batch_id=%s cancelled (not running)", batch_id)
        return True


# ============================================================
# SSE 流生成
# ============================================================


async def stream_batch_progress(
    db_path: str,
    batch_id: str,
    *,
    poll_interval_s: float = 1.0,
    max_iterations: int = 3600,  # 1 hour max
) -> AsyncGenerator[str, None]:
    """SSE 流：实时推送批次进度。

    Args:
        db_path: 数据库路径。
        batch_id: 批次 ID。
        poll_interval_s: 轮询间隔（秒）。
        max_iterations: 最大迭代次数。

    Yields:
        SSE 格式字符串 "data: {...}\\n\\n"。
    """
    import json

    iteration = 0
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row

            while iteration < max_iterations:
                # 查询批次状态
                batch = await batch_db.get_batch_job(db, batch_id)
                if not batch:
                    payload = json.dumps({"type": "error", "message": "batch not found"})
                    yield f"data: {payload}\n\n"
                    break

                # 查询进度统计
                stats = await batch_db.get_batch_progress_stats(db, batch_id)

                # 计算进度百分比
                total = stats["total"] or 1
                done = stats["completed"] + stats["failed"] + stats["skipped_empty"]
                progress_pct = int((done / total) * 100)

                # 推送批次更新事件
                batch_event = {
                    "type": "batch_update",
                    "batch_id": batch_id,
                    "status": batch["status"],
                    "completed": stats["completed"],
                    "failed": stats["failed"],
                    "skipped_empty": stats["skipped_empty"],
                    "total": stats["total"],
                    "progress_pct": progress_pct,
                }
                yield f"data: {json.dumps(batch_event)}\n\n"

                # 检查是否完成
                if batch["status"] in ("completed", "cancelled", "partial_failed"):
                    # 推送完成事件
                    complete_event = {
                        "type": "batch_complete",
                        "batch_id": batch_id,
                        "status": batch["status"],
                    }
                    yield f"data: {json.dumps(complete_event)}\n\n"
                    break

                iteration += 1
                await asyncio.sleep(poll_interval_s)

    except Exception as exc:
        logger.exception("stream_batch_progress: error for batch_id=%s", batch_id)
        payload = json.dumps({"type": "error", "message": str(exc)})
        yield f"data: {payload}\n\n"


__all__ = ["BatchScheduler", "stream_batch_progress"]
