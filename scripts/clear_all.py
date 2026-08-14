#!/usr/bin/env python3
"""Clear SQLite database + MemOS cubes for the specified date range and groups.

Usage:
    poetry run python scripts/clear_all.py \\
        --start 2026-05-18 --end 2026-05-20 --groups "群A" "群B"
    poetry run python scripts/clear_all.py \\
        --start 2026-05-18 --end 2026-05-20 --groups "群A" "群B" --dry-run

What it clears:
    - SQLite: raw_messages, parsed_contexts, topic_summaries, report_versions for matching dates+groups
    - SQLite: pipeline_runs, memos_sync_queue, feedback_events for matching dates+groups
    - SQLite: group_experiences by group_id (no date column — cross-day derived lessons)
    - Filesystem: data/processed/{group_id}/{date}/ and data/processed/{date}/ (L3 JSON)
    - Filesystem: data/stage_states/{group_id}/{date}/ (pipeline stage state)
    - Filesystem: data/e2e_results/ (previous test artifacts)
    - Filesystem: data/pipeline_stats*.json, data/memos_openapi.json, data/weflow_config_backup_*.txt
    - MemOS: deletes all memories in each group's cubes
    - Qdrant: deletes the neo4j_vec_db collection (stale vector cleanup), then
              recreates an EMPTY one (the MemOS server never auto-creates it)

The script resolves display names → group_ids via resolve_group_id_sync() before
running SQL DELETE queries, since data tables store the internal group_id PK.

The script does NOT drop tables or delete the database file — it only removes
data within the specified date range and groups.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

# Ensure the project src/ is on sys.path so we can import z_winnow
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("clear_all")

# Qdrant collection config for recreation after clear.
# MemOS server does NOT auto-create the collection on write (verified 2026-07-18):
# a missing collection makes vector_sync 404 on every write → search returns empty.
# These MUST match deployments/docker-compose.yml EMBEDDING_DIMENSION=3072 and the
# text-embedding-3-large model. Keep in sync with start_all.sh ensure-collection step.
_QDRANT_COLLECTION = "neo4j_vec_db"
_QDRANT_VECTOR_SIZE = 3072
_QDRANT_DISTANCE = "Cosine"


async def resolve_group_ids(group_inputs: list[str], db_path: str) -> dict[str, str]:
    """Resolve group identifiers (display_name, chatroom_id, or group_id) to internal group_id values.

    Uses resolve_group_id which matches by display_name → group_id → chatroom_id.
    Falls back to treating input as a raw group_id if it starts with 'g_' and DB lookup fails
    (useful for --all-dates when the groups table may be empty).

    Returns a dict mapping input → group_id.
    Raises ValueError if any group cannot be resolved.
    """
    from z_winnow.pipeline.group_config import resolve_group_id

    mapping: dict[str, str] = {}
    for name in group_inputs:
        try:
            gid = await resolve_group_id(name, db_path)
            mapping[name] = gid
            logger.info("Resolved group: '%s' → group_id=%s", name, gid)
        except (ValueError, FileNotFoundError):
            # Fallback: if input looks like a group_id (g_xxx), use it directly
            if name.startswith("g_"):
                mapping[name] = name
                logger.info("Using input as raw group_id: '%s'", name)
            else:
                logger.error("Failed to resolve group '%s'", name)
                raise
    return mapping


async def resolve_display_names(group_ids: list[str], db_path: str) -> dict[str, str]:
    """Look up display_name for each group_id from the groups table.

    Falls back to group_id as display_name if not found (empty groups table).
    Returns a dict mapping group_id → display_name.
    """
    import aiosqlite

    mapping: dict[str, str] = {}
    try:
        async with aiosqlite.connect(db_path) as db:
            for gid in group_ids:
                cursor = await db.execute(
                    "SELECT display_name FROM groups WHERE group_id = ?",
                    (gid,),
                )
                row = await cursor.fetchone()
                mapping[gid] = row[0] if row else gid
    except Exception:
        for gid in group_ids:
            mapping[gid] = gid
    return mapping


async def clear_sqlite(
    db_path: str,
    dates: list[str] | None,
    display_names: list[str],  # actual display_names for memos_sync_queue cube_id
    group_ids: list[str],  # used for data tables
    dry_run: bool = False,
) -> dict[str, int]:
    """Delete rows from all pipeline tables for the given date range and groups.

    If dates is None, clears all dates (no date filter).
    Returns counts of deleted rows per table.
    """
    import aiosqlite

    counts: dict[str, int] = {}
    action = "WOULD DELETE" if dry_run else "DELETING"

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")

        # Tables that use (date, group_id) matching
        # Also clean rows with empty group_id (orphaned by earlier pipeline bugs)
        # pipeline_runs uses display_name in group_id column, not internal group_id
        for table in [
            "raw_messages",
            "parsed_contexts",
            "topic_summaries",
            "report_versions",
        ]:
            group_params = [*group_ids, ""]  # include empty group_id
            placeholders_g = ",".join("?" for _ in group_params)
            if dates is not None:
                placeholders_d = ",".join("?" for _ in dates)
                where_clause = f"date IN ({placeholders_d}) AND group_id IN ({placeholders_g})"
                all_params = dates + group_params
            else:
                where_clause = f"group_id IN ({placeholders_g})"
                all_params = group_params

            if dry_run:
                cursor = await db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where_clause}",
                    all_params,
                )
                row = await cursor.fetchone()
                counts[table] = row[0] if row else 0
                if counts[table] > 0:
                    logger.info("  %s %s: %d rows", action, table, counts[table])
            else:
                cursor = await db.execute(
                    f"DELETE FROM {table} WHERE {where_clause}",
                    all_params,
                )
                counts[table] = cursor.rowcount
                if cursor.rowcount > 0:
                    logger.info("  DELETED %s: %d rows", table, cursor.rowcount)

        # pipeline_runs — group_id column stores display_name, not internal group_id
        pipeline_runs_params = group_ids + display_names + [""]
        placeholders_pr = ",".join("?" for _ in pipeline_runs_params)
        if dates is not None:
            placeholders_prd = ",".join("?" for _ in dates)
            where_pr = f"date IN ({placeholders_prd}) AND group_id IN ({placeholders_pr})"
            all_pr_params = dates + pipeline_runs_params
        else:
            where_pr = f"group_id IN ({placeholders_pr})"
            all_pr_params = pipeline_runs_params
        if dry_run:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM pipeline_runs WHERE {where_pr}", all_pr_params
            )
            row = await cursor.fetchone()
            counts["pipeline_runs"] = row[0] if row else 0
            if counts["pipeline_runs"] > 0:
                logger.info("  %s pipeline_runs: %d rows", action, counts["pipeline_runs"])
        else:
            cursor = await db.execute(
                f"DELETE FROM pipeline_runs WHERE {where_pr}", all_pr_params
            )
            counts["pipeline_runs"] = cursor.rowcount
            if cursor.rowcount > 0:
                logger.info("  DELETED pipeline_runs: %d rows", cursor.rowcount)

        # memos_sync_queue — cube_id uses group_id (e.g. winnow:g_xxx:topics)
        # Also match display_name for legacy entries
        cube_id_patterns: list[str] = []
        for gid in group_ids:
            for suffix in ["", ":topics", ":feedback"]:
                cube_id_patterns.append(f"winnow:{gid}{suffix}")
        for dname in display_names:
            for suffix in ["", ":topics", ":feedback"]:
                cube_id_patterns.append(f"winnow:{dname}{suffix}")
        # When all-dates, also match with date-suffix cube_ids
        if dates is None:
            cursor = await db.execute("SELECT DISTINCT cube_id FROM memos_sync_queue")
            existing_cubes = [r[0] for r in await cursor.fetchall()]
            for gid in group_ids:
                for ec in existing_cubes:
                    if ec not in cube_id_patterns and gid in ec:
                        cube_id_patterns.append(ec)

        placeholders_cubes = ",".join("?" for _ in cube_id_patterns)
        if cube_id_patterns:
            if dry_run:
                cursor = await db.execute(
                    f"SELECT COUNT(*) FROM memos_sync_queue WHERE cube_id IN ({placeholders_cubes})",
                    cube_id_patterns,
                )
                row = await cursor.fetchone()
                counts["memos_sync_queue"] = row[0] if row else 0
                if counts["memos_sync_queue"] > 0:
                    logger.info(
                        "  %s memos_sync_queue: %d rows", action, counts["memos_sync_queue"]
                    )
            else:
                cursor = await db.execute(
                    f"DELETE FROM memos_sync_queue WHERE cube_id IN ({placeholders_cubes})",
                    cube_id_patterns,
                )
                counts["memos_sync_queue"] = cursor.rowcount
                if cursor.rowcount > 0:
                    logger.info("  DELETED memos_sync_queue: %d rows", cursor.rowcount)

        # feedback_events — uses group_id (not display name)
        for gid in group_ids:
            if dates is not None:
                date_list = dates
            else:
                cursor = await db.execute(
                    "SELECT DISTINCT date FROM feedback_events WHERE group_id = ?",
                    (gid,),
                )
                date_list = [r[0] for r in await cursor.fetchall()]
            for date in date_list:
                if dry_run:
                    cursor = await db.execute(
                        "SELECT COUNT(*) FROM feedback_events WHERE date = ? AND group_id = ?",
                        (date, gid),
                    )
                    row = await cursor.fetchone()
                    c = row[0] if row else 0
                    if c > 0:
                        key = f"feedback_events[{gid}/{date}]"
                        counts[key] = c
                        logger.info("  %s %s: %d rows", action, key, c)
                else:
                    cursor = await db.execute(
                        "DELETE FROM feedback_events WHERE date = ? AND group_id = ?",
                        (date, gid),
                    )
                    if cursor.rowcount > 0:
                        key = f"feedback_events[{gid}/{date}]"
                        counts[key] = cursor.rowcount
                        logger.info("  DELETED %s: %d rows", key, cursor.rowcount)

        # group_experiences — group-bound, cross-day derived lessons
        # (correction_loader's primary source for unified_reporter). Has no
        # date column, so delete by group_id regardless of date range; partial
        # deletion of cross-day accumulated experiences is meaningless. The
        # empty-string group_id catches any orphan rows from earlier bugs.
        ge_params = [*group_ids, ""]
        placeholders_ge = ",".join("?" for _ in ge_params)
        where_ge = f"group_id IN ({placeholders_ge})"
        if dry_run:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM group_experiences WHERE {where_ge}",
                ge_params,
            )
            row = await cursor.fetchone()
            counts["group_experiences"] = row[0] if row else 0
            if counts["group_experiences"] > 0:
                logger.info(
                    "  %s group_experiences: %d rows", action, counts["group_experiences"]
                )
        else:
            cursor = await db.execute(
                f"DELETE FROM group_experiences WHERE {where_ge}", ge_params
            )
            counts["group_experiences"] = cursor.rowcount
            if cursor.rowcount > 0:
                logger.info("  DELETED group_experiences: %d rows", cursor.rowcount)

        if not dry_run:
            await db.commit()

    return counts


async def clear_memos(
    group_ids: list[str],
    display_names: list[str],
    dry_run: bool = False,
) -> dict[str, bool]:
    """Delete all memories from MemOS cubes for the given groups.

    Uses the MemOS adapter directly (not subprocess) to avoid PATH issues.

    Pipeline writes MemOS with user_id=group_id and cube_id=winnow:{group_id}:topics,
    so we must clear using group_id as the user_id and scope. Also tries display_name
    as fallback to catch any legacy data written under the old scheme.

    MemOS get_all response structure:
      { "data": [{ "memories": [{ "tree_structure": { "children": [{ "id": "..." }] } }] }] }
    """
    from z_winnow.memory.factory import create_memos_adapter

    def _extract_node_ids(resp: dict) -> list[str]:
        """Extract all node IDs from MemOS get_all response tree, recursively."""

        def _walk(node: dict, ids: list[str]) -> None:
            nid = node.get("id")
            if nid and nid != "root":
                ids.append(nid)
            for child in node.get("children", []):
                if isinstance(child, dict):
                    _walk(child, ids)

        ids: list[str] = []
        for cube_entry in resp.get("data", []):
            for mem in cube_entry.get("memories", []):
                tree = mem.get("tree_structure", {})
                _walk(tree, ids)
        return ids

    results: dict[str, bool] = {}

    adapter = None
    try:
        adapter = create_memos_adapter()
    except Exception as exc:
        logger.error("Failed to create MemOS adapter: %s", exc)
        return dict.fromkeys(group_ids, False)

    try:
        for gid, dname in zip(group_ids, display_names, strict=False):
            if dry_run:
                logger.info("WOULD DELETE MemOS cubes for group: %s (%s)", gid, dname)
                results[gid] = True
                continue

            logger.info("Deleting MemOS cubes for group: %s (%s)", gid, dname)
            try:
                # Try both group_id and display_name as scope to cover current + legacy data
                scopes_to_try: list[tuple[str, str]] = [
                    (gid, "group_id"),
                    (dname, "display_name"),
                ]
                for scope, source in scopes_to_try:
                    for suffix in [":topics", ":feedback", ""]:
                        cube_id = f"winnow:{scope}{suffix}"
                        all_data = await adapter.get_all_memories(cube_id=cube_id, group_id=gid)

                        node_ids = _extract_node_ids(all_data)
                        if not node_ids:
                            continue

                        logger.info(
                            "  -> Cube %s (%s): %d nodes to delete",
                            cube_id,
                            source,
                            len(node_ids),
                        )

                        # Delete in batches of 20 — large batches silently fail in MemOS
                        batch_size = 20
                        total_deleted = 0
                        for i in range(0, len(node_ids), batch_size):
                            batch = node_ids[i : i + batch_size]
                            ok = await adapter.delete_memory(
                                cube_id=cube_id,
                                group_id=gid,
                                memory_ids=batch,
                            )
                            if ok:
                                total_deleted += len(batch)
                            else:
                                logger.warning(
                                    "  -> FAILED batch [%d:%d] for %s",
                                    i,
                                    i + batch_size,
                                    cube_id,
                                )
                        logger.info(
                            "  -> OK: deleted %d/%d nodes from %s",
                            total_deleted,
                            len(node_ids),
                            cube_id,
                        )

                results[gid] = True
            except Exception as exc:
                logger.error("  -> ERROR clearing MemOS for '%s': %s", gid, exc)
                results[gid] = False
    finally:
        with contextlib.suppress(Exception):
            await adapter.close() if hasattr(adapter, "close") else None

    # 4. Clear Qdrant vector index — MemOS delete_memory only removes tree nodes,
    #    not the underlying embeddings. The Scheduler's RAG will find stale vectors
    #    and inject old wxid_ data into new memories. Delete the collection, then
    #    recreate an EMPTY one (MemOS server never auto-creates it; a missing
    #    collection breaks the next pipeline run's vector_sync).
    await clear_qdrant_collection(dry_run=dry_run)

    return results


async def clear_qdrant_collection(
    collection_name: str = "neo4j_vec_db",
    host: str = "127.0.0.1",
    port: int = 6333,
    dry_run: bool = False,
) -> bool:
    """Delete the MemOS Qdrant collection, then recreate an EMPTY one.

    MemOS stores all embeddings in a single collection (neo4j_vec_db).
    The Scheduler uses RAG over these vectors during memory processing.
    If vectors persist after delete_memory(), the Scheduler pulls in old
    data (e.g. wxid_ identifiers) and contaminates new memories.

    The MemOS server NEVER auto-creates the collection on write (verified
    2026-07-18). A missing collection makes every subsequent vector_sync
    404 and search return empty. So after deleting, we immediately recreate
    an empty collection (0 points) so the clear → rerun workflow keeps
    working. dry_run only previews, never deletes or recreates.
    """
    import httpx

    base_url = f"http://{host}:{port}/collections/{collection_name}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Check collection exists
            resp = await client.get(base_url)
            if resp.status_code == 404:
                # Collection already gone — still recreate an empty one so the
                # next pipeline write doesn't 404 on vector_sync.
                logger.info("  Qdrant collection '%s' does not exist", collection_name)
                if dry_run:
                    logger.info("  WOULD RECREATE empty collection '%s'", collection_name)
                    return True
                return await _recreate_empty_collection(client, base_url, collection_name)

            if dry_run:
                info = resp.json().get("result", {})
                points = info.get("points_count", "?")
                logger.info(
                    "  WOULD DELETE + recreate Qdrant collection '%s' (%s points)",
                    collection_name,
                    points,
                )
                return True

            resp = await client.delete(base_url)
            if resp.status_code not in (200, 201, 202, 204):
                logger.warning(
                    "  -> FAILED: Qdrant DELETE %s returned %d", collection_name, resp.status_code
                )
                return False
            logger.info("  -> OK: deleted Qdrant collection '%s'", collection_name)

            # Recreate empty collection so vector_sync works on the next write.
            return await _recreate_empty_collection(client, base_url, collection_name)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        logger.warning("  -> Qdrant not reachable (%s) — skip vector cleanup", exc)
        return True  # Non-fatal: Qdrant may not be running in all environments
    except Exception as exc:
        logger.warning("  -> Qdrant cleanup error: %s", exc)
        return False


async def _recreate_empty_collection(
    client, base_url: str, collection_name: str
) -> bool:
    """Recreate an empty Qdrant collection with the MemOS vector config.

    Non-fatal: returns True even on recreate failure (the DELETE already
    succeeded; a missing collection is the user's problem to fix via
    start_all.sh or the manual PUT in CLAUDE.md). Logs loudly on failure.
    """
    import httpx

    payload = {"vectors": {"size": _QDRANT_VECTOR_SIZE, "distance": _QDRANT_DISTANCE}}
    try:
        resp = await client.put(base_url, json=payload)
        if resp.status_code in (200, 201):
            logger.info(
                "  -> OK: recreated EMPTY Qdrant collection '%s' (dim=%d, %s)",
                collection_name,
                _QDRANT_VECTOR_SIZE,
                _QDRANT_DISTANCE,
            )
            return True
        logger.warning(
            "  -> ⚠️ recreate Qdrant collection '%s' returned %d: %s "
            "— next pipeline run will 404 on vector_sync. Fix: bash start_all.sh "
            "or curl -X PUT %s -H 'Content-Type: application/json' "
            "-d '{\"vectors\":{\"size\":%d,\"distance\":\"%s\"}}'",
            collection_name,
            resp.status_code,
            resp.text[:120],
            base_url,
            _QDRANT_VECTOR_SIZE,
            _QDRANT_DISTANCE,
        )
        return True
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        logger.warning(
            "  -> ⚠️ Qdrant unreachable during recreate (%s) — collection now MISSING, "
            "next pipeline run will 404 on vector_sync. Fix: run start_all.sh after "
            "Qdrant is back up.", exc
        )
        return True
    except Exception as exc:
        logger.warning("  -> ⚠️ recreate Qdrant collection error: %s", exc)
        return True


async def clear_files(
    data_dir: Path,
    dates: list[str] | None,
    group_ids: list[str],
    dry_run: bool = False,
) -> dict[str, int]:
    """Delete L3 JSON files, stage state files, and e2e artifacts from data/.

    If dates is None, clears all dates (removes entire group directories).

    Cleans:
      - data/processed/{group_id}/{date}/* (new per-group path)
      - data/processed/{date}/* (legacy flat path)
      - data/stage_states/{group_id}/{date}/*
      - data/tmp/chat_context_*.md (chat context temp files)
      - data/e2e_results/* (previous test artifacts)
      - data/pipeline_stats*.json, data/memos_openapi.json
      - data/weflow_config_backup_*.txt

    Returns counts of deleted items per category.
    """
    import shutil

    counts: dict[str, int] = {}
    action = "WOULD DELETE" if dry_run else "DELETING"

    processed_dir = data_dir / "processed"
    stage_dir = data_dir / "stage_states"
    e2e_dir = data_dir / "e2e_results"

    # 1. data/processed/{group_id}/{date}/*
    if processed_dir.exists():
        for gid in group_ids:
            group_dir = processed_dir / gid
            if not group_dir.exists():
                continue
            if dates is not None:
                # Specific dates — remove individual date subdirs
                for date in dates:
                    date_dir = group_dir / date
                    if not date_dir.exists():
                        continue
                    file_count = len(list(date_dir.iterdir()))
                    if file_count > 0:
                        logger.info(
                            "  %s processed/%s/%s/ (%d files)", action, gid, date, file_count
                        )
                        counts[f"processed/{gid}/{date}"] = file_count
                        if not dry_run:
                            shutil.rmtree(date_dir)
            else:
                # All dates — remove entire group directory
                file_count = sum(len(list(d.iterdir())) for d in group_dir.iterdir() if d.is_dir())
                if file_count > 0:
                    logger.info(
                        "  %s processed/%s/ (%d files across all dates)", action, gid, file_count
                    )
                    counts[f"processed/{gid}"] = file_count
                    if not dry_run:
                        shutil.rmtree(group_dir)
                    continue
            # Remove empty group dir
            if group_dir.exists() and not dry_run and not any(group_dir.iterdir()):
                group_dir.rmdir()

        # 2. data/processed/{date}/* (legacy flat path)
        if dates is not None:
            date_dirs = [processed_dir / d for d in dates]
        else:
            date_dirs = [
                d for d in processed_dir.iterdir() if d.is_dir() and not d.name.startswith("g_")
            ]
        for date_dir in date_dirs:
            if not date_dir.exists():
                continue
            file_count = len(list(date_dir.iterdir()))
            if file_count > 0:
                logger.info("  %s processed/%s/ (%d files)", action, date_dir.name, file_count)
                counts[f"processed/{date_dir.name}"] = file_count
                if not dry_run:
                    shutil.rmtree(date_dir)

        # Remove processed dir if empty
        if processed_dir.exists() and not dry_run and not any(processed_dir.iterdir()):
            processed_dir.rmdir()

    # 3. data/stage_states/{group_id}/{date}/*
    if stage_dir.exists():
        for gid in group_ids:
            group_dir = stage_dir / gid
            if not group_dir.exists():
                continue
            if dates is not None:
                for date in dates:
                    date_dir = group_dir / date
                    if not date_dir.exists():
                        continue
                    file_count = len(list(date_dir.iterdir()))
                    if file_count > 0:
                        logger.info(
                            "  %s stage_states/%s/%s/ (%d files)", action, gid, date, file_count
                        )
                        counts[f"stage_states/{gid}/{date}"] = file_count
                        if not dry_run:
                            shutil.rmtree(date_dir)
            else:
                file_count = sum(len(list(d.iterdir())) for d in group_dir.iterdir() if d.is_dir())
                if file_count > 0:
                    logger.info(
                        "  %s stage_states/%s/ (%d files across all dates)", action, gid, file_count
                    )
                    counts[f"stage_states/{gid}"] = file_count
                    if not dry_run:
                        shutil.rmtree(group_dir)
                    continue
            if group_dir.exists() and not dry_run and not any(group_dir.iterdir()):
                group_dir.rmdir()
        if stage_dir.exists() and not dry_run and not any(stage_dir.iterdir()):
            stage_dir.rmdir()

    # 4. data/e2e_results/*
    if e2e_dir.exists():
        files = list(e2e_dir.iterdir())
        if files:
            logger.info("  %s e2e_results/ (%d files)", action, len(files))
            counts["e2e_results"] = len(files)
            if not dry_run:
                shutil.rmtree(e2e_dir)

    # 5. Misc files
    misc_patterns = [
        "pipeline_stats*.json",
        "memos_openapi.json",
        "weflow_config_backup_*.txt",
    ]
    misc_count = 0
    for pattern in misc_patterns:
        for f in data_dir.glob(pattern):
            logger.info("  %s %s", action, f.name)
            misc_count += 1
            if not dry_run:
                f.unlink()
    if misc_count:
        counts["misc_files"] = misc_count

    # 6. data/tmp/chat_context_*.md (chat context temp files)
    tmp_dir = data_dir / "tmp"
    if tmp_dir.exists():
        tmp_files = list(tmp_dir.glob("chat_context_*.md"))
        if tmp_files:
            logger.info("  %s tmp/chat_context_*.md (%d files)", action, len(tmp_files))
            counts["tmp_chat_context"] = len(tmp_files)
            if not dry_run:
                for f in tmp_files:
                    f.unlink()

    return counts


async def main() -> None:
    parser = argparse.ArgumentParser(description="Clear pipeline SQLite + MemOS data")
    parser.add_argument("--start", help="Start date YYYY-MM-DD (required unless --all-dates)")
    parser.add_argument("--end", help="End date YYYY-MM-DD (required unless --all-dates)")
    parser.add_argument(
        "--all-dates",
        action="store_true",
        help="Clear all dates for specified groups (no date filter)",
    )
    parser.add_argument("--groups", nargs="+", required=True, help="Group display names or IDs")
    parser.add_argument("--db-path", default="data/winnow.db", help="SQLite database path")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be deleted without deleting"
    )
    args = parser.parse_args()

    if not args.all_dates and (not args.start or not args.end):
        parser.error("--start and --end are required unless --all-dates is specified")

    # Generate date list
    from datetime import datetime, timedelta

    if args.all_dates:
        dates: list[str] | None = None
    else:
        start = datetime.strptime(args.start, "%Y-%m-%d")
        end = datetime.strptime(args.end, "%Y-%m-%d")
        dates = []
        current = start
        while current <= end:
            dates.append(current.strftime("%Y%m%d"))
            current += timedelta(days=1)

    db_path = str(_PROJECT_ROOT / args.db_path)

    logger.info("=" * 60)
    if args.dry_run:
        logger.info("DRY RUN — no data will be deleted")
    if dates is None:
        logger.info("CLEAR: ALL DATES, groups=%s db=%s", args.groups, db_path)
    else:
        logger.info("CLEAR: dates=%s groups=%s db=%s", dates, args.groups, db_path)
    logger.info("=" * 60)

    # Resolve group identifiers → group_ids
    group_map = await resolve_group_ids(args.groups, db_path)
    resolved_ids = list(group_map.values())

    # Resolve display_names from group_ids (for MemOS cube scoping)
    display_map = await resolve_display_names(resolved_ids, db_path)
    display_names = list(display_map.values())
    logger.info("Resolved display_names: %s", display_names)

    # 1. Clear SQLite
    logger.info("Step 1/4: Clearing SQLite tables...")
    sqlite_counts = await clear_sqlite(
        db_path,
        dates,
        display_names=display_names,
        group_ids=resolved_ids,
        dry_run=args.dry_run,
    )

    total = sum(sqlite_counts.values())
    if total == 0:
        logger.info("  (no matching rows found)")
    elif args.dry_run:
        logger.info("  Total would delete: %d rows", total)

    # 2. Clear filesystem (processed JSON, stage_states, e2e_results, misc files)
    data_dir = _PROJECT_ROOT / "data"
    logger.info("Step 2/4: Clearing filesystem artifacts...")
    file_counts = await clear_files(
        data_dir,
        dates,
        group_ids=resolved_ids,
        dry_run=args.dry_run,
    )
    file_total = sum(file_counts.values())
    if file_total == 0:
        logger.info("  (no matching files found)")

    # 3. Clear MemOS
    logger.info("Step 3/4: Clearing MemOS cubes + Qdrant vectors...")
    memos_results = await clear_memos(
        group_ids=resolved_ids,
        display_names=display_names,
        dry_run=args.dry_run,
    )

    # Summary
    logger.info("=" * 60)
    if args.dry_run:
        logger.info("DRY RUN COMPLETE — no data was deleted")
    else:
        logger.info(
            "CLEAR COMPLETE: SQLite=%d rows, Files=%d, MemOS=%d/%d cubes cleared",
            total,
            file_total,
            sum(1 for v in memos_results.values() if v),
            len(args.groups),
        )


if __name__ == "__main__":
    asyncio.run(main())
