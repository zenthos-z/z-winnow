"""Export schema models.

Request/response models for report export functionality.
No direct table mapping — export is an async operation.

Pure Pydantic — no FastAPI dependency.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExportRequest(BaseModel):
    """Request body for triggering a report export."""

    report_id: str = Field(min_length=1, description="Report ID to export")
    format: str = Field(default="json", description="Export format: json | markdown | html")

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        """Validate export format."""
        valid = {"json", "markdown", "html"}
        if v not in valid:
            raise ValueError(f"format must be one of {valid}, got '{v}'")
        return v


class ExportStatusOut(BaseModel):
    """Response model for export task status."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    report_id: str
    format: str = "json"
    status: str = "pending"
    download_url: str | None = None
    error: str | None = None
    created_at: str | None = None


# ============================================================
# W15 new schemas — F-W15-SCHEMAS
# ============================================================


class RLExportRequest(BaseModel):
    """Request body for RL training data export.

    DIFFERENT from ExportRequest: ExportRequest uses report_id + format
    for single-report export. RLExportRequest uses group_id + date range
    for bulk RL training data export.

    B2: date normalization YYYY-MM-DD → YYYYMMDD.
        end_date < start_date raises ValidationError.
    """

    group_id: str = Field(min_length=1, description="Group ID to export data for")
    start_date: str = Field(min_length=1, description="Start date (YYYY-MM-DD or YYYYMMDD)")
    end_date: str = Field(min_length=1, description="End date (YYYY-MM-DD or YYYYMMDD)")
    format: str = Field(default="jsonl", description="Export format: jsonl")

    @field_validator("start_date", "end_date")
    @classmethod
    def normalize_dates(cls, v: str) -> str:
        """Normalize YYYY-MM-DD → YYYYMMDD. B2."""
        v = v.replace("-", "")
        if len(v) != 8 or not v.isdigit():
            raise ValueError(f"date must be YYYY-MM-DD or YYYYMMDD, got '{v}'")
        return v

    @model_validator(mode="after")
    def validate_date_range(self) -> RLExportRequest:
        """Ensure end_date >= start_date. B2."""
        if self.end_date < self.start_date:
            raise ValueError(
                f"end_date '{self.end_date}' must be on or after start_date '{self.start_date}'"
            )
        return self
