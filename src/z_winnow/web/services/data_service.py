"""T-W14-4: Data browser service — L1/L2/L3 queries + provenance tracing.

Wraps the three-layer data model (raw_messages → parsed_contexts → topic_summaries)
into pure async functions that accept an ``aiosqlite.Connection`` via dependency
injection.  No FastAPI imports, no global state.

Patterns applied:
  P032: Multi-layer data explorer — L1/L2/L3 query pattern
  A008: All JSON parsing inits data variable before try block
  L005: source_server_ids provenance via JSON LIKE matching
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# L1: Raw messages
# ---------------------------------------------------------------------------


async def get_l1_messages(
    db: aiosqlite.Connection,
    date: str,
    *,
    group_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Fetch L1 raw messages for a given date, with optional group filter.

    Args:
        db: Async SQLite connection (injected).
        date: Date string (YYYYMMDD).
        group_id: Optional group filter.
        page: 1-based page number.
        page_size: Items per page.

    Returns:
        Dict with ``items``, ``total``, ``page``, ``page_size`` keys.
    """
    # A008: init result before try
    result: dict[str, Any] = {"items": [], "total": 0, "page": page, "page_size": page_size}
    try:
        db.row_factory = aiosqlite.Row

        where_clause = "WHERE date = ?"
        params: list[Any] = [date]
        if group_id is not None:
            where_clause += " AND group_id = ?"
            params.append(group_id)

        # Count
        count_sql = f"SELECT COUNT(*) AS cnt FROM raw_messages {where_clause}"
        cursor = await db.execute(count_sql, params)
        row = await cursor.fetchone()
        result["total"] = row["cnt"] if row else 0

        # Fetch page
        offset = (page - 1) * page_size
        data_sql = f"SELECT * FROM raw_messages {where_clause} ORDER BY rowid ASC LIMIT ? OFFSET ?"
        cursor = await db.execute(data_sql, [*params, page_size, offset])
        result["items"] = [dict(r) for r in await cursor.fetchall()]
    except Exception:
        logger.exception("data_service.get_l1_messages failed for date=%s", date)
    return result


# ---------------------------------------------------------------------------
# L2: Parsed contexts
# ---------------------------------------------------------------------------


async def get_l2_contexts_by_server_ids(
    db: aiosqlite.Connection,
    server_ids: list[str],
) -> list[dict[str, Any]]:
    """Fetch L2 parsed contexts that reference any of the given server_ids.

    Uses ``LIKE`` matching on the JSON ``server_ids`` column, consistent with
    provenance.py (L005).

    Args:
        db: Async SQLite connection.
        server_ids: List of serverID values to match.

    Returns:
        List of context dicts.
    """
    # A008
    results: list[dict[str, Any]] = []
    try:
        db.row_factory = aiosqlite.Row
        for sid in server_ids:
            cursor = await db.execute(
                "SELECT * FROM parsed_contexts WHERE server_ids LIKE ?",
                (f"%{sid}%",),
            )
            rows = await cursor.fetchall()
            for row in rows:
                results.append(dict(row))
    except Exception:
        logger.exception("data_service.get_l2_contexts_by_server_ids failed")
    return results


async def get_l2_contexts(
    db: aiosqlite.Connection,
    date: str,
    *,
    group_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Fetch L2 parsed contexts for a given date, with optional group filter.

    Mirrors the paginated shape of ``get_l1_messages`` but queries
    the ``parsed_contexts`` table.

    Args:
        db: Async SQLite connection (injected).
        date: Date string (YYYYMMDD).
        group_id: Optional group filter.
        page: 1-based page number.
        page_size: Items per page.

    Returns:
        Dict with ``items``, ``total``, ``page``, ``page_size`` keys.
    """
    # A008: init result before try
    result: dict[str, Any] = {"items": [], "total": 0, "page": page, "page_size": page_size}
    try:
        db.row_factory = aiosqlite.Row

        where_clause = "WHERE date = ?"
        params: list[Any] = [date]
        if group_id is not None:
            where_clause += " AND group_id = ?"
            params.append(group_id)

        # Count
        count_sql = f"SELECT COUNT(*) AS cnt FROM parsed_contexts {where_clause}"
        cursor = await db.execute(count_sql, params)
        row = await cursor.fetchone()
        result["total"] = row["cnt"] if row else 0

        # Fetch page
        offset = (page - 1) * page_size
        data_sql = (
            f"SELECT * FROM parsed_contexts {where_clause} ORDER BY created_at ASC LIMIT ? OFFSET ?"
        )
        cursor = await db.execute(data_sql, [*params, page_size, offset])
        result["items"] = [dict(r) for r in await cursor.fetchall()]
    except Exception:
        logger.exception("data_service.get_l2_contexts failed for date=%s", date)
    return result


# ---------------------------------------------------------------------------
# L3: Topic summaries
# ---------------------------------------------------------------------------


async def get_l3_topics(
    db: aiosqlite.Connection,
    date: str,
    *,
    group_id: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch L3 topic summaries for a given date.

    Args:
        db: Async SQLite connection.
        date: Date string (YYYYMMDD).
        group_id: Optional group filter.

    Returns:
        List of topic summary dicts.
    """
    # A008
    results: list[dict[str, Any]] = []
    try:
        db.row_factory = aiosqlite.Row
        if group_id is not None:
            cursor = await db.execute(
                "SELECT * FROM topic_summaries WHERE date = ? AND group_id = ? ORDER BY topic_name",
                (date, group_id),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM topic_summaries WHERE date = ? ORDER BY topic_name",
                (date,),
            )
        results = [dict(r) for r in await cursor.fetchall()]
    except Exception:
        logger.exception("data_service.get_l3_topics failed for date=%s", date)
    return results


# ---------------------------------------------------------------------------
# Provenance tracing
# ---------------------------------------------------------------------------


async def trace_message_to_topics(
    db: aiosqlite.Connection,
    server_id: str,
) -> dict[str, Any]:
    """Forward trace: given a serverID, find all topics that reference it.

    Query chain (L005):
      raw_messages.serverID ← parsed_contexts.server_ids (JSON LIKE)
                            ← topic_summaries.source_server_ids (JSON LIKE)

    Args:
        db: Async SQLite connection.
        server_id: WeChat serverId.

    Returns:
        Dict with ``server_id``, ``message``, ``topics`` keys.
    """
    # A008
    result: dict[str, Any] = {
        "server_id": server_id,
        "message": None,
        "topics": [],
    }
    try:
        db.row_factory = aiosqlite.Row

        # Layer 1: fetch original message
        cursor = await db.execute(
            "SELECT * FROM raw_messages WHERE serverID = ?",
            (server_id,),
        )
        msg_row = await cursor.fetchone()
        if not msg_row:
            return result

        message = dict(msg_row)

        # Fetch parsed content for this message
        cursor = await db.execute(
            "SELECT context_text FROM parsed_contexts WHERE server_ids LIKE ? LIMIT 1",
            (f"%{server_id}%",),
        )
        ctx_row = await cursor.fetchone()
        message["parsed_content"] = ctx_row["context_text"] if ctx_row else None
        result["message"] = message

        # Layer 3: find topics referencing this server_id
        cursor = await db.execute(
            "SELECT * FROM topic_summaries WHERE source_server_ids LIKE ?",
            (f"%{server_id}%",),
        )
        topics = [dict(r) for r in await cursor.fetchall()]
        result["topics"] = topics
    except Exception:
        logger.exception("data_service.trace_message_to_topics failed for %s", server_id)
    return result


async def trace_topic_to_messages(
    db: aiosqlite.Connection,
    topic_name: str,
) -> dict[str, Any]:
    """Reverse trace: given a topic name, find all originating messages.

    Query chain:
      topic_summaries → source_server_ids (JSON) → raw_messages
                      → context_ids (JSON) → parsed_contexts

    Args:
        db: Async SQLite connection.
        topic_name: Topic name to trace.

    Returns:
        Dict with ``topic_name``, ``summaries``, ``contexts``, ``raw_messages`` keys.
    """
    # A008
    result: dict[str, Any] = {
        "topic_name": topic_name,
        "summaries": [],
        "contexts": [],
        "raw_messages": [],
    }
    try:
        db.row_factory = aiosqlite.Row

        # Step 1: find topic summaries
        cursor = await db.execute(
            "SELECT * FROM topic_summaries WHERE topic_name = ? ORDER BY date DESC",
            (topic_name,),
        )
        summaries = [dict(r) for r in await cursor.fetchall()]
        if not summaries:
            return result
        result["summaries"] = summaries

        # Step 2: collect source_server_ids
        all_server_ids: set[str] = set()
        for s in summaries:
            # A008: init data before try
            ids_data: Any = None
            try:
                ids_data = s.get("source_server_ids", "[]")
                ids = json.loads(ids_data)
                all_server_ids.update(ids)
            except (json.JSONDecodeError, TypeError):
                pass

        # Step 3: fetch raw messages
        raw_messages: list[dict[str, Any]] = []
        for sid in all_server_ids:
            cursor = await db.execute(
                "SELECT * FROM raw_messages WHERE serverID = ?",
                (sid,),
            )
            row = await cursor.fetchone()
            if row:
                msg = dict(row)
                # Fetch parsed content
                cursor2 = await db.execute(
                    "SELECT context_text FROM parsed_contexts WHERE server_ids LIKE ? LIMIT 1",
                    (f"%{sid}%",),
                )
                ctx_row = await cursor2.fetchone()
                msg["parsed_content"] = ctx_row["context_text"] if ctx_row else None
                raw_messages.append(msg)
        result["raw_messages"] = raw_messages

        # Step 4: collect context_ids
        all_context_ids: set[str] = set()
        for s in summaries:
            # A008
            ctx_data: Any = None
            try:
                ctx_data = s.get("context_ids", "[]")
                cids = json.loads(ctx_data)
                all_context_ids.update(cids)
            except (json.JSONDecodeError, TypeError):
                pass

        # Step 5: fetch contexts
        contexts: list[dict[str, Any]] = []
        for cid in all_context_ids:
            cursor = await db.execute(
                "SELECT * FROM parsed_contexts WHERE context_id = ?",
                (cid,),
            )
            row = await cursor.fetchone()
            if row:
                contexts.append(dict(row))
        result["contexts"] = contexts
    except Exception:
        logger.exception("data_service.trace_topic_to_messages failed for %s", topic_name)
    return result


# ---------------------------------------------------------------------------
# W15-P1-DATA: Cross-layer stats + provenance chain + L1 detail
# ---------------------------------------------------------------------------


async def get_data_stats(
    db: aiosqlite.Connection,
    *,
    group_id: str | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """Aggregate statistics across all 3 layers with optional filters.

    P022: Pure SQLite aggregation — zero LLM calls.
    P050: All queries use parameterized ? placeholders.

    Args:
        db: Async SQLite connection (injected).
        group_id: Optional group filter.
        date: Optional date filter (YYYYMMDD).

    Returns:
        Dict matching DataStatsOut schema:
        {total_messages, total_groups, total_topics, total_reports,
         date_range_start, date_range_end}
    """
    # A008: init result before try
    result: dict[str, Any] = {
        "total_messages": 0,
        "total_groups": 0,
        "total_topics": 0,
        "total_reports": 0,
        "date_range_start": None,
        "date_range_end": None,
    }
    try:
        db.row_factory = aiosqlite.Row

        # Build WHERE clause parts for optional filters — P050: parameterized
        msg_where_parts: list[str] = []
        msg_params: list[Any] = []
        topic_where_parts: list[str] = []
        topic_params: list[Any] = []
        report_where_parts: list[str] = []
        report_params: list[Any] = []

        if group_id is not None:
            msg_where_parts.append("group_id = ?")
            msg_params.append(group_id)
            topic_where_parts.append("group_id = ?")
            topic_params.append(group_id)
            report_where_parts.append("group_id = ?")
            report_params.append(group_id)
        if date is not None:
            msg_where_parts.append("date = ?")
            msg_params.append(date)
            topic_where_parts.append("date = ?")
            topic_params.append(date)
            report_where_parts.append("date = ?")
            report_params.append(date)

        msg_where = ("WHERE " + " AND ".join(msg_where_parts)) if msg_where_parts else ""
        topic_where = ("WHERE " + " AND ".join(topic_where_parts)) if topic_where_parts else ""
        report_where = ("WHERE " + " AND ".join(report_where_parts)) if report_where_parts else ""

        # total_messages — P050: COUNT with optional WHERE
        cursor = await db.execute(
            f"SELECT COUNT(*) AS cnt FROM raw_messages {msg_where}",
            msg_params,
        )
        row = await cursor.fetchone()
        if row:
            result["total_messages"] = row["cnt"]

        # total_groups — COUNT DISTINCT
        cursor = await db.execute(
            f"SELECT COUNT(DISTINCT group_id) AS cnt FROM raw_messages {msg_where}",
            msg_params,
        )
        row = await cursor.fetchone()
        if row:
            result["total_groups"] = row["cnt"]

        # total_topics
        cursor = await db.execute(
            f"SELECT COUNT(*) AS cnt FROM topic_summaries {topic_where}",
            topic_params,
        )
        row = await cursor.fetchone()
        if row:
            result["total_topics"] = row["cnt"]

        # total_reports — count report_versions
        cursor = await db.execute(
            f"SELECT COUNT(*) AS cnt FROM report_versions {report_where}",
            report_params,
        )
        row = await cursor.fetchone()
        if row:
            result["total_reports"] = row["cnt"]

        # date range from raw_messages
        cursor = await db.execute(
            f"SELECT MIN(date) AS dmin, MAX(date) AS dmax FROM raw_messages {msg_where}",
            msg_params,
        )
        row = await cursor.fetchone()
        if row:
            result["date_range_start"] = row["dmin"]
            result["date_range_end"] = row["dmax"]
    except Exception:
        logger.exception("data_service.get_data_stats failed")
    return result


async def get_provenance_chain(
    db: aiosqlite.Connection,
    server_id: str,
) -> dict[str, Any] | None:
    """Forward provenance chain: server_id → message + associated topics.

    Wraps the existing trace_message_to_topics with Pydantic
    ProvenanceChainOut schema mapping.

    P022: Pure data retrieval — zero LLM calls.
    A008: result initialized before try block.

    Args:
        db: Async SQLite connection (injected).
        server_id: WeChat serverId.

    Returns:
        Dict matching ProvenanceChainOut, or None if message not found.
    """
    # A008
    raw_result: dict[str, Any] | None = None
    try:
        raw_result = await trace_message_to_topics(db, server_id)
    except Exception:
        logger.exception("data_service.get_provenance_chain failed for %s", server_id)
        return None

    if raw_result is None or raw_result.get("message") is None:
        return None

    return {
        "server_id": raw_result["server_id"],
        "message": raw_result["message"],
        "topics": raw_result.get("topics", []),
    }


async def get_l1_message_detail(
    db: aiosqlite.Connection,
    group_id: str,
    date: str,
    server_id: str,
) -> dict[str, Any] | None:
    """Detailed L1 message view with linked contexts and topic summaries.

    P022: Pure SQLite retrieval — assembles L1 + L2 + L3 data.
    P050: All queries use parameterized ? placeholders.

    Args:
        db: Async SQLite connection (injected).
        group_id: Group identifier.
        date: Date string (YYYYMMDD).
        server_id: WeChat serverId.

    Returns:
        Dict matching L1MessageDetailOut, or None if message not found.
    """
    # A008: init result before try
    result: dict[str, Any] | None = None
    try:
        db.row_factory = aiosqlite.Row

        # Layer 1: fetch the raw message
        cursor = await db.execute(
            "SELECT * FROM raw_messages WHERE serverID = ? AND group_id = ? AND date = ?",
            (server_id, group_id, date),
        )
        msg_row = await cursor.fetchone()
        if not msg_row:
            return None

        message = dict(msg_row)

        # Also fetch parsed content from L2
        cursor = await db.execute(
            "SELECT context_text FROM parsed_contexts WHERE server_ids LIKE ? LIMIT 1",
            (f"%{server_id}%",),
        )
        ctx_row = await cursor.fetchone()
        message["parsed_content"] = ctx_row["context_text"] if ctx_row else None

        # Layer 2: find contexts referencing this server_id
        cursor = await db.execute(
            "SELECT * FROM parsed_contexts WHERE server_ids LIKE ?",
            (f"%{server_id}%",),
        )
        contexts = [dict(r) for r in await cursor.fetchall()]

        # Layer 3: find topic summaries referencing this server_id
        cursor = await db.execute(
            "SELECT * FROM topic_summaries WHERE source_server_ids LIKE ?",
            (f"%{server_id}%",),
        )
        summaries = [dict(r) for r in await cursor.fetchall()]

        result = {
            "serverID": message.get("serverID", server_id),
            "date": message.get("date", date),
            "group_id": message.get("group_id", group_id),
            "sender": message.get("sender", ""),
            "content": message.get("content", ""),
            "msg_type": message.get("msg_type", "text"),
            "image_path": message.get("image_path"),
            "sanitized": message.get("sanitized", 0),
            "raw_json": message.get("raw_json"),
            "created_at": message.get("created_at"),
            "contexts": contexts,
            "summaries": summaries,
        }
    except Exception:
        logger.exception(
            "data_service.get_l1_message_detail failed for %s/%s/%s",
            group_id,
            date,
            server_id,
        )
    return result


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------


async def find_server_id_page(
    db: aiosqlite.Connection,
    date: str,
    server_id: str,
    page_size: int = 50,
) -> int | None:
    """Find which page a server_id falls on within the L1 messages for a date.

    Args:
        db: Async SQLite connection.
        date: Date string.
        server_id: Target serverID.
        page_size: Page size for calculation.

    Returns:
        1-based page number, or None if not found.
    """
    # A008
    result: int | None = None
    try:
        cursor = await db.execute(
            """SELECT rn - 1 AS zero_idx FROM (
                   SELECT serverID, ROW_NUMBER() OVER (ORDER BY rowid) AS rn
                   FROM raw_messages WHERE date = ?
               ) WHERE serverID = ?""",
            (date, server_id),
        )
        row = await cursor.fetchone()
        if row:
            zero_idx = row[0]
            result = (zero_idx // page_size) + 1
    except Exception:
        logger.exception("data_service.find_server_id_page failed")
    return result
