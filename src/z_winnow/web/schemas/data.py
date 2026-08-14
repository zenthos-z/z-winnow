"""Data browser schema models.

Response models for Layer 1 (raw messages), Layer 2 (parsed contexts),
Layer 3 (topic summaries), and provenance tracking.
Field names match SQLite columns in SCHEMA_SQL tables.

Pure Pydantic — no FastAPI dependency.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class L1MessageOut(BaseModel):
    """Response model for a raw message — maps to raw_messages table.

    # P022: Pure DTO — field names match SQLite columns exactly.
    """

    model_config = ConfigDict(from_attributes=True)

    serverID: str  # noqa: N815 — matches SQLite column name
    date: str
    group_id: str | None = None
    sender: str
    content: str
    msg_type: str = "text"
    image_path: str | None = None
    sanitized: int = 0
    raw_json: str | None = None
    created_at: str | None = None


class L2ContextOut(BaseModel):
    """Response model for a parsed context — maps to parsed_contexts table."""

    model_config = ConfigDict(from_attributes=True)

    context_id: str
    date: str
    group_id: str | None = None
    server_ids: str
    context_text: str
    token_count: int | None = None
    source_subagent: str | None = None
    created_at: str | None = None


class L3SummaryOut(BaseModel):
    """Response model for a topic summary — maps to topic_summaries table."""

    model_config = ConfigDict(from_attributes=True)

    summary_id: str
    date: str
    group_id: str | None = None
    topic_name: str
    topic_id: str | None = None
    summary_text: str
    context_ids: str
    source_server_ids: str
    confidence: float | None = None
    model_used: str | None = None
    lifecycle: str = "emerging"
    matched_core_topic_id: str | None = None
    background: str | None = None
    process: str | None = None
    conclusion: str | None = None
    description: str | None = None
    participants: str | None = None
    trend: str | None = None
    created_at: str | None = None


class ProvenanceOut(BaseModel):
    """Response model for provenance tracking.

    Links a summary back to its source messages and contexts.
    Computed by the data browser service — no direct table mapping.
    """

    model_config = ConfigDict(from_attributes=True)

    summary_id: str
    server_ids: list[str] = []
    context_ids: list[str] = []


# ============================================================
# W15 new schemas — F-W15-SCHEMAS
# ============================================================


class DataStatsOut(BaseModel):
    """Aggregate statistics for the data browser dashboard.

    Computed from raw_messages, topic_summaries, and parsed_contexts
    tables. No direct 1:1 table mapping.
    """

    model_config = ConfigDict(from_attributes=True)

    total_messages: int = 0
    total_groups: int = 0
    total_topics: int = 0
    total_reports: int = 0
    date_range_start: str | None = None
    date_range_end: str | None = None


# P022: Pure DTO — field names match L1MessageOut extended with nested contexts/summaries
class L1MessageDetailOut(BaseModel):
    """Detailed message view with linked contexts and topic summaries.

    Extends L1MessageOut fields with nested context/summary data
    for the drill-down view. No direct table mapping — assembled
    by the data browser service.
    """

    model_config = ConfigDict(from_attributes=True)

    serverID: str  # noqa: N815 — matches SQLite column name
    date: str
    group_id: str | None = None
    sender: str
    content: str
    msg_type: str = "text"
    image_path: str | None = None
    sanitized: int = 0
    raw_json: str | None = None
    created_at: str | None = None
    contexts: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []


class ProvenanceChainOut(BaseModel):
    """Provenance chain — maps one server_id to its message details and topics.

    DIFFERENT from ProvenanceOut: ProvenanceOut traces summary_id →
    server_ids + context_ids. This schema traces server_id →
    message details + associated topic summaries. Assembled by the
    provenance chain service, not mapped to a single table.
    """

    model_config = ConfigDict(from_attributes=True)

    server_id: str
    message: dict[str, Any] | None = None
    topics: list[dict[str, Any]] = []
