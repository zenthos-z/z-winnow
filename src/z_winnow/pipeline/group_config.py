"""群组配置解析 — chatroom_id 优先级解析链。

提供 `resolve_chatroom_id()` 核心函数，按优先级解析群组的 chatroom_id：
  1. CLI 参数（直接传入 chatroom_id 参数）
  2. groups 表查询（chatroom_id WHERE group_id=X）
  3. 全局 settings 兜底（使用 group_id 自身作为 chatroom_id 或报错）

辅助函数:
- resolve_chatroom_id_with_fallback: 3 级回退解析（含 group_name/raw_json 回退）
- build_member_map: 从数据源 API 构建群成员映射
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


async def resolve_chatroom_id(
    group_id: str,
    db_path: str = "data/winnow.db",
) -> str:
    """从 groups 表按 group_id 解析 chatroom_id。

    若未找到匹配行 → 抛出 ValueError（不静默失败）。

    Args:
        group_id: 群组标识符（对应 groups.group_id 主键）
        db_path: SQLite 数据库路径

    Returns:
        groups 表中配置的 chatroom_id

    Raises:
        ValueError: groups 表中无匹配 group_id 时
        FileNotFoundError: 数据库文件不存在时
    """
    chatroom_id: str | None = None

    db_file = Path(db_path)
    if not db_file.exists():
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT chatroom_id FROM groups WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        if row is not None:
            chatroom_id = row[0]

    if chatroom_id is None:
        raise ValueError(
            f"groups 表中未找到 group_id='{group_id}' 的 chatroom_id 配置。"
            f"请检查数据库 groups 表或传入 chatroom_id 参数。"
        )

    logger.debug("Resolved chatroom_id=%s for group_id=%s", chatroom_id, group_id)
    return chatroom_id


def resolve_chatroom_id_sync(
    group_id: str,
    db_path: str = "data/winnow.db",
) -> str:
    """同步版本的 resolve_chatroom_id（用于非异步上下文）。

    注意：此函数在内部使用 asyncio.run()，不适用于已有运行中的 event loop。
    """
    import asyncio

    return asyncio.run(resolve_chatroom_id(group_id, db_path))


async def resolve_chatroom_id_with_fallback(
    group_id: str,
    group_name: str,
    raw_messages: list[dict[str, Any]],
    db_path: str = "data/winnow.db",
) -> str:
    """3 级回退解析 chatroom_id，从 builder.py/data_fetch.py 提取的共享逻辑。

    回退链（对齐 builder.py 的严格检查）：
    1. group_id 非空 → resolve_chatroom_id()（suppress 异常）
    2. "@chatroom" in group_name → 返回 group_name（严格检查）
    3. raw_messages[0].raw_json → 提取 sessionId/talker

    Returns:
        解析出的 chatroom_id，或空字符串（不抛异常）
    """
    chatroom_id = ""

    # Level 1: 从 groups 表解析
    if group_id:
        try:
            chatroom_id = await resolve_chatroom_id(group_id, db_path)
        except (ValueError, FileNotFoundError, Exception) as exc:
            logger.debug("resolve_chatroom_id failed for %s — %s", group_id, exc)

    # Level 2: group_name 本身可能就是 chatroom 格式
    if not chatroom_id and "@chatroom" in group_name:
        chatroom_id = group_name

    # Level 3: 从消息 raw_json 推断
    # Old format: sessionId/talker fields in cleaned dict
    # New format (raw CipherTalk API): no sessionId per-message, try group_id
    #   column from raw_messages or query groups table with group_id field.
    if not chatroom_id and raw_messages:
        first_raw = raw_messages[0].get("raw_json", "")
        if first_raw:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                first_data = json.loads(first_raw)
                chatroom_id = str(first_data.get("sessionId", "") or first_data.get("talker", ""))
        # Raw API format: raw_json has no sessionId/talker. Fall back to
        # the group_id column on the raw_messages row and resolve via groups.
        if not chatroom_id:
            msg_group_id = raw_messages[0].get("group_id", "")
            if msg_group_id:
                with contextlib.suppress(Exception):
                    chatroom_id = await resolve_chatroom_id(msg_group_id, db_path)

    return chatroom_id


async def build_member_map(
    chatroom_id: str,
    wf_client: Any,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """从数据源 API 获取群成员列表，构建 wxid → display_name 映射。

    优先使用 get_group_members()（单次 API 调用），
    若返回为空且有 messages，fallback 到 build_member_map_from_messages()。

    永不上抛异常（P014 优雅降级）。空 chatroom_id 或不含 @chatroom 时返回 {}。

    Args:
        chatroom_id: 微信群 ID，格式 xxx@chatroom
        wf_client: CipherTalkClient 实例（需支持 get_group_members / build_member_map_from_messages）
        messages: 可选消息列表，用于 fallback 按需反查

    Returns:
        wxid → display_name 映射字典
    """
    if not chatroom_id or "@chatroom" not in chatroom_id:
        return {}

    try:
        members_resp = await wf_client.get_group_members(chatroom_id)
        member_map: dict[str, str] = {}
        for member in members_resp.get("members", []):
            wxid = member.get("wxid", "")
            if wxid:
                display_name = (
                    member.get("group_nickname")
                    or member.get("displayName")
                    or member.get("nickname")
                    or member.get("remark")
                    or member.get("alias")
                    or wxid
                )
                member_map[wxid] = display_name

        # Fallback: 按消息中的 wxid 逐个反查
        if not member_map and messages and hasattr(wf_client, "build_member_map_from_messages"):
            member_map = await wf_client.build_member_map_from_messages(messages)

        return member_map
    except Exception as exc:
        logger.warning("build_member_map failed for %s — %s", chatroom_id, exc)
        return {}


async def resolve_group_id(
    group_name: str,
    db_path: str = "data/winnow.db",
) -> str:
    """从 groups 表按 display_name 或 group_id 解析 group_id。

    优先精确匹配 display_name，其次尝试 group_id 自身，最后尝试 chatroom_id。
    若未找到匹配行 → 抛出 ValueError。

    Args:
        group_name: 群聊显示名、group_id 或 chatroom_id
        db_path: SQLite 数据库路径

    Returns:
        groups 表中的 group_id

    Raises:
        ValueError: groups 表中无匹配时
        FileNotFoundError: 数据库文件不存在时
    """
    db_file = Path(db_path)
    if not db_file.exists():
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")

    resolved: str | None = None

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT group_id FROM groups WHERE display_name = ? LIMIT 1",
            (group_name,),
        )
        row = await cursor.fetchone()
        if row is not None:
            resolved = row[0]
        else:
            cursor = await db.execute(
                "SELECT group_id FROM groups WHERE group_id = ? LIMIT 1",
                (group_name,),
            )
            row = await cursor.fetchone()
            if row is not None:
                resolved = row[0]
            else:
                cursor = await db.execute(
                    "SELECT group_id FROM groups WHERE chatroom_id = ? LIMIT 1",
                    (group_name,),
                )
                row = await cursor.fetchone()
                if row is not None:
                    resolved = row[0]

    if resolved is None:
        raise ValueError(
            f"groups 表中未找到 display_name='{group_name}' 或 group_id='{group_name}' 的记录。"
            f"请先在 Web 控制面板或 groups 表中注册该群组。"
        )

    logger.debug("Resolved group_id=%s for input=%s", resolved, group_name)
    return resolved


def resolve_group_id_sync(
    group_name: str,
    db_path: str = "data/winnow.db",
) -> str:
    """同步版本的 resolve_group_id（用于非异步上下文）。"""
    import asyncio

    return asyncio.run(resolve_group_id(group_name, db_path))
