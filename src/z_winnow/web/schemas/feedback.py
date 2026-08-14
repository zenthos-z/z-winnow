"""Feedback schema models.

Request/response models for feedback events.
Field names match SQLite columns in feedback_events table
(WEB_SCHEMA_SQL).

Pure Pydantic — no FastAPI dependency.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from z_winnow.web.schemas.common import Severity, SignalType


class FeedbackCreate(BaseModel):
    """Request body for creating a feedback event."""

    group_id: str = Field(min_length=1, description="Group ID")
    date: str = Field(min_length=1, description="Date string YYYY-MM-DD")
    report_id: str | None = None
    target_type: str = Field(min_length=1, description="Target type (e.g., topic, section)")
    target_id: str | None = None
    target_path: str | None = None
    # M4 溯源定位：被反馈版本 + （议题级反馈时）被反馈议题 id。
    # 提交时即写库——regenerate 成功后才回填 produced_version_id / memos_*。
    target_version_id: str | None = Field(
        default=None, description="被反馈的日报版本 id（{report_id}-v{n}）"
    )
    target_topic_id: str | None = Field(
        default=None, description="议题级反馈时的被反馈议题 id"
    )
    signal: SignalType
    severity: Severity = Severity.INFO
    rating: str | None = None
    tags: str | None = None
    correction_mode: str | None = None
    original_text: str | None = None
    corrected_text: str | None = None
    correction_note: str | None = None
    reporter: str | None = None

    @field_validator("signal", mode="before")
    @classmethod
    def validate_signal(cls, v: Any) -> Any:
        """Ensure signal is a valid SignalType value."""
        valid = {s.value for s in SignalType}
        if isinstance(v, str) and v not in valid:
            raise ValueError(f"signal must be one of {sorted(valid)}, got '{v}'")
        return v


class FeedbackUpdate(BaseModel):
    """Request body for patching a feedback event."""

    severity: Severity | None = None
    tags: str | None = None
    correction_note: str | None = None


class FeedbackOut(BaseModel):
    """Response model for a feedback event — maps to feedback_events table.

    # P022: Pure DTO — field names match SQLite columns.
    """

    model_config = ConfigDict(from_attributes=True)

    feedback_id: str
    created_at: str | None = None
    group_id: str
    date: str
    report_id: str | None = None
    target_type: str
    target_id: str | None = None
    target_path: str | None = None
    signal: str
    severity: str = "info"
    rating: str | None = None
    tags: str | None = None
    correction_mode: str | None = None
    original_text: str | None = None
    corrected_text: str | None = None
    correction_note: str | None = None
    reporter: str | None = None
    consumed_at: str | None = None
    consumed_by: str | None = None
    # M4 溯源四元组字段（提交时写 target_*；regenerate/feedback_memory 后回填 produced/memos_*）
    target_version_id: str | None = None
    target_topic_id: str | None = None
    produced_version_id: str | None = None
    memos_cube_id: str | None = None
    memos_node_id: str | None = None
    archived_memos_id: str | None = None
    status: str | None = None
