"""z_winnow/subagents/contracts.py — Pydantic I/O contracts.

Defines strict input/output schemas for active subagents using Pydantic v2.
All models use `ConfigDict(extra='forbid')` to reject unexpected fields.

Topic Unification: UnifiedReporterOutput now lives in models.py and is
re-exported here for backward compatibility. Only OutputComposerInput
and OutputComposerOutput are defined locally.

Architecture reference: docs/architecture-detail.md §2
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Re-export UnifiedReporterOutput from models.py (single source of truth)
from z_winnow.subagents.unified_reporter.models import (
    UnifiedReporterOutput,
)

__all__ = ["OutputComposerInput", "OutputComposerOutput", "UnifiedReporterOutput"]


# ============================================================
# output-composer (输出合成)
# ============================================================


class OutputComposerInput(BaseModel):
    """output-composer subagent input.

    Aggregates results from all subagents to compose the final report.

    Attributes:
        daily_reports: Daily reporter subagent results.
        resource_reports: Resource extractor subagent results.
        engineering_reports: Engineering analyzer subagent results.
        report_type: Report type: daily | engineering | resources.
        date: Target date in YYYYMMDD format.
        group_name: Chat group display name.
        template: Custom template override (optional).
    """

    model_config = ConfigDict(extra="forbid")

    daily_reports: list[dict[str, Any]] = Field(
        default_factory=list, description="日报 subagent 结果"
    )
    resource_reports: list[dict[str, Any]] = Field(
        default_factory=list, description="资源 subagent 结果"
    )
    engineering_reports: list[dict[str, Any]] = Field(
        default_factory=list, description="工程 subagent 结果"
    )
    report_type: str = Field(description="daily | engineering | resources")
    date: str = Field(description="目标日期 YYYYMMDD")
    group_name: str = Field(description="群聊名称")
    template: str | None = Field(default=None, description="自定义模板（覆盖默认）")


class OutputComposerOutput(BaseModel):
    """output-composer subagent output.

    Attributes:
        final_report: Complete Markdown report.
        sections: Individual section content list.
        report_type: Report type.
        model_used: Model identifier used for generation.
    """

    model_config = ConfigDict(extra="forbid")

    final_report: str = Field(description="完整 Markdown 报告")
    sections: list[str] = Field(description="各章节内容列表")
    report_type: str = Field(description="daily | engineering | resources")
    model_used: str = Field(default="", description="使用的模型标识")


# ============================================================
# Aggregated type aliases (for convenient use by upper layers)
# ============================================================

SubagentInput = OutputComposerInput

SubagentOutput = UnifiedReporterOutput | OutputComposerOutput
