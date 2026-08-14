"""Overview endpoint schema models.

Provides the dashboard overview stats response model.
Pure Pydantic — no FastAPI dependency.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OverviewGroupItem(BaseModel):
    """Per-group dashboard row — one entry per active group.

    Composed by overview_service from the groups/report_versions/pipeline_runs
    tables plus L3 JSON counts (topic/resource/engineering) and a raw_messages
    active-member count for the group's latest report date.

    # P1-2: Added to back the per-group table on the dashboard.
    """

    model_config = ConfigDict(from_attributes=True)

    group_id: str
    display_name: str
    is_active: bool = True
    daily_report_enabled: bool = True
    engineering_enabled: bool = True
    world_models_enabled: bool = False  # 世界大模型动态自定义表开关
    latest_report_at: str | None = None
    latest_run_status: str | None = None
    report_version_id: str | None = None  # feeds GET /reports/{id}/content
    feishu_pushed_at: str | None = None  # 最新日报的最后成功推送飞书时间（null=未推送）
    active_member_count: int = 0  # raw_messages DISTINCT sender for latest date
    topic_count: int = 0  # daily.json topics[]
    resource_count: int = 0  # resources.json total_count
    engineering_count: int = 0  # engineering.json issues[]
    world_models_count: int = 0  # world_models.json items[]


class OverviewStatsOut(BaseModel):
    """Dashboard overview statistics.

    Aggregated stats for the main dashboard view — message counts,
    group summary, sync queue depth, etc.

    Not directly backed by a single table; composed by the
    overview_service from multiple sources.
    """

    model_config = ConfigDict(from_attributes=True)

    total_messages: int = 0
    total_groups: int = 0
    total_topics: int = 0
    total_reports: int = 0
    total_feedback: int = 0
    last_sync_at: str | None = None
    active_runs: int = 0
    groups: list[OverviewGroupItem] = []  # P1-2: per-group rows
