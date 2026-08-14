"""merger.py — ComposedData dataclass for output_composer internal merging.

P022: Pure data container — no SQL/formatting logic.
Holds merged report data before template rendering.

T-W13: Unified topics[] replaces topic_sections + new_topics + updated_topics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ComposedData:
    """Merged report data container for template rendering.

    Holds all sections produced by unified_reporter (daily report,
    resources, engineering issues) plus unified topic data.

    Attributes:
        date: Target date in YYYYMMDD format.
        group_name: Chat group display name.
        overview: Daily report overview text.
        important_notice: Optional important notice.
        topics: Unified topic list with lifecycle classification.
        trend_analysis: Trend analysis text.
        trend_summary: One-line topic evolution summary.
        highlights: List of highlight strings.
        resources: List of resource items.
        resource_count_by_type: Resource type distribution counts.
        issues: List of engineering issues.
        group_summary: Per-group summary dict.
        custom_tables: Custom table configurations (CT-3), or None.
        custom_table_data: Per-table DATA for Markdown rendering —
            ``{kind: {records_key: [...], summary_key: {...}}}``. Populated from
            the custom_tables slot / L3 files. engineering is also present but
            rendered by its own dedicated renderer; the generic renderer skips it.
    """

    date: str = ""
    group_name: str = ""
    overview: str = ""
    important_notice: str = ""
    topics: list[dict[str, Any]] = field(default_factory=list)
    trend_analysis: str | dict[str, Any] = ""
    trend_summary: str = ""
    highlights: list[str] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    resource_count_by_type: dict[str, int] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    group_summary: dict[str, str] = field(default_factory=dict)
    custom_tables: dict[str, Any] | None = None
    custom_table_data: dict[str, dict[str, Any]] = field(default_factory=dict)
