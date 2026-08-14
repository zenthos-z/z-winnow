"""Run schema models.

Request/response models for pipeline runs.
Field names match SQLite columns in pipeline_runs table
(SCHEMA_SQL).

Pure Pydantic — no FastAPI dependency.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RunCreate(BaseModel):
    """Request body for starting a new pipeline run."""

    component: str = Field(min_length=1, description="Pipeline component name")
    group_id: str | None = None
    date: str | None = Field(default=None, description="Target date YYYY-MM-DD")

    @field_validator("date")
    @classmethod
    def validate_date_format(cls, v: str | None) -> str | None:
        """Validate date format is YYYY-MM-DD."""
        if v is None:
            return v
        # P039: Structural validation rather than string matching
        parts = v.split("-")
        if len(parts) != 3 or len(parts[0]) != 4 or len(parts[1]) != 2 or len(parts[2]) != 2:
            raise ValueError(f"date must be in YYYY-MM-DD format, got '{v}'")
        try:
            int(parts[0])
            int(parts[1])
            int(parts[2])
        except ValueError as err:
            raise ValueError(f"date must be in YYYY-MM-DD format, got '{v}'") from err
        return v


class RunStatusOut(BaseModel):
    """Response model for a pipeline run — maps to pipeline_runs table.

    # P022: Pure DTO — field names match SQLite columns.
    """

    model_config = ConfigDict(from_attributes=True)

    run_id: str
    component: str
    status: str = "unknown"
    started_at: str | None = None
    completed_at: str | None = None
    message_count: int = 0
    error_message: str | None = None
    current_node: str | None = None
    progress_pct: int | None = None
    node_history: str | None = None
    group_id: str | None = None
    date: str | None = None
    created_at: str | None = None


# ============================================================
# W15 new schemas — F-W15-SCHEMAS
# ============================================================


class BatchRunItem(BaseModel):
    """Single item in a batch pipeline run request.

    Each item represents one pipeline component invocation
    with optional group/date targeting.
    """

    component: str = Field(min_length=1, description="Pipeline component name")
    group_id: str | None = None
    date: str | None = Field(default=None, description="Target date YYYY-MM-DD")

    @field_validator("date")
    @classmethod
    def validate_date_format(cls, v: str | None) -> str | None:
        """Validate date format is YYYY-MM-DD."""
        if v is None:
            return v
        parts = v.split("-")
        if len(parts) != 3 or len(parts[0]) != 4 or len(parts[1]) != 2 or len(parts[2]) != 2:
            raise ValueError(f"date must be in YYYY-MM-DD format, got '{v}'")
        try:
            int(parts[0])
            int(parts[1])
            int(parts[2])
        except ValueError as err:
            raise ValueError(f"date must be in YYYY-MM-DD format, got '{v}'") from err
        return v


class BatchRunRequest(BaseModel):
    """Request body for batch pipeline runs.

    Submits multiple pipeline components at once. Rejects empty
    items list with ValidationError (B3: 422 on empty).
    """

    items: list[BatchRunItem] = Field(
        min_length=1, description="Batch run items (at least 1 required)"
    )


class BatchRunResponse(BaseModel):
    """Response for batch run submission.

    Returns a batch identifier and per-item status tracking.
    No direct table mapping — assembled by the runs service.
    """

    model_config = ConfigDict(from_attributes=True)

    batch_id: str
    total: int
    status: str = "accepted"
    results: list[dict[str, Any]] = []
