"""LangGraph State schema — OverallState and Message TypedDict.

Defines the global state shared across nodes in the winnow LangGraph graphs.

  * ``OverallState`` — Used by the builder.py graph (linear pipeline, Wave 12).
    ``unified_reporter`` makes a single LLM call producing all report sections
    (overview, topics, resources, engineering_issues).
    ``errors`` uses Annotated[list, operator.add] reducer.

  * ``Message`` — Single chat message, aligned with CipherTalk API format.

Architecture reference: docs/architecture-detail.md §1.1-1.2
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class Message(TypedDict):
    """Single chat message, aligned with CipherTalk API format.

    DEPRECATED (Wave 7): ChatLab format. Verified 2026-05-02: grep -rn "chatlab" src/ --include="*.py"
    confirms zero active import/call paths. Comment retained per A002 protocol. Status: resolved.

    Attributes:
        server_id: WeChat serverId - globally unique (PRIMARY KEY).
        sender: Sender wxid.
        account_name: Display name.
        group_nickname: Group nickname.
        timestamp: Unix timestamp in milliseconds.
        msg_type: Message type: text | image | voice | video | file | link | system.
        content: Message text content.
        media_url: Media resource URL (may be empty).
        reply_to: Reply target serverId (may be empty).
    """

    server_id: str
    """微信 serverId，全局唯一 (PRIMARY KEY)."""

    sender: str
    """发送者 wxid."""

    account_name: str
    """显示名."""

    group_nickname: str
    """群昵称."""

    timestamp: int
    """Unix 时间戳（毫秒）."""

    msg_type: str
    """消息类型: text | image | voice | video | file | link | system."""

    content: str
    """消息文本内容."""

    media_url: str
    """媒体资源 URL（可空）."""

    reply_to: str
    """回复目标 serverId（可空）."""


# ============================================================
# OverallState — builder.py graph (linear pipeline, Wave 9)
# ============================================================
# ``unified_reporter`` replaces the former 3-subagent parallel dispatch
# with a single LLM call. ``errors`` uses Annotated[list, operator.add]
# reducer for accumulator semantics.


class OverallState(TypedDict):
    """LangGraph global State — linear pipeline graph (Wave 9: unified_reporter single LLM call).

    Fields are organized by lifecycle phase:

    Phase 0 — Input parameters:
        group_name, date, report_types, api_base_url, api_token

    Phase 1 — Data fetched by data_fetch node:
        messages

    Phase 2 — Unified report generation (single LLM call, all sections):
        unified_report

    Phase 3 — Output composition:
        final_report, report_sections

    Phase 4 — Persistence:
        raw_message_count, context_count, topic_summary_count, memory_file_path

    Global — Error collection (Annotated with operator.add reducer):
        errors
    """

    # ============ Phase 0: 输入参数 ============
    group_name: str
    """群聊名称."""

    group_id: str
    """groups 表 PK，由 node_data_fetch 从 group_name 解析."""

    run_id: str
    """Pipeline run UUID, used by with_progress wrappers for SSE tracking."""

    _progress: float
    """累计进度 0.0–1.0，with_progress 跨节点累加（不声明则 LangGraph 不透传，每节点重置）."""

    date: str
    """目标日期，格式 YYYYMMDD."""

    report_types: list[str]
    """要生成的报告类型: ["daily"]。合并后不再支持单独的资源/工程类型."""

    api_base_url: str
    """CipherTalk API 基础 URL，默认 http://127.0.0.1:5031."""

    api_token: str
    """CipherTalk API 认证 token."""

    # T-W10-D-b: Version tracking fields for persist node DB dual-write
    start_time: float
    """Graph 开始执行时间 (time.monotonic())，用于计算 build_duration_s."""

    source: str
    """报告来源: "daily_run" (首次生成) | "regenerate" (重生成) | "manual" (手动触发). 用于 persist 节点写入 report_versions.source."""

    # ============ Phase 1: 数据获取 ============
    messages: list[dict[str, Any]]
    """已获取并清洗的消息列表."""

    # P009: historical_topics removed (T-W8-4) — topic tracking is now
    # handled by dedicated topic_tracker sub-agent (W8-C).

    # ============ Phase 1.5: 内容增强 (T-V4: content_enrich 节点产出) ============
    image_descriptions: dict[str, str]
    """图片 AI 描述字典，{server_id: description_text}.

    由 content_enrich 节点通过 Vision API (T-V1) 生成。
    content_enrich 跳过时为空字典。
    """

    link_previews: dict[str, dict[str, str]]
    """链接预览字典，{server_id: {title, description, site_name, url, ...}}.

    由 content_enrich 节点通过 HTTP 预取 (T-V3) 生成。
    content_enrich 跳过时为空字典。
    """

    image_analysis_failed: bool
    """P1-2: 图片分析是否失败（超时或异常）。

    True 时表示 L2 的 image_descriptions 不完整，图片消息仍为 [图片] 占位符。
    下游节点可据此调整策略（如标记报告质量、触发重试）。
    """

    member_map: dict[str, str]
    """wxid → display_name 映射，由 data_fetch 构建，供 ChatContextBuilder 兜底使用。"""

    content_enrich_enabled: bool
    """是否启用内容增强（从 WINNOW_ENABLE_ENRICH 环境变量读取）.

    默认 True。content_enrich 节点依据此字段决定是否执行增强管线。
    """

    chat_context_markdown: str
    """Pre-formatted markdown chat context for LLM consumption.

    由 content_enrich 节点从 L2 消息生成，同时写入 data/tmp/ 临时文件。
    orchestrator 和 unified_reporter 直接消费此字段，不再各自格式化消息。
    content_enrich 跳过时为空字符串。
    """

    # ============ Phase 2: 统一日报生成 (unified_reporter 单次 LLM 调用) ============
    unified_report: dict[str, Any]
    """统一日报 agent 产出 — 单次 LLM 调用生成所有 section.

    包含: overview, topics (统一议题列表), trend_analysis, trend_summary, highlights (日报),
    resources, resource_count_by_type (资源), engineering_issues, group_summary (工程).
    topics 是唯一的议题数据来源，含 lifecycle (user_defined|sustained|emerging)、
    background、process、conclusion（因果链三段）、description、trend、participants 字段。
    """

    # CT-2: custom_tables carries the resolved per-group custom table config
    # (from node_unified_reporter) through to node_output_composer.
    # MUST be in state — otherwise LangGraph drops it (TypedDict enforcement),
    # output_composer falls back to ["engineering"], and world_models etc. are
    # silently skipped even when the LLM produced content for them.
    custom_tables: dict[str, Any] | None
    """CT-2: 解析后的群组自定义表配置 {kind: {enabled, config}}.

    由 node_unified_reporter 从 groups 表解析并写入 state，供 node_output_composer
    在 compose_json 时判断要写哪些 {kind}.json 文件。None 表示群组无自定义表配置，
    output_composer 回退到向后兼容默认值 ["engineering"]。
    """

    # T-W12-11: classified_topics field removed — lifecycle classification
    # is now integrated into unified_reporter (T-W12-9 merge).
    # T-W13: topic_reports + topic_tracker_output removed — topic tracking
    # merged into unified topics[] with lifecycle field.

    # T-W10-E-d: Memory context loaded by orchestrator node from MemOS
    memory_context: dict[str, Any] | None
    """orchestrator 节点填充的"上下文记忆包".

    结构: {"historical_topics": [...], "user_defined_topics": [...],
           "prior_corrections": [...],
           "memory_cube_status": "ok" | "degraded" | "disabled"}

    由 orchestrator 节点调用 MemOS search + core_topics 查询加载。
    user_defined_topics: 从 core_topics 表查询的活跃用户预设议题。
    None 表示 MemOS 不可用 (MEMOS_ENABLED=false)。
    memory_cube_status="degraded" 表示查询超时或部分成功 — 主流程继续。
    """

    # ============ Phase 4: 输出合成 ============
    final_report: str
    """最终 Markdown 报告全文."""

    report_sections: list[str]
    """各章节内容列表."""

    # ============ Phase 5: 持久化 ============
    raw_message_count: int
    """入库的原始消息数量."""

    context_count: int
    """入库的上下文块数量."""

    topic_summary_count: int
    """入库的议题总结数量."""

    memory_file_path: str
    """更新后的记忆文件路径."""

    report_file_path: str
    """Layer 4 输出文件路径 — 由 node_write_reports 写入 (T-W7-13)."""

    # ============ W10-D: Regenerate mode (P009: cascade through state) ============
    regenerate: bool
    """P009: regenerate 模式标志. True = 跳过 data_fetch/content_enrich，复用缓存数据重生成."""

    prior_corrections: list[dict[str, Any]]
    """管理员反馈修正列表. regenerate 时注入 unified_reporter prompt 实现增量修正."""

    report_id: str
    """报告 ID，格式 "{group_id}-{date}". 用于 report_versions 表关联."""

    # ============ 全局控制 ============
    errors: Annotated[list[str], operator.add]
    """错误收集列表。各节点可追加错误，不阻断整体流程."""

    current_phase: str
    """当前执行阶段标识."""
