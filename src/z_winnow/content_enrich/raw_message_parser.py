"""Raw message parser — CipherTalk API 原始消息 → 清洗后的消息 dict.

从 L1 原始 API 数据提取所有解析逻辑，作为 L2 content_enrich 的 Phase A 执行。
原 cipher_talk_client.fetch_messages() 中的解析代码全部移至此处。

Public API:
    - parse_raw_message: 单条原始消息 → 清洗 dict（或 None 表示应过滤）
    - parse_raw_messages: 批量解析，自动跳过已清洗的消息（向后兼容）

解析步骤:
    1. 系统/撤回消息过滤
    2. messageKind → msg_type 映射
    3. rawContent XML 解析（file/link/reply/emoji/weapp/location/appmsg）
    4. appmsg 卡片解析
    5. 引用消息深层处理（sanitize + refermsg ID 提取）
    6. 媒体路径提取
    7. 输出 14 字段清洗 dict
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ============================================================
# 常量（原 cipher_talk_client.py 移入）
# ============================================================

# CipherTalk messageKind → 内部 msg_type 映射
MESSAGE_KIND_MAP: dict[str, str] = {
    "text": "text",
    "image": "image",
    "video": "video",
    "voice": "voice",
    "file": "file",
    "emoji": "emoji",
    "sticker": "emoji",
    "quote": "reply",
    "link": "link",
    "appmsg": "appmsg",
    "weapp": "weapp",
    "location": "location",
    "system": "other",
    "redpacket": "redpacket",
    "transfer": "transfer",
    "contact": "contact",
    "livephoto": "image",
    "app_link": "appmsg",
    "app": "appmsg",
    "app_file": "appmsg",
}

# 已映射的内部 msg_type 值集合 — 用于向后兼容检测
_MAPPED_MSG_TYPES: set[str] = set(MESSAGE_KIND_MAP.values())

# 召回消息内容模式
RECALL_CONTENT_PATTERNS: tuple[str, ...] = ("撤回了一条消息", "You recalled a message")

# 机器人自动发送的消息 senderUsername（群成员入群时的欢迎消息，无实际内容价值）
_BOT_SENDERS: frozenset[str] = frozenset({"25984983287196487@openim"})


# ============================================================
# 辅助函数
# ============================================================


# @openim 企业 ID 前缀 — CipherTalk 解析 @mention 时残留，微信中不可见
_OPENIM_PREFIX_RE = re.compile(r"^\d+@openim:\s*", re.MULTILINE)

# CT.4.7: 通用 sender 前缀 — CipherTalk 在 rawContent 前追加 sender_username + ":\n"
# 匹配: wxid_xxx, l333308, 25984983287196487@openim 等任意非XML内容前缀
_SENDER_PREFIX_RE = re.compile(r"^[^<\n]+:\n")


def _strip_openim_prefix(text: str) -> str:
    """去除内容中的 @openim 企业 ID 前缀（如 '25984984597685919@openim:'）."""
    return _OPENIM_PREFIX_RE.sub("", text).strip()


def _map_message_kind(kind: str) -> str:
    """将 CipherTalk messageKind 映射为内部 msg_type."""
    return MESSAGE_KIND_MAP.get(kind, "other")


def _strip_wxid_prefix(raw_content: str) -> str:
    """去除 rawContent 的 sender:\\n 前缀（如 wxid_xxx, l333308, 12345@openim）."""
    if not raw_content:
        return raw_content
    stripped = _SENDER_PREFIX_RE.sub("", raw_content)
    return stripped if stripped else raw_content


def _is_already_parsed(msg: dict[str, Any]) -> bool:
    """检测消息是否已经是清洗后的格式（向后兼容）。

    旧 DB 数据的 raw_json 是 14 字段清洗 dict，msg_type 是映射后的值。
    新数据的 raw_json 是原始 API 响应，msg_type 是原始 messageKind。
    """
    # 最可靠的检测：检查 raw_json 中是否有 account_name（旧清洗格式独有）
    raw_json_str = msg.get("raw_json", "")
    if raw_json_str:
        try:
            data = json.loads(raw_json_str)
            return "account_name" in data
        except (json.JSONDecodeError, TypeError):
            pass
    return False


def _to_api_format(msg: dict[str, Any]) -> dict[str, Any]:
    """将内部格式的消息还原为 CipherTalk API 原始格式。

    fetch_messages() 返回的是内部格式 (msg_type, content, raw_json)。
    parse_raw_message() 需要 CipherTalk API 格式 (messageKind, rawContent, etc.)。
    如果 raw_json 存在，直接用它；否则从内部字段构造。
    """
    raw_json_str = msg.get("raw_json", "")
    if raw_json_str:
        try:
            data: dict[str, Any] = json.loads(raw_json_str)
            return data
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback: 从内部字段构造（不应发生，但作为兜底）
    return {
        "serverId": msg.get("server_id", ""),
        "messageKind": msg.get("msg_type", "text"),
        "senderUsername": msg.get("sender", ""),
        "rawContent": msg.get("raw_content", "") or msg.get("content", ""),
        "parsedContent": "",
        "metadata": {"isSystem": False},
        "sortSeq": msg.get("timestamp", 0),
        "createTimeMs": msg.get("timestamp", 0),
        "media": {},
    }


# ============================================================
# 核心解析函数
# ============================================================


def parse_raw_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """解析单条原始 CipherTalk API 消息。

    Args:
        msg: CipherTalk API 返回的原始消息 dict，包含 messageKind、
             rawContent、parsedContent、senderUsername、media 等字段。

    Returns:
        清洗后的 14 字段 dict，或 None（应过滤的消息）。
    """
    # ===== 类型映射 =====
    message_kind = str(msg.get("messageKind", "text"))
    metadata = msg.get("metadata", {})
    msg_type = _map_message_kind(message_kind)

    # 过滤系统消息
    if metadata.get("isSystem", False):
        return None

    # 过滤机器人自动消息（入群欢迎等）
    sender_username = str(msg.get("senderUsername", ""))
    if sender_username in _BOT_SENDERS:
        return None

    # 撤回消息 — 保留占位符
    content = "[消息已撤回]" if message_kind == "recall" else str(msg.get("parsedContent", ""))

    # 过滤撤回消息内容
    if any(pattern in content for pattern in RECALL_CONTENT_PATTERNS):
        return None

    # 过滤空内容（非媒体消息）
    if not content and msg_type == "text":
        return None

    # 清理 @openim 企业 ID 前缀（CipherTalk 解析残留）
    content = _strip_openim_prefix(content)

    _original_content = content

    # ===== rawContent 处理 =====
    raw_content_raw = str(msg.get("rawContent", ""))
    raw_content = _strip_wxid_prefix(raw_content_raw)

    # ===== XML 解析器: file/link/reply/emoji/weapp/location/appmsg =====
    if raw_content and msg_type in (
        "file",
        "link",
        "reply",
        "emoji",
        "weapp",
        "location",
        "appmsg",
    ):
        from z_winnow.content_enrich.xml_parsers import parse_raw_content

        enriched = parse_raw_content(raw_content, msg_type, content)
        if enriched != content:
            content = enriched

    # ===== appmsg XML 解析 =====
    if msg_type == "appmsg" and raw_content:
        from z_winnow.content_enrich.card_parser import (
            format_appmsg,
            try_parse_appmsg_safe,
        )

        parsed = try_parse_appmsg_safe(raw_content, 49)
        if parsed is not None:
            content = format_appmsg(parsed)

    # ===== 引用回复: 清理 XML 元数据，提取用户文本 =====
    reply_to = ""
    if msg_type == "reply":
        from z_winnow.content_enrich.xml_parsers import (
            _has_reply_xml_noise,
            sanitize_reply_content,
        )

        user_text = sanitize_reply_content(_original_content)

        if content != _original_content and not _has_reply_xml_noise(content):
            if user_text and content != user_text:
                content = f"{user_text} {content}"
        else:
            extra_parts: list[str] = []
            if raw_content and raw_content.strip().startswith("<"):
                if "<appmsg" in raw_content:
                    from z_winnow.content_enrich.xml_parsers import parse_link

                    link_result = parse_link(raw_content)
                    if link_result and link_result != raw_content:
                        extra_parts.append(link_result)
                if not extra_parts and (
                    "<fileupload>" in raw_content or "<appattach>" in raw_content
                ):
                    from z_winnow.content_enrich.xml_parsers import parse_file

                    file_result = parse_file(raw_content)
                    if file_result and file_result != raw_content:
                        extra_parts.append(file_result)

            if extra_parts:
                joined = " ".join(extra_parts)
                content = f"{user_text} {joined}" if user_text else joined
            else:
                content = user_text

        # 提取引用消息 ID (从 refermsg XML)
        if raw_content and "<refermsg>" in raw_content:
            svrid_match = re.search(r"<svrid>([^<]+)</svrid>", raw_content)
            if svrid_match:
                reply_to = svrid_match.group(1)
                content += f" [引用消息ID: {reply_to}]"

    # ===== 字段映射 =====
    server_id = str(msg.get("serverId", ""))
    sender = str(msg.get("senderUsername", ""))
    account_name = str(msg.get("account_name", "") or msg.get("senderUsername", ""))
    group_nickname = str(msg.get("group_nickname", "") or account_name)
    timestamp = (
        int(msg.get("sortSeq", 0))
        or int(msg.get("createTimeMs", 0))
        or int(msg.get("createTime", 0)) * 1000
    )

    # 媒体路径
    media_info = msg.get("media", {})
    media_url = str(
        media_info.get("imageCachePath", "")
        or media_info.get("emojiCachePath", "")
        or media_info.get("videoPath", "")
        or media_info.get("filePath", "")
        or media_info.get("voicePath", "")
    )
    media_local_path = media_url

    # Extract voice duration for voice messages (ms from CipherTalk API)
    voice_duration_ms = 0
    if msg_type == "voice":
        vd = (
            media_info.get("voiceDuration")
            or media_info.get("voicelength")
            or media_info.get("length")
        )
        if vd:
            try:
                voice_duration_ms = int(vd)
            except (ValueError, TypeError):
                voice_duration_ms = 0

    raw_json_str = json.dumps(msg, ensure_ascii=False)

    return {
        "server_id": server_id,
        "sender": sender,
        "account_name": account_name,
        "group_nickname": group_nickname,
        "timestamp": timestamp,
        "msg_type": msg_type,
        "content": content,
        "original_content": _original_content,
        "media_url": media_url,
        "media_local_path": media_local_path,
        "reply_to": reply_to,
        "raw_content": raw_content_raw,
        "raw_json": raw_json_str,
        "voice_duration_ms": voice_duration_ms,
    }


def parse_raw_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量解析原始 CipherTalk API 消息。

    支持三种输入格式:
      1. 已清洗格式 (旧 DB 数据) — 直接返回 (向后兼容)
      2. 内部 "raw" 格式 (fetch_messages 输出) — 从 raw_json 还原 API 格式后解析
      3. CipherTalk API 原始格式 — 直接解析

    Args:
        messages: 消息列表，支持多种格式。

    Returns:
        清洗后的消息列表（过滤掉系统/撤回/空消息）。
    """
    result: list[dict[str, Any]] = []
    for msg in messages:
        # 向后兼容：已清洗的消息直接返回
        if _is_already_parsed(msg):
            result.append(msg)
            continue

        # 从内部格式还原为 API 原始格式
        api_msg = _to_api_format(msg)
        parsed = parse_raw_message(api_msg)
        if parsed is not None:
            result.append(parsed)

    return result
