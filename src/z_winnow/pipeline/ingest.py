"""T-A4: 原始消息入库管道 (Layer 1: raw_messages).

实现 ingest_day(date) → dict[str, int]:
- 调用 CipherTalk client 获取消息
- 逐条 INSERT OR REPLACE (serverID 去重)
- sanitized 字段标记潜在的 prompt injection 模式
- 保留完整 raw_json
- 返回入库统计 (total/new/sanitized)

独立于后续的上下文组装，确保数据管道可单独运行。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from z_winnow.pipeline.cipher_talk_client import create_data_client
from z_winnow.pipeline.database import init_database_in_conn
from z_winnow.pipeline.sandbox import (
    contains_sanitized_pattern,
    detect_sanitized_patterns,
)

logger = logging.getLogger(__name__)


async def ingest_day(
    date: str,
    group_name: str,
    db_path: str = "data/winnow.db",
    base_url: str = "http://127.0.0.1:5031",
    token: str = "",
    group_id: str = "",
) -> dict[str, int]:
    """单天消息入库管道: fetch → sanitize → INSERT OR REPLACE.

    Args:
        date: 目标日期 YYYYMMDD
        group_name: 群聊显示名称
        db_path: SQLite 数据库路径
        base_url: CipherTalk API 基础 URL
        token: CipherTalk API 认证 token
        group_id: 群组标识符（groups 表 PK）

    Returns:
        {"total": ..., "new": ..., "updated": ..., "sanitized": ..., "skipped": ...}
    """
    async with create_data_client(base_url=base_url, token=token) as client:
        messages = await client.fetch_messages(
            group_name=group_name, date=date, group_id=group_id or None
        )

    if not messages:
        logger.info("No messages fetched for group '%s' on %s", group_name, date)
        return {"total": 0, "new": 0, "updated": 0, "sanitized": 0, "skipped": 0}

    async with aiosqlite.connect(db_path) as db:
        await init_database_in_conn(db)
        stats = await _persist_messages(db, messages, date, group_id=group_id)
        await db.commit()

    return stats


async def ingest_messages_batch(
    messages: list[dict[str, Any]],
    date: str,
    db_path: str = "data/winnow.db",
    group_id: str = "",
) -> dict[str, int]:
    """将已获取的消息列表批量入库（不调用 CipherTalk API）。

    Args:
        messages: 消息字典列表
        date: 数据日期 YYYYMMDD
        db_path: SQLite 数据库路径
        group_id: 群组标识符，写入 raw_messages.group_id

    Returns:
        入库统计 (同 ingest_day)
    """
    async with aiosqlite.connect(db_path) as db:
        await init_database_in_conn(db)
        stats = await _persist_messages(db, messages, date, group_id=group_id)
        await db.commit()

    return stats


async def _persist_messages(
    db: aiosqlite.Connection,
    messages: list[dict[str, Any]],
    date: str,
    group_id: str = "",
) -> dict[str, int]:
    """逐条写入 raw_messages 表，执行 INSERT OR REPLACE 去重。"""
    total = len(messages)
    new_count = 0
    updated_count = 0
    sanitized_count = 0
    skipped_count = 0

    for msg in messages:
        server_id = msg.get("server_id", "")
        if not server_id:
            skipped_count += 1
            continue

        content = msg.get("content", "")
        is_sanitized = contains_sanitized_pattern(content)
        if is_sanitized:
            sanitized_count += 1
            detect_sanitized_patterns(content)

        raw_json = msg.get("raw_json") or json.dumps(msg, ensure_ascii=False)

        cursor = await db.execute(
            "SELECT COUNT(*) FROM raw_messages WHERE serverID = ?", (server_id,)
        )
        row = await cursor.fetchone()
        is_new = row is None or row[0] == 0

        await db.execute(
            """INSERT OR REPLACE INTO raw_messages
               (serverID, date, sender, content, msg_type, image_path, sanitized, raw_json, group_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                server_id,
                date,
                msg.get("account_name", "") or msg.get("sender", ""),
                content,
                msg.get("msg_type", "text"),
                msg.get("media_url", "") or None,
                1 if is_sanitized else 0,
                raw_json,
                group_id,
            ),
        )

        if is_new:
            new_count += 1
        else:
            updated_count += 1

    logger.info(
        "Ingested %d messages: %d new, %d updated, %d sanitized, %d skipped",
        total,
        new_count,
        updated_count,
        sanitized_count,
        skipped_count,
    )

    return {
        "total": total,
        "new": new_count,
        "updated": updated_count,
        "sanitized": sanitized_count,
        "skipped": skipped_count,
    }
