"""unified_reporter I/O models — single LLM call produces all report sections.

Replaces the 3 separate subagent output models (DailyReporterOutput,
ResourceExtractorOutput, EngineeringAnalyzerOutput) with one unified model.
T-W12-9: Also absorbs topic_tracker output and lifecycle classification.

Topic Unification: topic_sections + topic_tracking merged into single
`topics[]` list. lifecycle values: user_defined | sustained | emerging.
New fields per topic: background, process, conclusion (因果链三段), description, trend, participants.

W16-A1: Topic / Resource / EngineeringIssue promoted to strongly-typed Pydantic
models — the SINGLE source of truth for the unified_reporter → composer →
renderer → Jinja2 → 飞书 → web response_model → 前端 full-chain schema (A026).
All fields default to root out None (B7 root cause: daily_report.j2:27
`t.trend|length` / `t.trend[:80]` raised TypeError on None). The three L3-record
models use ``extra='allow'`` (P045 dual-schema) so the downstream composer
(W16-A2 ``_dict_to_composed`` → ``Topic(**d)``) tolerates legacy L3 JSON
on-disk fields (topic_sections, legacy_old_field, ...) without raising
ValidationError. UnifiedReporterOutput itself keeps ``extra='forbid'`` to
reject legacy top-level fields (topic_sections / topic_tracking).

B1 global resolution (board r1-W16-A1-0 resolved): Topic deliberately has NO
``sections`` field — B1 is solved by the frontend reading flat fields (W16-A4),
not by a sections sub-model (which would be an A026 zombie).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Topic(BaseModel):
    """Single unified topic entry — the ONLY topic field definition (A026).

    ``extra='allow'`` (P045) absorbs legacy L3 on-disk fields
    (``topic_sections``, ``legacy_old_field``, ...) so W16-A2
    ``_dict_to_composed`` ``Topic(**d)`` tolerates old JSON without
    ValidationError. All fields default to root out None (B7).

    B1 global resolution (board r1-W16-A1-0 resolved): deliberately NO
    ``sections`` field — B1 is solved by the frontend reading flat fields.
    """

    model_config = ConfigDict(extra="allow")

    topic_id: str = Field(default="", description="唯一标识，由系统注入，禁止自行编造")
    topic_name: str = Field(default="", description="议题名称")
    lifecycle: str = Field(default="", description="user_defined|sustained|emerging")
    status: str = Field(default="", description="active|discussion|resolved|archived")
    # 议题因果链三段（与前端 reports.html「背景/过程/结论」三槽一一对应）
    background: str = Field(default="", description="议题背景：为什么讨论、上下文与起因")
    process: str = Field(default="", description="讨论过程：讨论了什么、关键观点与推进")
    conclusion: str = Field(
        default="", description="最终结论/结论性判断（仅当日，不再包含背景与过程）"
    )
    description: str = Field(default="", description="议题描述与汇入边界")
    trend: str = Field(default="", description="渐进式语义描述（非短词标签）")
    participants: list[str] = Field(
        default_factory=list, description="当天参与讨论的群成员昵称列表"
    )
    weight: float = Field(default=0.0, description="活跃度权重 0.0-1.0")
    first_seen: str = Field(default="", description="首次出现日期 YYYY-MM-DD")
    last_seen: str = Field(default="", description="最近出现日期 YYYY-MM-DD")
    source_server_ids: list[str] = Field(
        default_factory=list, description="关联消息的 serverId 列表（必填）"
    )


class Resource(BaseModel):
    """Single resource entry — the ONLY resource field definition (A026).

    ``extra='allow'`` (P045) tolerates legacy L3 on-disk fields. All fields
    default to root out None (B7).
    """

    model_config = ConfigDict(extra="allow")

    time_range: str = Field(default="", description="时间段 HH:MM - HH:MM")
    resource_type: str = Field(
        default="",
        description="link|paper|article|repo|site|doc|image|file|other",
    )
    resource_title: str = Field(default="", description="资源标题/名称")
    summary: str = Field(default="", description="资源作用描述（非复制链接标题）")
    content: str = Field(default="", description="资源链接，无链接填「手动上传」")
    shared_by: str = Field(default="", description="分享人昵称")
    source_server_ids: list[str] = Field(
        default_factory=list, description="关联消息的 serverId 列表（必填）"
    )
    cloud_key: str = Field(
        default="",
        description=(
            "R2 对象 key（私有桶；attachments/{gid}/{date}/{fn}）。"
            "MCP serve 时按此生成短期预签名 cloud_url；本地 web 走 local_url。"
        ),
    )


class EngineeringIssue(BaseModel):
    """Single engineering issue entry — the ONLY issue field definition (A026).

    ``extra='allow'`` (P045) tolerates legacy L3 on-disk fields. All fields
    default to root out None (B7).
    """

    model_config = ConfigDict(extra="allow")

    datetime: str = Field(default="", description="日期时间")
    group: str = Field(default="", description="所属工程分组")
    description: str = Field(default="", description="问题描述")
    solution: str = Field(default="", description="解决方案")
    status: str = Field(
        default="", description="✅ 已解决 | 📝 已知问题 | 🔄 方案待验证 | ⚠️ 待解决"
    )
    status_desc: str = Field(default="", description="状态描述(3-6字)")
    source_members: str = Field(default="", description="信息来源成员")
    key_operations: str = Field(default="", description="关键操作/工具")
    source_server_ids: list[str] = Field(
        default_factory=list, description="关联消息的 serverId 列表（必填）"
    )


class UnifiedReporterOutput(BaseModel):
    """Single output from unified_reporter — one LLM call, all sections.

    ``topics`` is the definitive and only topic list. Each entry is a
    :class:`Topic` (strongly typed, the single source of truth).

    W16-A1: ``topics`` / ``resources`` upgraded to strongly-typed
    ``list[Topic]`` / ``list[Resource]``; ``important_notice`` changed from
    ``str | None`` to ``str`` (default ``""``) to root out B7 None. This model
    itself keeps ``extra='forbid'`` (P045 dual-schema) so legacy top-level fields
    (``topic_sections`` / ``topic_tracking`` / ``engineering_issues`` /
    ``group_summary``) are still rejected — all custom-table data flows through
    the ``custom_tables`` slot.

    lifecycle values:
      - user_defined: matched from core_topics table (user-created)
      - sustained: appeared in historical MemOS records across multiple days
      - emerging: first appearance, no historical match
    """

    model_config = ConfigDict(extra="forbid")

    # ── Daily report section ──
    overview: str = Field(description="日报概览，3-5句话精辟总结")
    important_notice: str = Field(default="", description="重要提醒，没有则填空字符串")
    topics: list[Topic] = Field(
        default_factory=list,
        description=("统一议题列表 [Topic]。lifecycle: user_defined|sustained|emerging"),
    )
    trend_analysis: str = Field(description="讨论趋势总分析（自由文本）")
    trend_summary: str = Field(
        default="",
        description="议题演变汇总（一句话总结当日整体议题动态）",
    )
    highlights: list[str] = Field(default_factory=list, description="亮点/金句")

    # ── Resources section ──
    resources: list[Resource] = Field(
        default_factory=list,
        description="资源列表 [Resource]。source_server_ids: 关联消息的 serverId 列表（必填）",
    )
    resource_count_by_type: dict[str, int] = Field(
        default_factory=dict, description="按类型统计资源数量"
    )

    # ── Custom tables (CT-4: dynamic table slots) ──
    # 所有自定义表（engineering / world_models / …）的数据都走这个通用槽位，不再有
    # 硬编码顶层字段。每张表的槽位形状 = 其 YAML output_schema 形状，由 registry 的
    # 输出契约告诉 LLM 往 custom_tables.<table_id> 下输出。例：
    #   {"engineering": {"issues": [...], "group_summary": {...}},
    #    "world_models": {"items": [...], "school_summary": {...}}}
    custom_tables: dict[str, Any] = Field(
        default_factory=dict,
        description="动态表数据槽位，key=表名，value=该表数据（形状见各表 YAML output_schema）",
    )

    # ── Metadata ──
    model_used: str = Field(default="", description="使用的模型标识")
