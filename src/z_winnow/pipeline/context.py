"""T-A5: 上下文组装 + 解析入库 (Layer 2: parsed_contexts).

将 raw_messages 按天组装为结构化上下文块写入 parsed_contexts。

核心函数:
- assemble_daily_context(date) → list[dict]
- format_message(msg) → str
- format_for_llm(messages) → str  (NEW: T-W7-3 Layer 2 格式化)
- build_layer2_messages(raw_messages, image_descriptions) → list[dict]  (NEW: T-W7-3)
- store_layer2_context(date, group_name, messages) → str  (NEW: T-W7-3)
- estimate_tokens(text) → int

特性:
- 消息格式化为 [HH:MM] 发送者：内容 模板
- 图片标记 [图片: image_path]
- 过滤系统消息
- 超过 token 阈值 (128000) 自动切分
- context_id 格式 {date}-{seq}
- server_ids 维护为 JSON 数组关联 raw_messages
- T-W7-3: Layer 2 JSON 存储 (完整字段保留) + format_for_llm() 独立格式化
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from z_winnow.pipeline.database import (
    get_raw_messages_by_date,
    init_database_in_conn,
)
from z_winnow.pipeline.sandbox import (
    contains_sanitized_pattern,
    inject_role_boundary_prompt,
    wrap_user_message,
)

logger = logging.getLogger(__name__)


def _format_voice_duration(duration_ms: int) -> str:
    """Format voice duration in milliseconds to human-readable string."""
    if duration_ms <= 0:
        return "[语音]"
    seconds = duration_ms / 1000 if duration_ms > 1000 else duration_ms
    if seconds >= 60:
        return f"[语音: {int(seconds // 60)}分{int(seconds % 60)}秒]"
    return f"[语音: {int(seconds)}秒]"


# 北京时间
CST = timezone(timedelta(hours=8))

# 默认 token 阈值
DEFAULT_MAX_TOKENS = 128000

# 粗略 token 估算比例（中文约 1 字符 ≈ 1 token，英文 ~4 字符 ≈ 1 token）
CHARS_PER_TOKEN = 2


def estimate_tokens(text: str) -> int:
    """粗略估算文本的 token 数量。

    使用字符数 / 2 的简单估算，适用于中英混合文本。
    对生产环境建议使用 tiktoken 进行精确计数。

    Args:
        text: 要估算的文本

    Returns:
        token 估算值
    """
    return len(text) // CHARS_PER_TOKEN


def format_message(
    msg: dict,
    image_descriptions: dict[str, str] | None = None,
) -> str:
    """将单条消息格式化为 [HH:MM] 发送者：内容 模板。

    Args:
        msg: 消息字典，包含 timestamp, sender/content, msg_type,
             image 类型还需 server_id, media_url
        image_descriptions: 可选，{server_id: AI描述文本} 映射。
            来自 T-V1-1 analyze_images_batch() 返回值。
            为 None 或不含对应 server_id 时，图片消息使用旧格式。

    Returns:
        格式化后的消息字符串
    """
    # 时间戳 → 时间字符串
    ts = msg.get("timestamp", 0)
    if ts:
        try:
            dt = datetime.fromtimestamp(ts / 1000, tz=CST)
            time_str = dt.strftime("%H:%M")
        except (OSError, ValueError):
            time_str = "--:--"
    else:
        time_str = "--:--"

    sender = msg.get("account_name", "") or msg.get("sender", "") or "未知"
    msg_type = msg.get("msg_type", "text")
    content = msg.get("content", "")

    # messageKind 兼容：如果 msg_type 是原始 CipherTalk messageKind（未映射），
    # 通过 _map_message_kind 转为内部 msg_type
    if msg_type and msg_type not in (
        "text",
        "image",
        "video",
        "voice",
        "file",
        "emoji",
        "reply",
        "link",
        "appmsg",
        "weapp",
        "location",
        "other",
        "redpacket",
        "transfer",
        "contact",
    ):
        from z_winnow.content_enrich.raw_message_parser import _map_message_kind

        msg_type = _map_message_kind(msg_type)

    # 图片类型 — 支持 image_descriptions 注入 (T-V1-3)
    if msg_type == "image":
        media_url = msg.get("media_url", "")
        server_id = msg.get("server_id", "")
        desc = (image_descriptions or {}).get(server_id, "")
        if desc:
            # 纵深防御: 对 AI 描述文本做 prompt injection 检查
            # 注意: 此检查独立于原始消息的 sanitized 标记
            if contains_sanitized_pattern(desc):
                return f"[{time_str}] {sender}：[已过滤] {desc} [原始图片: {media_url}]"
            return f"[{time_str}] {sender}：{desc} [原始图片: {media_url}]"
        # 回退: 无 AI 描述时，保持旧行为
        return f"[{time_str}] {sender}：[图片: {media_url}]"

    # 文件类型 — 包含文件存储地址
    if msg_type == "file":
        media_url = msg.get("media_url", "")
        media_local_path = msg.get("media_local_path", "")
        file_info = f"[文件: {content}]"
        if media_url:
            file_info += f" [下载: {media_url}]"
        elif media_local_path:
            file_info += f" [本地: {media_local_path}]"
        return f"[{time_str}] {sender}：{file_info}"

    # 语音/视频类型
    if msg_type == "voice":
        return f"[{time_str}] {sender}：{_format_voice_duration(msg.get('voice_duration_ms', 0))}"

    if msg_type == "video":
        return f"[{time_str}] {sender}：[视频]"

    # P007: appmsg 类型 — 公众号文章/卡片消息
    # content 已在 data client 层完成 XML→可读文本转换 (T-V2-2)，
    # context.py 无需二次解析，直接渲染为纯文本
    if msg_type == "appmsg":
        return f"[{time_str}] {sender}：{content}"

    # 文本消息
    is_sanitized = msg.get("sanitized", 0)
    if is_sanitized:
        return f"[{time_str}] {sender}：[已过滤] {content}"

    return f"[{time_str}] {sender}：{content}"


def format_messages(
    messages: list[dict],
    image_descriptions: dict[str, str] | None = None,
) -> str:
    """将消息列表格式化为纯文本上下文。

    Args:
        messages: 消息字典列表
        image_descriptions: 可选，透传给 format_message()

    Returns:
        格式化的上下文字符串
    """
    lines: list[str] = []
    for msg in messages:
        formatted = format_message(msg, image_descriptions=image_descriptions)
        lines.append(formatted)
    return "\n".join(lines)


async def assemble_daily_context(
    date: str,
    db_path: str = "data/winnow.db",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    group_id: str = "",
) -> list[dict]:
    """将 raw_messages 按天组装为结构化上下文块。

    流程:
    1. 从 raw_messages 读取当天所有消息
    2. 按时间排序
    3. 格式化为文本
    4. 超过 token 阈值则自动切分
    5. 注入角色边界 prompt + XML 包装
    6. 写入 parsed_contexts 表

    Args:
        date: 日期 YYYYMMDD
        db_path: SQLite 数据库路径
        max_tokens: token 阈值，超过则切分

    Returns:
        写入的上下文块列表，每块包含:
        - context_id: 上下文 ID ({date}-{seq})
        - date: 日期
        - server_ids: 关联的 serverId JSON 数组
        - context_text: 格式化的上下文文本
        - token_count: token 估算值
        - source_subagent: 来源标识
    """
    async with aiosqlite.connect(db_path) as db:
        await init_database_in_conn(db)

        # 1. 读取原始消息
        messages = await get_raw_messages_by_date(db, date, group_id=group_id or None)
        if not messages:
            logger.info("No messages found for date %s", date)
            return []

        # 2. 按时间戳排序
        messages.sort(key=lambda m: m.get("timestamp", 0))

        # 3-5. 切分 + 格式化 + 注入
        chunks = _chunk_messages(messages, max_tokens)
        contexts: list[dict] = []

        for seq, chunk in enumerate(chunks, start=1):
            context_id = f"{date}-{seq:03d}"
            server_ids = [m["serverID"] for m in chunk]

            # XML 包装每条消息
            xml_wrapped_lines: list[str] = []
            for msg in chunk:
                sender = msg.get("account_name", "") or msg.get("sender", "")
                content = msg.get("content", "")
                # 防御性清洗: 清理 reply 消息残留的 XML 元数据（含 HTML 转义形式）
                if content and msg.get("msg_type") == "reply":
                    from z_winnow.content_enrich.xml_parsers import (
                        _has_reply_xml_noise,
                        sanitize_reply_content,
                    )

                    if _has_reply_xml_noise(content):
                        content = sanitize_reply_content(content)
                ts = msg.get("timestamp", 0)
                time_str = ""
                if ts:
                    try:
                        dt = datetime.fromtimestamp(ts / 1000, tz=CST)
                        time_str = dt.strftime("%H:%M")
                    except (OSError, ValueError):
                        pass
                wrapped = wrap_user_message(content, sender=sender, timestamp=time_str)
                xml_wrapped_lines.append(wrapped)
            xml_text = "\n".join(xml_wrapped_lines)

            # 注入角色边界 prompt
            context_text = inject_role_boundary_prompt(xml_text)
            token_count = estimate_tokens(context_text)

            contexts.append(
                {
                    "context_id": context_id,
                    "date": date,
                    "server_ids": json.dumps(server_ids, ensure_ascii=False),
                    "context_text": context_text,
                    "token_count": token_count,
                    "source_subagent": "pipeline",
                }
            )

        # 6. 写入 parsed_contexts
        for ctx in contexts:
            await db.execute(
                """INSERT OR REPLACE INTO parsed_contexts
                   (context_id, date, server_ids, context_text, token_count, source_subagent, group_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ctx["context_id"],
                    ctx["date"],
                    ctx["server_ids"],
                    ctx["context_text"],
                    ctx["token_count"],
                    ctx["source_subagent"],
                    group_id,
                ),
            )

        await db.commit()

    logger.info(
        "Assembled %d context chunks for date %s (%d messages)",
        len(contexts),
        date,
        len(messages),
    )

    return contexts


def _format_message_dicts(
    messages: list[dict],
    image_descriptions: dict[str, str] | None = None,
) -> str:
    """将 Row 字典格式的消息列表格式化为纯文本。

    Args:
        messages: 消息字典列表（含 serverID, sender, content 等字段）
        image_descriptions: 可选，透传给 format_messages()

    Returns:
        格式化的文本
    """
    formatted_list: list[dict] = []
    for m in messages:
        formatted_list.append(
            {
                "server_id": m.get("serverID", ""),
                "sender": m.get("sender", ""),
                "account_name": m.get("sender", ""),
                "timestamp": int(m.get("timestamp", 0)) if m.get("timestamp") else 0,
                "msg_type": m.get("msg_type", "text"),
                "content": m.get("content", ""),
                "media_url": m.get("image_path", ""),
                "sanitized": m.get("sanitized", 0),
            }
        )
    return format_messages(formatted_list, image_descriptions=image_descriptions)


def _chunk_messages(
    messages: list[dict],
    max_tokens: int,
    image_descriptions: dict[str, str] | None = None,
) -> list[list[dict]]:
    """将消息列表切分为不超过 token 阈值的多个块。

    贪婪切分策略: 逐条添加消息直到 token 估算值超过阈值，
    然后将当前块追加到结果中并开始新块。

    Args:
        messages: 消息字典列表
        max_tokens: token 阈值
        image_descriptions: 可选，透传给 format_message()，
            确保 token 估算纳入 AI 描述长度

    Returns:
        消息块列表
    """
    chunks: list[list[dict]] = []
    current_chunk: list[dict] = []
    current_tokens = 0

    for msg in messages:
        msg_text = format_message(
            {
                "server_id": msg.get("serverID", ""),
                "sender": msg.get("sender", ""),
                "timestamp": int(msg.get("timestamp", 0)) if msg.get("timestamp") else 0,
                "msg_type": msg.get("msg_type", "text"),
                "content": msg.get("content", ""),
                "media_url": msg.get("image_path", ""),
                "sanitized": msg.get("sanitized", 0),
            },
            image_descriptions=image_descriptions,
        )
        msg_tokens = estimate_tokens(msg_text)

        if current_chunk and (current_tokens + msg_tokens > max_tokens):
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0

        current_chunk.append(msg)
        current_tokens += msg_tokens

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


# ============================================================
# T-W7-3: Layer 2 JSON 存储 + LLM 格式化
# ============================================================
# 架构: 两层分离
#   Layer 2 存储 (JSON, 完整保留): 保留所有原始字段, image_description 附加而非覆盖
#   LLM 格式化 (独立步骤): 仅在调用 agent 前执行, image → description 替换 content
#
# 经验引用:
#   P009: 向后兼容可选参数级联 (default-None 透传)
#   L018: 显式路由分支 — 11 种消息类型全部显式分支
#   L019: Token 估算同步 — format_for_llm() 变更后 _chunk_messages() 需透传


def build_layer2_messages(
    raw_messages: list[dict],
    image_descriptions: dict[str, str] | None = None,
) -> list[dict]:
    """构建 Layer 2 JSON 消息列表。保留所有原始字段, image_description 附加到消息。

    P009: image_descriptions 使用 default-None 向后兼容。
    输入消息格式来自 data client 的清洗后格式。
    对 image 类型消息: content 保留原始标记 [图片], image_description 附加为独立字段,
    media_local_path 保留原始路径。

    Args:
        raw_messages: 清洗后的消息列表 (含 server_id, sender, msg_type, content,
                      media_local_path, timestamp 等字段)
        image_descriptions: {server_id: AI描述文本} 映射, 来自 T-V1-1 analyze_images_batch()

    Returns:
        JSON 消息列表, 每条消息保留所有字段, image_description 为独立字段
    """
    descriptions = image_descriptions or {}
    result: list[dict] = []

    for msg in raw_messages:
        entry: dict = {
            "server_id": str(msg.get("server_id", "")),
            "sender": str(msg.get("account_name", "") or msg.get("sender", "")),
            "msg_type": str(msg.get("msg_type", "text")),
            "content": str(msg.get("content", "")),
            "timestamp": int(msg.get("timestamp", 0)),
            "media_url": str(msg.get("media_url", "")),
            "media_local_path": str(msg.get("media_local_path", "")),
            "reply_to": str(msg.get("reply_to", "")),
        }

        # 附加 image_description (不覆盖 content)
        if entry["msg_type"] == "image":
            server_id = entry["server_id"]
            desc = descriptions.get(server_id, "")
            entry["image_description"] = desc

        # L2.3b: emoji 类型同样附加 image_description（Vision API 分析结果）
        if entry["msg_type"] == "emoji":
            server_id = entry["server_id"]
            desc = descriptions.get(server_id, "")
            entry["image_description"] = desc

        # 保留 sanitized 标记 (如果存在)
        if msg.get("sanitized"):
            entry["sanitized"] = int(msg["sanitized"])

        # 保留 raw_content (XML 原始数据, 来自 data source API)
        if msg.get("raw_content"):
            entry["raw_content"] = str(msg["raw_content"])

        result.append(entry)

    return result


async def store_layer2_context(
    date: str,
    group_name: str,
    messages: list[dict],
    db_path: str = "data/winnow.db",
    group_id: str = "",
) -> str:
    """将 Layer 2 JSON 消息列表写入 parsed_contexts 表。

    context_text 列存储完整 JSON 字符串, 包含所有消息字段。
    context_id 格式: {date}-{group_name}

    Args:
        date: 日期 YYYYMMDD
        group_name: 群聊名称
        messages: Layer 2 JSON 消息列表 (来自 build_layer2_messages())
        db_path: SQLite 数据库路径

    Returns:
        context_id 字符串
    """
    context_id = f"{date}-{group_name}"
    context_json = json.dumps(messages, ensure_ascii=False)
    token_count = estimate_tokens(context_json)
    server_ids = [m["server_id"] for m in messages if m.get("server_id")]

    # P1-6: Serialize SQLite writes across concurrent group pipelines
    from z_winnow.pipeline.db_lock import db_write_lock

    async with await db_write_lock():  # noqa: SIM117
        async with aiosqlite.connect(db_path) as db:
            await init_database_in_conn(db)

            await db.execute(
                """INSERT OR REPLACE INTO parsed_contexts
                   (context_id, date, server_ids, context_text, token_count, source_subagent, group_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    context_id,
                    date,
                    json.dumps(server_ids, ensure_ascii=False),
                    context_json,
                    token_count,
                    "pipeline",
                    group_id,
                ),
            )
            await db.commit()

    logger.info(
        "Stored Layer 2 context %s: %d messages, %d tokens",
        context_id,
        len(messages),
        token_count,
    )

    return context_id


def format_for_llm(messages: list[dict]) -> str:
    """将 Layer 2 JSON 消息格式化为 LLM 就绪的极致清爽纯文本。

    L018: 全 11 种消息类型显式路由分支, 无 fall through 到通用 default。
    独立步骤, 仅在调用 agent 前执行。不依赖 build_layer2_messages()。

    格式化模板:
    | msg_type | LLM 看到的格式 |
    |----------|---------------|
    | text     | [HH:MM] 发送者：内容 |
    | image    | [HH:MM] 发送者：AI图片描述内容 (image_description 替换 content) |
    | appmsg   | [HH:MM] 发送者：[分享链接] 标题：... |
    | file     | [HH:MM] 发送者：[文件: filename (size)] |
    | quote    | [HH:MM] 发送者：「引用 XXX: 原文」 |
    | link     | [HH:MM] 发送者：链接标题 | URL |
    | voice    | [HH:MM] 发送者：[语音] |
    | video    | [HH:MM] 发送者：[视频] |
    | recall   | [HH:MM] 发送者：[消息已撤回] |
    | weapp    | [HH:MM] 发送者：[小程序] 标题 |
    | other    | [HH:MM] 发送者：内容 |

    Args:
        messages: 消息字典列表, 每条含 server_id, sender, msg_type, content,
                  timestamp, 可选的 image_description, media_local_path 等字段

    Returns:
        纯文本格式化字符串, 每条消息一行
    """
    lines: list[str] = []

    for msg in messages:
        # 时间戳 → HH:MM
        ts = msg.get("timestamp", 0)
        if ts:
            try:
                dt = datetime.fromtimestamp(ts / 1000, tz=CST)
                time_str = dt.strftime("%H:%M")
            except (OSError, ValueError):
                time_str = "--:--"
        else:
            time_str = "--:--"

        sender = str(msg.get("account_name", "") or msg.get("sender", ""))
        msg_type = str(msg.get("msg_type", "text"))
        content = str(msg.get("content", ""))

        # L018: 显式分支 — 每种类型独立处理, 不 fall through
        if msg_type == "text":
            # [图片] 占位符：优先用 Vision API 描述替换
            if content.strip() == "[图片]":
                desc = str(msg.get("image_description", ""))
                if desc:
                    lines.append(f"[{time_str}] {sender}：{desc}")
                else:
                    lines.append(f"[{time_str}] {sender}：[图片]")
            else:
                lines.append(f"[{time_str}] {sender}：{content}")

        elif msg_type == "image":
            # P007: image_description 替换 content, 无路径, 无 media_local_path
            desc = str(msg.get("image_description", ""))
            if desc:
                lines.append(f"[{time_str}] {sender}：{desc}")
            else:
                # 无 AI 描述时回退到基础标记
                lines.append(f"[{time_str}] {sender}：[图片]")

        elif msg_type == "appmsg":
            # appmsg content 已在 data client 层完成 XML→可读文本转换
            lines.append(f"[{time_str}] {sender}：[分享链接] {content}")

        elif msg_type == "file":
            # file content 格式: filename (size) | 存储: cdn_url 来自 xml_parsers.parse_file()
            # 额外附加 media_url/media_local_path 作为文件下载地址
            media_url = str(msg.get("media_url", ""))
            media_local_path = str(msg.get("media_local_path", ""))
            file_info = f"[文件: {content}]"
            if media_url:
                file_info += f" [下载: {media_url}]"
            elif media_local_path:
                file_info += f" [本地: {media_local_path}]"
            lines.append(f"[{time_str}] {sender}：{file_info}")

        elif msg_type == "quote" or msg_type == "reply":
            # content 已在 data client 层格式化（[消息引用：id] 或完整引用文本）
            lines.append(f"[{time_str}] {sender}：{content}")

        elif msg_type == "link":
            # link content 格式: 标题 | URL 来自 xml_parsers.parse_link()
            lines.append(f"[{time_str}] {sender}：{content}")

        elif msg_type == "emoji":
            # P3-1: 表情消息 — 使用 image_description（Vision API 分析结果）
            desc = str(msg.get("image_description", ""))
            if desc:
                lines.append(f"[{time_str}] {sender}：[表情包: {desc}]")
            else:
                lines.append(f"[{time_str}] {sender}：{content if content.strip() else '[表情]'}")

        elif msg_type == "location":
            # P3-1: 位置消息 — 显式分支 (L018), content 已由 parse_raw_content 格式化
            lines.append(f"[{time_str}] {sender}：{content}")

        elif msg_type == "voice":
            lines.append(
                f"[{time_str}] {sender}：{_format_voice_duration(msg.get('voice_duration_ms', 0))}"
            )

        elif msg_type == "video":
            lines.append(f"[{time_str}] {sender}：[视频]")

        elif msg_type == "recall":
            # 经验: 原系统保留撤回消息 [消息已撤回], 不过滤
            lines.append(f"[{time_str}] {sender}：[消息已撤回]")

        elif msg_type == "weapp":
            # 小程序 — 显式分支, 不依赖 other 回退
            lines.append(f"[{time_str}] {sender}：[小程序] {content}")

        else:
            # other / 未知类型 — L018: 显式 other 分支, 命名明确
            lines.append(f"[{time_str}] {sender}：{content}")

    return "\n".join(lines)


def enrich_message_for_llm(
    msg: dict,
    image_descriptions: dict[str, str] | None = None,
    link_previews: dict[str, dict[str, str]] | None = None,
) -> dict:
    """将单条 L1 消息格式化为 L2 增强消息 dict（content 字段按 msg_type 路由格式化）。

    复用 format_for_llm() 的 11 类型路由逻辑，但返回 dict 而非文本行。
    content 只含消息正文，不含 [HH:MM] sender：前缀（由 wrap_messages_xml 负责加）。
    保留所有原始字段（server_id, sender, account_name, timestamp 等）。

    Args:
        msg: L1 消息字典
        image_descriptions: {server_id: AI 描述文本}
        link_previews: {server_id: {url, title, description, ...}}

    Returns:
        新 dict，content 字段已格式化，其余字段不变
    """
    msg_type = str(msg.get("msg_type", "text"))
    content = str(msg.get("content", ""))
    server_id = msg.get("server_id", "")

    # 按类型路由格式化 content
    if msg_type == "text":
        # [图片] 占位符：优先用 Vision API 描述替换
        if content.strip() == "[图片]":
            desc = (image_descriptions or {}).get(server_id, "")
            formatted = desc if desc else "[图片]"
        else:
            formatted = content

    elif msg_type == "image":
        desc = (image_descriptions or {}).get(server_id, "")
        formatted = desc if desc else "[图片]"

    elif msg_type == "appmsg":
        formatted = f"[分享链接] {content}" if content else "[分享链接]"

    elif msg_type == "file":
        media_url = str(msg.get("media_url", ""))
        media_local_path = str(msg.get("media_local_path", ""))
        file_info = f"[文件: {content}]" if content else "[文件]"
        if media_url:
            file_info += f" [下载: {media_url}]"
        elif media_local_path:
            file_info += f" [本地: {media_local_path}]"
        formatted = file_info

    elif msg_type in ("quote", "reply"):
        # 清洗 reply XML 元数据残留
        if content:
            try:
                from z_winnow.content_enrich.xml_parsers import (
                    _has_reply_xml_noise,
                    sanitize_reply_content,
                )

                if _has_reply_xml_noise(content):
                    content = sanitize_reply_content(content)
            except ImportError:
                pass
        formatted = content

    elif msg_type == "link":
        formatted = content

    elif msg_type == "emoji":
        desc = (image_descriptions or {}).get(server_id, "")
        formatted = f"[表情包: {desc}]" if desc else (content if content else "[表情]")

    elif msg_type == "location":
        formatted = content

    elif msg_type == "voice":
        formatted = _format_voice_duration(msg.get("voice_duration_ms", 0))

    elif msg_type == "video":
        formatted = "[视频]"

    elif msg_type == "recall":
        formatted = "[消息已撤回]"

    elif msg_type == "weapp":
        formatted = f"[小程序] {content}" if content else "[小程序]"

    else:
        formatted = content

    # 追加 link preview（额外增强，不属于 format_for_llm 标准逻辑）
    if link_previews and server_id in (link_previews or {}):
        _lp = link_previews[server_id]
        _title = _lp.get("title", "")
        if _title:
            formatted += f"\n[链接预览: {_title}]"

    # 返回新 dict，保留所有原始字段
    result = dict(msg)
    result["content"] = formatted
    return result
