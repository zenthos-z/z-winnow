"""T-A6: 议题总结层 + 全链路溯源查询.

实现:
- get_provenance_chain(server_id) → dict   正向溯源
- trace_message_to_topics(server_id) → dict  反向溯源
- trace_topic_to_messages(topic_name) → dict  议题 → 消息下钻
- export_jsonl(start_date, end_date, output_path) → int  JSONL 导出

全链路 JOIN: raw_message ←→ parsed_contexts ←→ topic_summaries
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


async def get_provenance_chain(
    server_id: str,
    db_path: str = "data/winnow.db",
) -> dict[str, Any]:
    """从单条消息 serverID 出发，正向追溯完整的议题引用链。

    查询链路:
    1. raw_messages → 获取原始消息
    2. parsed_contexts → 查找 JSON 包含该 server_id 的上下文
    3. topic_summaries → 查找 JSON 包含该 server_id 的议题总结

    Args:
        server_id: 微信 serverId
        db_path: SQLite 数据库路径

    Returns:
        {
            "server_id": "...",
            "message": {...} | null,
            "contexts": [...],
            "topic_summaries": [...],
        }
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Layer 1: 获取原始消息
        cursor = await db.execute(
            "SELECT * FROM raw_messages WHERE serverID = ?",
            (server_id,),
        )
        msg_row = await cursor.fetchone()
        message = dict(msg_row) if msg_row else None

        # Layer 1b: Also retrieve the L2 parsed content for this message.
        # raw_messages.content now stores raw rawContent (XML/encoded),
        # so the cleaned text lives in parsed_contexts.
        parsed_content: str | None = None
        if msg_row:
            cursor = await db.execute(
                "SELECT context_text FROM parsed_contexts WHERE server_ids LIKE ? LIMIT 1",
                (f"%{server_id}%",),
            )
            ctx_row = await cursor.fetchone()
            if ctx_row:
                parsed_content = ctx_row["context_text"]
        if message is not None:
            message["parsed_content"] = parsed_content

        # Layer 2: 查找包含该 server_id 的上下文块
        # server_ids 是 JSON 数组，用 LIKE 匹配
        cursor = await db.execute(
            "SELECT * FROM parsed_contexts WHERE server_ids LIKE ?",
            (f"%{server_id}%",),
        )
        contexts = [dict(row) for row in await cursor.fetchall()]

        # Layer 3: 查找包含该 server_id 的议题总结
        cursor = await db.execute(
            "SELECT * FROM topic_summaries WHERE source_server_ids LIKE ?",
            (f"%{server_id}%",),
        )
        topic_summaries = [dict(row) for row in await cursor.fetchall()]

    return {
        "server_id": server_id,
        "message": message,
        "contexts": contexts,
        "topic_summaries": topic_summaries,
    }


async def trace_message_to_topics(
    server_id: str,
    db_path: str = "data/winnow.db",
) -> dict[str, Any]:
    """反向溯源: 从单条消息 serverID 查询其被哪些议题引用。

    查询链路:
      raw_messages.serverID ← topic_summaries.source_server_ids (JSON LIKE)

    Args:
        server_id: 微信 serverId
        db_path: SQLite 数据库路径

    Returns:
        {
            "server_id": "...",
            "message": {...} | null,
            "referenced_by_topics": [...],
        }
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # 获取原始消息
        cursor = await db.execute(
            "SELECT * FROM raw_messages WHERE serverID = ?",
            (server_id,),
        )
        msg_row = await cursor.fetchone()
        if not msg_row:
            return {
                "server_id": server_id,
                "message": None,
                "referenced_by_topics": [],
            }

        # Also retrieve L2 parsed content for this message
        cursor = await db.execute(
            "SELECT context_text FROM parsed_contexts WHERE server_ids LIKE ? LIMIT 1",
            (f"%{server_id}%",),
        )
        ctx_row = await cursor.fetchone()
        parsed_content = ctx_row["context_text"] if ctx_row else None

        message = dict(msg_row)
        message["parsed_content"] = parsed_content

        # 搜索 topic_summaries 中包含该 serverID 的记录
        cursor = await db.execute(
            "SELECT * FROM topic_summaries WHERE source_server_ids LIKE ?",
            (f"%{server_id}%",),
        )
        topics = [dict(row) for row in await cursor.fetchall()]

    return {
        "server_id": server_id,
        "message": message,
        "referenced_by_topics": topics,
    }


async def trace_topic_to_messages(
    topic_name: str,
    db_path: str = "data/winnow.db",
) -> dict[str, Any]:
    """从议题名称出发，全链路溯源到原始消息。

    查询链路:
      topic_summaries → (source_server_ids) → raw_messages
                      → (context_ids) → parsed_contexts

    Args:
        topic_name: 议题名称
        db_path: SQLite 数据库路径

    Returns:
        {
            "topic_name": "...",
            "summaries": [...],
            "contexts": [...],
            "raw_messages": [...],
        }
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Step 1: 查找议题总结
        cursor = await db.execute(
            "SELECT * FROM topic_summaries WHERE topic_name = ? ORDER BY date DESC",
            (topic_name,),
        )
        summaries = [dict(row) for row in await cursor.fetchall()]

        if not summaries:
            return {
                "topic_name": topic_name,
                "summaries": [],
                "contexts": [],
                "raw_messages": [],
            }

        # Step 2: 收集所有 source_server_ids
        all_server_ids: set[str] = set()
        for s in summaries:
            try:
                ids = json.loads(s.get("source_server_ids", "[]"))
                all_server_ids.update(ids)
            except json.JSONDecodeError:
                pass

        # Step 3: 批量获取原始消息 + L2 parsed content
        raw_messages: list[dict] = []
        for sid in all_server_ids:
            cursor = await db.execute(
                "SELECT * FROM raw_messages WHERE serverID = ?",
                (sid,),
            )
            row = await cursor.fetchone()
            if row:
                msg = dict(row)
                # Look up L2 parsed content for this message
                cursor2 = await db.execute(
                    "SELECT context_text FROM parsed_contexts WHERE server_ids LIKE ? LIMIT 1",
                    (f"%{sid}%",),
                )
                ctx_row = await cursor2.fetchone()
                msg["parsed_content"] = ctx_row["context_text"] if ctx_row else None
                raw_messages.append(msg)

        # Step 4: 收集 context_ids
        all_context_ids: set[str] = set()
        for s in summaries:
            try:
                ids = json.loads(s.get("context_ids", "[]"))
                all_context_ids.update(ids)
            except json.JSONDecodeError:
                pass

        # Step 5: 批量获取上下文
        contexts: list[dict] = []
        for cid in all_context_ids:
            cursor = await db.execute(
                "SELECT * FROM parsed_contexts WHERE context_id = ?",
                (cid,),
            )
            row = await cursor.fetchone()
            if row:
                contexts.append(dict(row))

    return {
        "topic_name": topic_name,
        "summaries": summaries,
        "contexts": contexts,
        "raw_messages": raw_messages,
    }


async def export_jsonl(
    start_date: str,
    end_date: str,
    output_path: str = "data/rl_export.jsonl",
    db_path: str = "data/winnow.db",
) -> int:
    """导出 RL 训练数据为 JSONL 格式。

    每行格式:
    {
        "topic_name": "...",
        "date": "...",
        "summary": {...},
        "confidence": 0.8,
        "model_used": "...",
        "source_server_ids": [...],
        "human_signals": [],
    }

    Args:
        start_date: 开始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD (含)
        output_path: 输出文件路径
        db_path: SQLite 数据库路径

    Returns:
        导出的记录数
    """
    # 确保输出目录存在
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM topic_summaries
               WHERE date >= ? AND date <= ?
               ORDER BY date, topic_name""",
            (start_date, end_date),
        )
        rows = await cursor.fetchall()

    count = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for row in rows:
            record = dict(row)
            # 解析 source_server_ids
            try:
                server_ids = json.loads(record.get("source_server_ids", "[]"))
            except json.JSONDecodeError:
                server_ids = []

            # 解析 summary_text
            try:
                summary = json.loads(record.get("summary_text", "{}"))
            except json.JSONDecodeError:
                summary = {"raw": record.get("summary_text", "")}

            export_record = {
                "topic_name": record.get("topic_name", ""),
                "date": record.get("date", ""),
                "summary": summary,
                "confidence": record.get("confidence"),
                "model_used": record.get("model_used", ""),
                "source_server_ids": server_ids,
                "human_signals": [],  # 预留 RL 反馈标记
            }
            f.write(json.dumps(export_record, ensure_ascii=False) + "\n")
            count += 1

    logger.info(
        "Exported %d records to %s (date range: %s ~ %s)",
        count,
        output_path,
        start_date,
        end_date,
    )

    return count
