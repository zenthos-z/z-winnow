"""Report schema models.

Response models for report versions and diffs.
Field names match SQLite columns in report_versions table
(REPORT_SCHEMA_SQL).

Pure Pydantic — no FastAPI dependency.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReportOut(BaseModel):
    """Response model for a report — high-level metadata.

    Derived from report_versions table, represents the latest
    version of a report for a given group + date.
    """

    model_config = ConfigDict(from_attributes=True)

    report_id: str
    group_id: str
    date: str
    latest_version: int = 1
    source: str | None = None
    created_at: str | None = None


class ReportVersionOut(BaseModel):
    """Response model for a specific report version.

    # P022: Maps 1:1 to report_versions table columns.
    """

    model_config = ConfigDict(from_attributes=True)

    version_id: str
    report_id: str
    group_id: str
    date: str
    version_number: int
    content: str | None = None
    content_changed: int = 0
    source: str
    build_duration_s: float | None = None
    is_active: int = 1  # M4: 1=当前生效版本（回滚后可能≠最新）；前端据此标"当前"
    created_at: str | None = None


class ReportDiffOut(BaseModel):
    """Response model for diff between two report versions."""

    model_config = ConfigDict(from_attributes=True)

    report_id: str
    group_id: str
    date: str
    old_version: int
    new_version: int
    old_content: str | None = None
    new_content: str | None = None
    content_changed: bool = False


# ============================================================
# W15 new schemas — F-W15-SCHEMAS
# ============================================================


class RegenerateRequest(BaseModel):
    """Optional request body for POST /reports/{rid}/regenerate.

    All fields are optional overrides — when omitted, the report's
    stored group_id and date are used.
    """

    group_id: str | None = Field(default=None, description="Override group ID")
    date: str | None = Field(default=None, description="Override date (YYYYMMDD)")


class MarkdownExportRequest(BaseModel):
    """Optional query parameter model for GET /reports/{rid}/export.

    All fields are optional overrides — when omitted, the report's
    stored group_id and date are used to locate L3 JSON files.
    Different from ExportRequest (which is for async file export).
    """

    group_id: str | None = Field(default=None, description="Override group ID for L3 JSON lookup")
    date: str | None = Field(default=None, description="Override date YYYYMMDD for L3 JSON lookup")


class FeishuPushRequest(BaseModel):
    """Request body for pushing a report to Feishu.

    Triggers publishing a report version to a Feishu document
    or chat target.
    """

    # report_id 走 URL path 参数（/reports/{report_id}/feishu），不在 body 里重复——
    # 早期把它设成必填字段，导致前端发 body:'{}' 时 Pydantic 校验失败返回 422。
    doc_title: str | None = Field(default=None, description="Custom Feishu document title")
    overwrite: bool = Field(default=True, description="覆盖模式下先删除同日期旧记录再创建新记录")


class CoverRequest(BaseModel):
    """Request body for POST /reports/{report_id}/cover（生成日报配图）。

    全部可选——默认走 settings.image_gen_* 。report_id 走 URL path 参数。
    """

    count: int | None = Field(default=None, description="生成张数（默认 settings.image_gen_count）")
    ratio: str | None = Field(default=None, description="宽高比如 4:5（默认 settings.image_gen_ratio）")
    size: str | None = Field(default=None, description="分辨率如 2K（默认 settings.image_gen_size）")


class ReportContentOut(BaseModel):
    """Response model for GET /reports/{report_id}/content.

    # A026: Single source of truth — field names 1:1 aligned with the
    # service-layer model report_service.ReportContent
    # (report_service.py:42-54). tests/test_reports_content.py:93-97
    # hard-asserts these four field names, so any drift fails 4 tests.
    # P022: Pure DTO — no FastAPI dependency.
    """

    model_config = ConfigDict(from_attributes=True)

    report_type: str
    group_id: str
    date: str
    data: dict[str, Any] = {}
