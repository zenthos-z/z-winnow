"""T-W14-4 / W15-P2-MEMOS: MemOS adapter service with graceful degradation.

Wraps ``MemOSAdapterProtocol`` calls into service functions that follow
P082 asymmetric fault tolerance:

  - **Read methods** (search, get_all, list_cubes, get_cube_detail,
    get_memory_detail) **propagate** exceptions to the caller.
  - **Write methods** (add, delete, delete_cube, rebuild, vacuum, flush)
    **catch** ``httpx.ConnectError`` and return degraded status values
    (None / False / empty).
  - ``health_check`` catches ``httpx.ConnectError`` and returns
    ``{"status": "degraded", "error": "..."}``.

All functions accept a ``MemOSAdapterProtocol`` instance — no module-level
adapter resolution.

Patterns applied:
  P082: Read-write asymmetric fault tolerance
  P050: Strategy V Fabrication-Proof CRUD — reuses existing adapter
  P079: Two-step confirm gate for cube deletion
  P067: Async task queue for rebuild/vacuum/flush
  A008: All data variables initialized before try blocks
  L018: Explicit degraded status dicts, not silent empty results
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from typing import Any

import aiosqlite
import httpx

from z_winnow.memory.types import (
    MemOSAdapterProtocol,
    StructuredMemoryItem,
    TextualMemoryMetadata,
)

logger = logging.getLogger(__name__)

# Module-level sync worker handle (managed by start/stop)
_sync_worker_task: asyncio.Task | None = None

# M4: cube scope 真源 — 固定 cube（每份日报必有）+ registry 自定义表 cube（按群开关激活）。
# 加新自定义表 = 加 YAML，cube 自动出现在 list/purge/rebuild，无需改本模块。
FIXED_CUBE_SCOPES: tuple[str, ...] = ("topics", "resources", "daily")

# Legacy scope retained during M4 transition: feedback_sync (P1) 仍写已废弃的 feedback
# cube，直到 P2 把纠正改走 feedback_memory → 内容 cube。期间保留它可被 purge/list，
# 以清理陈旧反馈数据。P2 完成后移除。
_LEGACY_CUBE_SCOPES: tuple[str, ...] = ("feedback",)


def all_known_cube_scopes() -> list[str]:
    """固定 cube + 所有已注册自定义表 kind + 过渡期 legacy cube。

    供 list/purge/rebuild 等"无 per-group config 上下文"的运维操作用——
    枚举所有可能的 cube（含群未启用的表，对应空 cube，purge 时 0 删除，无害）。
    """
    scopes = list(FIXED_CUBE_SCOPES) + list(_LEGACY_CUBE_SCOPES)
    try:
        from z_winnow.custom_tables import registry as ct_registry

        for table in ct_registry.get_all_tables():
            if table.id not in scopes:
                scopes.append(table.id)
    except Exception:
        logger.debug(
            "all_known_cube_scopes: registry unavailable — fixed scopes only", exc_info=True
        )
    return scopes


def cube_scopes_for_group(custom_tables_config: dict[str, Any] | None) -> list[str]:
    """固定 cube + 该群激活的自定义表 kind（精确 per-group 激活集）。

    供需要 per-group 精度的写入/查询路径用。
    """
    scopes = list(FIXED_CUBE_SCOPES)
    try:
        from z_winnow.custom_tables import registry as ct_registry

        for table in ct_registry.get_all_tables():
            cfg = (custom_tables_config or {}).get(table.id)
            if isinstance(cfg, dict) and cfg.get("enabled") and table.id not in scopes:
                scopes.append(table.id)
    except Exception:
        logger.debug(
            "cube_scopes_for_group: registry unavailable — fixed scopes only", exc_info=True
        )
    return scopes


# ---------------------------------------------------------------------------
# Read methods — propagate exceptions (P082)
# ---------------------------------------------------------------------------


async def search_memos(
    adapter: MemOSAdapterProtocol,
    cube: str,
    query: str,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Search memos in a cube. Propagates httpx.ConnectError (P082).

    Args:
        adapter: MemOS adapter instance.
        cube: MemCube ID.
        query: Search query.
        top_k: Maximum results.

    Returns:
        List of memory result dicts.

    Raises:
        httpx.ConnectError: If the MemOS backend is unreachable.
    """
    # Read method — exceptions propagate (P082)
    results = await adapter.search_memories(
        query=query,
        group_id="",
        readable_cube_ids=[cube],
        top_k=top_k,
    )
    # Convert MemoryResult dataclasses to dicts
    return [
        {"id": r.id, "memory": r.memory, "metadata": r.metadata, "score": r.score} for r in results
    ]


async def get_all_memos(
    adapter: MemOSAdapterProtocol,
    cube: str,
) -> list[dict[str, Any]]:
    """Get all memos from a cube. Propagates httpx.ConnectError (P082).

    Args:
        adapter: MemOS adapter instance.
        cube: MemCube ID.

    Returns:
        List of memory dicts.

    Raises:
        httpx.ConnectError: If the MemOS backend is unreachable.
    """
    # Read method — exceptions propagate (P082)
    response = await adapter.get_all_memories(cube_id=cube, group_id="")
    # Response structure: {"text_mem": [...], "act_mem": [...], "para_mem": [...]}
    all_items: list[dict[str, Any]] = []
    for key in ("text_mem", "act_mem", "para_mem"):
        items = response.get(key, [])
        if isinstance(items, list):
            all_items.extend(items)
    return all_items


# ---------------------------------------------------------------------------
# Write methods — catch httpx.ConnectError (P082)
# ---------------------------------------------------------------------------


async def add_memo(
    adapter: MemOSAdapterProtocol,
    cube: str,
    content: str,
    metadata: dict | None = None,
) -> str | None:
    """Add a memo to a cube. Returns None on ConnectError (P082).

    Args:
        adapter: MemOS adapter instance.
        cube: MemCube ID.
        content: Memory text content.
        metadata: Optional metadata dict.

    Returns:
        Memory ID string on success, None on connection failure.
    """
    # A008
    result: str | None = None
    try:
        item = StructuredMemoryItem(memory=content, metadata=metadata or {})
        response = await adapter.add_structured_memory(
            cube_id=cube,
            group_id="",
            items=[item],
        )
        # Extract ID from response if available
        if isinstance(response, dict):
            result = response.get("id") or response.get("memory_id")
    except httpx.ConnectError as exc:
        logger.warning("memos_service.add_memo degraded: %s", exc)
        return None
    except Exception:
        logger.exception("memos_service.add_memo unexpected error")
        return None
    return result


async def delete_memo(
    adapter: MemOSAdapterProtocol,
    cube: str,
    memory_id: str,
) -> bool:
    """Delete a memo from a cube. Returns False on ConnectError (P082).

    Args:
        adapter: MemOS adapter instance.
        cube: MemCube ID.
        memory_id: Memory ID to delete.

    Returns:
        True on success, False on failure or connection error.
    """
    # A008
    result: bool = False
    try:
        result = await adapter.delete_memory(
            cube_id=cube,
            group_id="",
            memory_ids=[memory_id],
        )
    except httpx.ConnectError as exc:
        logger.warning("memos_service.delete_memo degraded: %s", exc)
        return False
    except Exception:
        logger.exception("memos_service.delete_memo unexpected error")
        return False
    return bool(result)


# ---------------------------------------------------------------------------
# W15-P2-MEMOS: 8 new service functions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cube management — read methods (P082: propagate exceptions)
# ---------------------------------------------------------------------------


async def list_cubes(
    adapter: MemOSAdapterProtocol,
    group_id: str,
) -> list[dict[str, Any]]:
    """List all memory cubes for a group. Propagates httpx.ConnectError (P082).

    Enumerates known cube scopes ({group}:topics, {group}:daily,
    {group}:feedback), resolves each scope to a cube_id via the adapter,
    and queries memory count via get_all_memories.

    P082: Read method — exceptions propagate to caller (route → 502).

    Args:
        adapter: MemOS adapter instance.
        group_id: Group identifier for scope filtering.

    Returns:
        List of cube dicts with keys: cube_id, group_id, scope, memory_count, status.

    Raises:
        httpx.ConnectError: If the MemOS backend is unreachable.
    """
    # A008: initialize result variable before try
    cubes: list[dict[str, Any]] = []
    for scope in all_known_cube_scopes():
        scope_name = f"{group_id}:{scope}"
        try:
            cube_id = await adapter.get_or_create_cube(scope_name)
        except httpx.ConnectError:
            # P082: Read method — propagate
            raise
        except Exception:
            logger.warning(
                "list_cubes: get_or_create_cube failed for scope=%s", scope_name, exc_info=True
            )
            continue

        # P082: get_all_memories is a read — propagate errors
        all_memories = await adapter.get_all_memories(cube_id=cube_id, group_id="")
        memory_count = 0
        for key in ("text_mem", "act_mem", "para_mem"):
            items = all_memories.get(key, [])
            if isinstance(items, list):
                memory_count += len(items)

        cubes.append(
            {
                "cube_id": cube_id,
                "group_id": group_id,
                "date": datetime.date.today().isoformat(),
                "scope": scope,
                "memory_count": memory_count,
                "status": "active",
                "created_at": None,
            }
        )
    return cubes


async def get_cube_detail(
    adapter: MemOSAdapterProtocol,
    cube_id: str,
) -> dict[str, Any] | None:
    """Get detail for a specific memory cube. Propagates httpx.ConnectError (P082).

    P082: Read method — exceptions propagate.

    Args:
        adapter: MemOS adapter instance.
        cube_id: MemCube ID to query.

    Returns:
        Cube detail dict (cube_id, memory_count, status) or None if not found.

    Raises:
        httpx.ConnectError: If the MemOS backend is unreachable.
    """
    # A008
    cube_detail: dict[str, Any] | None = None
    # P082: get_all_memories is a read — let exceptions propagate
    all_memories = await adapter.get_all_memories(cube_id=cube_id, group_id="")
    memory_count = 0
    for key in ("text_mem", "act_mem", "para_mem"):
        items = all_memories.get(key, [])
        if isinstance(items, list):
            memory_count += len(items)

    # Derive group_id from cube_id pattern (scope format: {group}:{type})
    # For deterministic UUIDs, we can't reverse them — use empty string as fallback
    cube_detail = {
        "cube_id": cube_id,
        "group_id": "",
        "date": datetime.date.today().isoformat(),
        "memory_count": memory_count,
        "status": "active",
        "created_at": None,
    }
    return cube_detail


# ---------------------------------------------------------------------------
# Cube deletion — write method (P082: catch errors, degrade)
# P079: Two-step confirm gate enforced at route layer
# ---------------------------------------------------------------------------


async def delete_cube(
    adapter: MemOSAdapterProtocol,
    cube_id: str,
) -> bool:
    """Delete a memory cube and all its contents (robust purge).

    P079: Confirm gate enforced at route layer.
    Refactored to delegate to :func:`purge_cube_memories` (F1/F2 fixes:
    cross-memory-type ID collection, real group_id, verify-after).

    Args:
        adapter: MemOS adapter instance.
        cube_id: MemCube ID to delete.

    Returns:
        True when verified empty (or cube was already empty), False otherwise.
    """
    try:
        res = await purge_cube_memories(adapter, cube_id, group_id=_group_id_from_cube(cube_id))
        return bool(res.get("ok"))
    except httpx.ConnectError as exc:
        logger.warning("memos_service.delete_cube degraded: %s", exc)
        return False
    except Exception:
        logger.exception("memos_service.delete_cube unexpected error")
        return False


# ---------------------------------------------------------------------------
# Memory purge — robust list-then-delete with cross-memory-type collection
# and verify-after. Powers group-deletion cascade (scenario ①) and
# wipe-all (scenario ②). No native MemOS drop exists, so this is the
# complete-removal primitive.
# ---------------------------------------------------------------------------


def _group_id_from_cube(cube_id: str) -> str:
    """Best-effort extract group_id from canonical cube_id ``winnow:{gid}:{scope}``."""
    parts = cube_id.split(":")
    if len(parts) >= 3 and parts[0] == "winnow":
        return parts[1]
    return ""


def _collect_memory_ids(all_memories: dict[str, Any]) -> list[str]:
    """Flatten a get_all_memories result into a de-duplicated list of IDs."""
    ids: list[str] = []
    for key in ("text_mem", "act_mem", "para_mem"):
        items = all_memories.get(key, [])
        if isinstance(items, list):
            for item in items:
                item_id = item.get("id", "") if isinstance(item, dict) else ""
                if item_id:
                    ids.append(item_id)
    return ids


async def _collect_all_memory_ids(
    adapter: MemOSAdapterProtocol,
    cube_id: str,
    group_id: str,
) -> list[str]:
    """Collect ALL memory IDs in a cube across memory types.

    F1: ``get_all_memories`` defaults to ``memory_type=text_mem`` and so misses
    feedback / UserMemory nodes. Query both text_mem (default) and UserMemory
    (via the ``filters`` override the adapter already supports) and union the
    IDs — robust against MemOS server-side filter behaviour.
    """
    seen: set[str] = set()
    ids: list[str] = []
    # 1) default call → text_mem
    try:
        data = await adapter.get_all_memories(cube_id=cube_id, group_id=group_id)
        for mid in _collect_memory_ids(data):
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError):
        raise
    except Exception:
        logger.exception("_collect_all_memory_ids: text_mem fetch failed cube=%s", cube_id)
    # 2) UserMemory (feedback nodes)
    try:
        data = await adapter.get_all_memories(
            cube_id=cube_id,
            group_id=group_id,
            filters={"memory_type": "UserMemory"},
        )
        for mid in _collect_memory_ids(data):
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError):
        raise
    except Exception:
        logger.exception("_collect_all_memory_ids: UserMemory fetch failed cube=%s", cube_id)
    return ids


async def purge_cube_memories(
    adapter: MemOSAdapterProtocol,
    cube_id: str,
    group_id: str = "",
) -> dict[str, Any]:
    """Purge ALL memories from a single cube. Robust list-then-delete + verify.

    Collects IDs across text_mem + UserMemory, deletes, then re-fetches to
    confirm empty (retries the delete once if remnants remain — MemOS deletion
    can be processed asynchronously).

    Returns:
        ``{cube_id, removed, verified_empty, ok}`` — ``ok`` is True only when
        the cube is verified empty afterwards.
    """
    result: dict[str, Any] = {
        "cube_id": cube_id,
        "removed": 0,
        "verified_empty": False,
        "ok": False,
    }
    try:
        ids = await _collect_all_memory_ids(adapter, cube_id, group_id)
        if not ids:
            result["verified_empty"] = True
            result["ok"] = True
            return result

        if not await adapter.delete_memory(cube_id=cube_id, group_id=group_id, memory_ids=ids):
            return result  # ok=False
        result["removed"] = len(ids)

        # Verify empty; retry delete once for any remnants
        for attempt in (1, 2):
            remaining = await _collect_all_memory_ids(adapter, cube_id, group_id)
            if not remaining:
                result["verified_empty"] = True
                result["ok"] = True
                return result
            if attempt == 1:
                await adapter.delete_memory(
                    cube_id=cube_id, group_id=group_id, memory_ids=remaining
                )
        return result  # still non-empty after retry → partial, ok=False
    except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
        logger.warning("purge_cube_memories network error cube=%s: %s", cube_id, exc)
        return result
    except Exception:
        logger.exception("purge_cube_memories unexpected error cube=%s", cube_id)
        return result


async def purge_group_memories(
    adapter: MemOSAdapterProtocol,
    group_id: str,
) -> dict[str, Any]:
    """Purge ALL cubes for a group (topics/daily/feedback). Best-effort.

    Targets the canonical cube ids ``winnow:{group_id}:{scope}`` — the same
    literal ids the graph builder writes memories under (not via
    ``get_or_create_cube``, whose mock implementation diverges).

    Returns:
        ``{group_id, cubes: [...], total_removed, all_ok}``.
    """
    cubes: list[dict[str, Any]] = []
    total_removed = 0
    all_ok = True
    for scope in all_known_cube_scopes():
        cube_id = f"winnow:{group_id}:{scope}"
        res = await purge_cube_memories(adapter, cube_id, group_id=group_id)
        cubes.append(res)
        total_removed += res.get("removed", 0)
        if not res.get("ok"):
            all_ok = False
    return {"group_id": group_id, "cubes": cubes, "total_removed": total_removed, "all_ok": all_ok}


async def wipe_all_memories(
    adapter: MemOSAdapterProtocol,
    group_ids: list[str],
) -> dict[str, Any]:
    """Purge memories for every group. Best-effort aggregate.

    Returns:
        ``{groups, cubes: [...], total_removed, all_ok}``.
    """
    all_cubes: list[dict[str, Any]] = []
    total_removed = 0
    all_ok = True
    for gid in group_ids:
        res = await purge_group_memories(adapter, gid)
        all_cubes.extend(res.get("cubes", []))
        total_removed += res.get("total_removed", 0)
        if not res.get("all_ok"):
            all_ok = False
    return {
        "groups": len(group_ids),
        "cubes": all_cubes,
        "total_removed": total_removed,
        "all_ok": all_ok,
    }


# ---------------------------------------------------------------------------
# Rebuild — write method (P082: catch errors)
# R2-P1-5: Logic extracted from CLI _cmd_memos_rebuild
# ---------------------------------------------------------------------------


async def rebuild_memos_cube(
    adapter: MemOSAdapterProtocol,
    cube_id: str,
    group_id: str,
    db_path: str,
) -> dict[str, Any]:
    """Rebuild a memory cube from SQLite data. Degrades gracefully (P082).

    P050: Reuses adapter.add_structured_memory (existing interface).
    R2-P1-5: Logic extracted from CLI _cmd_memos_rebuild — reads topic_summaries
    and report_versions from SQLite, converts to StructuredMemoryItem,
    batch-writes to MemOS.

    P082: Write method — catch errors, return degraded status.

    Args:
        adapter: MemOS adapter instance.
        cube_id: Target MemCube ID.
        group_id: Group identifier for SQLite queries.
        db_path: SQLite database path.

    Returns:
        Dict with status, sqlite_record_count, total_written.
    """
    # A008
    result: dict[str, Any] = {
        "status": "degraded",
        "cube_id": cube_id,
        "group_id": group_id,
        "sqlite_record_count": 0,
        "total_written": 0,
    }
    items: list[StructuredMemoryItem] = []
    sqlite_record_count = 0

    try:
        # Read from SQLite — extracted from CLI _cmd_memos_rebuild
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row

            # Read topic_summaries for the group
            cursor = await db.execute(
                "SELECT * FROM topic_summaries WHERE group_id = ? ORDER BY date",
                (group_id,),
            )
            topic_rows = await cursor.fetchall()
            for row in topic_rows:
                row_dict = dict(row)
                name = row_dict.get("topic_name", "")
                desc = row_dict.get("summary_text", "") or ""
                items.append(
                    StructuredMemoryItem(
                        memory=desc or name,
                        metadata=TextualMemoryMetadata(
                            type="topic",
                            source=f"sqlite:topic_summaries:{row_dict.get('date', '')}",
                            confidence=80.0,
                            entities=[],
                            tags=[group_id, name, "topic_summary"],
                            visibility="private",
                            memory_time=row_dict.get("date", ""),
                            updated_at=datetime.datetime.now().isoformat(),
                        ),
                    )
                )
                sqlite_record_count += 1

            # Read report_versions for the group
            cursor = await db.execute(
                "SELECT * FROM report_versions WHERE group_id = ? ORDER BY date",
                (group_id,),
            )
            rv_rows = await cursor.fetchall()
            for row in rv_rows:
                row_dict = dict(row)
                content = row_dict.get("content", "") or ""
                items.append(
                    StructuredMemoryItem(
                        memory=content[:2000],
                        metadata=TextualMemoryMetadata(
                            type="topic",
                            source=f"sqlite:report_versions:{row_dict.get('date', '')}",
                            confidence=80.0,
                            entities=[],
                            tags=[group_id, "report"],
                            visibility="private",
                            memory_time=row_dict.get("date", ""),
                            updated_at=datetime.datetime.now().isoformat(),
                        ),
                    )
                )
                sqlite_record_count += 1

            # Also read core_topics
            cursor = await db.execute(
                "SELECT * FROM core_topics WHERE group_id = ? ORDER BY last_matched_date",
                (group_id,),
            )
            ct_rows = await cursor.fetchall()
            for row in ct_rows:
                row_dict = dict(row)
                name = row_dict.get("name", "")
                desc = row_dict.get("description", "") or ""
                # Only include if is_active
                if row_dict.get("is_active", 1):
                    items.append(
                        StructuredMemoryItem(
                            memory=desc or name,
                            metadata=TextualMemoryMetadata(
                                type="topic",
                                source=f"sqlite:core_topics:{row_dict.get('last_matched_date', '')}",
                                confidence=80.0,
                                entities=[],
                                tags=[group_id, name, "core_topic"],
                                visibility="private",
                                memory_time=row_dict.get("last_matched_date", ""),
                                updated_at=datetime.datetime.now().isoformat(),
                            ),
                        )
                    )
                    sqlite_record_count += 1

    except Exception as exc:
        logger.exception("rebuild_memos_cube: SQLite read failed")
        result["status"] = "degraded"
        result["error"] = f"SQLite read failed: {exc}"
        return result

    result["sqlite_record_count"] = sqlite_record_count

    # Batch write to MemOS (50 per batch) — from CLI _cmd_memos_rebuild
    batch_size = 50
    total_written = 0
    try:
        for i in range(0, len(items), batch_size):
            batch = items[i : i + batch_size]
            try:
                write_result = await adapter.add_structured_memory(
                    cube_id=cube_id,
                    group_id=group_id,
                    items=batch,
                )
                stored = write_result.get("data", {}).get("stored", len(batch))
                total_written += stored
            except httpx.ConnectError:
                logger.warning(
                    "rebuild_memos_cube: batch %d degraded (connect error)", i // batch_size + 1
                )
                # P082: degraded — continue with next batch
            except Exception:
                logger.exception("rebuild_memos_cube: batch %d failed", i // batch_size + 1)

        result["status"] = "completed"
        result["total_written"] = total_written
    except Exception as exc:
        logger.exception("rebuild_memos_cube: MemOS write failed")
        result["status"] = "degraded"
        result["error"] = f"MemOS write failed: {exc}"
        result["total_written"] = total_written

    return result


# ---------------------------------------------------------------------------
# Vacuum — write method (P082: catch errors)
# ---------------------------------------------------------------------------


async def vacuum_cube(
    adapter: MemOSAdapterProtocol,
    cube_id: str,
    group_id: str,
) -> dict[str, Any]:
    """Vacuum a memory cube — lifecycle scan with cleanup. Degrades gracefully (P082).

    Scans all memories in the cube and applies lifecycle rules:
    - confidence < 20 and status="activated" → archive (mark status="archived")
    - status="archived" older than 30 days → delete

    P082: Write method — catch errors, return degraded status.

    Args:
        adapter: MemOS adapter instance.
        cube_id: MemCube ID to vacuum.
        group_id: Group identifier.

    Returns:
        LifecycleReportOut dict with archived_count, deleted_count, scanned_count.
    """
    # A008
    result: dict[str, Any] = {
        "status": "degraded",
        "cube_id": cube_id,
        "scanned_count": 0,
        "archived_count": 0,
        "deleted_count": 0,
    }
    try:
        all_memories = await adapter.get_all_memories(cube_id=cube_id, group_id=group_id)
        scanned = 0
        archived = 0
        deleted = 0

        now = datetime.datetime.now(datetime.UTC)
        archive_threshold_days = 30

        for key in ("text_mem", "act_mem", "para_mem"):
            items = all_memories.get(key, [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                scanned += 1
                item_id = item.get("id", "")
                meta = item.get("metadata", {})
                confidence = meta.get("confidence", 50.0)
                status = meta.get("status", "activated")
                created_at = item.get("created_at", "")

                # Rule 1: low confidence + activated → archive
                if confidence < 20 and status == "activated":
                    try:
                        # Delete and re-add with archived status
                        await adapter.delete_memory(
                            cube_id=cube_id, group_id=group_id, memory_ids=[item_id]
                        )
                        archived_item = StructuredMemoryItem(
                            memory=item.get("memory", ""),
                            metadata={**meta, "status": "archived"},
                        )
                        await adapter.add_structured_memory(
                            cube_id=cube_id, group_id=group_id, items=[archived_item]
                        )
                        archived += 1
                    except Exception:
                        logger.warning("vacuum_cube: failed to archive memory %s", item_id)

                # Rule 2: archived older than 30 days → delete
                elif status == "archived" and created_at:
                    try:
                        # Parse created_at timestamp
                        if isinstance(created_at, int | float):
                            created_dt = datetime.datetime.fromtimestamp(
                                created_at, tz=datetime.UTC
                            )
                        else:
                            created_dt = datetime.datetime.fromisoformat(
                                str(created_at).replace("Z", "+00:00")
                            )
                        age_days = (now - created_dt).days
                        if age_days > archive_threshold_days:
                            await adapter.delete_memory(
                                cube_id=cube_id, group_id=group_id, memory_ids=[item_id]
                            )
                            deleted += 1
                    except (ValueError, TypeError):
                        logger.debug("vacuum_cube: could not parse created_at for %s", item_id)

        result["status"] = "completed"
        result["scanned_count"] = scanned
        result["archived_count"] = archived
        result["deleted_count"] = deleted
    except httpx.ConnectError as exc:
        logger.warning("memos_service.vacuum_cube degraded: %s", exc)
        result["status"] = "degraded"
        result["error"] = str(exc)
    except Exception:
        logger.exception("memos_service.vacuum_cube unexpected error")
        result["status"] = "degraded"

    return result


# ---------------------------------------------------------------------------
# Memory detail / delete — read propagates, write degrades (P082)
# ---------------------------------------------------------------------------


async def get_memory_detail(
    adapter: MemOSAdapterProtocol,
    memory_id: str,
    cube_id: str = "default",
) -> dict[str, Any] | None:
    """Get detail for a specific memory. Propagates httpx.ConnectError (P082).

    Searches through all memories in the cube for a matching ID.

    P082: Read method — exceptions propagate.

    Args:
        adapter: MemOS adapter instance.
        memory_id: Memory ID to look up.
        cube_id: Cube to search in.

    Returns:
        Memory detail dict or None if not found.

    Raises:
        httpx.ConnectError: If the MemOS backend is unreachable.
    """
    # A008
    memory_detail: dict[str, Any] | None = None
    # P082: get_all_memories is a read — let exceptions propagate
    all_memories = await adapter.get_all_memories(cube_id=cube_id, group_id="")
    for key in ("text_mem", "act_mem", "para_mem"):
        items = all_memories.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("id", "") == memory_id:
                memory_detail = {
                    "memory_id": memory_id,
                    "cube_id": cube_id,
                    "content": item.get("memory", ""),
                    "metadata": item.get("metadata", {}),
                    "created_at": item.get("created_at"),
                    "source": item.get("metadata", {}).get("source"),
                }
                return memory_detail
    return None


async def delete_memory_by_id(
    adapter: MemOSAdapterProtocol,
    memory_id: str,
    cube_id: str = "default",
) -> bool:
    """Delete a specific memory by ID. Degrades gracefully (P082).

    P082: Write method — catch errors, return False.

    Args:
        adapter: MemOS adapter instance.
        memory_id: Memory ID to delete.
        cube_id: Cube to delete from.

    Returns:
        True on success, False on failure or not found.
    """
    # A008
    result: bool = False
    try:
        result = await adapter.delete_memory(
            cube_id=cube_id,
            group_id="",
            memory_ids=[memory_id],
        )
    except httpx.ConnectError as exc:
        logger.warning("memos_service.delete_memory_by_id degraded: %s", exc)
        return False
    except Exception:
        logger.exception("memos_service.delete_memory_by_id unexpected error")
        return False
    return bool(result)


# ---------------------------------------------------------------------------
# Flush — write method (P082: catch errors)
# R2-P0-2: flush_pending implements flush as loop over sync queue
# ---------------------------------------------------------------------------


async def flush_pending(
    db_path: str,
) -> dict[str, Any]:
    """Flush all pending sync queue jobs. Degrades gracefully (P082).

    R2-P0-2: Implements flush as loop — fetch_pending_jobs from sync queue,
    dispatch each operation, return FlushOut.

    P082: Write method — catch errors, return degraded status.

    Args:
        db_path: SQLite database path.

    Returns:
        FlushOut dict with status, flushed_count, message.
    """
    # A008
    result: dict[str, Any] = {
        "status": "degraded",
        "flushed_count": 0,
        "message": None,
    }
    flushed = 0
    try:
        from z_winnow.memory.factory import create_memos_adapter
        from z_winnow.pipeline.database import (
            fetch_pending_jobs,
            mark_done,
            mark_processing,
        )

        adapter = create_memos_adapter()

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row

            # R2-P0-2: Loop over pending jobs, dispatch each
            while True:
                rows = await fetch_pending_jobs(db, limit=50)
                if not rows:
                    break

                for row in rows:
                    queue_id = row["queue_id"]
                    await mark_processing(db, queue_id)
                    try:
                        from z_winnow.memory.sync_ops import dispatch_op

                        await dispatch_op(adapter, row)
                        await mark_done(db, queue_id)
                        flushed += 1
                    except Exception as exc:
                        logger.warning("flush_pending: job %d failed — %s", queue_id, exc)

        result["status"] = "completed"
        result["flushed_count"] = flushed
        if flushed == 0:
            result["message"] = "No pending jobs to flush"
        else:
            result["message"] = f"Flushed {flushed} pending jobs"
    except httpx.ConnectError as exc:
        logger.warning("memos_service.flush_pending degraded (connect): %s", exc)
        result["flushed_count"] = flushed
        result["message"] = f"MemOS unavailable, flushed {flushed} before error"
    except Exception:
        logger.exception("memos_service.flush_pending unexpected error")
        result["flushed_count"] = flushed
        result["message"] = f"Partial flush: {flushed} processed before error"

    return result


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


async def health_check(
    adapter: MemOSAdapterProtocol,
) -> dict[str, Any]:
    """Check MemOS adapter health with graceful degradation (P082).

    Args:
        adapter: MemOS adapter instance.

    Returns:
        Healthy: ``{"status": "ok", ...}``
        Degraded: ``{"status": "degraded", "error": "..."}``
    """
    # A008
    result: dict[str, Any] = {"status": "unknown"}
    try:
        result = await adapter.health_check()
        # Ensure status field exists
        if "status" not in result:
            result["status"] = "ok"
    except httpx.ConnectError as exc:
        logger.warning("memos_service.health_check degraded: %s", exc)
        result = {"status": "degraded", "error": str(exc)}
    except Exception as exc:
        logger.exception("memos_service.health_check unexpected error")
        result = {"status": "error", "error": str(exc)}
    return result


# ---------------------------------------------------------------------------
# Sync worker lifecycle
# ---------------------------------------------------------------------------


async def start_sync_worker() -> None:
    """Start the background memos sync worker task.

    Safe to call multiple times — idempotent.
    """
    global _sync_worker_task
    if _sync_worker_task is not None and not _sync_worker_task.done():
        return
    logger.info("memos_service.start_sync_worker: starting")

    async def _worker_loop() -> None:
        """Background loop placeholder for memos sync operations."""
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break

    _sync_worker_task = asyncio.create_task(_worker_loop())


async def stop_sync_worker() -> None:
    """Stop the background memos sync worker task.

    Safe to call when no worker is running.
    """
    global _sync_worker_task
    if _sync_worker_task is None:
        return
    _sync_worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _sync_worker_task
    _sync_worker_task = None
    logger.info("memos_service.stop_sync_worker: stopped")
