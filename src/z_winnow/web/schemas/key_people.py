"""Key people schema models.

Request/response models for key people (sender statistics).
Key people are derived from raw_messages aggregation — there is
no dedicated key_people SQLite table. Fields align with the
key_people_service interface (T-W14-3).

Pure Pydantic — no FastAPI dependency.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class KeyPeopleOut(BaseModel):
    """Response model for a key person — a registered group_members row.

    Backed by the ``group_members`` table (name/role/note/is_active) joined to
    raw_messages sender statistics (message_count/first_seen/last_seen).
    POST/PUT write to group_members; GET reads from the same source so the
    data is no longer split between two tables.

    # W15: is_active field added per F-W15-SCHEMAS (B4).
    # P1-3: display_name/role/notes added; GET now reads group_members.
    """

    model_config = ConfigDict(from_attributes=True)

    sender: str
    display_name: str | None = None
    role: str | None = None
    notes: str | None = None
    message_count: int = 0
    first_seen: str | None = None
    last_seen: str | None = None
    group_id: str | None = None
    is_active: bool = True


class KeyPeopleCreate(BaseModel):
    """Request body for creating a key person entry.

    Used when manually designating a sender as a key person
    with additional metadata.
    """

    sender: str = Field(min_length=1, description="Sender name or wxid")
    display_name: str | None = None
    role: str | None = None
    notes: str | None = None


class KeyPeopleUpdate(BaseModel):
    """Request body for patching key person metadata.

    # W15: is_active field added per F-W15-SCHEMAS.
    """

    display_name: str | None = None
    role: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class SourceMemberOut(BaseModel):
    """A member fetched from the CipherTalk data source (not yet saved to group_members).

    Used by the frontend member-picker to let users select from real group members
    rather than manually typing wxid/name fields.

    Display priority: nickname > remark > groupNickname > displayName > wxid.
    """

    model_config = ConfigDict(from_attributes=True)

    wxid: str
    nickname: str | None = None
    remark: str | None = None
    display_name: str | None = None
    group_nickname: str | None = None
