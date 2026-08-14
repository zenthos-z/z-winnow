"""MemOS 空信号登记。

当日无消息时，在 MemOS 中登记空信号，用于趋势分析和活跃度监控。

空信号结构:
  - memory_type: "empty_day_signal"
  - group_id: 群组 ID
  - date: 日期
  - consecutive_empty_days: 连续空天数
  - last_activity_date: 最近有消息的日期
"""

from __future__ import annotations

import logging
from datetime import datetime

import aiosqlite

logger = logging.getLogger(__name__)


async def register_empty_day_signal(
    db: aiosqlite.Connection,
    group_id: str,
    date: str,
    db_path: str | None = None,
) -> int:
    """在 MemOS 中登记该群某天无消息的空信号。

    空信号通过 memos_sync_queue 异步写入，不阻塞调度器。

    Args:
        db: aiosqlite 数据库连接（用于查询连续空天数和写入队列）。
        group_id: 群组 ID。
        date: 日期字符串（YYYYMMDD 或 YYYY-MM-DD 格式均可）。
        db_path: 数据库路径（用于 enqueue_sync_job，可选）。

    Returns:
        写入的 queue_id（>0 表示成功）。
    """
    from z_winnow.pipeline.database import enqueue_sync_job

    # 规范化日期格式
    normalized_date = date.replace("-", "") if "-" in date else date
    display_date = f"{normalized_date[:4]}-{normalized_date[4:6]}-{normalized_date[6:8]}"

    # 查询连续空天数
    consecutive = await _compute_consecutive_empty_days(db, group_id, normalized_date)
    last_activity = await _get_last_activity_date(db, group_id, normalized_date)

    # 构建空信号记忆文本
    memory_text = (
        f"空信号登记\n"
        f"群ID: {group_id}\n"
        f"日期: {display_date}\n"
        f"状态: 当日无消息\n"
        f"连续空天数: {consecutive}\n"
        f"最近活跃: {last_activity or '未知'}"
    )

    cube_id = f"{group_id}:empty_days"

    # 异步写入 MemOS
    queue_id = await enqueue_sync_job(
        db,
        op_type="add_topic",
        cube_id=cube_id,
        payload={
            "group_id": group_id,
            "summary": memory_text,
            "source": "batch_empty_check",
            "dedupe_key": f"{group_id}:{normalized_date}:empty_signal",
            "metadata": {
                "memory_type": "empty_day_signal",
                "consecutive_empty_days": consecutive,
                "last_activity_date": last_activity,
                "signal_strength": "high"
                if consecutive > 7
                else "medium"
                if consecutive > 3
                else "low",
            },
        },
    )

    logger.info(
        "register_empty_day_signal: group=%s date=%s consecutive=%d queue_id=%d",
        group_id,
        display_date,
        consecutive,
        queue_id,
    )

    return queue_id


async def _compute_consecutive_empty_days(
    db: aiosqlite.Connection,
    group_id: str,
    date: str,
) -> int:
    """计算截至指定日期的连续空天数。

    从指定日期往前倒推，直到遇到有消息的日期。

    Args:
        db: 数据库连接。
        group_id: 群组 ID。
        date: 日期字符串 YYYYMMDD。

    Returns:
        连续空天数（含当天）。
    """
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT date FROM raw_messages WHERE group_id = ? AND date < ? ORDER BY date DESC LIMIT 1",
        (group_id, date),
    )
    row = await cursor.fetchone()
    if not row:
        # 没有历史消息，无法计算连续天数，返回 1（当天）
        return 1

    last_msg_date_str = row["date"]
    try:
        last_msg_date = datetime.strptime(last_msg_date_str, "%Y%m%d")
        current_date = datetime.strptime(date, "%Y%m%d")
    except ValueError:
        return 1

    delta_days = (current_date - last_msg_date).days
    # 连续空天数 = 当前日期与最近有消息日期的差值
    return max(1, delta_days)


async def _get_last_activity_date(
    db: aiosqlite.Connection,
    group_id: str,
    date: str,
) -> str | None:
    """获取最近有消息的日期。

    Args:
        db: 数据库连接。
        group_id: 群组 ID。
        date: 日期字符串 YYYYMMDD。

    Returns:
        最近有消息的日期（YYYY-MM-DD 格式），若无历史消息则返回 None。
    """
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT date FROM raw_messages WHERE group_id = ? AND date < ? ORDER BY date DESC LIMIT 1",
        (group_id, date),
    )
    row = await cursor.fetchone()
    if not row:
        return None

    last_date = row["date"]
    return f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:8]}"


__all__ = ["register_empty_day_signal"]
