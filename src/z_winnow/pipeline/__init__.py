"""Pipeline package — CipherTalk client, SQLite storage, data ingestion, context assembly.

Provides the complete data pipeline from CipherTalk API to SQLite three-layer storage:

Layer 1 — raw_messages:
    ingest_day / ingest_messages_batch → INSERT OR REPLACE into raw_messages

Layer 2 — parsed_contexts:
    assemble_daily_context → context blocks with server_ids JSON FK

Layer 3 — topic_summaries:
    provenance trace queries → bidirectional serverID provenance

Sandbox:
    Prompt injection detection + XML wrapping + role boundary injection

Orchestration:
    run(date, group_name) → fetch → ingest → context → provenance → stats

Re-exports all public symbols for convenient single-package import:
    from z_winnow.pipeline import CipherTalkClient, ingest_day, run, ...
"""

from __future__ import annotations

import logging

from z_winnow.pipeline.cipher_talk_client import (
    CipherTalkClient,
    create_data_client,
)
from z_winnow.pipeline.context import (
    assemble_daily_context,
    estimate_tokens,
    format_message,
    format_messages,
)
from z_winnow.pipeline.database import (
    SCHEMA_SQL,
    get_context_count,
    get_contexts_by_date,
    get_message_count,
    get_message_stats,
    get_raw_message,
    get_raw_messages_by_date,
    get_topic_count,
    get_topics_by_date,
    init_database,
    init_database_in_conn,
    insert_raw_messages,
)
from z_winnow.pipeline.ingest import (
    ingest_day,
    ingest_messages_batch,
)
from z_winnow.pipeline.provenance import (
    export_jsonl,
    get_provenance_chain,
    trace_message_to_topics,
    trace_topic_to_messages,
)
from z_winnow.pipeline.sandbox import (
    contains_sanitized_pattern,
    detect_sanitized_patterns,
    inject_role_boundary_prompt,
    sanitize_messages,
)
from z_winnow.storage import Storage

logger = logging.getLogger(__name__)

# ============================================================
# Pipeline orchestration
# ============================================================


async def run(
    date: str,
    group_name: str | None = None,
    *,
    db_path: str = "data/winnow.db",
    base_url: str | None = None,
    token: str | None = None,
) -> dict[str, int]:
    """执行完整的数据管道: fetch → ingest (L1) → context (L2) → provenance check.

    单次调用涵盖从 CipherTalk API 拉取消息到 SQLite 两层入库的完整流程，
    并在末尾执行采样溯源验证，确保数据完整性。

    Args:
        date: 目标日期，格式 YYYYMMDD（如 20260428）。
        group_name: 群聊显示名称。若为 None，从环境变量
            ``WINNOW_GROUP_NAME`` 读取。
        db_path: SQLite 数据库文件路径，默认 ``data/winnow.db``。
        base_url: CipherTalk API 基础 URL。若为 None，从 Settings 读取，
            默认 ``http://127.0.0.1:5031``。
        token: CipherTalk API 认证 token。若为 None，从 Settings 读取。

    Returns:
        管道统计字典:
        - ``total``: 从 CipherTalk 获取的消息总数 (清洗后)
        - ``new``: 本次新入库的消息数
        - ``sanitized``: 被标记为 prompt injection 的消息数
        - ``context_count``: 组装并写入 parsed_contexts 的上下文块数

    Raises:
        ValueError: 未配置 group_name 或 CipherTalk 未返回任何消息。
        httpx.HTTPError: CipherTalk API 调用失败。

    Example:
        >>> import os
        >>> os.environ["WINNOW_ENV"] = "test"
        >>> os.environ["WINNOW_GROUP_NAME"] = "测试群聊"
        >>> stats = await run("20260428")
        >>> print(stats)
        {'total': 10, 'new': 10, 'sanitized': 0, 'context_count': 1}
    """
    import random

    # ── 0. 解析配置 ──────────────────────────────────────────
    # T-W12-5: S7 配置单源 — read defaults from Settings
    from z_winnow.config.settings import get_settings

    _settings = get_settings()
    resolved_group = group_name or _settings.group_name.strip()
    if not resolved_group:
        raise ValueError("group_name 未指定且 Settings.group_name 未设置。")

    resolved_base_url: str = base_url or _settings.effective_data_base_url
    resolved_token: str = token or _settings.effective_data_token

    # ── 0.5 解析群组标识 — display_name → group_id → chatroom_id ──
    # chatroom_id 是 CipherTalk 原生唯一标识，直传避免 displayName 模糊匹配
    from z_winnow.pipeline.group_config import (
        resolve_chatroom_id,
        resolve_group_id,
    )

    resolved_group_id = await resolve_group_id(resolved_group, db_path=db_path)
    resolved_chatroom_id = await resolve_chatroom_id(resolved_group_id, db_path=db_path)

    # ── 1. Fetch (CipherTalk Client) ──────────────────────────
    client = create_data_client(
        base_url=resolved_base_url,
        token=resolved_token,
    )
    member_map: dict[str, str] = {}
    try:
        messages = await client.fetch_messages(
            group_name=resolved_group,
            date=date,
            chatroom_id=resolved_chatroom_id,
        )

        # ── 1.5 构建 member_map (wxid → 昵称) ─────────────────
        if resolved_chatroom_id and "@chatroom" in resolved_chatroom_id:
            try:
                from z_winnow.pipeline.group_config import build_member_map

                member_map = await build_member_map(resolved_chatroom_id, client)
            except Exception:
                pass

        # ── 1.6 用 member_map 丰富消息的 account_name ──────────
        if member_map:
            for msg in messages:
                wxid = msg.get("sender", "")
                if wxid in member_map:
                    current = msg.get("account_name", "")
                    if (
                        not current
                        or current == wxid
                        or current.startswith("wxid_")
                        or current.endswith("@openim")
                    ):
                        msg["account_name"] = member_map[wxid]
                        msg["group_nickname"] = member_map[wxid]
    finally:
        await client.close()

    if not messages:
        logger.info("No messages fetched for group '%s' on %s", resolved_group, date)
        return {"total": 0, "new": 0, "sanitized": 0, "context_count": 0}

    # ── 2. Ingest (Layer 1: raw_messages) ─────────────────────
    ingest_stats = await ingest_messages_batch(
        messages=messages,
        date=date,
        db_path=db_path,
        group_id=resolved_group_id,
    )

    # ── 3. Context Assembly (Layer 2: parsed_contexts) ────────
    contexts = await assemble_daily_context(
        date=date,
        db_path=db_path,
        group_id=resolved_group_id,
    )

    # ── 4. Provenance Check ───────────────────────────────────
    # 采样几条消息验证三层溯源链路可通
    sample_size = min(5, len(messages))
    if sample_size > 0:
        sample = random.sample(messages, sample_size)
        verified = 0
        for msg in sample:
            server_id = msg.get("server_id", "")
            if server_id:
                chain = await get_provenance_chain(
                    server_id=server_id,
                    db_path=db_path,
                )
                if chain.get("message"):
                    verified += 1
        logger.info(
            "Provenance check: %d/%d sampled messages verified in DB.",
            verified,
            sample_size,
        )

    # ── 5. Compile stats ──────────────────────────────────────
    stats: dict[str, int] = {
        "total": ingest_stats.get("total", len(messages)),
        "new": ingest_stats.get("new", 0),
        "sanitized": ingest_stats.get("sanitized", 0),
        "context_count": len(contexts),
    }

    logger.info(
        "Pipeline run complete for %s / %s: total=%d new=%d sanitized=%d contexts=%d",
        resolved_group,
        date,
        stats["total"],
        stats["new"],
        stats["sanitized"],
        stats["context_count"],
    )

    return stats


__all__ = [
    "SCHEMA_SQL",
    "CipherTalkClient",
    "Storage",
    "assemble_daily_context",
    "contains_sanitized_pattern",
    "create_data_client",
    "detect_sanitized_patterns",
    "estimate_tokens",
    "export_jsonl",
    "format_message",
    "format_messages",
    "get_context_count",
    "get_contexts_by_date",
    "get_message_count",
    "get_message_stats",
    "get_provenance_chain",
    "get_raw_message",
    "get_raw_messages_by_date",
    "get_topic_count",
    "get_topics_by_date",
    "ingest_day",
    "ingest_messages_batch",
    "init_database",
    "init_database_in_conn",
    "inject_role_boundary_prompt",
    "insert_raw_messages",
    "run",
    "sanitize_messages",
    "trace_message_to_topics",
    "trace_topic_to_messages",
]
