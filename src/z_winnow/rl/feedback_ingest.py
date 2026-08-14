"""Feedback ingest — tag-to-dimension mapping and feedback-to-signal conversion.

Bridges administrator feedback events into RLRewardSignal entries so that
human feedback data flows into the RL training data pool.

Reference:
    docs/rl-schema-v1.md §3.1.3 — TAG_TO_DIMENSION design
    rl/schema.py — RLRewardSignal model (not modified here)
"""

from __future__ import annotations

from typing import Any

from z_winnow.rl.schema import RLRewardSignal

# ============================================================
# P016: TAG_TO_DIMENSION mapping — single-point tag→dimension bridge
# ============================================================

TAG_TO_DIMENSION: dict[str, str] = {
    "fact_error": "accuracy",
    "missing": "completeness",
    "redundant": "conciseness",
    "wrong_category": "accuracy",
    "poor_wording": "actionability",
    "trend_reversed": "accuracy",
    "trend_overclaim": "accuracy",
    "irrelevant": "conciseness",
    "duplicate_link": "conciseness",
}


# ============================================================
# feedback_to_signal — single feedback dict → RLRewardSignal
# ============================================================


def feedback_to_signal(fb: dict[str, Any]) -> RLRewardSignal:
    """Convert a single administrator feedback dict to an RLRewardSignal.

    A008: Uses .get() for all dict access to avoid KeyError when feedback
    comes from sources other than the feedback_events table (e.g. test fixtures,
    in-memory dicts).

    Args:
        fb: Feedback dict with keys:
            - signal: "positive" | "negative" | "neutral"
            - tags: list[str] — classification tags
            - created_at: ISO 8601 timestamp
            - correction_note: optional human-readable note
            - corrected_text: optional corrected output text

    Returns:
        RLRewardSignal with signal_type="human_feedback", source="user_feedback".
    """
    # A008: defensive access — use .get() with sensible defaults
    signal = fb.get("signal", "neutral")
    tags: list[str] = fb.get("tags", [])

    # Map signal string to normalized value
    if signal == "positive":
        value: float = 1.0
    elif signal == "negative":
        value = 0.0
    else:
        value = 0.5  # neutral / unknown

    # Resolve dimension from first tag, default to "completeness"
    first_tag = tags[0] if tags else None
    dimension = TAG_TO_DIMENSION.get(first_tag, "completeness") if first_tag else "completeness"

    # Build evidence string
    note = fb.get("correction_note") or fb.get("corrected_text") or ""
    evidence = f"[{','.join(tags)}] {note}".strip()

    return RLRewardSignal(
        signal_type="human_feedback",
        source="user_feedback",
        value=value,
        dimension=dimension,
        evidence=evidence,
        timestamp=fb.get("created_at", ""),
    )


# ============================================================
# feedback_to_signals_batch — batch conversion with error isolation
# ============================================================


def feedback_to_signals_batch(feedbacks: list[dict[str, Any]]) -> list[RLRewardSignal]:
    """Convert a batch of feedback dicts to RLRewardSignal entries.

    A008: Each individual conversion is wrapped so a single bad entry
    does not crash the entire batch.

    Args:
        feedbacks: List of feedback dicts (see feedback_to_signal for schema).

    Returns:
        List of successfully converted RLRewardSignal entries.
        Malformed entries are silently dropped.
    """
    results: list[RLRewardSignal] = []

    for fb in feedbacks:
        try:
            results.append(feedback_to_signal(fb))
        except Exception:
            # A008: skip malformed entries, don't abort the batch
            continue

    return results
