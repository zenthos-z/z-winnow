"""Group service -- group, member, and core topic CRUD with pagination.

Wraps existing ``Storage`` class and raw SQL queries into typed async
methods returning Pydantic models.

# P022: Pure data retrieval / formatting -- zero LLM calls.
# P050: Parameterized SQL for all new queries (pagination, search).
# P009: All filter params default to None/empty and cascade transparently.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from z_winnow.memory.types import MemOSAdapterProtocol

from z_winnow.web.services import PaginatedResult

# L070: Conditional imports
try:
    from z_winnow.web.schemas.core_topics import CoreTopicOut
    from z_winnow.web.schemas.groups import (
        CipherTalkSessionOut,
        CipherTalkSessionsResponse,
        GroupMemberOut,
        GroupOut,
        GroupUpdate,
    )
except ImportError:

    class GroupOut:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    class GroupUpdate:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    class GroupMemberOut:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    class CipherTalkSessionOut:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    class CipherTalkSessionsResponse:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    class CoreTopicOut:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)


logger = logging.getLogger(__name__)


def _normalize_custom_tables(group: GroupOut) -> GroupOut:
    """Ensure custom_tables carries a resolved ``enabled`` flag for every optional kind.

    custom_tables blob is **authoritative** when present (UI writes it). For kinds
    missing from the blob — legacy groups where the UI never wrote custom_tables —
    derive ``enabled`` from feishu_tables via ``active_kinds``. We deliberately do
    NOT derive from the deprecated ``engineering_enabled`` column: that column is
    never updated by the UI toggle and stays 1, which would wrongly re-enable
    engineering the user turned off.
    """
    from z_winnow.pipeline.feishu import schema as feishu_schema

    if group.custom_tables is None:
        group.custom_tables = {}
    existing = group.custom_tables if isinstance(group.custom_tables, dict) else {}

    ft = group.feishu_tables if isinstance(group.feishu_tables, dict) else None
    for kind in feishu_schema.TABLE_CATALOG:
        if kind in feishu_schema.MANDATORY_KINDS:
            continue  # mandatory kinds are always on — not part of the per-group toggle
        entry = existing.get(kind)
        if isinstance(entry, dict) and "enabled" in entry:
            continue  # authoritative — custom_tables blob already decides
        # Derive from the report-side resolver (custom_tables > feishu_tables >
        # deprecated column > default off). Engineering has a column fallback; other
        # optional kinds (future plugins) resolve via active_kinds over feishu_tables.
        if kind == "engineering":
            resolved = feishu_schema.engineering_enabled_for_report(
                existing, ft, group.engineering_enabled
            )
        else:
            resolved = kind in feishu_schema.active_kinds(ft, existing)
        prev_cfg = entry.get("config", {}) if isinstance(entry, dict) else {}
        existing[kind] = {"enabled": resolved, "config": prev_cfg}

    group.custom_tables = existing
    return group


async def list_groups(
    db: aiosqlite.Connection,
    *,
    page: int = 1,
    page_size: int = 20,
    is_active: bool | None = True,
    search: str = "",
) -> PaginatedResult:
    """List groups with pagination, active filter, and search.

    # P050: Parameterized SQL -- no string interpolation of user input.
    # P009: is_active=None returns all groups (no filter), True=active only.

    Args:
        db: aiosqlite database connection.
        page: Page number (1-based).
        page_size: Items per page.
        is_active: Filter by active status. None = all.
        search: Search term for display_name (case-insensitive LIKE).

    Returns:
        PaginatedResult of GroupOut items.
    """
    # A008: explicit initialization
    result: PaginatedResult = PaginatedResult(items=[], total=0, page=page, page_size=page_size)

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        conditions: list[str] = []
        params: list[Any] = []

        if is_active is not None:
            conditions.append("is_active = ?")
            params.append(1 if is_active else 0)

        if search:
            conditions.append("display_name LIKE ?")
            params.append(f"%{search}%")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # Count total matching rows
        cursor = await db.execute(f"SELECT COUNT(*) FROM groups {where}", tuple(params))
        row = await cursor.fetchone()
        total: int = row[0] if row else 0

        # Fetch page
        offset = (page - 1) * page_size
        cursor = await db.execute(
            f"SELECT * FROM groups {where} ORDER BY group_id LIMIT ? OFFSET ?",
            (*tuple(params), page_size, offset),
        )
        rows = await cursor.fetchall()

        items = [_normalize_custom_tables(GroupOut.model_validate(dict(r))) for r in rows]
        result = PaginatedResult(items=items, total=total, page=page, page_size=page_size)
    except Exception:
        # P014: log and return empty result
        logger.exception("list_groups failed")
        result = PaginatedResult(items=[], total=0, page=page, page_size=page_size)
    finally:
        db.row_factory = original_factory

    return result


async def get_group_detail(
    db: aiosqlite.Connection,
    group_id: str,
) -> GroupOut | None:
    """Get a single group by ID.

    Args:
        db: aiosqlite database connection.
        group_id: Group identifier.

    Returns:
        GroupOut or None if not found.
    """
    # A008: explicit initialization
    result: GroupOut | None = None

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            "SELECT * FROM groups WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        if row is not None:
            result = _normalize_custom_tables(GroupOut.model_validate(dict(row)))
    except Exception:
        logger.exception("get_group_detail failed for group_id=%s", group_id)
        result = None
    finally:
        db.row_factory = original_factory

    return result


async def list_members(
    db: aiosqlite.Connection,
    group_id: str,
    *,
    is_active: bool = True,
) -> list[GroupMemberOut]:
    """List members for a group.

    Args:
        db: aiosqlite database connection.
        group_id: Group identifier.
        is_active: If True, return only active members.

    Returns:
        List of GroupMemberOut.
    """
    # A008: explicit initialization
    results: list[GroupMemberOut] = []

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        if is_active:
            cursor = await db.execute(
                "SELECT * FROM group_members WHERE group_id = ? AND is_active = 1 ORDER BY member_id",
                (group_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM group_members WHERE group_id = ? ORDER BY member_id",
                (group_id,),
            )
        rows = await cursor.fetchall()
        results = [GroupMemberOut.model_validate(dict(r)) for r in rows]
    except Exception:
        logger.exception("list_members failed for group_id=%s", group_id)
        results = []
    finally:
        db.row_factory = original_factory

    return results


async def list_core_topics(
    db: aiosqlite.Connection,
    group_id: str,
    *,
    is_active: bool = True,
) -> list[CoreTopicOut]:
    """List core topics for a group.

    Args:
        db: aiosqlite database connection.
        group_id: Group identifier.
        is_active: If True, return only active topics.

    Returns:
        List of CoreTopicOut.
    """
    # A008: explicit initialization
    results: list[CoreTopicOut] = []

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        if is_active:
            cursor = await db.execute(
                "SELECT * FROM core_topics WHERE group_id = ? AND is_active = 1 ORDER BY priority, created_at",
                (group_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM core_topics WHERE group_id = ? ORDER BY priority, created_at",
                (group_id,),
            )
        rows = await cursor.fetchall()
        results = [CoreTopicOut.model_validate(dict(r)) for r in rows]
    except Exception:
        logger.exception("list_core_topics failed for group_id=%s", group_id)
        results = []
    finally:
        db.row_factory = original_factory

    return results


# ---------------------------------------------------------------------------
# Group CRUD (create, update, delete)
# ---------------------------------------------------------------------------


async def list_cipher_talk_sessions(
    db: aiosqlite.Connection,
    client: Any,
) -> CipherTalkSessionsResponse:
    """List real chatrooms from CipherTalk, flagging locally-registered ones.

    Powers the「新建群」picker so display_name comes from CipherTalk instead of
    manual entry. CipherTalk unreachable -> available=False + empty list
    (never raises -- caller falls back to manual entry).

    # P014: Never raise on upstream failure -- degrade to available=False.
    # P050: Parameterized SQL for the registered-set lookup.
    """
    # A008: explicit initialization
    try:
        sessions = await client.get_sessions()
    except Exception:
        logger.warning("list_cipher_talk_sessions: CipherTalk unreachable", exc_info=True)
        return CipherTalkSessionsResponse(sessions=[], available=False)

    # 优先用 sessionType 字段（weflow 有），回退到 @chatroom（ciphertalk 兼容）
    all_sessions = sessions
    rooms: list[dict[str, Any]] = []
    for s in sessions:
        username = s.get("username") or ""
        session_type = s.get("sessionType", "")
        if session_type == "group" or "@chatroom" in username:
            rooms.append(s)
    logger.info(
        "list_cipher_talk_sessions: %d total → %d chatrooms (sessionType/username filter)",
        len(all_sessions), len(rooms),
    )

    # Local registered chatroom_id set
    registered: set[str] = set()
    try:
        cursor = await db.execute("SELECT chatroom_id FROM groups")
        rows = await cursor.fetchall()
        registered = {row[0] for row in rows if row[0]}
    except Exception:
        logger.exception("list_cipher_talk_sessions: failed reading groups table")

    items = [
        CipherTalkSessionOut(
            chatroom_id=s.get("username", ""),
            display_name=(s.get("displayName") or s.get("username") or ""),
            is_registered=(s.get("username", "") in registered),
        )
        for s in rooms
    ]
    # Unregistered first, then alphabetical by display_name
    items.sort(key=lambda x: (x.is_registered, x.display_name))
    return CipherTalkSessionsResponse(sessions=items, available=True)


async def create_group(
    db: aiosqlite.Connection,
    body: Any,
) -> GroupOut:
    """Create a new group from a Pydantic model or dict.

    # P050: Parameterized SQL -- no string interpolation of user input.
    """
    import uuid
    from datetime import UTC, datetime

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    group_id = str(uuid.uuid4())

    data = body.model_dump() if hasattr(body, "model_dump") else dict(body)

    # Idempotent: chatroom_id has no unique constraint, so a repeat registration
    # (e.g. onboarding wizard re-save) would duplicate the row. Return the existing
    # group instead. P050: parameterized lookup.
    chatroom_id = data.get("chatroom_id", "")
    if chatroom_id:
        cursor = await db.execute(
            "SELECT group_id FROM groups WHERE chatroom_id = ?", (chatroom_id,)
        )
        row = await cursor.fetchone()
        if row:
            existing = await get_group_detail(db, row[0])
            if existing is not None:
                return existing

    await db.execute(
        """INSERT INTO groups
           (group_id, display_name, chatroom_id, output_dir, feishu_enabled,
            custom_prompt_hints, is_active,
            daily_report_enabled, daily_schedule_cron,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            group_id,
            data.get("display_name", ""),
            data.get("chatroom_id", ""),
            data.get("output_dir"),
            1 if data.get("feishu_enabled") else 0,
            data.get("custom_prompt_hints"),
            1 if data.get("is_active", True) else 0,
            1 if data.get("daily_report_enabled", True) else 0,
            data.get("daily_schedule_cron"),
            now,
            now,
        ),
    )
    await db.commit()

    result = await get_group_detail(db, group_id)
    if result is None:
        raise RuntimeError("Failed to read back created group")
    return result


async def update_group(
    db: aiosqlite.Connection,
    group_id: str,
    body: Any,
) -> GroupOut | None:
    """Update an existing group. Returns None if group not found.

    # P050: Parameterized SQL for all updates.
    """
    from datetime import UTC, datetime

    data = (
        body.model_dump(exclude_none=True)
        if hasattr(body, "model_dump")
        else {k: v for k, v in dict(body).items() if v is not None}
    )
    if not data:
        return await get_group_detail(db, group_id)

    # Convert boolean fields to int for SQLite
    bool_fields = {
        "feishu_enabled",
        "is_active",
        "daily_report_enabled",
        "feishu_framework_initialized",
        "feishu_engineering_enabled",
        "engineering_enabled",
    }
    for field in bool_fields:
        if field in data and isinstance(data[field], bool):
            data[field] = 1 if data[field] else 0

    # custom_tables is the source of truth for table enable/disable. When the
    # caller writes custom_tables, re-derive feishu_tables as a projection
    # (mirroring each kind's enabled flag) while preserving existing table_ids
    # fetched from the current row. Feishu push reads feishu_tables, so this
    # keeps it consistent without the UI having to touch feishu_tables.
    if "custom_tables" in data and isinstance(data["custom_tables"], dict):
        import json as _json

        from z_winnow.pipeline.feishu import schema as feishu_schema

        cur = await db.execute("SELECT feishu_tables FROM groups WHERE group_id = ?", (group_id,))
        row = await cur.fetchone()
        prev_ft: dict[str, Any] = {}
        if row and row[0]:
            try:
                loaded = _json.loads(row[0])
                if isinstance(loaded, dict):
                    prev_ft = loaded
            except (ValueError, TypeError):
                prev_ft = {}
        derived_ft: dict[str, Any] = {}
        for kind in feishu_schema.TABLE_CATALOG:
            ct_entry = data["custom_tables"].get(kind)
            enabled = (
                bool(ct_entry.get("enabled"))
                if isinstance(ct_entry, dict)
                else kind in feishu_schema.MANDATORY_KINDS
            )
            prev_entry = prev_ft.get(kind) if isinstance(prev_ft.get(kind), dict) else {}
            derived_ft[kind] = {"enabled": enabled, "table_id": prev_entry.get("table_id", "")}
        data["feishu_tables"] = derived_ft

    # Serialize feishu_tables dict → JSON TEXT for SQLite (#9.4).
    if "feishu_tables" in data and isinstance(data["feishu_tables"], dict):
        import json

        data["feishu_tables"] = json.dumps(data["feishu_tables"], ensure_ascii=False)

    # Serialize custom_tables dict → JSON TEXT for SQLite (#9.4).
    if "custom_tables" in data and isinstance(data["custom_tables"], dict):
        import json

        data["custom_tables"] = json.dumps(data["custom_tables"], ensure_ascii=False)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["updated_at"] = now

    set_clauses = ", ".join(f"{k} = ?" for k in data)
    values = [*data.values(), group_id]

    cursor = await db.execute(
        f"UPDATE groups SET {set_clauses} WHERE group_id = ?",
        tuple(values),
    )
    await db.commit()

    if cursor.rowcount == 0:
        return None
    return await get_group_detail(db, group_id)


def tables_config_from_group(group: GroupOut) -> dict[str, dict[str, Any]]:
    """Build the per-group tables blob. Prefer group.feishu_tables (dict); fall
    back to the legacy 4 columns + feishu_engineering_enabled for groups persisted
    before the blob column existed (#9.4 back-compat)."""
    if group.feishu_tables:
        return {k: dict(v) for k, v in group.feishu_tables.items() if isinstance(v, dict)}
    return {
        "summary": {"enabled": True, "table_id": group.feishu_table_summary or ""},
        "topics": {"enabled": True, "table_id": group.feishu_table_topics or ""},
        "resources": {"enabled": True, "table_id": group.feishu_table_resources or ""},
        "engineering": {
            "enabled": bool(group.feishu_engineering_enabled),
            "table_id": group.feishu_table_engineering or "",
        },
    }


def apply_enabled_kinds(
    tables_config: dict[str, dict[str, Any]], enabled_kinds: list[str] | None
) -> dict[str, dict[str, Any]]:
    """Override optional kinds' enabled flag from an explicit UI selection.

    Mandatory kinds stay enabled; unknown kinds (not in TABLE_CATALOG) are dropped.
    Preserves existing table_ids. Returns a fresh blob over the full catalog.
    """
    from z_winnow.pipeline.feishu import schema

    if enabled_kinds is None:
        return tables_config
    selected = {k for k in enabled_kinds if k in schema.TABLE_CATALOG}
    out: dict[str, dict[str, Any]] = {}
    for kind in schema.TABLE_CATALOG:
        prev = tables_config.get(kind) if isinstance(tables_config.get(kind), dict) else {}
        out[kind] = {
            "enabled": kind in schema.MANDATORY_KINDS or kind in selected,
            "table_id": prev.get("table_id", ""),
        }
    return out


def feishu_update_from_blob(
    tables_config: dict[str, dict[str, Any]], base_token: str
) -> GroupUpdate:
    """GroupUpdate persisting the blob + shadowing legacy columns (old frontend
    stays alive until #9.4 Phase 2 migrates it to the blob)."""
    from z_winnow.web.schemas.groups import GroupUpdate

    def _tid(k: str) -> str | None:
        return (tables_config.get(k) or {}).get("table_id") or None

    eng_cfg = tables_config.get("engineering") or {}
    return GroupUpdate(
        feishu_base_token=base_token,
        feishu_tables=tables_config,
        feishu_table_summary=_tid("summary"),
        feishu_table_topics=_tid("topics"),
        feishu_table_resources=_tid("resources"),
        feishu_table_engineering=_tid("engineering"),
        feishu_framework_initialized=True,
        feishu_engineering_enabled=bool(eng_cfg.get("enabled", False)),
    )


def get_feishu_table_catalog() -> list[dict[str, Any]]:
    """The global table catalog as a serializable list (UI checklist source).

    The catalog itself lives in ``pipeline.feishu.schema.TABLE_CATALOG``; this
    helper projects it to the fields the frontend needs (kind / display_name /
    mandatory / default_enabled / field_count).
    """
    from z_winnow.pipeline.feishu import schema

    return [
        {
            "kind": kind,
            "display_name": tdef.display_name,
            "mandatory": tdef.mandatory,
            "default_enabled": kind in schema.DEFAULT_ENABLED_KINDS,
            "field_count": len(tdef.fields),
        }
        for kind, tdef in schema.TABLE_CATALOG.items()
    ]


async def init_group_feishu_framework(
    db: aiosqlite.Connection,
    group_id: str,
    base_target: str | None = None,
    enabled_kinds: list[str] | None = None,
) -> GroupOut:
    """Initialize a group's Feishu Bitable framework (create Base + data tables).

    Resolves ``base_target`` (URL → token via url-resolve, raw token as-is, empty →
    create new), calls :func:`uploader.ensure_framework`, and persists the resolved
    base_token + per-table IDs + framework_initialized back to the group row.

    When ``enabled_kinds`` is not provided (e.g. called from onboarding), derives it
    from ``group.engineering_enabled``: if disabled, engineering is excluded.

    Args:
        db: aiosqlite connection.
        group_id: Group to initialize.
        base_target: Base share URL, app_token, or empty (auto-create new).

    Returns:
        Updated GroupOut.

    Raises:
        RuntimeError: if the group doesn't exist or framework init fails.
    """
    from z_winnow.pipeline.feishu import schema, uploader

    group = await get_group_detail(db, group_id)
    if not group:
        raise RuntimeError(f"group {group_id} not found")

    # Build the per-group blob; apply UI selection or derive from group config.
    tables_config = tables_config_from_group(group)
    if enabled_kinds is not None:
        tables_config = apply_enabled_kinds(tables_config, enabled_kinds)
    else:
        # Derive from group.engineering_enabled (independent toggle).
        derived: list[str] = list(schema.MANDATORY_KINDS)
        if bool(group.engineering_enabled):
            derived.append("engineering")
        tables_config = apply_enabled_kinds(tables_config, derived)

    # Resolve the base target to a clean app_token (empty ⇒ ensure_framework creates new).
    target = (base_target or "").strip()
    base_token = group.feishu_base_token or ""
    if target:
        base_token = await _resolve_base_target(target)

    fw = await uploader.ensure_framework(
        base_name=group.display_name or group_id,
        base_token=base_token,
        tables_config=tables_config,
    )
    if fw["status"] == "failed":
        raise RuntimeError(fw["reason"])

    await update_group(db, group_id, feishu_update_from_blob(fw["tables_config"], fw["base_token"]))
    result = await get_group_detail(db, group_id)
    assert result is not None  # update_group just confirmed the row exists
    return result


async def _resolve_base_target(target: str) -> str:
    """Resolve a Base share URL or raw token to a clean app_token.

    URLs (feishu.cn / larkoffice.com / http…) go through ``lark-cli base +url-resolve``;
    anything else is treated as a raw app_token and returned verbatim.
    """
    from z_winnow.pipeline.feishu import lark_cli

    looks_like_url = (
        target.startswith("http")
        or "feishu.cn" in target
        or "larkoffice" in target
        or "larksuite" in target
    )
    if not looks_like_url:
        return target
    res = await lark_cli.url_resolve(target)
    for path in (("data", "base_token"), ("data", "base", "base_token"), ("data", "app_token")):
        cur: object = res
        ok = True
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                ok = False
                break
            cur = cur[key]
        if ok and isinstance(cur, str) and cur:
            return cur
    raise RuntimeError(f"could not extract base_token from url-resolve for {target}")


async def purge_group_local_data(db: aiosqlite.Connection, group_id: str) -> dict[str, int]:
    """Delete a group's local data orphans (tables without FK CASCADE) + disk L3.

    Cleans: topic_summaries, raw_messages, parsed_contexts, report_versions,
    feedback_events, pipeline_runs (by group_id); memos_sync_queue (by cube_id,
    both canonical colon + legacy hyphen patterns); and the on-disk
    ``data/processed/{group_id}/`` L3 JSON. Returns per-resource delete counts.
    """
    counts: dict[str, int] = {}
    # group_id-keyed tables (no FK CASCADE → otherwise orphaned)
    tables = (
        "topic_summaries",
        "raw_messages",
        "parsed_contexts",
        "report_versions",
        "feedback_events",
        "pipeline_runs",
    )
    for table in tables:
        cur = await db.execute(f"DELETE FROM {table} WHERE group_id = ?", (group_id,))
        counts[table] = cur.rowcount or 0
    # memos_sync_queue keyed by cube_id — match both naming conventions
    cur = await db.execute(
        "DELETE FROM memos_sync_queue WHERE cube_id LIKE ? OR cube_id LIKE ?",
        (f"winnow:{group_id}:%", f"winnow-{group_id}-%"),
    )
    counts["memos_sync_queue"] = cur.rowcount or 0
    await db.commit()

    # Disk L3 JSON
    removed_dir = False
    try:
        from z_winnow.config.settings import get_settings

        processed_root = Path(get_settings().db_path).parent / "processed" / group_id
        if processed_root.exists() and processed_root.is_dir():
            shutil.rmtree(processed_root)
            removed_dir = True
    except Exception:
        logger.exception("purge_group_local_data: rmtree failed for %s", group_id)
    counts["disk_l3_removed"] = 1 if removed_dir else 0
    return counts


async def delete_group(
    db: aiosqlite.Connection,
    group_id: str,
    adapter: MemOSAdapterProtocol | None = None,
) -> bool:
    """Delete a group by ID + cascade-clean its data. Returns True if deleted.

    Scenario ①: deleting a group clears its local data orphans + disk L3 +
    MemOS memories (best-effort — MemOS failure does NOT block deletion).
    FK CASCADE still handles group_members / core_topics.
    """
    # Local cleanup — always (fast, safe, fixes the existing orphan bug)
    try:
        local_counts = await purge_group_local_data(db, group_id)
        logger.info("delete_group %s: local purge %s", group_id, local_counts)
    except Exception:
        logger.exception("delete_group %s: local purge failed", group_id)

    # MemOS cleanup — best-effort, must not block group deletion
    if adapter is not None:
        try:
            from z_winnow.web.services.memos_service import purge_group_memories

            memos_res = await purge_group_memories(adapter, group_id)
            logger.info("delete_group %s: memos purge %s", group_id, memos_res)
        except Exception:
            logger.exception("delete_group %s: memos purge failed (non-blocking)", group_id)

    # Finally remove the groups row (FK CASCADE → group_members, core_topics)
    cursor = await db.execute(
        "DELETE FROM groups WHERE group_id = ?",
        (group_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_group_ids(db: aiosqlite.Connection) -> list[str]:
    """Return all registered group_ids (for wipe-all enumeration)."""
    cur = await db.execute("SELECT group_id FROM groups")
    rows = await cur.fetchall()
    return [r[0] for r in rows if r and r[0]]


# ---------------------------------------------------------------------------
# Core topic CRUD (create, update, delete)
# ---------------------------------------------------------------------------


async def create_core_topic(
    db: aiosqlite.Connection,
    body: Any,
) -> CoreTopicOut:
    """Create a new core topic from a Pydantic model or dict.

    # P050: Parameterized SQL.
    """
    import uuid
    from datetime import UTC, datetime

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    core_topic_id = str(uuid.uuid4())

    data = body.model_dump() if hasattr(body, "model_dump") else dict(body)

    await db.execute(
        """INSERT INTO core_topics
           (core_topic_id, group_id, name, description, keywords,
            priority, is_active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            core_topic_id,
            data.get("group_id", ""),
            data.get("name", ""),
            data.get("description"),
            data.get("keywords"),
            data.get("priority", 1),
            1 if data.get("is_active", True) else 0,
            now,
            now,
        ),
    )
    await db.commit()

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            "SELECT * FROM core_topics WHERE core_topic_id = ?",
            (core_topic_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Failed to read back created core topic")
        return CoreTopicOut.model_validate(dict(row))
    finally:
        db.row_factory = original_factory


async def update_core_topic(
    db: aiosqlite.Connection,
    topic_id: str,
    body: Any,
) -> CoreTopicOut | None:
    """Update an existing core topic. Returns None if not found.

    # P050: Parameterized SQL.
    """
    from datetime import UTC, datetime

    data = (
        body.model_dump(exclude_none=True)
        if hasattr(body, "model_dump")
        else {k: v for k, v in dict(body).items() if v is not None}
    )
    if not data:
        # Just return current
        original_factory = db.row_factory
        db.row_factory = aiosqlite.Row
        try:
            cursor = await db.execute(
                "SELECT * FROM core_topics WHERE core_topic_id = ?",
                (topic_id,),
            )
            row = await cursor.fetchone()
            return CoreTopicOut.model_validate(dict(row)) if row else None
        finally:
            db.row_factory = original_factory

    # Convert boolean fields
    if "is_active" in data and isinstance(data["is_active"], bool):
        data["is_active"] = 1 if data["is_active"] else 0

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["updated_at"] = now

    set_clauses = ", ".join(f"{k} = ?" for k in data)
    values = [*data.values(), topic_id]

    cursor = await db.execute(
        f"UPDATE core_topics SET {set_clauses} WHERE core_topic_id = ?",
        tuple(values),
    )
    await db.commit()

    if cursor.rowcount == 0:
        return None

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            "SELECT * FROM core_topics WHERE core_topic_id = ?",
            (topic_id,),
        )
        row = await cursor.fetchone()
        return CoreTopicOut.model_validate(dict(row)) if row else None
    finally:
        db.row_factory = original_factory


async def delete_core_topic(
    db: aiosqlite.Connection,
    topic_id: str,
) -> bool:
    """Delete a core topic by ID. Returns True if deleted."""
    cursor = await db.execute(
        "DELETE FROM core_topics WHERE core_topic_id = ?",
        (topic_id,),
    )
    await db.commit()
    return cursor.rowcount > 0
