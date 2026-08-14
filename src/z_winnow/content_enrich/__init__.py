"""content_enrich — 内容增强管线集成.

将 T-V1 (图片分析), T-V2 (卡片解析), T-V3 (链接预取) 整合为
content_enrich 节点，插入现有 StateGraph 的 data_fetch 和
orchestrator 之间。统一升级消息格式化，确保增强数据正确流入子 agent。

Public API:
    - node_content_enrich: 异步节点函数，执行图片分析 + 链接预取
    - ImageDescription: 图片分析结果 (Pydantic model, 7 类型分类)
    - AppMsgInfo: 卡片解析结果 (dataclass, 7 种子类型)
    - LinkPreview: 链接预取结果 (dataclass, SSRF 防护)
    - analyze_images_batch: 批量图片分析 (Vision API / MCP 双模式)
    - analyze_single_image: 单图片分析
    - try_parse_appmsg: 卡片 XML 解析
    - try_parse_appmsg_safe: 安全解析 (含 JSON 转义回退)
    - format_appmsg: 卡片格式化输出
    - parse_quote: 引用消息 XML 解析
    - parse_file: 文件消息 XML 解析
    - parse_link: 链接分享 XML 解析
    - parse_raw_content: 按类型分发解析器
    - fetch_link_preview: 单链接预取
    - fetch_all_links: 批量链接预取 (去重 + 并发控制 + SSRF)

Integration points:
    - T-V4-2 graph/builder.py: build_graph() 注册 content_enrich 节点 + 边
    - T-V4-3 pipeline/context.py: format_message() 注入增强内容
    - T-V4-5 orchestrator_graph.py: Send.arg 传递 image_descriptions / link_previews
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

# T-V1: 图片分析 — ImageDescription + analyze_images_batch
from z_winnow.content_enrich.card_parser import (
    AppMsgInfo,
    format_appmsg,
    try_parse_appmsg,
    try_parse_appmsg_safe,
)

# file_dedup: SMB file content hash suffix injection
from z_winnow.content_enrich.file_dedup import (
    apply_file_content_hash_suffix,
    strip_hash_suffix,
)
from z_winnow.content_enrich.image_analyzer import (
    ImageDescription,
    analyze_images_batch,
    analyze_single_image,
)

# T-V3: 链接预取 — LinkPreview + fetch_all_links
from z_winnow.content_enrich.link_fetcher import (
    LinkPreview,
    fetch_all_links,
    fetch_link_preview,
)

# T-W7-2: XML 解析器 — parse_quote / parse_file / parse_link
from z_winnow.content_enrich.xml_parsers import (
    parse_file,
    parse_link,
    parse_quote,
    parse_raw_content,
)

# P008: 超时优先增加时限 — CONTENT_ENRICH_TIMEOUT 默认 180s
# A001: 超时后先检查产出+增加时限，不跳过诊断直接拆分
# 三级降级: 全功能 → 部分功能 (仅链接预取) → 禁用

logger = logging.getLogger(__name__)

__all__ = [
    "AppMsgInfo",
    "ImageDescription",
    "LinkPreview",
    "analyze_images_batch",
    "analyze_single_image",
    "apply_file_content_hash_suffix",
    "fetch_all_links",
    "fetch_link_preview",
    "format_appmsg",
    "node_content_enrich",
    "parse_file",
    "parse_link",
    "parse_quote",
    "parse_raw_content",
    "parse_raw_messages",
    "strip_hash_suffix",
    "try_parse_appmsg",
    "try_parse_appmsg_safe",
]

# ============================================================
# Chat context helper
# ============================================================


def _build_chat_context(
    messages: list[dict],
    image_descriptions: dict[str, str],
    link_previews: dict[str, dict[str, str]],
    state: dict[str, Any],
) -> str:
    """Build markdown chat context from L2 messages."""
    if not messages:
        logger.warning("content_enrich: _build_chat_context — messages is empty, returning ''")
        return ""
    from z_winnow.pipeline.chat_context import ChatContextBuilder

    builder = ChatContextBuilder(
        messages,
        image_descriptions,
        link_previews,
        group_id=state.get("group_id", ""),
        date=state.get("date", ""),
        group_name=state.get("group_name", ""),
        member_map=state.get("member_map", {}),
    )
    try:
        md = builder.build()
        logger.info(
            "content_enrich: _build_chat_context — built %d chars, %d messages",
            len(md), len(messages),
        )
        return md
    except Exception:
        logger.warning("content_enrich: chat context build failed (non-blocking)", exc_info=True)
        return ""


# ============================================================
# node_content_enrich — 内容增强节点
# ============================================================


async def node_content_enrich(state: dict[str, Any]) -> dict[str, Any]:
    """内容增强节点: 原始消息解析 + 图片分析 + 链接预取.

    插入 graph/builder.py 的 data_fetch 和 orchestrator 节点之间。
    三阶段处理:
      - Phase A: 原始消息解析 (messageKind 映射 + XML 解析 + 内容清洗)
      - Phase B: Vision API / MCP 批量图片分析
      - Phase C: HTTP 链接预取 (含 SSRF 防护)

    三级降级策略 (P008 / A001):
      1. WINNOW_ENABLE_ENRICH=false → 跳过全部，返回空字典
      2. 图片分析或链接预取单个失败 → 使用已有结果继续 (独立 try/except)
      3. 全部失败 → 返回空字典，不阻断后续节点

    Args:
        state: OverallState (TypedDict)，至少包含:
            - messages: 消息列表 (T-V2 卡片解析已完成)
            - 其他字段 (透传不变)

    Returns:
        dict with:
            - messages: 原消息列表 (透传)
            - image_descriptions: {server_id: formatted_description_text}
            - link_previews: {server_id: {url, title, description, ...}}
            - current_phase: "content_enrich" 或 "content_enrich_skipped"
    """
    # T-W12-5: Use Settings for enable_enrich toggle (S7 配置单源)
    from z_winnow.config.settings import get_settings

    settings = get_settings()

    # ── Determine enrichment strategy ──
    # 「做不做 enrich」和「构不构建 chat_context_markdown」解耦：
    #   前者决定 image_descriptions/link_previews 的来源；
    #   后者只要 messages 存在就必须执行，确保 orchestrator / unified_reporter
    #   始终能拿到完整的聊天正文。
    is_regenerate = bool(state.get("regenerate"))
    skip_enrich = is_regenerate or (not settings.enable_enrich)

    # M4: regen 时从 L2 parsed_contexts 还原已富化内容（含图片描述/链接预取），
    # 否则 skip_enrich 后用空 image_descriptions 重建 chat_context 会丢失图片语义
    # （[图片] 标记残留）。L2 context_text 是原始 run 已 Vision 分析后的富化文本。
    l2_enriched_map: dict[str, str] = {}
    if is_regenerate:
        logger.info("content_enrich: regenerate mode — reusing cached enrichment data")
        current_phase = "content_enrich_cached"
        try:
            import aiosqlite as _aio_l2

            from z_winnow.pipeline.database import (
                get_contexts_by_date as _get_ctx,
            )
            from z_winnow.pipeline.database import (
                init_database_in_conn as _init_db_l2,
            )

            _date_l2 = (state.get("date", "") or "").replace("-", "")
            _gid_l2 = state.get("group_id", "")
            if _date_l2 and _gid_l2:
                async with _aio_l2.connect(settings.sqlite_db_path) as _db_l2:
                    await _init_db_l2(_db_l2)
                    _l2_rows = await _get_ctx(_db_l2, _date_l2, group_id=_gid_l2)
                for _c in _l2_rows:
                    _txt = _c.get("context_text", "") or ""
                    for _sid in _c.get("server_ids") or []:
                        if _txt:
                            l2_enriched_map[_sid] = _txt
                if l2_enriched_map:
                    logger.info(
                        "content_enrich: regenerate — loaded %d L2 enriched contexts (含图片描述)",
                        len(l2_enriched_map),
                    )
        except Exception as _l2_exc:
            logger.warning("content_enrich: regenerate L2 load failed — %s", _l2_exc)
    elif not settings.enable_enrich:
        logger.info("content_enrich: enrichment disabled via enable_enrich=False")
        current_phase = "content_enrich_skipped"
    else:
        current_phase = "content_enrich"

    # Phase A: 始终解析原始消息（幂等，已清洗消息直接透传）
    # 即使 enrich 被跳过，LLM 仍需可读的消息正文
    messages = list(state.get("messages", []))
    from z_winnow.content_enrich.raw_message_parser import parse_raw_messages

    messages = parse_raw_messages(messages)

    # M4: regen 用 L2 富化内容覆盖原始消息正文（图片描述等已存 L2）
    if l2_enriched_map:
        _n_replaced = 0
        for _m in messages:
            _sid = _m.get("server_id", "")
            if _sid and _sid in l2_enriched_map:
                _m["content"] = l2_enriched_map[_sid]
                _n_replaced += 1
        if _n_replaced:
            logger.info("content_enrich: regenerate — %d messages enriched from L2", _n_replaced)

    # Phase A+: 文件内容哈希后缀修正 — 从 SMB 扫描同名文件, 计算 SHA256,
    # 在 content 字段注入 _{hash}.{ext} 后缀, 确保 LLM 看到的文件名全局唯一.
    if messages:
        try:
            _wc_dir = (settings.wechat_file_storage_dir or "").strip()
            if _wc_dir:
                _corrected = await apply_file_content_hash_suffix(
                    messages, _wc_dir
                )
                if _corrected:
                    logger.info(
                        "content_enrich: file dedup — %d messages corrected", _corrected
                    )
        except Exception as _dedup_exc:
            logger.warning(
                "content_enrich: file dedup failed (non-blocking) — %s", _dedup_exc
            )

    image_descriptions: dict[str, str] = {}
    link_previews: dict[str, dict[str, str]] = {}
    image_analysis_failed = False
    context_count = 0

    if skip_enrich:
        # ── 跳过昂贵的 enrich 操作，使用缓存或空数据 ──
        if is_regenerate:
            image_descriptions = state.get("image_descriptions", {})
            link_previews = state.get("link_previews", {})
            context_count = state.get("context_count", 0)
    else:
        # ── 完整增强路径：媒体下载 + 图片分析 + 链接预取 + L2 持久化 ──
        # T-W12-5: Use Settings for content_enrich_timeout (S7 配置单源)
        timeout_s = settings.content_enrich_timeout

        # [Phase A+] 媒体文件下载落盘 (#9.3): 图片/表情/file/appmsg文件型 → attachments/
        # 下载成功后回写 m["media_local_path"], Phase B image_analyzer 自动用本地路径
        # (顺带修复 CipherTalk 远程部署 open(远程URL) 崩溃). 失败非阻断, 降级原路径.
        if settings.media_download_enabled and messages:
            import os as _os

            _group_id = state.get("group_id", "")
            _date = state.get("date", "")
            if _group_id and _date:
                try:
                    from z_winnow.content_enrich.media_downloader import (
                        download_media_batch,
                    )

                    _media_dest = _os.path.join(
                        settings.layer3_output_dir, _group_id, _date, "attachments"
                    )
                    _media_paths = await asyncio.wait_for(
                        download_media_batch(
                            messages,
                            _media_dest,
                            base_url=settings.effective_data_base_url,
                            token=settings.effective_data_token,
                            max_bytes=settings.media_max_bytes,
                            timeout=settings.media_download_timeout,
                        ),
                        timeout=max(timeout_s * 0.4, 30.0),
                    )
                    if _media_paths:
                        for _m in messages:
                            _sid = str(_m.get("server_id") or "")
                            if _sid in _media_paths:
                                _m["media_local_path"] = _media_paths[_sid]
                        logger.info(
                            "content_enrich: media download — %d files saved to %s",
                            len(_media_paths),
                            _media_dest,
                        )
                except Exception as _media_exc:
                    logger.warning(
                        "content_enrich: media download failed (non-blocking) — %s",
                        _media_exc,
                    )

        # Count image messages for degradation tracking
        _image_msg_count = sum(
            1
            for m in messages
            if m.get("msg_type") in ("image", 3) or "[图片]" in m.get("content", "")
        )

        # T-V1: 图片分析 (独立错误处理 — 失败不阻断链接预取)
        try:
            image_timeout = max(timeout_s * 0.6, 30.0)  # 至少 30s
            image_descriptions = await asyncio.wait_for(
                analyze_images_batch(messages),
                timeout=image_timeout,
            )
        except TimeoutError:
            image_analysis_failed = True
            logger.warning(
                "content_enrich: image analysis timed out after %.0fs"
                " (%d image messages degraded)",
                timeout_s * 0.6,
                _image_msg_count,
            )
        except Exception as exc:
            image_analysis_failed = True
            logger.warning(
                "content_enrich: image analysis failed (%s), %d image messages degraded",
                exc,
                _image_msg_count,
            )

        # T-V3: 链接预取 (独立错误处理 — 使用已有图片分析结果)
        try:
            link_timeout = max(timeout_s * 0.35, 10.0)  # 至少 10s
            link_previews_raw = await asyncio.wait_for(
                fetch_all_links(messages),
                timeout=link_timeout,
            )
            # LinkPreview 是 dataclass，使用 asdict() 序列化为普通 dict
            # A008: data initialized before mapping
            link_previews = {}
            for sid, preview_obj in link_previews_raw.items():
                try:
                    link_previews[sid] = asdict(preview_obj)
                except Exception:
                    # 回退：手动提取字段
                    link_previews[sid] = {
                        "url": getattr(preview_obj, "url", ""),
                        "title": getattr(preview_obj, "title", ""),
                        "description": getattr(preview_obj, "description", ""),
                        "site_name": getattr(preview_obj, "site_name", ""),
                        "status_code": getattr(preview_obj, "status_code", "0"),
                        "error": getattr(preview_obj, "error", ""),
                        "fetched_at": getattr(preview_obj, "fetched_at", ""),
                    }
        except TimeoutError:
            logger.warning(
                "content_enrich: link prefetch timed out, using partial results",
            )
        except Exception as exc:
            logger.warning(
                "content_enrich: link prefetch failed (%s), continuing",
                exc,
            )

        logger.info(
            "content_enrich: %d image descriptions, %d link previews from %d messages"
            " (image_failed=%s)",
            len(image_descriptions),
            len(link_previews),
            len(messages),
            image_analysis_failed,
        )

        # T-W12-7: S1 即时固化 — L2 写入在阶段 B 结束即执行，不可变
        # P022: Storage 层独立写入，与节点业务逻辑零耦合
        if messages:
            try:
                import aiosqlite as _aiosqlite

                from z_winnow.config.settings import (
                    get_settings as _get_settings,
                )
                from z_winnow.pipeline.database import (
                    init_database_in_conn as _init_db,
                )
                from z_winnow.pipeline.database import (
                    insert_parsed_contexts as _insert_ctx,
                )

                _settings = _get_settings()
                _db_path = _settings.sqlite_db_path
                _date = state.get("date", "")
                _group_id = state.get("group_id", "")
                _contexts: list[dict[str, Any]] = []

                for _msg in messages:
                    _sid = _msg.get("server_id", "")
                    if not _sid:
                        continue

                    # Use enrich_message_for_llm for consistent L2 formatting
                    # across DB storage and LLM prompt consumption
                    from z_winnow.pipeline.context import (
                        enrich_message_for_llm,
                    )

                    _enriched = enrich_message_for_llm(_msg, image_descriptions, link_previews)
                    _context_text = _enriched.get("content", "")
                    if not _context_text:
                        continue

                    # Rough token estimate: ~4 chars per token
                    _token_est = max(1, len(_context_text) // 4)

                    _contexts.append(
                        {
                            "context_id": f"ctx_{_sid}",
                            "server_ids": [_sid],
                            "context_text": _context_text,
                            "token_count": _token_est,
                            "source_subagent": "content_enrich",
                        }
                    )

                if _contexts and _date:
                    from pathlib import Path as _Path

                    _Path(_db_path).parent.mkdir(parents=True, exist_ok=True)
                    async with _aiosqlite.connect(_db_path) as _db:
                        await _init_db(_db)
                        context_count = await _insert_ctx(
                            _db, _contexts, _date, group_id=_group_id
                        )

                    logger.info(
                        "content_enrich: L2 persist — wrote %d parsed contexts",
                        context_count,
                    )
            except Exception as _l2_exc:
                logger.warning(
                    "content_enrich: L2 persist failed (non-blocking) — %s", _l2_exc
                )

    # ── 始终构建 chat_context_markdown ──
    # 无论 enrich 是否执行，只要 messages 存在就产出 markdown，
    # 确保 orchestrator / unified_reporter 始终能消费完整的聊天正文。
    chat_context_md = _build_chat_context(
        messages, image_descriptions, link_previews, state
    )
    logger.info(
        "content_enrich: node returning — phase=%s messages=%d img_desc=%d link_prev=%d md_chars=%d",
        current_phase, len(messages), len(image_descriptions), len(link_previews),
        len(chat_context_md),
    )
    return {
        "messages": messages,
        "image_descriptions": image_descriptions,
        "link_previews": link_previews,
        "image_analysis_failed": image_analysis_failed,
        "current_phase": current_phase,
        "context_count": context_count,
        "chat_context_markdown": chat_context_md,
    }
