"""Group and member schema models.

Request/response models for group CRUD and member management.
Field names match SQLite columns in groups / group_members tables
(WEB_SCHEMA_SQL).

Pure Pydantic — no FastAPI dependency.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _parse_tables_blob(v: Any) -> Any:
    """coerce a feishu_tables value into a dict (or None).

    The groups table stores it as a JSON TEXT column, so DB reads arrive as a
    string; API input arrives as a dict already. Accept both, plus None.
    """
    if v is None or v == "":
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return None
    return v if isinstance(v, dict) else None


# ============================================================
# Group models
# ============================================================


class GroupCreate(BaseModel):
    """Request body for creating a new group."""

    display_name: str = Field(min_length=1, description="Human-readable group name")
    chatroom_id: str = Field(min_length=1, description="WeChat chatroom ID")
    output_dir: str | None = None
    feishu_enabled: bool = False
    # Feishu Bitable target (one Base per group, up to 4 data tables).
    feishu_base_token: str | None = None
    feishu_table_summary: str | None = None
    feishu_table_topics: str | None = None
    feishu_table_resources: str | None = None
    feishu_table_engineering: str | None = None
    feishu_framework_initialized: bool = False
    feishu_engineering_enabled: bool = True
    # Independent engineering content toggle (#7.1).
    engineering_enabled: bool = True
    # Per-group pluggable table set (#9.4): {kind: {enabled, table_id}}.
    feishu_tables: dict[str, Any] | None = None
    # Custom table configurations (#9.4): {kind: {enabled: bool, config: dict}}.
    custom_tables: dict[str, Any] | None = None
    custom_prompt_hints: str | None = None
    is_active: bool = True
    daily_report_enabled: bool = True
    daily_schedule_cron: str | None = None

    @field_validator("feishu_tables", mode="before")
    @classmethod
    def _coerce_feishu_tables(cls, v: Any) -> Any:
        return _parse_tables_blob(v)

    @field_validator("custom_tables", mode="before")
    @classmethod
    def _coerce_custom_tables(cls, v: Any) -> Any:
        return _parse_tables_blob(v)


class GroupUpdate(BaseModel):
    """Request body for patching a group — all fields optional."""

    display_name: str | None = None
    chatroom_id: str | None = None
    output_dir: str | None = None
    feishu_enabled: bool | None = None
    feishu_base_token: str | None = None
    feishu_table_summary: str | None = None
    feishu_table_topics: str | None = None
    feishu_table_resources: str | None = None
    feishu_table_engineering: str | None = None
    feishu_framework_initialized: bool | None = None
    feishu_engineering_enabled: bool | None = None
    engineering_enabled: bool | None = None
    feishu_tables: dict[str, Any] | None = None
    # Custom table configurations (#9.4): {kind: {enabled: bool, config: dict}}.
    custom_tables: dict[str, Any] | None = None
    custom_prompt_hints: str | None = None
    is_active: bool | None = None
    daily_report_enabled: bool | None = None
    daily_schedule_cron: str | None = None

    @field_validator("feishu_tables", mode="before")
    @classmethod
    def _coerce_feishu_tables(cls, v: Any) -> Any:
        return _parse_tables_blob(v)

    @field_validator("custom_tables", mode="before")
    @classmethod
    def _coerce_custom_tables(cls, v: Any) -> Any:
        return _parse_tables_blob(v)


class FeishuInitRequest(BaseModel):
    """Request body for POST /groups/{id}/feishu/init — create/reuse the Base framework."""

    base_target: str | None = Field(
        default=None,
        description="Base 分享链接或 app_token；留空=自动新建一个 Base",
    )
    enabled_kinds: list[str] | None = Field(
        default=None,
        description="UI 勾选的可选表种类（mandatory 隐含必建）。未传则从群配置（engineering_enabled）推导。",
    )


class FeishuTableKindOut(BaseModel):
    """One entry in the global Feishu table catalog (#9.4)."""

    kind: str
    display_name: str
    mandatory: bool
    default_enabled: bool
    field_count: int


class FeishuCatalogOut(BaseModel):
    """The global table catalog — drives the per-group 「要哪些表」 checklist."""

    kinds: list[FeishuTableKindOut]


class GroupOut(BaseModel):
    """Response model for a group — maps 1:1 to groups table columns.

    # P022: Pure DTO — field names match SQLite columns, no storage logic.
    """

    model_config = ConfigDict(from_attributes=True)

    group_id: str
    display_name: str
    chatroom_id: str
    output_dir: str | None = None
    feishu_enabled: int = 0
    feishu_base_token: str | None = None
    feishu_table_summary: str | None = None
    feishu_table_topics: str | None = None
    feishu_table_resources: str | None = None
    feishu_table_engineering: str | None = None
    feishu_framework_initialized: int = 0
    feishu_engineering_enabled: int = 1
    # deprecated: use custom_tables.engineering.enabled instead
    engineering_enabled: int = 1
    # Per-group pluggable table set (#9.4): {kind: {enabled, table_id}}.
    # Stored as JSON TEXT; coerced from str on read via the validator below.
    feishu_tables: dict[str, Any] | None = None
    # Custom table configurations (#9.4): {kind: {enabled: bool, config: dict}}.
    # Stored as JSON TEXT; coerced from str on read via the validator below.
    # On read, group_service normalizes legacy engineering_enabled into this field.
    custom_tables: dict[str, Any] | None = None
    custom_prompt_hints: str | None = None
    is_active: int = 1
    daily_report_enabled: int = 1
    daily_schedule_cron: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    created_by: str | None = None

    @field_validator("feishu_tables", mode="before")
    @classmethod
    def _coerce_feishu_tables(cls, v: Any) -> Any:
        return _parse_tables_blob(v)

    @field_validator("custom_tables", mode="before")
    @classmethod
    def _coerce_custom_tables(cls, v: Any) -> Any:
        return _parse_tables_blob(v)


# ============================================================
# Member models
# ============================================================


class GroupMemberOut(BaseModel):
    """Response model for a group member — maps to group_members table."""

    model_config = ConfigDict(from_attributes=True)

    member_id: str
    group_id: str
    name: str
    wxid: str | None = None
    role: str
    weight: float = 1.0
    note: str | None = None
    is_active: int = 1
    created_at: str | None = None


# ============================================================
# CipherTalk session models (新建群 picker)
# ============================================================


class CipherTalkSessionOut(BaseModel):
    """A CipherTalk chatroom session -- for the「新建群」picker.

    # P022: Pure DTO -- chatroom_id == session['username'], display_name ==
    # session['displayName'] (real WeChat group name, not user-typed).
    """

    model_config = ConfigDict(from_attributes=True)

    chatroom_id: str = Field(description="session['username']，如 xxx@chatroom")
    display_name: str = Field(description="session['displayName']，真实群名")
    is_registered: bool = Field(default=False, description="是否已在本地 groups 表注册")


class CipherTalkSessionsResponse(BaseModel):
    """Response for GET /groups/sessions -- carries an availability flag so the
    UI can fall back to manual entry when CipherTalk is unreachable."""

    sessions: list[CipherTalkSessionOut]
    available: bool = Field(default=True, description="CipherTalk 是否可达")
