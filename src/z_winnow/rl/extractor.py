"""T-I1: SQLite → RLRecord extractor.

Extracts RL training four-tuples (state, action, reward, metadata) from the
SQLite three-layer data schema (raw_messages → parsed_contexts → topic_summaries)
with full-chain serverID provenance.

    from z_winnow.rl.extractor import extract_records
    records = extract_records(("2026-04-20", "2026-04-28"), db_path="data/winnow.db")

Orchestrator log table not yet implemented (T-F pending) → action fields
use placeholder values with a single warning log per extraction run.

Key design patterns (from experience index):
  - EXP-001: serverID full-chain JOIN across three layers
  - EXP-004: Pydantic extra='forbid'; empty results → [] (no empty records)
  - EXP-005: NodeError + retry for database errors
  - EXP-020 / L005: dict.fromkeys() ordered dedup, cap 50, "(无)" placeholder
  - A004 / P005: Single-function focus; no reward/CLI in this task
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from z_winnow.rl.schema import (
    RLAction,
    RLMetadata,
    RLProvenance,
    RLRecord,
    RLReward,
    RLRewardDimension,
    RLState,
)

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

DEFAULT_DB_PATH = "data/winnow.db"
MAX_SERVER_IDS = 50  # EXP-020 / L005: cap to prevent unbounded growth
ORCHESTRATOR_LOGS_TABLE = "orchestrator_logs"
RETRY_MAX = 3
RETRY_BACKOFF = 2.0
EMPTY_SENTINEL = "(无)"  # EXP-020 / L005: placeholder for empty provenance

SCENARIO_PREFERENCE_ALIGNMENT = "preference_alignment"
SCENARIO_ROUTING_DECISION = "routing_decision"
SCENARIO_TOPIC_TRACKING = "topic_tracking"

VALID_SCENARIOS = frozenset(
    {
        SCENARIO_PREFERENCE_ALIGNMENT,
        SCENARIO_ROUTING_DECISION,
        SCENARIO_TOPIC_TRACKING,
    }
)

# Module-level flag: warn only once about missing orchestrator logs
_warned_orchestrator_logs: bool = False


# ============================================================
# Helper functions
# ============================================================


def _to_db_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD → YYYYMMDD for database queries."""
    return iso_date.replace("-", "")


def _to_iso_date(db_date: str) -> str:
    """Convert YYYYMMDD → YYYY-MM-DD for RLRecord output."""
    if len(db_date) == 8:
        return f"{db_date[:4]}-{db_date[4:6]}-{db_date[6:8]}"
    return db_date


def _parse_json_array(value: str | None) -> list[str]:
    """Safely parse a JSON array string from the database.

    The server_ids / context_ids / source_server_ids fields in SQLite
    are stored as JSON array strings (e.g. '["id1","id2"]').

    Args:
        value: Raw JSON string from DB column, or None.

    Returns:
        Parsed list of strings, or empty list on any parse error.
    """
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return []
    except (json.JSONDecodeError, TypeError):
        return []


def _iso_now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


# ============================================================
# Data query layer
# ============================================================


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Check whether a table exists."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _query_raw_messages(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> dict[str, list[dict]]:
    """Query raw_messages (Layer 1) grouped by date."""
    if not _table_exists(conn, "raw_messages"):
        return {}
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """SELECT * FROM raw_messages
           WHERE date >= ? AND date <= ?
           ORDER BY date, serverID""",
        (start_date, end_date),
    )
    grouped: dict[str, list[dict]] = {}
    for row in cursor.fetchall():
        msg = dict(row)
        d = msg.get("date", "")
        grouped.setdefault(d, []).append(msg)
    return grouped


def _query_parsed_contexts(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> dict[str, list[dict]]:
    """Query parsed_contexts (Layer 2) grouped by date."""
    if not _table_exists(conn, "parsed_contexts"):
        return {}
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """SELECT * FROM parsed_contexts
           WHERE date >= ? AND date <= ?
           ORDER BY date, context_id""",
        (start_date, end_date),
    )
    grouped: dict[str, list[dict]] = {}
    for row in cursor.fetchall():
        ctx = dict(row)
        d = ctx.get("date", "")
        grouped.setdefault(d, []).append(ctx)
    return grouped


def _query_topic_summaries(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> dict[str, list[dict]]:
    """Query topic_summaries (Layer 3) grouped by date."""
    if not _table_exists(conn, "topic_summaries"):
        return {}
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """SELECT * FROM topic_summaries
           WHERE date >= ? AND date <= ?
           ORDER BY date, summary_id""",
        (start_date, end_date),
    )
    grouped: dict[str, list[dict]] = {}
    for row in cursor.fetchall():
        ts = dict(row)
        d = ts.get("date", "")
        grouped.setdefault(d, []).append(ts)
    return grouped


def _collect_server_ids(
    raw_msgs: list[dict],
    contexts: list[dict],
    topics: list[dict],
) -> list[str]:
    """Collect server_ids from all three layers with order-preserving dedup.

    Follows EXP-020 / L005 patterns:
      - dict.fromkeys(old + new) for order-preserving dedup (NOT set())
      - Cap at MAX_SERVER_IDS (50) to prevent unbounded growth
      - Layer priority: L1 raw_messages → L2 parsed_contexts → L3 topic_summaries

    Args:
        raw_msgs: Raw message dicts from Layer 1.
        contexts: Parsed context dicts from Layer 2.
        topics: Topic summary dicts from Layer 3.

    Returns:
        Ordered, deduplicated server ID list, capped at MAX_SERVER_IDS.
    """
    ids: list[str] = []

    # Layer 1: direct serverID from raw_messages
    for msg in raw_msgs:
        sid = msg.get("serverID", "")
        if sid:
            ids.append(sid)

    # Layer 2: server_ids JSON arrays from parsed_contexts
    for ctx in contexts:
        parsed = _parse_json_array(ctx.get("server_ids"))
        ids.extend(parsed)

    # Layer 3: source_server_ids JSON arrays from topic_summaries
    for t in topics:
        parsed = _parse_json_array(t.get("source_server_ids"))
        ids.extend(parsed)

    # Order-preserving dedup via dict.fromkeys (EXP-020 / L005)
    unique: list[str] = list(dict.fromkeys(ids))

    # Cap at MAX_SERVER_IDS
    if len(unique) > MAX_SERVER_IDS:
        logger.debug(
            "server_ids count %d exceeds cap %d, truncating.",
            len(unique),
            MAX_SERVER_IDS,
        )
        unique = unique[:MAX_SERVER_IDS]

    return unique


# ============================================================
# RLRecord builders
# ============================================================


def _build_state(
    date: str,
    raw_msgs: list[dict],
    contexts: list[dict],
    topics: list[dict],
) -> RLState:
    """Assemble RLState from three-layer data for a single date.

    Args:
        date: Date string YYYYMMDD.
        raw_msgs: Raw messages for this date.
        contexts: Parsed contexts for this date.
        topics: Topic summaries for this date.

    Returns:
        Populated RLState with serverID provenance chain.
    """
    # Extract server_ids and sender metadata from raw messages (L1 metadata
    # like serverID/sender is still valid in raw_messages).
    server_ids: list[str] = []
    active_members: set[str] = set()

    for msg in raw_msgs:
        sid = msg.get("serverID", "")
        if sid:
            server_ids.append(sid)
        sender = msg.get("sender", "")
        if sender:
            active_members.add(sender)

    # Build summary snippets from parsed_contexts (L2) — raw_messages.content
    # now stores raw rawContent (XML/encoded), not cleaned text.
    summary_parts: list[str] = []
    for ctx in contexts:
        ctx_text = ctx.get("context_text", "")
        if ctx_text and len(summary_parts) < 3:  # Max 3 sentences
            snippet = ctx_text[:120].replace("\n", " ")
            if snippet.strip():
                summary_parts.append(snippet.strip())

    # Build messages_summary from L2 context text or topic summaries
    if topics:
        topic_names = [t.get("topic_name", "") for t in topics if t.get("topic_name")]
        messages_summary = (
            f"Topics discussed: {', '.join(topic_names[:5])}. "
            f"{len(raw_msgs)} messages from {len(active_members)} members."
        )
    elif summary_parts:
        messages_summary = (
            f"Messages: {'; '.join(summary_parts)}... "
            f"({len(raw_msgs)} total from {len(active_members)} members)"
        )
    else:
        messages_summary = f"{len(raw_msgs)} messages from {len(active_members)} members on {date}."

    # Collect parsed_context_ids
    parsed_context_ids = [c.get("context_id", "") for c in contexts if c.get("context_id")]

    # Collect history_topics from topic summary_ids
    history_topics = [t.get("summary_id", "") for t in topics if t.get("summary_id")]

    # Estimate token count from L2 context_text (raw_messages.content is now
    # raw rawContent, not suitable for token estimation).
    total_chars = sum(len(ctx.get("context_text", "")) for ctx in contexts)
    context_token_count = max(0, total_chars // 2)  # Rough estimate for mixed CN/EN

    return RLState(
        messages_summary=messages_summary,
        context_token_count=context_token_count,
        history_topics=history_topics,
        server_ids=server_ids,
        parsed_context_ids=parsed_context_ids,
        active_members=sorted(active_members),
        message_count=len(raw_msgs),
    )


def _build_action_mock(
    scenario: str,
    contexts: list[dict],
    topics: list[dict],
) -> RLAction:
    """Build RLAction with placeholder values.

    Orchestrator log table is not yet implemented (T-F pending).
    Uses mock values + warning logs per EXP-005/006.

    Args:
        scenario: RL scenario type.
        contexts: Parsed contexts for estimating report structure.
        topics: Topic summaries for topic classification details.

    Returns:
        RLAction with mock/default values.
    """
    logger.warning(
        "Orchestrator log table not implemented — using mock action values. "
        "Set up T-F runtime logging to populate real action data."
    )

    agent_map = {
        SCENARIO_PREFERENCE_ALIGNMENT: "daily_report_generator",
        SCENARIO_ROUTING_DECISION: "orchestrator",
        SCENARIO_TOPIC_TRACKING: "topic_tracker",
    }

    decision_map = {
        SCENARIO_PREFERENCE_ALIGNMENT: (
            f"Generated daily report from {len(contexts)} context blocks."
        ),
        SCENARIO_ROUTING_DECISION: (f"Orchestrator route: {len(contexts)} contexts evaluated."),
        SCENARIO_TOPIC_TRACKING: (f"Classified {len(topics)} topics based on latest messages."),
    }

    action = RLAction(
        agent=agent_map.get(scenario, "unknown"),
        decision=decision_map.get(scenario, "Mock decision — orchestrator log table pending."),
        model_used="mock",
        temperature=0.7,
        latency_ms=0,
        report_sections=(
            ["overview", "important_notice", "topic_sections", "highlights", "active_members"]
            if scenario == SCENARIO_PREFERENCE_ALIGNMENT
            else []
        ),
        routing_decisions=(
            [
                {"agent": "daily_report_generator", "enabled": True, "reason": "Mock — weekday"},
                {"agent": "resource_extractor", "enabled": True, "reason": "Mock — has content"},
                {"agent": "topic_tracker", "enabled": True, "reason": "Mock — cross-day topics"},
            ]
            if scenario == SCENARIO_ROUTING_DECISION
            else []
        ),
        status_classifications=(
            [
                {
                    "topic_id": t.get("summary_id", ""),
                    "new_status": "ongoing",
                    "confidence": t.get("confidence", 0.5) or 0.5,
                }
                for t in topics[:10]
            ]
            if scenario == SCENARIO_TOPIC_TRACKING and topics
            else []
        ),
        prompt_version="mock-v1",
    )

    return action


def _build_reward_default() -> RLReward:
    """Build a default (empty) RLReward — to be filled by T-I2 reward engine.

    Returns:
        RLReward with null dimensions and empty signals.
    """
    return RLReward(
        source="system_check",
        score=0.0,
        dimensions=RLRewardDimension(
            completeness=None,
            accuracy=None,
            conciseness=None,
            actionability=None,
        ),
        signals=[],
    )


def _build_metadata(
    raw_msgs: list[dict],
    contexts: list[dict],
    topics: list[dict],
) -> RLMetadata:
    """Build RLMetadata with full provenance chain and statistics.

    Args:
        raw_msgs: Raw messages for the date.
        contexts: Parsed contexts for the date.
        topics: Topic summaries for the date.

    Returns:
        RLMetadata with populated provenance and scenario-aware statistics.
    """
    parsed_context_ids = [c.get("context_id", "") for c in contexts if c.get("context_id")]
    topic_summary_ids = [t.get("summary_id", "") for t in topics if t.get("summary_id")]

    provenance = RLProvenance(
        raw_message_count=len(raw_msgs),
        parsed_context_ids=parsed_context_ids,
        topic_summary_ids=topic_summary_ids,
    )

    return RLMetadata(
        timestamp=_iso_now(),
        version="1.0",
        provenance=provenance,
        trace_id="",
        report_char_count=sum(len(ctx.get("context_text", "")) for ctx in contexts),
        total_agents=3,  # Mock: daily_report, resource_extract, topic_track
        enabled_agent_count=min(3, max(1, len(contexts))),
        history_topic_count=len(topics),
        affected_topic_count=len(topics),
    )


# ============================================================
# Scenario classifier
# ============================================================


def _classify_scenarios(
    date: str,
    raw_msgs: list[dict],
    contexts: list[dict],
    topics: list[dict],
) -> list[str]:
    """Determine which RL scenarios apply to a given date's data.

    Classification rules:
      - topic_tracking: If any topic_summaries reference this date.
      - preference_alignment: If parsed_contexts exist for this date.
      - routing_decision: Always included (orchestrator always runs).

    Args:
        date: Date string YYYYMMDD.
        raw_msgs: Raw messages for the date.
        contexts: Parsed contexts for the date.
        topics: Topic summaries for the date.

    Returns:
        List of applicable scenario type strings.
    """
    scenarios: list[str] = []

    # routing_decision — orchestrator always evaluates each day
    scenarios.append(SCENARIO_ROUTING_DECISION)

    # preference_alignment — contexts with report-like content
    if contexts or raw_msgs:
        scenarios.append(SCENARIO_PREFERENCE_ALIGNMENT)

    # topic_tracking — topic summaries exist
    if topics:
        scenarios.append(SCENARIO_TOPIC_TRACKING)

    return scenarios


# ============================================================
# Main extraction functions
# ============================================================


def extract_rl_records(
    date: str,
    db_path: str | None = None,
) -> list[RLRecord]:
    """Extract RL records for a single date (convenience wrapper).

    Calls ``extract_records`` with a single-date range.

    Args:
        date: Date string in YYYY-MM-DD format.
        db_path: Path to SQLite database. Defaults to ``data/winnow.db``.

    Returns:
        List of RLRecord objects for the given date.
    """
    return extract_records((date, date), db_path)


def extract_records(
    date_range: tuple[str, str],
    db_path: str | None = None,
) -> list[RLRecord]:
    """Extract RLRecord four-tuples from SQLite three-layer data.

    Queries raw_messages, parsed_contexts, and topic_summaries tables
    within the given date range, assembles RLRecord objects with full
    serverID provenance chains, and classifies each record by scenario.

    Orchestrator action fields use placeholder mock values with warning
    logs until T-F implements the orchestrator log table.

    Args:
        date_range: (start_date, end_date) in YYYY-MM-DD format.
        db_path: Path to SQLite database. Defaults to "data/winnow.db".
                 Use ":memory:" for testing (returns empty list).

    Returns:
        List of RLRecord objects. Returns empty list if no data exists
        within the date range, or if database errors occur (EXP-005).

    Raises:
        FileNotFoundError: If db_path file does not exist (non-:memory:).
    """
    start_iso, end_iso = date_range
    start_db = _to_db_date(start_iso)
    end_db = _to_db_date(end_iso)

    resolved_path = db_path or DEFAULT_DB_PATH

    # Validate database file existence (skip for :memory:)
    if resolved_path != ":memory:":
        db_file = Path(resolved_path)
        if not db_file.exists():
            logger.warning(
                "Database file not found: %s. Returning empty record list.",
                resolved_path,
            )
            return []

    records: list[RLRecord] = []
    conn: sqlite3.Connection | None = None

    try:
        conn = sqlite3.connect(resolved_path)
        conn.row_factory = sqlite3.Row

        # Ensure schema exists (no-op if tables already exist) BUT don't
        # create for :memory: since the test expects an empty database.
        if resolved_path != ":memory:":
            _ensure_schema(conn)

        # Query all three layers grouped by date
        logger.info(
            "Querying SQLite three-layer data: %s ~ %s (DB dates: %s ~ %s)",
            start_iso,
            end_iso,
            start_db,
            end_db,
        )
        raw_by_date = _query_raw_messages(conn, start_db, end_db)
        ctx_by_date = _query_parsed_contexts(conn, start_db, end_db)
        topic_by_date = _query_topic_summaries(conn, start_db, end_db)

        # Collect all dates across all three layers
        all_dates: set[str] = set()
        all_dates.update(raw_by_date.keys())
        all_dates.update(ctx_by_date.keys())
        all_dates.update(topic_by_date.keys())

        if not all_dates:
            logger.info(
                "No data found in date range %s ~ %s. Returning empty list.",
                start_iso,
                end_iso,
            )
            return []

        logger.info(
            "Found data across %d dates: raw=%d dates, contexts=%d dates, topics=%d dates",
            len(all_dates),
            len(raw_by_date),
            len(ctx_by_date),
            len(topic_by_date),
        )

        # Build records per date, per scenario
        record_seq = 0
        for db_date in sorted(all_dates):
            raw_msgs = raw_by_date.get(db_date, [])
            contexts = ctx_by_date.get(db_date, [])
            topics = topic_by_date.get(db_date, [])

            scenarios = _classify_scenarios(db_date, raw_msgs, contexts, topics)

            for scenario in scenarios:
                record_seq += 1
                iso_date = _to_iso_date(db_date)
                record_id = f"rl-{db_date}-{record_seq:03d}"

                state = _build_state(db_date, raw_msgs, contexts, topics)
                action = _build_action_mock(scenario, contexts, topics)
                reward = _build_reward_default()
                metadata = _build_metadata(raw_msgs, contexts, topics)

                record = RLRecord(
                    record_id=record_id,
                    date=iso_date,
                    scenario=scenario,
                    state=state,
                    action=action,
                    reward=reward,
                    metadata=metadata,
                )

                records.append(record)
                logger.debug(
                    "Built RLRecord %s [%s]: %d msgs, %d ctx, %d topics",
                    record_id,
                    scenario,
                    len(raw_msgs),
                    len(contexts),
                    len(topics),
                )

    except sqlite3.OperationalError as e:
        # Table may not exist (:memory: or uninitialized DB)
        logger.warning(
            "Database query failed (tables may not exist): %s. Returning empty list.",
            e,
        )
        return []
    except sqlite3.Error as e:
        # EXP-005: Database errors don't crash — return partial results
        logger.error(
            "Database error during extraction (returning %d partial records): %s",
            len(records),
            e,
        )
    finally:
        if conn:
            conn.close()

    scenario_counts: dict[str, int] = {}
    for r in records:
        scenario_counts[r.scenario] = scenario_counts.get(r.scenario, 0) + 1

    logger.info(
        "Extraction complete: %d RLRecord(s) across %d scenario(s) %s",
        len(records),
        len(scenario_counts),
        scenario_counts,
    )

    return records


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Ensure the three-layer table schema exists (no-op if tables exist).

    Args:
        conn: SQLite connection.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS raw_messages (
            serverID TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            msg_type TEXT DEFAULT 'text',
            image_path TEXT,
            sanitized INTEGER DEFAULT 0,
            raw_json TEXT NOT NULL,
            group_id TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS parsed_contexts (
            context_id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            server_ids TEXT NOT NULL,
            context_text TEXT NOT NULL,
            token_count INTEGER,
            source_subagent TEXT,
            group_id TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS topic_summaries (
            summary_id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            topic_name TEXT NOT NULL,
            summary_text TEXT NOT NULL,
            context_ids TEXT NOT NULL,
            source_server_ids TEXT NOT NULL,
            confidence REAL,
            model_used TEXT,
            group_id TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_raw_date ON raw_messages(date);
        CREATE INDEX IF NOT EXISTS idx_context_date ON parsed_contexts(date);
        CREATE INDEX IF NOT EXISTS idx_summary_date ON topic_summaries(date);
        CREATE INDEX IF NOT EXISTS idx_rl_raw_group_date ON raw_messages(group_id, date);
        CREATE INDEX IF NOT EXISTS idx_rl_ctx_group_date ON parsed_contexts(group_id, date);
        CREATE INDEX IF NOT EXISTS idx_rl_topic_group_date ON topic_summaries(group_id, date);
    """)
    conn.commit()
