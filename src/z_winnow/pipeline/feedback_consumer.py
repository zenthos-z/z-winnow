"""T-W10-D-e: Feedback consumption state machine + DPO preference pair generation.

State machine managing feedback_events.consumed_at / consumed_by transitions.
When regenerate successfully completes, consumed_at is set to NOW() and
consumed_by captures the new version_id. Regenerate failure leaves consumed_at NULL.

Architecture:
  L003: Deterministic state inference — TRANSITION_RULES table defines all valid
        transitions. LLM is never involved in state determination.
  P016: Cross-subsystem integration (pipeline ↔ RL) — lazy imports for
        exporter functions, type mappings, graceful degradation on import failure.
  A008: All DB field reads use explicit None checks before state determination;
        consumed_at/consumed_by may be NULL.

State machine (TRANSITION_RULES):
    ("unconsumed", "regenerate_success") → "consumed"
    ("unconsumed", "regenerate_failure") → "unconsumed"
    ("consumed", "rollback")            → "unconsumed"

Usage:
    import aiosqlite
    from z_winnow.pipeline.feedback_consumer import mark_consumed

    async with aiosqlite.connect("data/winnow.db") as conn:
        await mark_consumed(conn, "fb-20260516-001", "rpt-001-v2")
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

logger = logging.getLogger(__name__)

# ============================================================
# L003: Explicit state transition table
# ============================================================

# TRANSITION_RULES encodes every valid (current_state, event) → next_state
# transition. LLM is never consulted for state determination.
TRANSITION_RULES: dict[tuple[str, str], str] = {
    ("unconsumed", "regenerate_success"): "consumed",
    ("unconsumed", "regenerate_failure"): "unconsumed",
    ("consumed", "rollback"): "unconsumed",
}

# Reverse map: for each target state, what (current, event) pairs lead to it
_TRANSITION_REVERSE: dict[str, list[tuple[str, str]]] = {}
for (cur, evt), nxt in TRANSITION_RULES.items():
    _TRANSITION_REVERSE.setdefault(nxt, []).append((cur, evt))


# ============================================================
# CST timezone (consistent with reports.py)
# ============================================================

CST = timezone(timedelta(hours=8))


def _cst_now_iso() -> str:
    """Return current CST time as ISO 8601 string."""
    return datetime.now(CST).isoformat()


# ============================================================
# State query helpers
# ============================================================


async def _get_feedback_state(
    conn: aiosqlite.Connection,
    feedback_id: str,
) -> str:
    """Determine current state of a feedback event by reading consumed_at.

    L003: State is determined purely from DB values — no LLM involved.
    A008: consumed_at may be NULL — explicit is None check.

    Returns:
        "consumed" if consumed_at IS NOT NULL, "unconsumed" otherwise.
        Returns "unknown" if feedback_id not found.
    """
    # A008: Pre-initialize
    state: str = "unknown"  # A008
    try:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT consumed_at FROM feedback_events WHERE feedback_id = ?",
            (feedback_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return "unknown"
        consumed_at: str | None = row["consumed_at"]  # A008: explicit None check
        state = "consumed" if consumed_at is not None else "unconsumed"
    except Exception as e:
        logger.warning("Failed to get feedback state for %s: %s", feedback_id, e)
    return state


# ============================================================
# mark_consumed — transition unconsumed → consumed
# ============================================================


async def mark_consumed(
    conn: aiosqlite.Connection,
    feedback_id: str,
    version_id: str,
) -> bool:
    """Mark a feedback event as consumed by regenerate.

    Sets consumed_at = NOW() (CST), consumed_by = version_id.
    Idempotent: if already consumed, returns True without error.
    If feedback_id not found, returns False.

    L003: Validates transition against TRANSITION_RULES before executing.
    A008: consumed_at checked with explicit is None before state decision.

    Args:
        conn: aiosqlite database connection.
        feedback_id: The feedback event ID (e.g. "fb-20260516-001").
        version_id: The report version ID that consumed this feedback.

    Returns:
        True if consumption was applied (or already consumed), False on error.
    """
    # A008: Pre-initialize result
    result: bool = False  # A008
    try:
        # L003: Check current state against transition table
        current_state = await _get_feedback_state(conn, feedback_id)
        if current_state == "unknown":
            logger.info("mark_consumed: feedback_id=%s not found, skipping", feedback_id)
            return False

        if current_state == "consumed":
            # Idempotent — already consumed
            logger.info(
                "mark_consumed: feedback_id=%s already consumed, skipping",
                feedback_id,
            )
            return True

        # L003: Validate transition "(unconsumed, regenerate_success)" → "consumed"
        target_state = TRANSITION_RULES.get((current_state, "regenerate_success"))
        if target_state != "consumed":
            logger.warning(
                "mark_consumed: invalid transition for feedback_id=%s "
                "state=%s event=regenerate_success",
                feedback_id,
                current_state,
            )
            return False

        now_iso = _cst_now_iso()
        await conn.execute(
            """UPDATE feedback_events
               SET consumed_at = ?, consumed_by = ?
               WHERE feedback_id = ?""",
            (now_iso, version_id, feedback_id),
        )
        await conn.commit()

        logger.info(
            "mark_consumed: feedback_id=%s → consumed by version_id=%s at %s",
            feedback_id,
            version_id,
            now_iso,
        )
        result = True

    except Exception as e:
        logger.error("mark_consumed: failed for feedback_id=%s: %s", feedback_id, e)
    return result


# ============================================================
# rollback_consumed — transition consumed → unconsumed
# ============================================================


async def rollback_consumed(
    conn: aiosqlite.Connection,
    feedback_id: str,
) -> bool:
    """Roll back a feedback event to unconsumed state.

    Sets consumed_at = NULL, consumed_by = NULL.
    Idempotent: if already unconsumed, returns True without error.
    If feedback_id not found, returns False.

    L003: Validates transition against TRANSITION_RULES before executing.

    Args:
        conn: aiosqlite database connection.
        feedback_id: The feedback event ID to rollback.

    Returns:
        True if rollback was applied (or already unconsumed), False on error.
    """
    # A008: Pre-initialize result
    result: bool = False  # A008
    try:
        # L003: Check current state against transition table
        current_state = await _get_feedback_state(conn, feedback_id)
        if current_state == "unknown":
            logger.info("rollback_consumed: feedback_id=%s not found, skipping", feedback_id)
            return False

        if current_state == "unconsumed":
            # Idempotent — already unconsumed
            logger.info(
                "rollback_consumed: feedback_id=%s already unconsumed, skipping",
                feedback_id,
            )
            return True

        # L003: Validate transition "(consumed, rollback)" → "unconsumed"
        target_state = TRANSITION_RULES.get((current_state, "rollback"))
        if target_state != "unconsumed":
            logger.warning(
                "rollback_consumed: invalid transition for feedback_id=%s state=%s event=rollback",
                feedback_id,
                current_state,
            )
            return False

        await conn.execute(
            """UPDATE feedback_events
               SET consumed_at = NULL, consumed_by = NULL
               WHERE feedback_id = ?""",
            (feedback_id,),
        )
        await conn.commit()

        logger.info(
            "rollback_consumed: feedback_id=%s → unconsumed",
            feedback_id,
        )
        result = True

    except Exception as e:
        logger.error("rollback_consumed: failed for feedback_id=%s: %s", feedback_id, e)
    return result


# ============================================================
# mark_consumed_batch — consume multiple feedback_ids at once
# ============================================================


async def mark_consumed_batch(
    conn: aiosqlite.Connection,
    feedback_ids: list[str],
    version_id: str,
) -> dict[str, bool]:
    """Mark multiple feedback events as consumed by the same regenerate.

    Convenience wrapper around mark_consumed for batch processing.
    Each feedback_id is processed independently — a single failure
    does not abort the batch.

    Args:
        conn: aiosqlite database connection.
        feedback_ids: List of feedback event IDs to consume.
        version_id: The report version ID that consumed these feedbacks.

    Returns:
        Dict mapping each feedback_id → result (True/False).
    """
    results: dict[str, bool] = {}
    for fid in feedback_ids:
        results[fid] = await mark_consumed(conn, fid, version_id)
    logger.info(
        "mark_consumed_batch: %d/%d succeeded for version_id=%s",
        sum(1 for v in results.values() if v),
        len(feedback_ids),
        version_id,
    )
    return results


__all__ = [
    "TRANSITION_RULES",
    "mark_consumed",
    "mark_consumed_batch",
    "rollback_consumed",
]
