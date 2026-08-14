"""Shared schema types for the web API.

Provides generic envelope types (PaginatedResponse, ErrorResponse),
async task tracking models, and reusable enums used across domain schemas.

All models are pure Pydantic BaseModel — no FastAPI dependency.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

# ============================================================
# Enums shared across domain schemas
# ============================================================


class RunStatus(StrEnum):
    """Pipeline run status values — matches pipeline_runs.status column."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class SignalType(StrEnum):
    """Feedback signal type — matches feedback_events.signal column values."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    CORRECTION = "correction"


class Severity(StrEnum):
    """Feedback severity level — matches feedback_events.severity column."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Lifecycle(StrEnum):
    """Topic lifecycle stage — matches topic_summaries.lifecycle column."""

    EMERGING = "emerging"
    ACTIVE = "active"
    DECLINING = "declining"
    CLOSED = "closed"


# ============================================================
# Generic envelope
# ============================================================


class PaginatedResponse[T](BaseModel):
    """Generic paginated response envelope.

    Usage::

        PaginatedResponse[GroupOut](total=10, page=1, page_size=50, items=[...])
    """

    model_config = ConfigDict(from_attributes=True)

    total: int
    page: int
    page_size: int
    items: list[T]


# ============================================================
# Error / async task models
# ============================================================


class ErrorResponse(BaseModel):
    """Standard error response body."""

    model_config = ConfigDict(from_attributes=True)

    error: str
    detail: dict[str, Any] | None = None


class AsyncTaskResponse(BaseModel):
    """Response body returned when an async task is started."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    status_url: str


class TaskStatusResponse(BaseModel):
    """Polling response for async task status."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    status: str
    progress: float | None = None
    result: Any | None = None
    error: str | None = None
