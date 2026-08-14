"""Memos integration schema models.

Response models for Memos (personal knowledge base) integration
health checks and search.
No direct table mapping — these represent the external Memos API
response shapes.

Pure Pydantic — no FastAPI dependency.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MemosHealthOut(BaseModel):
    """Response model for Memos service health check."""

    model_config = ConfigDict(from_attributes=True)

    status: str = "unknown"
    url: str | None = None
    connected: bool = False
    last_sync: str | None = None
    pending_queue: int = 0


class MemosSearchOut(BaseModel):
    """Response model for Memos search results."""

    model_config = ConfigDict(from_attributes=True)

    query: str
    total: int = 0
    results: list[MemosSearchItem] = []

    # Allow forward reference for the nested model
    model_config = ConfigDict(from_attributes=True)


class MemosSearchItem(BaseModel):
    """Single Memos search result item."""

    model_config = ConfigDict(from_attributes=True)

    memo_id: str
    content: str
    tags: list[str] = []
    created_at: str | None = None
    updated_at: str | None = None


# Fix forward reference
MemosSearchOut.model_rebuild()


# ============================================================
# W15 new schemas — F-W15-SCHEMAS
# ============================================================


class MemCubeOut(BaseModel):
    """Response model for a single memory cube.

    A memory cube represents a themed grouping of related
    conversation memories extracted by the MemOS pipeline.
    """

    model_config = ConfigDict(from_attributes=True)

    cube_id: str
    group_id: str
    date: str
    summary: str | None = None
    message_count: int = 0
    status: str = "pending"
    created_at: str | None = None


class MemCubeListOut(BaseModel):
    """Response model for listing memory cubes.

    Paginated-style wrapper around a collection of MemCubeOut items.
    """

    model_config = ConfigDict(from_attributes=True)

    total: int = 0
    cubes: list[MemCubeOut] = []


class CubeDeleteConfirm(BaseModel):
    """Confirmation model for cube deletion.

    B3: Requires confirm=true to authorize deletion.
    Rejects with ValidationError otherwise.
    """

    confirm: bool = Field(
        default=False, validate_default=True, description="Must be true to confirm deletion"
    )
    cube_id: str | None = Field(default=None, description="Cube ID to delete")

    @field_validator("confirm")
    @classmethod
    def must_confirm(cls, v: bool) -> bool:
        """Validate that confirm is explicitly true — B3."""
        if v is not True:
            raise ValueError("confirm must be exactly true to authorize deletion")
        return v


class RebuildRequest(BaseModel):
    """Request body for rebuilding memory cube indexes.

    Triggers full or partial re-indexing of stored memories.
    """

    group_id: str | None = Field(
        default=None, description="Specific group to rebuild, or all if omitted"
    )
    full: bool = Field(default=False, description="Perform full rebuild (clear existing first)")


class VacuumRequest(BaseModel):
    """Request body for vacuuming memory storage.

    Cleans up orphaned or stale memory records.
    """

    group_id: str | None = Field(
        default=None, description="Specific group to vacuum, or all if omitted"
    )
    dry_run: bool = Field(default=False, description="Preview affected records without deleting")


class MemoryDetailOut(BaseModel):
    """Detailed view of a single memory record."""

    model_config = ConfigDict(from_attributes=True)

    memory_id: str
    group_id: str
    date: str
    content: str | None = None
    source: str | None = None
    metadata_json: str | None = None
    created_at: str | None = None


class FlushOut(BaseModel):
    """Response model for a memory flush operation.

    Reports the result of clearing or resetting memory caches.
    """

    model_config = ConfigDict(from_attributes=True)

    status: str = "completed"
    flushed_count: int = 0
    message: str | None = None
