"""T-I2: Reward rule engine — deterministic rule-based reward computation.

Computes multi-dimensional reward scores for RLRecord four-tuples using
code-level rules (no LLM). Follows EXP-016: deterministic state inference.

Reward dimensions:
    - completeness (rule_based):    Are daily report sections complete?
    - conciseness (rule_based):     Is the output appropriately concise?
    - actionability (rule_based):   Are there actionable items in the output?
    - accuracy (auto_comparison):   How does output compare to legacy system?
                                    (NaN if legacy_output not available)

Usage:
    from z_winnow.rl.reward import compute_reward

    reward = compute_reward(record)
    reward = compute_reward(record, legacy_output={"topics": [...]})

Reference:
    plans/tracks/track-i.md T-I2
    docs/rl-training-requirements.md §4 Reward Source Strategy
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from z_winnow.rl.schema import (
    RLRecord,
    RLReward,
    RLRewardDimension,
    RLRewardSignal,
)

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

# Required report sections for preference_alignment scenario
REQUIRED_SECTIONS = {
    "overview",
    "important_notice",
    "topic_sections",
    "highlights",
    "active_members",
}

# All expected sections (including optional). Custom tables (engineering /
# world_models / …) now live under the ``custom_tables`` slot rather than a
# hardcoded top-level ``engineering_issues`` key.
ALL_EXPECTED_SECTIONS = REQUIRED_SECTIONS | {
    "trend_analysis",
    "resources",
    "custom_tables",
}

# Actionability keywords for Chinese tech discussions
ACTION_KEYWORDS = [
    "TODO",
    "FIXME",
    "建议",
    "方案",
    "待办",
    "任务",
    "下一步",
    "跟进",
    "分配",
    "负责人",
    "截止",
    "action",
    "action item",
    "next step",
    "follow up",
    "assign",
    "deadline",
]

# Compiled regex for action keywords (case-insensitive)
_ACTION_PATTERN = re.compile(
    "|".join(re.escape(kw) for kw in ACTION_KEYWORDS),
    re.IGNORECASE,
)

# Conciseness thresholds
CONCISENESS_MIN_CHARS = 200  # Below this, considered too short
CONCISENESS_TARGET_MIN = 800  # Ideal range start
CONCISENESS_TARGET_MAX = 3000  # Ideal range end
CONCISENESS_MAX_CHARS = 8000  # Above this, severely penalized

# Weight for each dimension when computing aggregate score
DIMENSION_WEIGHTS = {
    "completeness": 0.30,
    "accuracy": 0.25,
    "conciseness": 0.25,
    "actionability": 0.20,
}


# ============================================================
# Rule-based dimension calculators
# ============================================================


def _compute_completeness(record: RLRecord) -> tuple[float, str]:
    """Calculate completeness score based on report section coverage.

    Checks whether the action report_sections contains the expected
    sections for the given scenario.

    Args:
        record: The RL record to evaluate.

    Returns:
        (score, evidence) tuple. Score in [0, 1].
    """
    sections = record.action.report_sections

    if not sections:
        # No report sections at all — likely not a preference_alignment record
        if record.scenario == "preference_alignment":
            return (0.0, "No report sections found for preference_alignment scenario.")
        return (1.0, "No report sections expected for non-preference_alignment scenario.")

    # Count how many required sections are present
    found_required = REQUIRED_SECTIONS & set(sections)
    found_all = ALL_EXPECTED_SECTIONS & set(sections)

    if len(found_required) == 0:
        return (
            0.0,
            f"None of the {len(REQUIRED_SECTIONS)} required sections present. Found: {sections}",
        )

    # Score: proportion of required sections present
    score = len(found_required) / len(REQUIRED_SECTIONS)

    evidence = (
        f"{len(found_required)}/{len(REQUIRED_SECTIONS)} required sections present "
        f"({len(found_all)}/{len(ALL_EXPECTED_SECTIONS)} including optional). "
        f"Found: {sorted(found_required)}. "
        f"Missing: {sorted(REQUIRED_SECTIONS - found_required)}."
    )

    return (round(score, 2), evidence)


def _compute_conciseness(record: RLRecord) -> tuple[float, str]:
    """Calculate conciseness score based on output length vs message count.

    Evaluates whether the report length is appropriate relative to the
    amount of input data. Too short or too long both reduce the score.

    Args:
        record: The RL record to evaluate.

    Returns:
        (score, evidence) tuple. Score in [0, 1].
    """
    char_count = record.metadata.report_char_count
    msg_count = record.metadata.provenance.raw_message_count

    if char_count == 0 and msg_count == 0:
        return (0.0, "No report content and no messages — cannot assess conciseness.")

    if char_count == 0:
        return (0.0, f"Report has 0 characters for {msg_count} messages — empty output.")

    if msg_count == 0:
        # Report exists but no source messages — unusual
        return (0.5, f"Report has {char_count} chars but 0 source messages — cannot normalize.")

    # Compute ratio: chars per message
    chars_per_msg = char_count / msg_count

    if chars_per_msg <= 20:
        # Very terse — might be too brief
        score = max(0.3, chars_per_msg / 20.0)
        evidence = (
            f"Very terse: {chars_per_msg:.0f} chars/msg ({char_count} chars / "
            f"{msg_count} msgs). Score scaled from brevity."
        )
    elif chars_per_msg <= 80:
        # Good range — concise but not sparse
        score = 1.0
        evidence = (
            f"Good conciseness: {chars_per_msg:.0f} chars/msg "
            f"({char_count} chars / {msg_count} msgs)."
        )
    elif chars_per_msg <= 200:
        # Slightly verbose — linear decay
        ratio = (chars_per_msg - 80) / 120.0  # 0 at 80, 1 at 200
        score = round(1.0 - 0.4 * ratio, 2)
        evidence = (
            f"Moderately verbose: {chars_per_msg:.0f} chars/msg "
            f"({char_count} chars / {msg_count} msgs). Score={score:.2f}."
        )
    elif chars_per_msg <= 500:
        # Verbose — steeper decay
        ratio = (chars_per_msg - 200) / 300.0  # 0 at 200, 1 at 500
        score = round(max(0.1, 0.6 - 0.5 * ratio), 2)
        evidence = (
            f"Verbose: {chars_per_msg:.0f} chars/msg "
            f"({char_count} chars / {msg_count} msgs). Score={score:.2f}."
        )
    else:
        # Very verbose — heavily penalized
        score = 0.1
        evidence = (
            f"Excessively verbose: {chars_per_msg:.0f} chars/msg "
            f"({char_count} chars / {msg_count} msgs). Score={score:.2f}."
        )

    return (score, evidence)


def _compute_actionability(record: RLRecord) -> tuple[float, str]:
    """Calculate actionability score based on presence of action keywords.

    Searches the agent decision text and report content for actionable
    items (TODO, FIXME, suggestions, assignments, deadlines).

    Args:
        record: The RL record to evaluate.

    Returns:
        (score, evidence) tuple. Score in [0, 1].
    """
    # Collect all searchable text from the record
    search_texts: list[str] = []

    # Agent decision
    if record.action.decision:
        search_texts.append(record.action.decision)

    # Messages summary
    if record.state.messages_summary:
        search_texts.append(record.state.messages_summary)

    # Routing decisions (scenario 2)
    for rd in record.action.routing_decisions:
        if isinstance(rd, dict):
            search_texts.append(rd.get("reason", ""))

    # Status classifications (scenario 3)
    for sc in record.action.status_classifications:
        if isinstance(sc, dict):
            search_texts.append(sc.get("new_status", ""))
            search_texts.append(sc.get("topic_id", ""))

    combined_text = " ".join(search_texts)

    if not combined_text.strip():
        return (0.0, "No searchable text available for actionability analysis.")

    # Find all action keyword matches
    matches = _ACTION_PATTERN.findall(combined_text)
    unique_matches = {m.lower() for m in matches}

    if not matches:
        return (
            0.0,
            "No action keywords found in decision or context. "
            "Consider adding TODO items, suggestions (建议), or action plans (方案).",
        )

    # Score based on number of unique action keywords found
    # 1 keyword = 0.4, 2 = 0.6, 3 = 0.75, 4 = 0.85, 5+ = 0.95
    unique_count = len(unique_matches)
    total_count = len(matches)

    if unique_count >= 5:
        score = 0.95
    elif unique_count == 4:
        score = 0.85
    elif unique_count == 3:
        score = 0.75
    elif unique_count == 2:
        score = 0.60
    else:
        score = 0.40

    evidence = (
        f"Found {total_count} action keyword(s) ({unique_count} unique): "
        f"{sorted(unique_matches)}. Score={score:.2f}."
    )

    return (score, evidence)


def _compute_accuracy(
    record: RLRecord,
    legacy_output: dict[str, Any] | None = None,
) -> tuple[float | None, str]:
    """Calculate accuracy score by comparing with legacy system output.

    Uses simple text overlap comparison (Jaccard similarity on token sets)
    as a lightweight proxy for BLEU/ROUGE. Returns NaN (None) if legacy
    output is not available.

    Args:
        record: The RL record to evaluate.
        legacy_output: Optional dict with legacy system output for comparison.
            Expected keys: 'topics', 'summary', 'report_text'.

    Returns:
        (score_or_None, evidence) tuple. score is None if not computable.
    """
    if legacy_output is None:
        return (None, "No legacy output available for accuracy comparison. Marked as NaN.")

    # Build reference text from legacy output
    ref_parts: list[str] = []
    if isinstance(legacy_output, dict):
        ref_parts.append(str(legacy_output.get("summary", "")))
        ref_parts.append(str(legacy_output.get("report_text", "")))
        topics = legacy_output.get("topics", [])
        if isinstance(topics, list):
            for t in topics:
                if isinstance(t, dict):
                    ref_parts.append(str(t.get("content", "")))
                    ref_parts.append(str(t.get("name", "")))
                else:
                    ref_parts.append(str(t))
    else:
        ref_parts.append(str(legacy_output))

    ref_text = " ".join(ref_parts).strip()
    if not ref_text:
        return (None, "Legacy output provided but contains no usable text content.")

    # Build candidate text from record
    cand_parts: list[str] = []
    cand_parts.append(record.action.decision)
    cand_parts.append(record.state.messages_summary)
    for rd in record.action.routing_decisions:
        if isinstance(rd, dict):
            cand_parts.append(str(rd.get("reason", "")))
    for sc in record.action.status_classifications:
        if isinstance(sc, dict):
            cand_parts.append(str(sc.get("topic_id", "")))

    cand_text = " ".join(cand_parts).strip()

    if not cand_text:
        return (None, "No candidate text available for accuracy comparison.")

    # Simple token-set Jaccard similarity
    ref_tokens = set(ref_text.lower().split())
    cand_tokens = set(cand_text.lower().split())

    if not ref_tokens or not cand_tokens:
        return (None, "Empty token sets — cannot compute similarity.")

    intersection = ref_tokens & cand_tokens
    union = ref_tokens | cand_tokens

    if len(union) == 0:
        return (None, "Empty token union — cannot compute similarity.")

    jaccard = len(intersection) / len(union)

    # Jaccard on token sets tends to be low; apply scaling heuristic
    # Typical range: 0.05-0.40 for related texts
    # Map: 0.0→0.0, 0.05→0.3, 0.15→0.6, 0.25→0.8, 0.4→1.0
    if jaccard >= 0.40:
        score = 1.0
    elif jaccard >= 0.25:
        score = 0.8 + 0.2 * (jaccard - 0.25) / 0.15
    elif jaccard >= 0.15:
        score = 0.6 + 0.2 * (jaccard - 0.15) / 0.10
    elif jaccard >= 0.05:
        score = 0.3 + 0.3 * (jaccard - 0.05) / 0.10
    else:
        score = 0.3 * jaccard / 0.05

    score = round(min(1.0, max(0.0, score)), 2)

    evidence = (
        f"Jaccard similarity with legacy output: {jaccard:.4f} "
        f"(ref_tokens={len(ref_tokens)}, cand_tokens={len(cand_tokens)}, "
        f"intersection={len(intersection)}). Scaled score={score:.2f}."
    )

    return (score, evidence)


# ============================================================
# Main compute_reward function
# ============================================================


def compute_reward(
    record: RLRecord,
    legacy_output: dict[str, Any] | None = None,
) -> RLReward:
    """Compute multi-dimensional reward for an RL record.

    Computes four quality dimensions using deterministic rule-based checks
    and optional legacy system comparison. Returns a fully populated RLReward
    with per-dimension scores, evidence strings, and signal entries.

    Args:
        record: The RLRecord to evaluate. Must have state and action populated.
        legacy_output: Optional dict with legacy system output for accuracy
            comparison. If None, accuracy dimension is set to NaN (None).

    Returns:
        RLReward with:
          - dimensions: Per-dimension scores (completeness, accuracy, conciseness, actionability)
          - signals: Individual signal entries with evidence
          - score: Weighted aggregate score [0, 1]
          - source: "rule_based" | "auto_comparison" (dominant source)

    Raises:
        TypeError: If record is not an RLRecord instance.
    """
    if not isinstance(record, RLRecord):
        raise TypeError(
            f"Expected RLRecord, got {type(record).__name__}. "
            f"Pass a valid RLRecord instance from the extractor."
        )

    timestamp = datetime.now(UTC).isoformat()
    signals: list[RLRewardSignal] = []

    # --- Rule-based dimensions ---

    # 1. Completeness (rule_based)
    completeness_score, completeness_evidence = _compute_completeness(record)
    signals.append(
        RLRewardSignal(
            signal_type="rule_based",
            source="system_check",
            value=completeness_score,
            dimension="completeness",
            evidence=completeness_evidence,
            timestamp=timestamp,
        )
    )

    # 2. Conciseness (rule_based)
    conciseness_score, conciseness_evidence = _compute_conciseness(record)
    signals.append(
        RLRewardSignal(
            signal_type="rule_based",
            source="system_check",
            value=conciseness_score,
            dimension="conciseness",
            evidence=conciseness_evidence,
            timestamp=timestamp,
        )
    )

    # 3. Actionability (rule_based)
    actionability_score, actionability_evidence = _compute_actionability(record)
    signals.append(
        RLRewardSignal(
            signal_type="rule_based",
            source="system_check",
            value=actionability_score,
            dimension="actionability",
            evidence=actionability_evidence,
            timestamp=timestamp,
        )
    )

    # --- Comparison dimension ---

    # 4. Accuracy (auto_comparison or NaN)
    accuracy_score, accuracy_evidence = _compute_accuracy(record, legacy_output)

    if accuracy_score is not None:
        signals.append(
            RLRewardSignal(
                signal_type="auto_comparison",
                source="old_system",
                value=accuracy_score,
                dimension="accuracy",
                evidence=accuracy_evidence,
                timestamp=timestamp,
            )
        )

    # --- Build dimensions object ---
    dimensions = RLRewardDimension(
        completeness=completeness_score,
        accuracy=accuracy_score,
        conciseness=conciseness_score,
        actionability=actionability_score,
    )

    # --- Determine dominant source ---
    # If accuracy was computed, this includes auto_comparison
    has_auto_comparison = accuracy_score is not None
    dominant_source = "auto_comparison" if has_auto_comparison else "rule_based"

    # --- Compute aggregate score ---
    # Weighted average of available dimensions
    active_weights: dict[str, float] = {}
    active_scores: dict[str, float] = {}

    if completeness_score is not None:
        active_weights["completeness"] = DIMENSION_WEIGHTS["completeness"]
        active_scores["completeness"] = completeness_score
    if accuracy_score is not None:
        active_weights["accuracy"] = DIMENSION_WEIGHTS["accuracy"]
        active_scores["accuracy"] = accuracy_score
    if conciseness_score is not None:
        active_weights["conciseness"] = DIMENSION_WEIGHTS["conciseness"]
        active_scores["conciseness"] = conciseness_score
    if actionability_score is not None:
        active_weights["actionability"] = DIMENSION_WEIGHTS["actionability"]
        active_scores["actionability"] = actionability_score

    if active_weights:
        total_weight = sum(active_weights.values())
        # Renormalize weights for available dimensions
        aggregate_score = sum(
            active_scores[dim] * (active_weights[dim] / total_weight) for dim in active_weights
        )
    else:
        aggregate_score = 0.0

    aggregate_score = round(min(1.0, max(0.0, aggregate_score)), 4)

    # Track which scenario-inapplicable dimensions were set to null
    # (Scenario 2 routing_decision: completeness, conciseness not primary)
    # (Scenario 3 topic_tracking: completeness, conciseness, actionability not primary)

    logger.debug(
        "Reward computed for %s [%s]: score=%.4f, source=%s, "
        "completeness=%s, accuracy=%s, conciseness=%s, actionability=%s",
        record.record_id,
        record.scenario,
        aggregate_score,
        dominant_source,
        completeness_score,
        accuracy_score,
        conciseness_score,
        actionability_score,
    )

    return RLReward(
        source=dominant_source,
        score=aggregate_score,
        dimensions=dimensions,
        signals=signals,
    )
