"""Key people service -- sender statistics aggregated from raw_messages.

This is the one service that requires a new SQL query (GROUP BY sender,
COUNT, ORDER BY count DESC) not covered by existing database functions.

# P050: Parameterized SQL for all queries.
# P022: Pure data retrieval -- zero LLM calls.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

# L070: Conditional import
try:
    from z_winnow.web.schemas.key_people import KeyPeopleOut
except ImportError:

    class KeyPeopleOut:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)


logger = logging.getLogger(__name__)


async def list_key_people(
    db: aiosqlite.Connection,
    group_id: str,
    date: str | None = None,
    *,
    limit: int = 100,
) -> list[KeyPeopleOut]:
    """List registered key people for a group, enriched with message stats.

    # P050: Parameterized SQL. Reads from ``group_members`` (the same table
    # POST/PUT write to) and LEFT JOINs raw_messages sender statistics so the
    # GET and write paths share one source of truth.
    # P1-3: Previously aggregated raw_messages directly — now reads the
    # group_members table so role/note/display_name round-trip correctly.

    Args:
        db: aiosqlite database connection.
        group_id: Group identifier.
        date: Optional date string YYYYMMDD. When given, message stats are
            scoped to that day; when None, stats span all dates for the group.
        limit: Maximum number of members to return.

    Returns:
        List of KeyPeopleOut (active members only) sorted by message_count
        descending. Members with no messages get message_count=0.
    """
    # A008: explicit initialization
    results: list[KeyPeopleOut] = []

    # P050: date filter is structural (changes the subquery), not a value, so
    # it is composed here from our own code — all user values stay parameterized.
    if date:
        msg_subquery = (
            "SELECT sender, COUNT(*) AS message_count, "
            "MIN(date) AS first_seen, MAX(date) AS last_seen "
            "FROM raw_messages WHERE group_id = ? AND date = ? GROUP BY sender"
        )
        join_params: list[object] = [group_id, date]
    else:
        msg_subquery = (
            "SELECT sender, COUNT(*) AS message_count, "
            "MIN(date) AS first_seen, MAX(date) AS last_seen "
            "FROM raw_messages WHERE group_id = ? GROUP BY sender"
        )
        join_params = [group_id]

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        sql = (
            "SELECT gm.wxid AS sender, gm.name AS display_name, gm.role AS role, "
            "gm.note AS notes, gm.is_active AS is_active, gm.group_id AS group_id, "
            "COALESCE(m.message_count, 0) AS message_count, "
            "m.first_seen AS first_seen, m.last_seen AS last_seen "
            f"FROM group_members gm LEFT JOIN ({msg_subquery}) m "
            "ON m.sender = gm.wxid "
            "WHERE gm.group_id = ? AND gm.is_active = 1 "
            "ORDER BY message_count DESC LIMIT ?"
        )
        params = [*join_params, group_id, limit]
        cursor = await db.execute(sql, params)  # type: ignore[arg-type]
        rows = await cursor.fetchall()
        results = [KeyPeopleOut.model_validate(dict(r)) for r in rows]
    except Exception:
        # P014: log and return empty list
        logger.exception("list_key_people failed for group=%s date=%s", group_id, date)
        results = []
    finally:
        db.row_factory = original_factory

    return results


async def create_key_person(
    db: aiosqlite.Connection,
    *,
    group_id: str,
    sender: str,
    display_name: str | None = None,
    role: str = "member",
    notes: str | None = None,
) -> bool:
    """Insert a manually designated key person into group_members.

    Uses INSERT OR IGNORE for idempotency (UNIQUE constraint on group_id+wxid).

    Args:
        db: aiosqlite database connection.
        group_id: Group identifier.
        sender: Sender name or wxid.
        display_name: Optional display name override.
        role: Member role (default: "member").
        notes: Optional notes.

    Returns:
        True if insert succeeded.
    """
    import uuid

    # A008
    success: bool = False
    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        member_id = str(uuid.uuid4())
        name = display_name or sender
        cursor = await db.execute(
            """INSERT OR IGNORE INTO group_members
               (member_id, group_id, name, wxid, role, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (member_id, group_id, name, sender, role, notes),
        )
        await db.commit()
        success = cursor.rowcount > 0
    except Exception:
        logger.exception("create_key_person failed for sender=%s", sender)
    finally:
        db.row_factory = original_factory
    return success


# ---------------------------------------------------------------------------
# W15-P1-KEYPEOPLE: update_key_person + delete_key_person
# ---------------------------------------------------------------------------


# P050: Whitelist of allowed DB column names for dynamic SET clause.
# Only these columns may appear in an UPDATE on group_members.
_ALLOWED_UPDATE_COLUMNS = frozenset({"name", "role", "note", "is_active"})

# RF3: Field-level translation — API field name → DB column name.
_FIELD_TRANSLATION: dict[str, str] = {
    "display_name": "name",
    "role": "role",
    "notes": "note",
    "is_active": "is_active",
}


async def update_key_person(
    db: aiosqlite.Connection,
    *,
    sender: str,
    group_id: str,
    update_fields: dict[str, object],
) -> KeyPeopleOut | None:
    """Update key person metadata with dynamic SET clause (P009).

    P009: Only non-None fields in ``update_fields`` contribute to the
    SET clause — absent fields are left unchanged in the DB.

    P050: Parameterized compound WHERE wxid=? AND group_id=?.
    SET clause columns are restricted to ``_ALLOWED_UPDATE_COLUMNS``.

    RF3: Field-level translation (display_name→name, notes→note)
    happens here in the service layer.

    Args:
        db: aiosqlite database connection.
        sender: Sender wxid (path parameter).
        group_id: Group identifier (query parameter).
        update_fields: Dict mapping API field names to new values.
            Recognised keys: display_name, role, notes, is_active.

    Returns:
        KeyPeopleOut if the row was found and updated, None if no
        matching sender+group_id pair exists.
    """
    # A008: explicit initialization before try/except
    result: KeyPeopleOut | None = None

    # P009: Build dynamic SET clause from non-None fields only
    set_clauses: list[str] = []
    params: list[object] = []

    for api_field, db_column in _FIELD_TRANSLATION.items():
        if api_field not in update_fields:
            continue
        value = update_fields[api_field]
        if value is None:
            continue  # P009: skip None — leave DB column unchanged
        # P050: Only whitelisted columns are allowed
        if db_column not in _ALLOWED_UPDATE_COLUMNS:
            continue
        # Normalise bool → int for SQLite (is_active column is INTEGER)
        if isinstance(value, bool):
            value = 1 if value else 0
        set_clauses.append(f"{db_column} = ?")
        params.append(value)

    if not set_clauses:
        # Nothing to update — return None (caller treats as no-op / not-found)
        return None

    # P050: Parameterized compound WHERE
    params.extend([sender, group_id])

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        sql = f"UPDATE group_members SET {', '.join(set_clauses)} WHERE wxid = ? AND group_id = ?"
        cursor = await db.execute(sql, params)  # type: ignore[arg-type]
        await db.commit()

        if cursor.rowcount == 0:
            return None

        # Fetch the updated row so we can return a KeyPeopleOut
        cursor = await db.execute(
            "SELECT wxid, name, role, note, is_active, group_id "
            "FROM group_members WHERE wxid = ? AND group_id = ?",
            (sender, group_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            row_dict = dict(row)
            result = KeyPeopleOut(
                sender=row_dict["wxid"],
                display_name=row_dict["name"],
                role=row_dict["role"],
                notes=row_dict["note"],
                message_count=0,
                group_id=row_dict["group_id"],
                is_active=bool(row_dict["is_active"]),
            )
    except Exception:
        logger.exception("update_key_person failed for sender=%s group_id=%s", sender, group_id)
    finally:
        db.row_factory = original_factory

    return result


async def delete_key_person(
    db: aiosqlite.Connection,
    *,
    sender: str,
    group_id: str,
) -> bool:
    """Soft-delete a key person by setting is_active = 0.

    P050: Parameterized compound WHERE wxid=? AND group_id=?.

    Args:
        db: aiosqlite database connection.
        sender: Sender wxid (path parameter).
        group_id: Group identifier (query parameter).

    Returns:
        True if a matching row was found and soft-deleted, False otherwise.
    """
    # A008: explicit initialization
    deleted: bool = False

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            "UPDATE group_members SET is_active = 0 WHERE wxid = ? AND group_id = ?",
            (sender, group_id),
        )
        await db.commit()
        deleted = cursor.rowcount > 0
    except Exception:
        logger.exception("delete_key_person failed for sender=%s group_id=%s", sender, group_id)
    finally:
        db.row_factory = original_factory

    return deleted


# ---------------------------------------------------------------------------
# Source members — fetch real group members from CipherTalk data source
# ---------------------------------------------------------------------------


async def list_source_members(
    db: aiosqlite.Connection,
    group_id: str,
) -> list[dict[str, Any]]:
    """Fetch group members from the active data source (CipherTalk/WeFlow) for member-picker UI.

    Resolves ``chatroom_id`` from ``group_id`` via the groups table, then calls
    the data source's ``get_group_members`` API via ``create_data_client()``,
    which selects WeFlowClient/CipherTalkClient per ``settings.data_source``.

    Members are sorted by priority: nickname (non-empty first) → remark →
    groupNickname → displayName → wxid.

    Args:
        db: aiosqlite database connection.
        group_id: Group identifier (groups.group_id).

    Returns:
        List of dicts with keys: wxid, nickname, remark, display_name,
        group_nickname.  Empty list on any error (P014 graceful degradation).
    """
    # A008: explicit initialization
    results: list[dict[str, Any]] = []

    # Step 1: Resolve chatroom_id from group_id
    try:
        from z_winnow.pipeline.group_config import resolve_chatroom_id

        chatroom_id = await resolve_chatroom_id(group_id, db_path="data/winnow.db")
    except (ValueError, FileNotFoundError) as exc:
        logger.warning(
            "list_source_members: cannot resolve chatroom_id for group_id=%s — %s",
            group_id,
            exc,
        )
        return results

    if not chatroom_id or "@chatroom" not in chatroom_id:
        logger.warning(
            "list_source_members: invalid chatroom_id=%s for group_id=%s",
            chatroom_id,
            group_id,
        )
        return results

    # Step 2: Fetch members from the active data source (weflow/ciphertalk)
    try:
        from z_winnow.pipeline.cipher_talk_client import create_data_client

        # create_data_client() 按 settings.data_source 选 WeFlowClient/CipherTalkClient,
        # 并用 effective_data_base_url/token (自动选 weflow_*/ciphertalk_*).
        # WeFlowClient 继承父类 get_group_members → /api/v1/group-members
        # (WeFlow 服务端实测兼容该端点, 返回 success/members 结构一致).
        async with create_data_client() as client:
            resp = await client.get_group_members(chatroom_id)
            raw_members = resp.get("members", [])

        # Step 3: Build result list with display name priority
        for m in raw_members:
            wxid = m.get("wxid", "")
            if not wxid:
                continue
            nickname = m.get("nickname", "") or None
            remark = m.get("remark", "") or None
            group_nickname = m.get("group_nickname", "") or None
            raw_display = m.get("displayName", "") or None

            # Best display name: nickname > remark > groupNickname > displayName > wxid
            best_display = nickname or remark or group_nickname or raw_display or wxid

            results.append(
                {
                    "wxid": wxid,
                    "nickname": nickname,
                    "remark": remark,
                    "display_name": best_display,
                    "group_nickname": group_nickname,
                }
            )

        # Step 4: Sort — members with nickname first, then by display_name
        def _sort_key(m: dict[str, Any]) -> tuple[int, str]:
            has_nickname = 0 if m.get("nickname") else 1
            return (has_nickname, m.get("display_name", ""))

        results.sort(key=_sort_key)

    except Exception:
        # P014: log and return empty list — never block the UI
        logger.exception(
            "list_source_members failed for group_id=%s chatroom_id=%s",
            group_id,
            chatroom_id,
        )
        results = []

    return results
