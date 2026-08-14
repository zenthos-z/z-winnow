"""Core topic schema models.

Request/response models for core topic CRUD.
Field names match SQLite columns in core_topics table
(WEB_SCHEMA_SQL).

Pure Pydantic — no FastAPI dependency.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CoreTopicCreate(BaseModel):
    """Request body for creating a core topic."""

    group_id: str = Field(min_length=1, description="Owning group ID")
    name: str = Field(min_length=1, description="Topic name")
    description: str | None = None
    keywords: str | None = None
    priority: int = Field(default=1, ge=1, le=10)
    is_active: bool = True


class CoreTopicUpdate(BaseModel):
    """Request body for patching a core topic."""

    name: str | None = None
    description: str | None = None
    keywords: str | None = None
    priority: int | None = None
    is_active: bool | None = None


class CoreTopicOut(BaseModel):
    """Response model for a core topic — maps 1:1 to core_topics table.

    # P022: Pure DTO — field names match SQLite columns.
    """

    model_config = ConfigDict(from_attributes=True)

    core_topic_id: str
    group_id: str
    name: str
    description: str | None = None
    keywords: str | None = None
    priority: int = 1
    is_active: int = 1
    last_matched_date: str | None = None
    match_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    created_by: str | None = None
