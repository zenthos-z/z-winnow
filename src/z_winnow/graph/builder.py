"""LangGraph StateGraph — Single-agent unified workflow (Wave 12).

Builds the winnow graph with a single unified_reporter agent:

    START → data_fetch → content_enrich → orchestrator →
        unified_reporter (1 LLM call: daily + resources + engineering +
                          topic_tracking + trend_summary + lifecycle) →
        output_composer (L3 JSON + L3 DB) → [feishu chain] → END

T-W12-7: S1 即时固化 — persist 拆分为各阶段各自写入:
- data_fetch 末尾: L1 写入 (raw_messages, 不可变)
- content_enrich 末尾: L2 写入 (parsed_contexts, 不可变)
- output_composer 末尾: L3 写入 (topic_summaries + report_versions, 允许增量)
- 集中 persist 节点已删除

T-W12-10: Markdown rendering deferred to Phase H (S4).
- write_reports removed from main flow.
- Main flow writes L3 JSON + report_versions.content=NULL.
- export_markdown() is the Phase H manual trigger: L3 JSON -> Jinja2 -> .md.
- P022: Storage (per-stage persist) and Formatting (export_markdown) are decoupled.

T-W12-9: Merged parallel agents into single unified_reporter.
- No Send API fan-out, no parallel agents, no merge node.
- topic_tracker logic (cross-day tracking, trend_summary) inlined into unified_reporter.
- lifecycle classification (core/continuous/new) handled by unified_reporter LLM.
- S2 Design Standard: single agent, single LLM call, no intermediate nodes.

Real mode (default): data_fetch calls CipherTalk API, unified_reporter calls LLM,
output_composer does JSON composition. Falls back to mock on errors.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from z_winnow.content_enrich import node_content_enrich
from z_winnow.state import OverallState

# LangSmith sub-step tracing: module-level @traceable functions auto-nest
# under the LangGraph auto-trace node spans.  Neither get_current_run_tree()
# nor trace() context manager work inside @traceable in langsmith≥0.7 —
# nested @traceable is the only supported nesting mechanism.
try:
    from langsmith.run_helpers import traceable as _ls_traceable
except ImportError:  # pragma: no cover — langsmith is a core dependency

    def _ls_traceable(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
        """No-op fallback when langsmith is not installed."""
        return lambda f: f


logger = logging.getLogger(__name__)

# M.8.2: wxid_ 清理 — 防止 MemOS 反馈放大循环
_WXID_RE = re.compile(r"wxid_[a-zA-Z0-9_-]+")


def _scrub_wxid(text: str) -> str:
    """替换 wxid_ 模式为 [成员]，防止 MemOS 反馈放大循环。"""
    return _WXID_RE.sub("[成员]", text)


def _format_custom_record_memory(kind: str, rec: dict, display_date: str) -> str:
    """M4: 泛化渲染自定义表记录为 MemOS memory_content。

    含 ``{kind}_id``（feedback_memory search 定位锚点）+ 主要字段。
    跳过 ``source_server_ids`` 与 ``*_id`` 字段（id 已在头部单独呈现）。
    一表一逻辑，加新自定义表无需改本函数。
    """
    kid = rec.get(f"{kind}_id", "")
    parts = [f"[{display_date}] {kind}" + (f" ({kid})" if kid else "")]
    for k, v in rec.items():
        if not v:
            continue
        if k == "source_server_ids" or k.endswith("_id"):
            continue
        parts.append(f"{k}: {v}")
    return " | ".join(parts)


# ============================================================
# Timeout Configuration (P008 / L020 / A009)
# ============================================================
# P008: 超时 = 历史 P95 耗时 × 1.5 — 基于 progress.json timings:
#   - data_fetch ~10s, content_enrich ~180s, subagents ~30-60s each
#   - P95 ≈ 200s → × 1.5 → GRAPH_NODE_TIMEOUT 默认 300s
# L020: 区分派发超时 (harness 等待) 与执行超时 (node 内部完成).
#   - 执行超时: GRAPH_NODE_TIMEOUT 控制, 按 P008 校准
#   - 派发超时: Box0 cfg.timeout_s 控制, 轻型 300s / 复杂 600-900s
# A009: Box0 daemon 300s 硬超时直接 SIGKILL. 所有节点执行超时 ≤ 300s.


def _get_graph_node_timeout() -> int:
    """Read graph node timeout from Settings (converged from os.getenv).

    T-W12-5: S7 配置单源 — 直接从 Settings 读取, 不再 os.getenv().
    A013: 此函数应在函数体内调用, 不在模块顶层.

    P008: 默认 300s = P95 ~200s × 1.5.
    L020: 此为执行超时 (node 内部完成), 非派发超时.

    Returns:
        int: Graph node timeout in seconds.
    """
    try:
        from z_winnow.config.settings import get_settings

        return get_settings().graph_node_timeout
    except Exception:
        return 300


# A013: GRAPH_NODE_TIMEOUT 不再在模块顶层调用 get_settings().
# 需要超时值的代码应直接调用 _get_graph_node_timeout() 或
# get_settings().graph_node_timeout.


# ============================================================
# Real LLM / data source call toggle (T-W12-8: moved to Settings)
# ============================================================
# A013 fix: Mock checks go through Settings (lazy, call-time reads).
# Migration mapping:
#   _USE_REAL_LLM  → not get_settings().use_mock_llm

# ============================================================
# Mock data helpers
# ============================================================

_MOCK_MESSAGES: list[dict[str, Any]] = [
    {
        "server_id": "msg_001",
        "sender": "user_alice",
        "account_name": "Alice",
        "group_nickname": "Alice",
        "timestamp": 1714291200000,
        "msg_type": "text",
        "content": "今天天气真好，适合写代码",
        "media_url": "",
        "reply_to": "",
    },
    {
        "server_id": "msg_002",
        "sender": "user_bob",
        "account_name": "Bob",
        "group_nickname": "Bob",
        "timestamp": 1714291260000,
        "msg_type": "text",
        "content": "有人看了最新的 LangGraph 文档吗？Send API 挺好用的",
        "media_url": "",
        "reply_to": "",
    },
    {
        "server_id": "msg_003",
        "sender": "user_charlie",
        "account_name": "Charlie",
        "group_nickname": "Charlie",
        "timestamp": 1714291320000,
        "msg_type": "text",
        "content": "我试了一下，并行 fan-out 很稳定",
        "media_url": "",
        "reply_to": "",
    },
    {
        "server_id": "msg_004",
        "sender": "user_alice",
        "account_name": "Alice",
        "group_nickname": "Alice",
        "timestamp": 1714291380000,
        "msg_type": "text",
        "content": "关于 prompt injection 的防护，大家有什么建议？",
        "media_url": "",
        "reply_to": "",
    },
    {
        "server_id": "msg_005",
        "sender": "user_bob",
        "account_name": "Bob",
        "group_nickname": "Bob",
        "timestamp": 1714291440000,
        "msg_type": "text",
        "content": "日报系统可以考虑加个议题追踪功能，长期讨论可以跨天追踪",
        "media_url": "",
        "reply_to": "",
    },
    {
        "server_id": "msg_006",
        "sender": "user_charlie",
        "account_name": "Charlie",
        "group_nickname": "Charlie",
        "timestamp": 1714291500000,
        "msg_type": "text",
        "content": "分享一个链接 https://github.com/langchain-ai/langgraph",
        "media_url": "",
        "reply_to": "",
    },
    {
        "server_id": "msg_007",
        "sender": "user_alice",
        "account_name": "Alice",
        "group_nickname": "Alice",
        "timestamp": 1714291560000,
        "msg_type": "text",
        "content": "那个 bug 修好了，是 SQLite WAL 模式的问题",
        "media_url": "",
        "reply_to": "",
    },
    {
        "server_id": "msg_008",
        "sender": "user_bob",
        "account_name": "Bob",
        "group_nickname": "Bob",
        "timestamp": 1714291620000,
        "msg_type": "text",
        "content": "部署到生产环境的时候要注意环境变量配置",
        "media_url": "",
        "reply_to": "",
    },
    {
        "server_id": "msg_009",
        "sender": "user_charlie",
        "account_name": "Charlie",
        "group_nickname": "Charlie",
        "timestamp": 1714291680000,
        "msg_type": "image",
        "content": "架构图",
        "media_url": "/assets/arch.png",
        "reply_to": "",
    },
    {
        "server_id": "msg_010",
        "sender": "user_alice",
        "account_name": "Alice",
        "group_nickname": "Alice",
        "timestamp": 1714291740000,
        "msg_type": "text",
        "content": "周末愉快！下周继续推进议题追踪模块",
        "media_url": "",
        "reply_to": "",
    },
]


def _load_messages_from_data(group_name: str, date: str) -> list[dict[str, Any]]:
    """Try to load messages from data/ directory; fall back to mock data.

    Looks for files matching patterns:
      - data/{group_name}_{date}.json
      - data/messages_{date}.json
      - data/mock_messages.json

    Returns mock messages if no data file is found.
    """
    data_dir = Path("data")
    if not data_dir.exists():
        logger.info("data/ directory not found, using mock messages")
        return _MOCK_MESSAGES

    # Try specific file first
    candidates = [
        data_dir / f"{group_name}_{date}.json",
        data_dir / f"messages_{date}.json",
        data_dir / "mock_messages.json",
    ]

    for filepath in candidates:
        if filepath.exists():
            try:
                content = filepath.read_text(encoding="utf-8")
                messages = json.loads(content)
                if isinstance(messages, list) and len(messages) > 0:
                    logger.info("Loaded %d messages from %s", len(messages), filepath)
                    return messages
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load %s: %s", filepath, exc)

    logger.warning("No data files found for group=%s date=%s", group_name, date)
    return []


# ============================================================
# Node 1: data_fetch
# ============================================================


async def node_data_fetch(state: OverallState) -> dict[str, Any]:
    """Fetch chat messages via CipherTalk API (or fall back to file/mock).

    Timeout: GRAPH_NODE_TIMEOUT (default 300s). P008: data_fetch ~10s, well within limit.

    Tries in order:
      1. CipherTalk HTTP API (if api_token is configured)
      2. data/ directory JSON files
      3. Built-in mock messages

    Input:  state.group_name, state.date, state.api_base_url, state.api_token
    Output: state.messages, state.current_phase
    """
    # P009: regenerate mode — skip data fetch, reuse cached messages from state
    if state.get("regenerate"):
        logger.info(
            "data_fetch: regenerate mode — skipping fetch, %d cached messages",
            len(state.get("messages", [])),
        )
        return {"current_phase": "data_fetch_cached"}

    try:
        from z_winnow.config.settings import get_settings

        group_name = state["group_name"]
        # 统一日期为 YYYYMMDD（graph 内部约定：renderer.py / degraded.py / display_date 均按 8 位紧凑格式处理）。
        # 接受 YYYY-MM-DD 或 YYYYMMDD，规范化后写回 state 供下游一致使用。
        date = state["date"].replace("-", "")
        # base_url：优先用 state 显式传入，否则读 settings.ciphertalk_base_url（修复原先硬编码 5031，无法适配非默认端口）。
        api_base_url = state.get("api_base_url") or get_settings().effective_data_base_url
        api_token = state.get("api_token", "")

        # Resolve group_id → chatroom_id for stable data source matching
        group_id = state.get("group_id", "")
        chatroom_id = ""
        if not group_id:
            from z_winnow.pipeline.group_config import resolve_group_id

            group_id = await resolve_group_id(group_name)

        # Resolve chatroom_id BEFORE fetch — fetch_messages needs it to skip the
        # fragile find_group_session(group_name) lookup, which fails when the
        # registered display_name differs from the data source's real session
        # displayName (e.g. a custom group name). Falls back to post-fetch
        # resolve below if this misses (no registered chatroom_id).
        if not chatroom_id and group_id:
            try:
                from z_winnow.pipeline.group_config import (
                    resolve_chatroom_id_with_fallback,
                )

                chatroom_id = await resolve_chatroom_id_with_fallback(
                    group_id=group_id,
                    group_name=group_name,
                    raw_messages=[],
                    db_path=get_settings().sqlite_db_path,
                )
            except Exception as exc:
                logger.warning("resolve_chatroom_id (pre-fetch) failed: %s", exc)

        logger.info(
            "data_fetch: group=%s, group_id=%s, chatroom_id=%s, date=%s",
            group_name,
            group_id,
            chatroom_id,
            date,
        )

        messages: list[dict[str, Any]] = []

        # Attempt 1: CipherTalk API
        # T-W12-8: get_settings imported at top of this try block
        settings = get_settings()
        if not api_token:
            api_token = settings.effective_data_token
        if api_token:
            try:
                from z_winnow.pipeline.cipher_talk_client import create_data_client

                async with create_data_client(
                    base_url=api_base_url,
                    token=api_token,
                ) as client:
                    raw_messages = await client.fetch_messages(
                        group_name=group_name,
                        date=date,
                        chatroom_id=chatroom_id or None,
                    )
                    if raw_messages:
                        messages = raw_messages
                        logger.info(
                            "data_fetch: fetched %d messages from data source API",
                            len(messages),
                        )
            except Exception as exc:
                logger.warning("Data source API fetch failed: %s. Trying file fallback.", exc)

        # Attempt 2: File-based data
        if not messages:
            messages = _load_messages_from_data(group_name, date)

        logger.info(
            "data_fetch: loaded %d messages",
            len(messages),
        )

        # 回填 pipeline_runs.message_count（供前端/记录展示真实抓取条数）
        _run_id = state.get("run_id")
        if _run_id:
            try:
                from z_winnow.graph.progress import update_pipeline_run

                await update_pipeline_run(_run_id, message_count=len(messages))
            except Exception as _mc_exc:
                logger.debug("data_fetch: message_count update failed (non-blocking): %s", _mc_exc)

        # T-W13-2: Enrich nicknames via group-members API
        member_map: dict[str, str] = {}
        if messages:
            try:
                from z_winnow.pipeline.cipher_talk_client import create_data_client
                from z_winnow.pipeline.group_config import (
                    build_member_map,
                    resolve_chatroom_id_with_fallback,
                )

                chatroom_id = await resolve_chatroom_id_with_fallback(
                    group_id=group_id,
                    group_name=group_name,
                    raw_messages=messages,
                    db_path=get_settings().sqlite_db_path,
                )

                # Resolve display_name from groups table
                if group_id:
                    import aiosqlite as _aiosqlite_dn

                    async with _aiosqlite_dn.connect(get_settings().sqlite_db_path) as _dn_db:
                        cur = await _dn_db.execute(
                            "SELECT display_name FROM groups WHERE group_id = ?",
                            (group_id,),
                        )
                        row = await cur.fetchone()
                        if row and row[0]:
                            group_name = row[0]

                if chatroom_id and "@chatroom" in chatroom_id:
                    async with create_data_client(
                        base_url=api_base_url, token=api_token
                    ) as wf_client:
                        member_map = await build_member_map(
                            chatroom_id, wf_client, messages=messages
                        )

                if member_map:
                    for msg in messages:
                        sender_wxid = msg.get("sender", "")
                        if sender_wxid in member_map:
                            display = member_map[sender_wxid]
                            current_name = msg.get("account_name", "")
                            if (
                                not current_name
                                or current_name == sender_wxid
                                or current_name.startswith("wxid_")
                                or current_name.endswith("@openim")
                            ):
                                msg["account_name"] = display
                                msg["group_nickname"] = display

                logger.info(
                    "data_fetch: member_map built — %d members for %s",
                    len(member_map),
                    chatroom_id,
                )
            except Exception as _mm_exc:
                logger.warning(
                    "data_fetch: group-members enrichment failed — %s (continuing)", _mm_exc
                )

        # T-W12-7: S1 即时固化 — L1 写入在阶段 A 结束即执行，不可变
        # P022: Storage 层独立写入，与业务逻辑零耦合
        raw_count = 0
        if messages:
            try:
                import aiosqlite as _aiosqlite

                from z_winnow.pipeline.database import (
                    init_database_in_conn as _init_db,
                )
                from z_winnow.pipeline.database import (
                    insert_raw_messages as _insert_raw,
                )

                _settings = get_settings()
                _db_path = _settings.sqlite_db_path
                Path(_db_path).parent.mkdir(parents=True, exist_ok=True)

                # P1-6: Serialize SQLite writes across concurrent group pipelines
                from z_winnow.pipeline.db_lock import db_write_lock

                async with await db_write_lock(), _aiosqlite.connect(_db_path) as _db:
                    await _init_db(_db)
                    raw_count = await _insert_raw(_db, messages, date, group_id=group_id)

                logger.info(
                    "data_fetch: L1 persist — wrote %d raw messages to %s",
                    raw_count,
                    _db_path,
                )
            except Exception as _l1_exc:
                logger.warning("data_fetch: L1 persist failed (non-blocking) — %s", _l1_exc)

        return {
            "messages": messages,
            "current_phase": "data_fetch",
            "raw_message_count": raw_count,
            "group_id": group_id,
            "member_map": member_map,
            "group_name": group_name,
            "date": date,
        }
    except Exception as e:
        logger.error("node_data_fetch failed: %s", e, exc_info=True)
        return {
            "messages": [],
            "current_phase": "data_fetch_error",
            "member_map": {},
            "errors": [{"node": "data_fetch", "error": str(e)}],
        }


# ============================================================
# Node 1.5: orchestrator — T-W10-E-d: memory context loading
# ============================================================
# P016: Lazy import + single insertion point — create_memos_adapter()
# imported only within orchestrator node. MemOS search loads the
# "context memory pack" (recent LongTermMemory + feedback/corrections)
# into state.memory_context.
# T-W12-6: S3 — MemOS is required service. Read failures raise and
# interrupt the pipeline (no graceful degradation for read path).


async def _generate_semantic_queries(
    chat_context_md: str,
    group_name: str,
    date: str,
) -> list[str]:
    """Generate 2-3 semantic search queries via lightweight LLM call.

    Uses chat_context_markdown (enriched with nicknames, image descriptions,
    link previews) as input — richer than raw message fragments.
    Truncated to ~2000 chars to keep LLM input compact.

    Falls back to [group_name, date] on LLM failure.
    """
    if not chat_context_md or not chat_context_md.strip():
        return [f"{group_name} {date}"]

    display_date = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else date

    system_prompt = (
        "你是议题分析专家。根据今日群聊上下文，提炼出2-3个核心讨论主题。"
        "每个主题用一句话概括（15-30字），侧重议题本质而非消息原文。"
        '输出JSON数组，如：["AI安全与伦理治理", "微信生态技术方案"]'
        "不要解释，只输出JSON数组。"
    )
    user_prompt = f"群聊：{group_name}，日期：{display_date}\n今日群聊上下文：\n{chat_context_md}"

    try:
        import json

        from z_winnow.config.models import create_model_for_subagent

        llm = create_model_for_subagent("unified-reporter", temperature=0.0)
        import asyncio as _asyncio_mod

        result = await _asyncio_mod.wait_for(
            llm.ainvoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            ),
            timeout=10.0,
        )
        raw = result.content.strip()
        # Strip markdown code fence if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        queries = json.loads(raw)
        if isinstance(queries, list) and all(isinstance(q, str) for q in queries) and queries:
            logger.info(
                "orchestrator: semantic queries generated — %s",
                queries,
            )
            return queries[:3]
    except Exception as e:
        logger.warning("orchestrator: semantic query generation failed, falling back — %s", e)

    return [f"{group_name} {date}"]


# ============================================================
# Orchestrator sub-step helpers (@traceable for LangSmith nesting)
# ============================================================
# langsmith≥0.7 的 @traceable 不通过 contextvars 暴露内部上下文,
# trace() context manager 和 get_current_run_tree() 均不可用.
# 嵌套 @traceable 函数是唯一可靠的子 span 机制:
#   orchestrator (LangGraph auto-trace span)
#       └── @_ls_traceable("orch:...")    ← 自动嵌套为子 span
#
# 这些函数是 orchestrator 内部子步骤的薄封装, 只做 trace 嵌套 +
# 参数透传. 实际逻辑保持在 _generate_semantic_queries (已有) 和
# node_orchestrator 内部.


@_ls_traceable(name="orch:validate_types", run_type="chain")
def _orch_validate(report_types: list[str]) -> list[str]:
    """Validate and normalize report types (pure function, no I/O)."""
    _valid = frozenset({"daily"})
    validated = [t.lower().strip() for t in report_types if t.lower().strip() in _valid]
    return validated if validated else ["daily"]


@_ls_traceable(name="orch:semantic_queries", run_type="chain")
async def _orch_semantic_queries(chat_context_md: str, group_name: str, date: str) -> list[str]:
    """Generate MemOS semantic search queries via lightweight LLM call."""
    return await _generate_semantic_queries(chat_context_md, group_name, date)


async def _do_mem_search_one_cube(
    adapter: Any,
    group_id: str,
    group_name: str,
    queries: list[str],
    cube_scope: str,
    prefix: str,
    timeout: float,
) -> list[Any]:
    """Shared MemOS cube search logic — called by the @traceable wrappers below.

    Creates the cube (if needed), runs parallel queries, merges and deduplicates
    results.  Individual query failures are isolated (log + skip).
    """
    import asyncio as _asyncio

    cube_id = await adapter.get_or_create_cube(f"{group_id}:{cube_scope}")
    legacy_ids = [group_name] if group_name != group_id else None

    async def _single(q: str) -> list[Any]:
        try:
            query = f"{prefix}{q}" if prefix else q
            return await _asyncio.wait_for(
                adapter.search_memories(
                    query=query,
                    group_id=group_id,
                    readable_cube_ids=[cube_id],
                    top_k=5,
                    legacy_group_ids=legacy_ids,
                ),
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning(
                "orchestrator: MemOS %s search failed for query=%s — %s",
                cube_scope,
                q[:40],
                exc,
            )
            return []

    batches = await _asyncio.gather(*[_single(q) for q in queries])
    seen: set[str] = set()
    merged: list[Any] = []
    for batch in batches:
        for r in batch:
            if r.id not in seen:
                seen.add(r.id)
                merged.append(r)
    return merged


@_ls_traceable(name="orch:mem_search:topics", run_type="chain")
async def _orch_mem_search_topics(
    adapter: Any,
    group_id: str,
    group_name: str,
    queries: list[str],
    timeout: float,
) -> list[Any]:
    return await _do_mem_search_one_cube(
        adapter, group_id, group_name, queries, "topics", "", timeout
    )


@_ls_traceable(name="orch:mem_search:feedback", run_type="chain")
async def _orch_mem_search_feedback(
    adapter: Any,
    group_id: str,
    group_name: str,
    queries: list[str],
    timeout: float,
) -> list[Any]:
    return await _do_mem_search_one_cube(
        adapter, group_id, group_name, queries, "feedback", "反馈修正 ", timeout
    )


async def _filter_rolled_back_nodes(
    topics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """M4: 从 topics 召回里剔除被回滚反馈纠正过的 MemOS 节点。

    被回滚的 feedback_event 其 memos_node_id 指向 topics cube 里一个"纠正后"节点；
    该反馈 rolled_back 时，对应节点应从历史召回中排除（效果随版本撤回）。
    节点 id 全局唯一，无需按 group 过滤（topics 列表本身已 group-scoped）。
    """
    if not topics:
        return topics
    try:
        import aiosqlite as _aio

        from z_winnow.config.settings import get_settings

        async with _aio.connect(get_settings().sqlite_db_path) as _db:
            cur = await _db.execute(
                "SELECT memos_node_id FROM feedback_events "
                "WHERE status = 'rolled_back' AND memos_node_id IS NOT NULL"
            )
            rolled = {r[0] for r in await cur.fetchall()}
    except Exception:
        return topics
    if not rolled:
        return topics
    return [t for t in topics if t.get("id") not in rolled]


@_ls_traceable(name="orch:load_core_topics", run_type="chain")
async def _orch_load_core_topics(db_path: str, group_id: str) -> list[dict[str, Any]]:
    """Load user-defined core topics from SQLite."""
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT name, description, keywords, core_topic_id "
            "FROM core_topics WHERE group_id = ? AND is_active = 1 "
            "ORDER BY priority",
            (group_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def node_orchestrator(state: OverallState) -> dict[str, Any]:
    """Orchestrator node — validate report_types + load memory context from MemOS.

    T-W10-E-d: Loads "context memory pack" (recent LongTermMemory + feedback)
    into state.memory_context. Replaces the removed memory_inject stub.

    T-W12-6 (S3): MemOS is a required service. Read failures raise and
    interrupt the LangGraph pipeline — no graceful degradation.

    P016: Lazily imports create_memos_adapter() and searches MemOS for:
    1. Historical topics (topics cube, top_k=10, timeout 30s)
    2. Prior corrections/feedback (feedback cube, top_k=10, timeout 30s)

    Timeout: GRAPH_NODE_TIMEOUT (default 300s). Orchestrator ~5-10s.
    L020: This is execution timeout (node internal), not dispatch timeout.

    Memory context structure (T-W12-6):
        {"historical_topics": [...], "prior_corrections": [...],
         "memory_cube_status": "ok"}
    - ok: MemOS search succeeded
    - If MemOS search fails → exception propagates, pipeline interrupts

    Input:
        state.messages, state.report_types, state.date, state.group_name

    Output:
        state.report_types (validated), state.memory_context, state.current_phase
    """

    # ── Extract state fields ──
    report_types = state.get("report_types", [])
    date = state.get("date", "")
    messages = state.get("messages", [])
    group_name = state.get("group_name", "")
    group_id = state.get("group_id", "")

    logger.info(
        "orchestrator: date=%s, report_types=%s, messages=%d, group=%s, group_id=%s",
        date,
        report_types,
        len(messages),
        group_name,
        group_id,
    )

    # ① orch:validate_types (module-level @traceable → LangSmith child span)
    try:
        validated_types = _orch_validate(report_types)
        if set(validated_types) != set(report_types):
            logger.info(
                "orchestrator: report types normalized %s -> %s",
                report_types,
                validated_types,
            )
    except Exception as e:
        logger.error("node_orchestrator validation failed: %s", e, exc_info=True)
        return {
            "report_types": state.get("report_types", ["daily"]),
            "current_phase": "orchestrating_error",
            "memory_context": None,
            "errors": [{"node": "orchestrator", "error": str(e)}],
        }

    # Phase 2: MemOS search
    from z_winnow.config.settings import get_settings
    from z_winnow.memory.factory import create_memos_adapter

    memos_enabled = get_settings().memos_enabled
    _memos_search_timeout = float(get_settings().memos_search_timeout)
    memory_context: dict[str, Any] | None = None

    if memos_enabled:
        adapter = create_memos_adapter()

        # ② orch:semantic_queries (module-level @traceable → LLM call child span)
        semantic_queries = await _orch_semantic_queries(
            state.get("chat_context_markdown", ""),
            group_name,
            date,
        )

        # ③ orch:mem_search — topics only（M4：feedback cube 已废弃——纠正走
        # correction_loader 读 group_experiences；topics 召回过滤 rolled_back 节点）
        topic_results = await _orch_mem_search_topics(
            adapter,
            group_id,
            group_name,
            semantic_queries,
            _memos_search_timeout,
        )

        historical_topics = [
            {"id": r.id, "memory": r.memory, "score": r.score} for r in topic_results
        ]
        # M4: 剔除被回滚反馈纠正过的 MemOS 节点（其 memos_node_id ∈ rolled_back feedback）
        historical_topics = await _filter_rolled_back_nodes(historical_topics)

        logger.info(
            "orchestrator: MemOS topics search OK — %d (queries=%s)",
            len(historical_topics),
            semantic_queries,
        )

        memory_context = {
            "historical_topics": historical_topics,
            # M4: prior_corrections 主源切 correction_loader(group_experiences)，
            # 不再从 MemOS feedback cube 召回（该 cube 已废弃）。
            "prior_corrections": [],
            "memory_cube_status": "ok",
        }
        logger.info(
            "orchestrator: memory_context status=ok, topics=%d",
            len(historical_topics),
        )
    else:
        logger.info("orchestrator: MemOS disabled (memos_enabled=false)")

    # ④ orch:load_core_topics (module-level @traceable → SQLite child span)
    try:
        user_defined_topics = await _orch_load_core_topics(
            get_settings().db_path,
            group_id,
        )
    except Exception as exc:
        logger.warning(
            "orchestrator: core_topics query failed (non-blocking): %s",
            exc,
        )
        user_defined_topics = []

    if user_defined_topics:
        if memory_context is None:
            memory_context = {}
        memory_context["user_defined_topics"] = user_defined_topics
        logger.info(
            "orchestrator: loaded %d user-defined core_topics for group=%s",
            len(user_defined_topics),
            group_id,
        )

    return {
        "report_types": validated_types,
        "current_phase": "orchestrating",
        "memory_context": memory_context,
    }


# ============================================================
# Node 2: unified_reporter (single agent, single LLM call)
# ============================================================


def _count_custom_table_records(slot: object) -> int:
    """Count total records across all custom_tables in an output slot (for logging)."""
    if not isinstance(slot, dict):
        return 0
    from z_winnow.custom_tables import registry as _ct_reg

    total = 0
    for table_id, table_data in slot.items():
        if not isinstance(table_data, dict):
            continue
        tdef = _ct_reg.get_table(table_id)
        records_key = tdef.records_key if tdef else "items"
        records = table_data.get(records_key)
        if isinstance(records, list):
            total += len(records)
    return total


async def node_unified_reporter(state: OverallState) -> dict[str, Any]:
    """Generate unified report from chat messages — single LLM call.

    Replaces 3 parallel subagent calls (daily_reporter + resource_extractor
    + engineering_analyzer) with one unified LLM call that produces all
    sections: daily overview, topics (unified), resources, engineering issues.

    Input:  state.messages, state.date, state.group_name
    Output: state.unified_report (single dict with all sections)
    """
    date = state["date"]
    messages = state.get("messages", [])
    group_name = state.get("group_name", "群聊")
    group_id = state.get("group_id", group_name)

    # Use pre-built chat context markdown from content_enrich
    chat_context_md = state.get("chat_context_markdown", "")
    enriched_messages = messages

    # ── 0-message short-circuit ──────────────────────────────────────
    # 当日无消息：跳过 LLM 与下游 group_cfg/members/corrections/feedback 查询（省开销），
    # 直接返回空报告。不终止链路 —— output_composer 照常落空 L3 + 写一条
    # "当日无交流" MemOS 记忆（见 output_composer 的 n_enqueued==0 分支）。
    if not enriched_messages:
        logger.info(
            "unified_reporter: 0 messages for group=%s date=%s — skipping LLM, empty report",
            group_id,
            date,
        )
        return {
            "unified_report": {
                "overview": "今日群聊无任何消息，无议题、资源与工程问题产出。",
                "important_notice": "",
                "topics": [],
                "trend_analysis": "今日无消息，无议题动态。",
                "trend_summary": "今日无交流。",
                "highlights": [],
                "resources": [],
                "resource_count_by_type": {},
                "custom_tables": {},
            }
        }

    # P0-1/P0-2: Query group config + VIP members for prompt injection
    group_cfg: dict | None = None
    members: list[dict] | None = None
    engineering_enabled: bool = True  # default to enabled
    custom_tables: dict | None = None  # CT-2: custom table configurations
    try:
        import json as _json_ct

        import aiosqlite as _aiosqlite_gc

        from z_winnow.config.settings import get_settings

        _gc_db_path = get_settings().sqlite_db_path
        async with _aiosqlite_gc.connect(_gc_db_path) as _gc_db:
            _gc_db.row_factory = _aiosqlite_gc.Row
            # Query group config (display_name, custom_prompt_hints, custom_tables, feishu_tables)
            _gc_cursor = await _gc_db.execute(
                "SELECT group_id, display_name, custom_prompt_hints, engineering_enabled, custom_tables, feishu_tables FROM groups WHERE group_id = ?",
                (group_id,),
            )
            _gc_row = await _gc_cursor.fetchone()
            if _gc_row:
                group_cfg = {
                    "display_name": _gc_row["display_name"] or group_name,
                    "custom_prompt_hints": _gc_row["custom_prompt_hints"] or "",
                }

                # CT-2: Parse custom_tables JSON blob (authoritative table config)
                _ct_raw = _gc_row["custom_tables"]
                if _ct_raw and isinstance(_ct_raw, str) and _ct_raw.strip():
                    try:
                        custom_tables = _json_ct.loads(_ct_raw)
                        logger.info(
                            "unified_reporter: loaded custom_tables for group=%s (%d kinds)",
                            group_id,
                            len(custom_tables) if isinstance(custom_tables, dict) else 0,
                        )
                    except (_json_ct.JSONDecodeError, TypeError) as _ct_err:
                        logger.warning(
                            "unified_reporter: custom_tables JSON parse failed for group=%s: %s",
                            group_id,
                            _ct_err,
                        )
                        custom_tables = None

                # Parse feishu_tables blob (legacy table config; custom_tables overrides it).
                _ft: dict | None = None
                _ft_raw = _gc_row["feishu_tables"]
                if _ft_raw and isinstance(_ft_raw, str) and _ft_raw.strip():
                    try:
                        _ft_parsed = _json_ct.loads(_ft_raw)
                        _ft = _ft_parsed if isinstance(_ft_parsed, dict) else None
                    except (_json_ct.JSONDecodeError, TypeError):
                        _ft = None

                # Resolve via the single report-side resolver (custom_tables >
                # feishu_tables > deprecated engineering_enabled column > default off).
                from z_winnow.pipeline.feishu import schema as _feishu_schema

                _ct_dict = custom_tables if isinstance(custom_tables, dict) else None
                _eng_on = _feishu_schema.engineering_enabled_for_report(
                    _ct_dict, _ft, _gc_row["engineering_enabled"]
                )
                engineering_enabled = _eng_on

                # Build a resolved custom_tables blob for prompt injection + L3 persistence:
                # engineering reflects the resolved flag (honoring the column fallback so
                # legacy groups keep their content); other optional kinds (future plugins)
                # follow active_kinds. config preserved per-kind.
                _active = _feishu_schema.active_kinds(_ft, _ct_dict)
                _resolved_ct: dict = {}
                for _kind in _feishu_schema.TABLE_CATALOG:
                    if _kind in _feishu_schema.MANDATORY_KINDS:
                        continue
                    _prev = (_ct_dict or {}).get(_kind)
                    _prev_cfg = _prev.get("config", {}) if isinstance(_prev, dict) else {}
                    _on = _eng_on if _kind == "engineering" else (_kind in _active)
                    _resolved_ct[_kind] = {"enabled": _on, "config": _prev_cfg}
                custom_tables = _resolved_ct

                logger.info(
                    "unified_reporter: loaded group_cfg for group=%s (hints=%d chars, eng=%s)",
                    group_id,
                    len(group_cfg.get("custom_prompt_hints", "")),
                    engineering_enabled,
                )

            # Query VIP members (role + weight)
            _gm_cursor = await _gc_db.execute(
                "SELECT name, role, weight FROM group_members WHERE group_id = ? AND is_active = 1 ORDER BY weight DESC",
                (group_id,),
            )
            _gm_rows = await _gm_cursor.fetchall()
            if _gm_rows:
                members = [dict(r) for r in _gm_rows]
                logger.info(
                    "unified_reporter: loaded %d active members for group=%s",
                    len(members),
                    group_id,
                )
    except Exception as _gc_exc:
        logger.debug("unified_reporter: group_cfg query failed (non-blocking): %s", _gc_exc)

    # T-W10-C-b: Load historical correction examples for prompt injection
    prior_corrections = None
    try:
        from z_winnow.rl.correction_loader import load_corrections

        prior_corrections = await load_corrections(
            group_id=group_id,
            date=date,
            days=30,
        )
        if prior_corrections:
            logger.info(
                "unified_reporter: loaded %d correction examples for group=%s",
                len(prior_corrections),
                group_name,
            )
    except ImportError:
        logger.debug("unified_reporter: correction_loader not available, skipping")
    except Exception as exc:
        logger.warning("unified_reporter: load_corrections failed (non-blocking): %s", exc)

    # P009: Merge state.prior_corrections (from regenerate endpoint) with loaded corrections
    state_corrections = state.get("prior_corrections", [])
    if state_corrections:
        if prior_corrections is None:
            prior_corrections = list(state_corrections)
        else:
            prior_corrections = list(state_corrections) + list(prior_corrections)
        logger.info(
            "unified_reporter: merged %d state corrections + %d loaded corrections = %d total",
            len(state_corrections),
            len(prior_corrections) - len(state_corrections),
            len(prior_corrections),
        )

    # P0-3: Load unconsumed feedback hints for prompt injection
    feedback_hints: list[str] | None = None
    try:
        from z_winnow.pipeline.database import get_unconsumed_feedback

        _fb_db_path = get_settings().sqlite_db_path
        async with _aiosqlite_gc.connect(_fb_db_path) as _fb_db:
            _fb_db.row_factory = _aiosqlite_gc.Row
            unconsumed_fb = await get_unconsumed_feedback(_fb_db, group_id, date)
            if unconsumed_fb:
                feedback_hints = []
                for _fb in unconsumed_fb:
                    _line = f"- [{_fb.get('signal', '')}] {_fb.get('target_type', '')}"
                    if _fb.get("original_text"):
                        _line += f": {_fb['original_text'][:120]}"
                    if _fb.get("corrected_text"):
                        _line += f" → 修正: {_fb['corrected_text'][:120]}"
                    if _fb.get("correction_note"):
                        _line += f" (备注: {_fb['correction_note'][:80]})"
                    feedback_hints.append(_line)
                logger.info(
                    "unified_reporter: loaded %d unconsumed feedback hints for group=%s date=%s",
                    len(feedback_hints),
                    group_id,
                    date,
                )
    except Exception as _fb_exc:
        logger.debug("unified_reporter: feedback hints query failed (non-blocking): %s", _fb_exc)

    # MemOS: Extract historical topics from state.memory_context
    historical_topics = None
    user_defined_topics = None
    memory_context = state.get("memory_context")
    if memory_context and isinstance(memory_context, dict):
        historical_topics = memory_context.get("historical_topics")
        user_defined_topics = memory_context.get("user_defined_topics")
        if historical_topics:
            logger.info(
                "unified_reporter: using %d historical topics from MemOS memory_context",
                len(historical_topics),
            )
        if user_defined_topics:
            logger.info(
                "unified_reporter: using %d user-defined core_topics",
                len(user_defined_topics),
            )

    # T-W12-8: Use Settings.use_mock_llm instead of _USE_REAL_LLM
    # A013: Read at call time, not module import time
    from z_winnow.config.settings import get_settings

    _use_real_llm = not get_settings().use_mock_llm
    if _use_real_llm:
        try:
            from z_winnow.subagents.unified_reporter import (
                generate_unified_report,
            )

            logger.info(
                "unified_reporter: calling LLM, messages=%d (with L2 enrichment) corrections=%d",
                len(enriched_messages),
                len(prior_corrections or []),
            )
            result = await generate_unified_report(
                enriched_messages,
                date,
                group_name,
                prior_corrections=prior_corrections,
                historical_topics=historical_topics,
                user_defined_topics=user_defined_topics,
                chat_context_md=chat_context_md or None,
                group_cfg=group_cfg,
                members=members,
                feedback_hints=feedback_hints,
                custom_tables=custom_tables,
            )

            report = result.model_dump()

            # Defensive strip: keep only custom tables enabled in the resolved config.
            # The LLM is only prompted for enabled tables, but this guarantees a
            # disabled table never leaks into the output slot (e.g. engineering off).
            _enabled_kinds = {
                k
                for k, c in (custom_tables or {}).items()
                if isinstance(c, dict) and c.get("enabled")
            }
            _out_ct = report.get("custom_tables")
            if isinstance(_out_ct, dict) and _enabled_kinds:
                for _k in list(_out_ct.keys()):
                    if _k not in _enabled_kinds:
                        _out_ct.pop(_k)
                        logger.info(
                            "unified_reporter: stripped disabled custom table %s for group=%s",
                            _k,
                            group_id,
                        )

            _ct_record_total = _count_custom_table_records(report.get("custom_tables"))
            logger.info(
                "unified_reporter: LLM report generated, topics=%d resources=%d custom_table_records=%d",
                len(report.get("topics", [])),
                len(report.get("resources", [])),
                _ct_record_total,
            )
            return {"unified_report": report, "custom_tables": custom_tables}
        except Exception as e:
            logger.error("unified_reporter LLM call failed: %s", e)
            raise RuntimeError(
                f"unified_reporter LLM call failed (mock disabled in production): {e}"
            ) from e

    # Mock only allowed in test environment
    if get_settings().environment == "test":
        logger.info("unified_reporter: using mock (test environment)")
        result = _mock_unified_reporter(state)
        result["custom_tables"] = custom_tables
        return result

    raise RuntimeError(
        "unified_reporter: no LLM result (use_mock_llm=false, no real call executed)"
    )


def _mock_unified_reporter(state: OverallState) -> dict[str, Any]:
    """Deterministic mock for unified_reporter."""
    from z_winnow.subagents.unified_reporter.mock import (
        _mock_generate_unified_report,
    )

    logger.info("unified_reporter: generating mock unified report")
    report = _mock_generate_unified_report(
        state.get("messages", []), state["date"], state.get("group_name", "群聊")
    )
    if isinstance(report, dict):
        return {"unified_report": report}
    return {"unified_report": report.model_dump()}


# ============================================================
# Node 3: output_composer
# ============================================================


async def node_output_composer(state: OverallState) -> dict[str, Any]:
    """Phase E: Write 4 L3 JSON files + L3 DB records (topic_summaries + report_versions).

    T-W12-3: Replaced inline Markdown splicing with call to the formal
    output_composer module's compose_json function. No Markdown is generated
    in the main flow (S4: review before export). Markdown rendering is
    Phase H (render_markdown), not in the graph edges.

    T-W12-7: S1 即时固化 — L3 写入在阶段 E 结束即执行。
    topic_summaries + report_versions 从集中 persist 迁移到此节点。
    L3 允许增量更新（与 L1/L2 不可变不同）。

    Input:
        state.unified_report, state.date

    Output:
        state.final_report: "" (no Markdown in main flow)
        state.report_sections: list of written JSON file names
        state.topic_summary_count: number of topic summaries written
        state.current_phase: updated to "composed"
    """
    try:
        date = state["date"]
        unified = state.get("unified_report", {})

        # P016: 惰性导入 — compose_json from formal output_composer module
        # T-W12-5: Determine output directory via Settings (S7 配置单源)
        from z_winnow.config.settings import get_settings
        from z_winnow.subagents.output_composer import compose_json

        settings = get_settings()
        output_dir = settings.layer3_output_dir
        group_id = state.get("group_id", "")
        report_id = f"{group_id}-{date}"

        # M4: 版本化 L3 目录 — 预知版本号，写 v{n}/，与 report_versions 行对齐
        next_version_number = 1
        try:
            import aiosqlite as _peek_aio

            from z_winnow.pipeline.database import init_database_in_conn as _init_peek
            from z_winnow.pipeline.report_version import peek_next_version_number

            async with _peek_aio.connect(settings.sqlite_db_path) as _pdb:
                await _init_peek(_pdb)
                next_version_number = await peek_next_version_number(_pdb, report_id)
        except Exception as _peek_exc:
            logger.debug("output_composer: version peek failed, defaulting to v1 — %s", _peek_exc)

        date_output_dir = os.path.join(output_dir, group_id, date, f"v{next_version_number}")

        # Phase E: Write L3 JSON files (CT-3: dynamic based on custom_tables config)
        ct_config = state.get("custom_tables")
        json_paths = await compose_json(
            unified_report_output=unified if unified else None,
            output_dir=date_output_dir,
            date=date,
            custom_tables_config=ct_config,
        )

        # #9.3: 回填 resource.local_path — content_enrich 下载的本地媒体路径,
        # 通过 resource.source_server_ids 关联, 供飞书资源表附件上传消费.
        # 同时扫描外部微信文件存储目录(SMB), 匹配到时自动拷贝到 attachments/.
        try:
            from z_winnow.subagents.output_composer import (
                patch_resources_local_path,
            )

            _res_path = json_paths.get("resources")
            _msgs = state.get("messages") or []
            if _res_path and _msgs:
                _ext_dirs: list[str] = []
                _wc_dir = (get_settings().wechat_file_storage_dir or "").strip()
                if _wc_dir:
                    _ext_dirs.append(_wc_dir)
                _n = patch_resources_local_path(_res_path, _msgs, external_dirs=_ext_dirs or None)
                if _n:
                    logger.info("output_composer: patched local_path into %d resources", _n)
        except Exception as _patch_exc:
            logger.debug("output_composer: local_path patch skipped — %s", _patch_exc)

        # R2 上传：给已回填 local_path 的资源传私有 R2 桶 + 记 cloud_key
        # （MCP serve 时按 cloud_key 生成短期预签名 cloud_url；本地 web 仍走 local_url）
        if _res_path and get_settings().r2_upload_enabled:
            from pathlib import Path as _Path

            from z_winnow.object_storage.r2 import upload_resources

            try:
                _n_r2 = await upload_resources(
                    _Path(_res_path), state.get("group_id", ""), date
                )
                if _n_r2:
                    logger.info("output_composer: uploaded %d resources to R2", _n_r2)
            except Exception as _r2_exc:
                logger.warning("output_composer: R2 upload skipped — %s", _r2_exc)

        sections = list(json_paths.keys())

        logger.info(
            "output_composer: Phase E complete — %d L3 JSON files written to %s",
            len(json_paths),
            date_output_dir,
        )

        # T-W12-7: L3 写入 — topic_summaries + report_versions
        # 从集中 persist 迁移，现在在阶段 E 完成时立即写入
        topic_count = 0
        version_id: str | None = None  # A008: 显式初始化
        errors: list[str] = []

        try:
            import hashlib

            import aiosqlite as _aiosqlite

            from z_winnow.pipeline.database import init_database_in_conn

            db_path = settings.sqlite_db_path
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

            # P1-6: Serialize SQLite writes across concurrent group pipelines
            from z_winnow.pipeline.db_lock import db_write_lock

            async with await db_write_lock():  # noqa: SIM117
                async with _aiosqlite.connect(db_path) as db:
                    await init_database_in_conn(db)

                    # L3: Insert topic_summaries — deterministic PK for idempotent re-runs
                    _gid = state.get("group_id", "")
                    for topic in unified.get("topics", []):
                        if not isinstance(topic, dict):
                            continue
                        topic_name = topic.get("topic_name", "unknown")
                        topic_id = topic.get(
                            "topic_id",
                            f"tp_{hashlib.md5(topic_name.encode(), usedforsecurity=False).hexdigest()[:8]}",
                        )
                        summary_id = f"sum_{hashlib.md5(f'{date}:{_gid}:{topic_name}'.encode(), usedforsecurity=False).hexdigest()[:12]}"
                        await db.execute(
                            """INSERT OR REPLACE INTO topic_summaries
                               (summary_id, date, topic_name, topic_id, summary_text, context_ids,
                                source_server_ids, confidence, model_used, lifecycle,
                                matched_core_topic_id, background, process, conclusion, description,
                                participants, trend, group_id)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                summary_id,
                                date,
                                topic_name,
                                topic_id,
                                json.dumps(topic, ensure_ascii=False),
                                json.dumps([], ensure_ascii=False),
                                json.dumps(topic.get("source_server_ids", []), ensure_ascii=False),
                                topic.get("weight", 0.5),
                                unified.get("model_used", ""),
                                topic.get("lifecycle", "emerging"),
                                topic.get("matched_core_topic_id"),
                                topic.get("background", ""),
                                topic.get("process", ""),
                                topic.get("conclusion", ""),
                                topic.get("description", ""),
                                json.dumps(topic.get("participants", []), ensure_ascii=False),
                                topic.get("trend", ""),
                                _gid,
                            ),
                        )
                        topic_count += 1

                    await db.commit()

                    # MemOS sync: enqueue items for async processing by sync worker.
                    # Replaces the old fire-and-forget _memos_write_back() which was
                    # unreliable because asyncio.create_task() gets cancelled when the
                    # event loop shuts down. Writing to the sync queue is fast (SQLite
                    # INSERT) and durable — the sync worker processes later.
                    if settings.memos_enabled and _gid:
                        try:
                            from z_winnow.pipeline.database import (
                                enqueue_sync_job,
                            )

                            display_date = date
                            if len(date) == 8:
                                display_date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
                            topics_cube = f"winnow:{_gid}:topics"
                            resources_cube = f"winnow:{_gid}:resources"
                            daily_cube = f"winnow:{_gid}:daily"
                            n_enqueued = 0

                            # A) Topic snapshots → topics cube（含 topic_id 供反馈 search 定位）
                            for topic in unified.get("topics", []):
                                if not isinstance(topic, dict):
                                    continue
                                topic_name = topic.get("topic_name", "unknown")
                                topic_id = topic.get("topic_id", "")
                                lifecycle = topic.get("lifecycle", "emerging")
                                background = topic.get("background", "")
                                process_text = topic.get("process", "")
                                conclusion = topic.get("conclusion", "")
                                trend_text = topic.get("trend", "")
                                participants = topic.get("participants", [])
                                description = topic.get("description", "")

                                parts = ["每日议题快照"]
                                parts.append(f"日期: {display_date}")
                                parts.append(f"议题: {topic_name}")
                                if topic_id:
                                    parts.append(f"议题ID: {topic_id}")
                                parts.append(f"类型: {lifecycle}")
                                # 群成员置顶 + 明确"群成员"身份，区别于被提到的外部人物。
                                # fine MemReader 提取人名时偏向正文(背景/过程里被提到的人),
                                # 群成员若放末尾会被忽略 → 移到开头 + 强标记。
                                parts_str = [
                                    p
                                    for p in participants
                                    if isinstance(p, str) and not p.startswith("wxid_")
                                ]
                                if parts_str:
                                    parts.append(f"参与群成员: {', '.join(parts_str)}")
                                if background:
                                    parts.append(f"背景: {background}")
                                if process_text:
                                    parts.append(f"过程: {process_text}")
                                if conclusion:
                                    parts.append(f"结论: {conclusion}")
                                if trend_text:
                                    parts.append(f"趋势演进: {trend_text}")
                                if description:
                                    parts.append(f"议题概述: {description}")

                                memory_text = "\n".join(parts)
                                # M.8.2: 过滤 wxid_ 残留，防止 MemOS 反馈放大循环
                                memory_text = _scrub_wxid(memory_text)
                                await enqueue_sync_job(
                                    db,
                                    op_type="add_topic",
                                    cube_id=topics_cube,
                                    payload={
                                        "group_id": _gid,
                                        "summary": memory_text,
                                        "source": "pipeline",
                                        "dedupe_key": f"{_gid}:{date}:topic:{topic_name}",
                                    },
                                )
                                n_enqueued += 1

                            # B) Resources → resources cube（含 resource_title 供反馈 search 定位）
                            for res in unified.get("resources", []):
                                if not isinstance(res, dict):
                                    continue
                                res_type = res.get("resource_type", "other")
                                summary = res.get("summary", "")
                                res_title = res.get("resource_title") or summary
                                content = res.get("content", "")
                                memory_text = f"[{display_date}] 资源 ({res_type}): {summary}"
                                if res_title and res_title != summary:
                                    memory_text += f" | 标题: {res_title}"
                                if content:
                                    memory_text += f" | 内容: {content[:200]}"
                                # M.8.2: 过滤 wxid_ 残留
                                memory_text = _scrub_wxid(memory_text)
                                await enqueue_sync_job(
                                    db,
                                    op_type="add_topic",
                                    cube_id=resources_cube,
                                    payload={
                                        "group_id": _gid,
                                        "summary": memory_text,
                                        "source": "pipeline",
                                        "dedupe_key": f"{_gid}:{date}:resource:{res_title[:50]}",
                                    },
                                )
                                n_enqueued += 1

                            # C) Custom tables (registry 驱动) → winnow:{gid}:{kind}，每表一 cube
                            # 记录的 {kind}_id 由 output_composer.inject_custom_record_ids 注入到
                            # unified（JSON 与 memory 共用同一对象），作反馈 target_id 与 search 锚点。
                            from z_winnow.custom_tables import registry as _ct_reg
                            from z_winnow.subagents.output_composer import (
                                _resolve_enabled_table_kinds as _resolve_kinds,
                            )

                            _ct_slot = unified.get("custom_tables") or {}
                            for _kind in _resolve_kinds(ct_config):
                                _tdef = _ct_reg.get_table(_kind)
                                _rkey = _tdef.records_key if _tdef else "items"
                                _kind_slot = _ct_slot.get(_kind)
                                _records = (
                                    _kind_slot.get(_rkey, [])
                                    if isinstance(_kind_slot, dict)
                                    else []
                                )
                                for _rec in _records:
                                    if not isinstance(_rec, dict):
                                        continue
                                    _kid = _rec.get(f"{_kind}_id", "")
                                    _mem = _format_custom_record_memory(_kind, _rec, display_date)
                                    _mem = _scrub_wxid(_mem)
                                    await enqueue_sync_job(
                                        db,
                                        op_type="add_topic",
                                        cube_id=f"winnow:{_gid}:{_kind}",
                                        payload={
                                            "group_id": _gid,
                                            "summary": _mem,
                                            "source": "pipeline",
                                            "dedupe_key": f"{_gid}:{date}:{_kind}:{_kid}",
                                        },
                                    )
                                    n_enqueued += 1

                            # E) 固定内容（overview + 趋势）→ daily cube（report/trend 反馈目标）
                            _overview = unified.get("overview", "")
                            if _overview:
                                _ov = _scrub_wxid(f"[{display_date}] 日报概览: {_overview}")
                                await enqueue_sync_job(
                                    db,
                                    op_type="add_topic",
                                    cube_id=daily_cube,
                                    payload={
                                        "group_id": _gid,
                                        "summary": _ov,
                                        "source": "pipeline",
                                        "dedupe_key": f"{_gid}:{date}:overview",
                                    },
                                )
                                n_enqueued += 1
                            _trend_text = unified.get("trend_analysis", "") or unified.get(
                                "trend_summary", ""
                            )
                            if _trend_text:
                                _tv = _scrub_wxid(f"[{display_date}] 议题趋势: {_trend_text}")
                                await enqueue_sync_job(
                                    db,
                                    op_type="add_topic",
                                    cube_id=daily_cube,
                                    payload={
                                        "group_id": _gid,
                                        "summary": _tv,
                                        "source": "pipeline",
                                        "dedupe_key": f"{_gid}:{date}:trend",
                                    },
                                )
                                n_enqueued += 1

                            # D) 当日无交流：0 议题/资源/自定义/固定内容 → 写一条"无交流"记忆 → daily cube
                            # （0 消息短路后内容为空，n_enqueued 仍为 0 即命中）
                            if n_enqueued == 0:
                                _no_act_text = f"[{display_date}] 当日无交流（群聊无任何消息）"
                                await enqueue_sync_job(
                                    db,
                                    op_type="add_topic",
                                    cube_id=daily_cube,
                                    payload={
                                        "group_id": _gid,
                                        "summary": _no_act_text,
                                        "source": "pipeline",
                                        "dedupe_key": f"{_gid}:{date}:no_activity",
                                    },
                                )
                                n_enqueued += 1
                                logger.info(
                                    "output_composer: 0 content for date=%s — enqueued no-activity memory to daily cube",
                                    date,
                                )

                            if n_enqueued:
                                logger.info(
                                    "output_composer: enqueued %d MemOS sync jobs (topics/resources/daily/custom cubes)",
                                    n_enqueued,
                                )
                        except Exception as _mq_exc:
                            logger.warning(
                                "output_composer: MemOS sync queue write failed — %s",
                                _mq_exc,
                            )
        except Exception as _l3_exc:
            _msg = f"output_composer: L3 topic_summaries write failed — {_l3_exc}"
            logger.warning(_msg)
            errors.append(_msg)

        # Quality check on daily report
        if unified:
            try:
                from z_winnow.graph.error_handling import (
                    check_daily_report_quality,
                )

                quality = check_daily_report_quality(unified)
                if not quality.passed:
                    logger.warning(
                        "output_composer: daily report quality issues (score=%.2f): %s",
                        quality.score,
                        quality.issues,
                    )
            except ImportError:
                logger.debug(
                    "output_composer: error_handling module not available for quality check"
                )

        # MemOS sync queue writes are now done synchronously in the db block above.
        # The sync worker (winnow memos flush) processes the queue asynchronously.

        # T-W12-7: report_versions write (was in persist)
        # T-W12-10: content=None in Phase E (S4). content_changed=False
        try:
            import time as _time

            from z_winnow.pipeline.report_version import create_version

            group_name = state.get("group_name", "unknown")
            if not group_id:
                group_id = state.get("group_id", group_name)
            source = state.get("source", "daily_run")

            start_t = state.get("start_time", 0.0)
            build_duration_s: float | None = None
            if start_t > 0:
                build_duration_s = round(_time.monotonic() - start_t, 2)

            async with _aiosqlite.connect(db_path) as db:
                await init_database_in_conn(db)
                version_id = await create_version(
                    db,
                    report_id=f"{group_id}-{date}",
                    group_id=group_id,
                    date=date,
                    content=None,  # P022: NULL until stage H export
                    source=source,
                    build_duration_s=build_duration_s,
                    content_changed=False,
                    version_number=next_version_number,  # M4: 与 v{n}/ 目录对齐
                )

            logger.info(
                "output_composer: version record created: %s (source=%s, content=NULL, duration=%s)",
                version_id,
                source,
                f"{build_duration_s:.1f}s" if build_duration_s is not None else "N/A",
            )
        except ImportError:
            logger.debug("output_composer: report_version module not available")
        except Exception as _rv_exc:
            logger.warning("output_composer: version write failed (non-blocking) — %s", _rv_exc)

        return {
            "final_report": "",  # S4: No Markdown in main flow
            "report_sections": sections,
            "topic_summary_count": topic_count,
            "memory_file_path": f"memory/topic_tracker_{date}.md",
            "current_phase": "composed",
            "errors": errors,
        }
    except Exception as e:  # pylint: disable=broad-except
        logger.error("node_output_composer failed: %s", e, exc_info=True)
        return {
            "final_report": "",
            "report_sections": [],
            "topic_summary_count": 0,
            "memory_file_path": "",
            "current_phase": "composed_error",
            "errors": [{"node": "output_composer", "error": str(e)}],
        }


# ============================================================
# Node 7: write_reports (Layer 4 output + routing) — T-W7-13
# ============================================================
# P016: write_reports is a single-point insertion node that handles
# Layer 4 daily report writing and delegates routing to conditional edges.
# T-W12-7: persist removed — L1/L2/L3 writes distributed to per-stage nodes.


async def node_write_reports(state: OverallState) -> dict[str, Any]:
    """Write daily Markdown report to disk and set routing via conditional edge.

    Layer 4 output node — reads ``state.final_report`` and writes daily
    to ``reports/daily/{date}.md`` if ``"daily"`` in ``report_types``.

    Input:
        state.final_report, state.date, state.report_types

    Output:
        state.report_file_path: absolute path to the written daily report
        state.current_phase: updated to "reports_written"
    """
    try:
        final_report = state.get("final_report", "")
        date = state.get("date", "")
        report_types = state.get("report_types", [])

        report_path = ""
        # T-W12-5: Use Settings for report dir (S7 配置单源)
        from z_winnow.config.settings import get_settings

        report_dir = get_settings().reports_dir

        if "daily" in report_types and final_report:
            try:
                from z_winnow.outputs.report_writer import (
                    write_daily_report,
                )

                report_path = write_daily_report(
                    markdown=final_report,
                    date=date,
                    output_dir=report_dir,
                    group_id=state.get("group_id") or None,
                )

                logger.info(
                    "node_write_reports: daily report written to %s (%d chars)",
                    report_path,
                    len(final_report),
                )
            except ImportError:
                logger.warning(
                    "node_write_reports: report_writer module not available, "
                    "writing directly to %s",
                    report_dir,
                )
                # Fallback: direct file write
                from pathlib import Path

                out_dir = Path(report_dir) / "daily"
                out_dir.mkdir(parents=True, exist_ok=True)
                filepath = out_dir / f"{date}.md"
                filepath.write_text(final_report, encoding="utf-8")
                report_path = str(filepath.resolve())
        else:
            logger.info(
                "node_write_reports: 'daily' not in report_types=%s or no final_report, "
                "skipping daily write",
                report_types,
            )

        return {
            "report_file_path": report_path,
            "current_phase": "reports_written",
        }
    except Exception as e:
        logger.error("node_write_reports failed: %s", e, exc_info=True)
        return {
            "report_file_path": "",
            "current_phase": "report_write_error",
            "errors": [{"node": "write_reports", "error": str(e)}],
        }


# ============================================================
# Graph construction
# ============================================================


def build_graph(checkpointer: Any = None) -> StateGraph:
    """Build the complete winnow LangGraph workflow — single-agent unified design.

    Graph structure (Wave 12: single-agent unified_reporter):
        START → data_fetch → content_enrich → orchestrator →
            unified_reporter (single LLM call: daily + resources + engineering
                              + topic_tracking + trend_summary + lifecycle) →
        output_composer (L3 JSON + L3 DB: topic_summaries + report_versions) → END

    Feishu upload is NOT a graph node — it is invoked independently from the web
    UI (POST /reports/{id}/feishu → report_service → pipeline/feishu/uploader via
    lark-cli). The legacy graph feishu nodes were removed with outputs/feishu.py.

    T-W12-7: S1 即时固化 — persist 拆分为各阶段各自写入:
      - data_fetch 末尾: L1 写入 (raw_messages, 不可变)
      - content_enrich 末尾: L2 写入 (parsed_contexts, 不可变)
      - output_composer 末尾: L3 写入 (topic_summaries + report_versions, 允许增量)
      集中 persist 节点已删除。

    T-W12-10: S4 Design Standard — Markdown rendering deferred to Phase H.
    write_reports removed from main flow. Main flow writes L3 JSON +
    report_versions.content=NULL. export_markdown() triggers Phase H:
    L3 JSON → Jinja2 → .md → update content.

    T-W12-9: S2 Design Standard — single agent, single LLM call.
    No Send API fan-out, no parallel agents, no merge node.
    unified_reporter produces: daily report + resources + engineering +
    topic_tracking + trend_summary + lifecycle classification.

    Node responsibilities:
      - data_fetch: Fetch messages + persist L1 (raw_messages)
      - content_enrich: Enrich messages + persist L2 (parsed_contexts)
      - orchestrator: Validate report_types, load memory context from MemOS
      - unified_reporter: Single LLM call for ALL report sections including
        topic_tracking, trend_summary, and lifecycle (S2 standard)
      - output_composer: Write L3 JSON files + L3 DB (topic_summaries + report_versions)

    Returns:
        StateGraph: Configured but uncompiled graph. Call .compile()
                   to get the compiled Runnable.
    """
    builder = StateGraph(OverallState)

    # P0-4: Wrap nodes with with_progress for real-time SSE tracking
    # P1-5: Wrap external-API nodes with with_retry for transient error recovery
    from z_winnow.graph.error_handling import with_retry as _with_retry
    from z_winnow.graph.progress import with_progress

    builder.add_node(
        "data_fetch",
        with_progress(_with_retry(max_retries=3)(node_data_fetch), "data_fetch", 0.15),
    )
    builder.add_node("content_enrich", with_progress(node_content_enrich, "content_enrich", 0.15))
    builder.add_node(
        "orchestrator",
        with_progress(_with_retry(max_retries=2)(node_orchestrator), "orchestrator", 0.10),
    )
    builder.add_node(
        "unified_reporter",
        with_progress(_with_retry(max_retries=2)(node_unified_reporter), "unified_reporter", 0.35),
    )
    builder.add_node(
        "output_composer", with_progress(node_output_composer, "output_composer", 0.25)
    )
    # T-W12-7: persist node REMOVED — writes distributed to per-stage nodes (S1)
    # T-W12-10: write_reports NOT in main flow — S4 defers Markdown to Phase H
    # The node function is kept for reference but not registered in the graph.
    # Main flow: START → data_fetch → content_enrich → orchestrator → unified_reporter
    builder.add_edge(START, "data_fetch")
    builder.add_edge("data_fetch", "content_enrich")
    builder.add_edge("content_enrich", "orchestrator")

    # T-W12-9: S2 — orchestrator → unified_reporter direct, no intermediate nodes
    builder.add_edge("orchestrator", "unified_reporter")
    builder.add_edge("unified_reporter", "output_composer")

    # T-W12-7: output_composer → END. Main pipeline terminates here.
    # Feishu upload is invoked independently from the web UI, not via graph edges.
    # P030: Static Graph Compile Verification — verify with compile().
    builder.add_edge("output_composer", END)

    return builder


# ============================================================
# T-W12-10: export_markdown — Phase H manual trigger
# ============================================================
# P022: Storage/Formatting Layer Separation — this function is the
# formatting layer entry point. It reads L3 JSON (storage layer),
# renders Markdown via Jinja2, writes .md file, and updates
# report_versions.content in SQLite.


async def export_markdown(date: str, group_id: str) -> Path:
    """Phase H: Export Markdown from L3 JSON via Jinja2 rendering.

    T-W12-10: Manual trigger entry point. NOT called in main flow (S4).
    Reads 4 L3 JSON files from data/processed/{date}, renders Markdown
    via Jinja2 template, writes .md file, and updates report_versions.content.

    P022: Formatting layer only — no storage mutation beyond content update.

    Args:
        date: Date string in YYYYMMDD format.
        group_id: Group identifier for report_versions lookup.

    Returns:
        Path to the written Markdown file.

    Raises:
        FileNotFoundError: If L3 JSON files not found for the given date.
        RuntimeError: If Markdown rendering or DB update fails.
    """
    # T-W12-5: Use Settings for paths (S7 配置单源)
    from z_winnow.config.settings import get_settings
    from z_winnow.subagents.output_composer import render_markdown

    _settings = get_settings()
    report_id = f"{group_id}-{date}"

    # M4: 解析 active 版本号（回滚后导出 active 而非 latest）；无 active 回退 latest
    target_version_number: int | None = None
    try:
        import aiosqlite as _peek_aio

        from z_winnow.pipeline.database import init_database_in_conn as _init_peek
        from z_winnow.pipeline.report_version import (
            get_active_version as _peek_active,
        )
        from z_winnow.pipeline.report_version import (
            get_latest_version as _peek_latest,
        )

        async with _peek_aio.connect(_settings.sqlite_db_path) as _pdb:
            await _init_peek(_pdb)
            _tv = await _peek_active(_pdb, report_id) or await _peek_latest(_pdb, report_id)
            if _tv is not None:
                target_version_number = _tv.version_number
    except Exception as _peek_exc:
        logger.debug("export_markdown: active version peek failed — %s", _peek_exc)

    # Step 1: Read L3 JSON → Jinja2 → .md file
    from z_winnow.pipeline.l3_paths import resolve_l3_dir

    json_dir = resolve_l3_dir(
        _settings.layer3_output_dir, group_id, date, version_number=target_version_number
    )
    if not json_dir.exists():
        raise FileNotFoundError(
            f"L3 JSON directory not found: {json_dir}. "
            f"Run the main pipeline first to generate L3 JSON files."
        )

    md_path = render_markdown(json_dir=json_dir)
    logger.info(
        "export_markdown: rendered Markdown to %s (%d chars)",
        md_path,
        md_path.stat().st_size,
    )

    # Step 2: Update report_versions.content
    content = md_path.read_text(encoding="utf-8")
    db_path = _settings.sqlite_db_path

    try:
        import aiosqlite

        from z_winnow.pipeline.database import init_database_in_conn
        from z_winnow.pipeline.report_version import (
            get_active_version,
            get_latest_version,
            update_version_content,
        )

        report_id = f"{group_id}-{date}"

        async with aiosqlite.connect(db_path) as db:
            await init_database_in_conn(db)
            # M4: 写 active 版本 content（回滚后导出/更新的是 active，非 latest）
            target = await get_active_version(db, report_id) or await get_latest_version(db, report_id)
            if target is not None:
                updated = await update_version_content(db, target.version_id, content)
                if updated:
                    logger.info(
                        "export_markdown: updated %s content (%d chars)",
                        target.version_id,
                        len(content),
                    )
                else:
                    logger.warning(
                        "export_markdown: failed to update %s",
                        target.version_id,
                    )
            else:
                logger.warning(
                    "export_markdown: no version found for report_id=%s. "
                    "Content written to file but not stored in DB.",
                    report_id,
                )
    except ImportError:
        logger.warning(
            "export_markdown: report_version module not available. "
            "Markdown written to %s but content not stored in DB.",
            md_path,
        )
    except Exception as exc:
        logger.warning("export_markdown: DB update failed (non-blocking): %s", exc)

    return md_path


# ============================================================
# T-W12-13: incremental_reprocess — feedback-driven incremental correction
# ============================================================
# S5 Design Standard: feedback-driven corrections only reprocess affected
# L3 records, not the entire pipeline.
#
# Flow:
#   feedback_events(unconsumed) -> L3 source_server_ids -> L2 parsed_contexts
#   -> incremental prompt (L2 context + correction + MemOS history)
#   -> agent correction -> partial update L3 JSON + topic_summaries
#   -> report_versions(source=incremental_fix) -> MemOS feedback memory
#   -> mark consumed_at
#
# Constraints:
#   - Does NOT modify L1/L2
#   - Does NOT touch other L3 records
#   - feedback state machine: unconsumed -> consumed (success) /
#     stays unconsumed (failure) -> rollback
#
# P016: Lazy import — all new dependencies imported inside function body.
# L014: asyncio.gather exception handling — single record failure does not
#   block the overall flow.


async def incremental_reprocess(
    group_id: str,
    date: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Incremental reprocessing — feedback-driven correction of affected L3 records.

    T-W12-13: S5 Design Standard. Only reprocesses L3 records associated with
    unconsumed feedback events. Does NOT run the full pipeline.

    Per-record flow:
      1. Read unconsumed feedback_events for group_id + date
      2. For each feedback: map target_id -> L3 topic_summaries -> source_server_ids
      3. Query L2 parsed_contexts by source_server_ids
      4. Build incremental prompt (L2 context + original output + correction)
      5. Call LLM agent for correction (or mock if no API key)
      6. Update topic_summaries.summary_text (L3 partial update)
      7. Create report_versions(source=incremental_fix)
      8. Enqueue MemOS feedback memory
      9. Mark feedback consumed_at

    L014: Single record failure does not block other records.
    A002: Actual DB writes (execute + commit), not just in-memory.

    Args:
        group_id: Group identifier.
        date: Date string YYYYMMDD.
        dry_run: If True, simulate corrections without writing to DB.

    Returns:
        Dict with keys:
          - processed: number of feedback events processed
          - succeeded: number of successful corrections
          - failed: number of failed corrections
          - details: list of per-feedback processing results
          - errors: list of error messages
    """
    import aiosqlite

    from z_winnow.config.settings import get_settings
    from z_winnow.pipeline.database import (
        get_l2_contexts_by_server_ids,
        get_topics_by_date,
        get_unconsumed_feedback,
        init_database_in_conn,
        mark_feedback_consumed,
        update_topic_summary_text,
    )

    settings = get_settings()
    db_path = settings.sqlite_db_path

    # A008: defensive initialization
    result: dict[str, Any] = {
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "details": [],
        "errors": [],
    }

    try:
        # P016: Lazy import for incremental prompt module
        from z_winnow.subagents.incremental_prompt import (
            IncrementalPromptInput,
            build_incremental_prompt,
            parse_incremental_output,
        )
    except ImportError:
        logger.error("incremental_reprocess: incremental_prompt module not available")
        result["errors"].append("incremental_prompt module import failed")
        return result

    async with aiosqlite.connect(db_path) as db:
        await init_database_in_conn(db)
        db.row_factory = aiosqlite.Row

        # Step 1: Read unconsumed feedback events
        feedback_events = await get_unconsumed_feedback(db, group_id, date)
        if not feedback_events:
            logger.info(
                "incremental_reprocess: no unconsumed feedback for group=%s date=%s",
                group_id,
                date,
            )
            return result

        logger.info(
            "incremental_reprocess: found %d unconsumed feedback events for group=%s date=%s",
            len(feedback_events),
            group_id,
            date,
        )

        # Step 2: Get all L3 topic_summaries for this date (for target lookup)
        all_topics = await get_topics_by_date(db, date, group_id=group_id or None)
        # Build lookup: summary_id -> topic dict
        topic_by_id: dict[str, dict[str, Any]] = {t["summary_id"]: t for t in all_topics}

        # Step 3: Process each feedback event
        for fb in feedback_events:
            result["processed"] += 1
            fb_id = fb.get("feedback_id", "")
            target_type = fb.get("target_type", "")
            target_id = fb.get("target_id", "")

            try:
                # Find the target L3 record
                topic: dict[str, Any] | None = topic_by_id.get(target_id) if target_id else None

                if topic is None:
                    # Try to find by topic_name match if target_id is not a summary_id
                    for t in all_topics:
                        if t.get("topic_name") == target_id:
                            topic = t
                            break

                if topic is None:
                    logger.warning(
                        "incremental_reprocess: target_id=%s not found in L3 topics",
                        target_id,
                    )
                    result["failed"] += 1
                    result["details"].append(
                        {
                            "feedback_id": fb_id,
                            "status": "target_not_found",
                            "error": f"target_id={target_id} not found",
                        }
                    )
                    # L014: Continue processing other feedback events
                    continue

                # Step 3a: Extract source_server_ids from topic
                import json as _json

                raw_sids = topic.get("source_server_ids", "[]")
                server_ids: list[str] = []
                if isinstance(raw_sids, str):
                    try:
                        server_ids = _json.loads(raw_sids)
                    except (_json.JSONDecodeError, TypeError):
                        server_ids = []
                elif isinstance(raw_sids, list):
                    server_ids = raw_sids

                # L005: Deduplicate while preserving order
                server_ids = list(dict.fromkeys(server_ids))

                # Step 3b: Query L2 contexts by server_ids
                l2_contexts = await get_l2_contexts_by_server_ids(db, server_ids)
                l2_text = "\n".join(
                    c.get("context_text", "") for c in l2_contexts if c.get("context_text")
                )

                # Step 3c: Build incremental prompt
                correction_text: str = fb.get("corrected_text") or fb.get("correction_note") or ""
                if not correction_text:
                    correction_text = fb.get("tags", "") or "(correction via tags only)"

                original_output: str = topic.get("summary_text", "")

                prompt_input = IncrementalPromptInput(
                    target_type=target_type,
                    target_id=target_id,
                    l2_context=l2_text,
                    original_output=original_output,
                    correction=correction_text,
                    memory_context=None,  # P009: optional, no MemOS context in this path
                    feedback_id=fb_id,
                    group_id=group_id,
                    date=date,
                )

                _system_prompt, user_prompt = build_incremental_prompt(prompt_input)

                # Step 3d: Call LLM for correction (or mock)
                # T-W12-8: Use Settings.use_mock_llm instead of _USE_REAL_LLM
                from z_winnow.config.settings import get_settings as _get_settings

                _use_real_llm = not _get_settings().use_mock_llm
                corrected_text: str = ""
                if _use_real_llm:
                    try:
                        # P016: Lazy import LLM client
                        corrected_text = await _call_llm_for_correction(
                            system_prompt=_system_prompt,
                            user_prompt=user_prompt,
                        )
                        parsed = parse_incremental_output(corrected_text)
                        if parsed.success:
                            corrected_text = parsed.corrected_item
                        else:
                            logger.warning(
                                "incremental_reprocess: parse failed for %s — %s",
                                fb_id,
                                parsed.error,
                            )
                            corrected_text = original_output  # Keep original
                    except Exception as exc:
                        logger.warning(
                            "incremental_reprocess: LLM call failed for %s — %s",
                            fb_id,
                            exc,
                        )
                        corrected_text = _apply_simple_correction(
                            original=original_output,
                            correction=correction_text,
                        )
                else:
                    # Mock mode: apply simple text replacement
                    corrected_text = _apply_simple_correction(
                        original=original_output,
                        correction=correction_text,
                    )

                # Step 3e: Update L3 record (A002: actual DB write)
                if not dry_run:
                    summary_id = topic.get("summary_id", "")
                    updated = await update_topic_summary_text(db, summary_id, corrected_text)
                    if not updated:
                        logger.warning(
                            "incremental_reprocess: failed to update summary_id=%s",
                            summary_id,
                        )
                        result["failed"] += 1
                        result["details"].append(
                            {
                                "feedback_id": fb_id,
                                "status": "update_failed",
                            }
                        )
                        continue

                    # Step 3f: Create report_versions (source=incremental_fix)
                    try:
                        from z_winnow.pipeline.report_version import create_version

                        report_id = f"{group_id}-{date}"
                        await create_version(
                            db,
                            report_id=report_id,
                            group_id=group_id,
                            date=date,
                            content=None,
                            source="incremental_fix",  # T-W12-1 corrected enum value
                            build_duration_s=None,
                            content_changed=True,
                        )
                    except Exception as exc:
                        logger.warning(
                            "incremental_reprocess: report_version write failed — %s",
                            exc,
                        )

                    # Step 3g: Mark feedback consumed
                    consumed = await mark_feedback_consumed(db, fb_id)
                    if not consumed:
                        logger.warning(
                            "incremental_reprocess: failed to mark consumed for %s",
                            fb_id,
                        )

                    # Step 3h: Enqueue MemOS feedback memory (best-effort, P014)
                    try:
                        from z_winnow.memory.feedback_sync import (
                            enqueue_feedback_sync,
                        )

                        await enqueue_feedback_sync(db, fb_id)
                    except Exception as exc:
                        logger.debug(
                            "incremental_reprocess: MemOS sync enqueue failed (non-blocking) — %s",
                            exc,
                        )

                result["succeeded"] += 1
                result["details"].append(
                    {
                        "feedback_id": fb_id,
                        "target_id": target_id,
                        "status": "corrected",
                        "dry_run": dry_run,
                    }
                )

            except Exception as exc:
                # L014: Single record failure does not block overall flow
                logger.warning(
                    "incremental_reprocess: failed to process feedback %s — %s",
                    fb_id,
                    exc,
                )
                result["failed"] += 1
                result["details"].append(
                    {
                        "feedback_id": fb_id,
                        "status": "error",
                        "error": str(exc),
                    }
                )
                result["errors"].append(f"{fb_id}: {exc}")

    logger.info(
        "incremental_reprocess: group=%s date=%s — processed=%d succeeded=%d failed=%d",
        group_id,
        date,
        result["processed"],
        result["succeeded"],
        result["failed"],
    )

    return result


# ============================================================
# T-W12-13: LLM call helper for incremental correction
# ============================================================
# P016: Lazy import — ChatModel imported only when needed.


async def _call_llm_for_correction(
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Call LLM for incremental correction.

    P016: Lazy import of ChatModel. Falls back to simple correction
    if LLM is unavailable.

    Args:
        system_prompt: System prompt for correction.
        user_prompt: User prompt with context and correction.

    Returns:
        Raw LLM response string.
    """
    try:
        from z_winnow.config.settings import get_settings

        settings = get_settings()

        # Use the unified_reporter model for corrections
        model_name = settings.effective_unified_reporter_model

        if settings.anthropic_api_key:
            import anthropic

            client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                base_url=settings.anthropic_base_url or None,
            )
            response = await client.messages.create(
                model=model_name,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response.content[0].text
        elif settings.deepseek_api_key:
            import httpx

            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{settings.deepseek_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                    json={
                        "model": settings.deepseek_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "max_tokens": 4096,
                    },
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        else:
            logger.warning("_call_llm_for_correction: no API key configured, using mock")
            return '{"corrected_item": "mock correction applied", "mem_feedback_record": "mock feedback"}'

    except Exception as exc:
        logger.warning("_call_llm_for_correction: LLM call failed — %s", exc)
        return '{"corrected_item": "", "mem_feedback_record": "", "error": "LLM unavailable"}'


def _apply_simple_correction(
    original: str,
    correction: str,
) -> str:
    """Apply a simple mock correction when LLM is unavailable.

    For testing purposes: appends the correction note to the original content.

    Args:
        original: Original L3 content string.
        correction: Correction text from feedback.

    Returns:
        Modified content string.
    """
    if not original:
        return correction

    # Simple strategy: append correction as a note
    separator = "\n\n---\n[Incremental correction applied]:\n"
    return original + separator + correction


# ============================================================
# Pre-compiled graph (lazy)
# ============================================================

_compiled_graph = None


def get_graph(checkpointer: Any = None):
    """Get or create a compiled graph instance (singleton pattern).

    Args:
        checkpointer: LangGraph checkpointer (MemorySaver for test,
                      SqliteSaver for production).

    Returns:
        CompiledStateGraph: The compiled LangGraph runnable.
    """
    global _compiled_graph
    if _compiled_graph is None:
        graph = build_graph(checkpointer=checkpointer)
        _compiled_graph = graph.compile(checkpointer=checkpointer)
    return _compiled_graph
