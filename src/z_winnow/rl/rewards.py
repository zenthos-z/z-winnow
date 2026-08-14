"""T-I2: Rule-based reward computation engine.

Computes multi-dimensional quality scores for RLRecord four-tuples without
requiring human annotation. Uses rule-based heuristics for automatic scoring.

Dimensions:
    - completeness (0-1): Report section coverage and summary quality.
    - conciseness (0-1): Output brevity relative to input size.
    - actionability (0-1): Presence of actionable keywords in decisions.
    - accuracy (0-1): Text similarity with legacy system output (NaN if unavailable).

Source marking: "rule_based" for auto dimensions, "auto_comparison" for legacy-based.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import UTC, datetime

from z_winnow.rl.schema import (
    RLRecord,
    RLReward,
    RLRewardDimension,
    RLRewardSignal,
)

logger = logging.getLogger(__name__)

# ============================================================
# Action keyword patterns
# ============================================================

# Chinese action keywords in agent decisions
_ACTION_KEYWORDS_CN = [
    r"TODO",
    r"FIXME",
    r"建议",
    r"方案",
    r"待办",
    r"计划",
    r"执行",
    r"实施",
    r"跟进",
    r"优化",
    r"改进",
    r"修复",
    r"升级",
    r"部署",
    r"配置",
    r"调整",
    r"确认",
    r"验证",
    r"测试",
    r"发布",
]

# English action keywords
_ACTION_KEYWORDS_EN = [
    r"\bTODO\b",
    r"\bFIXME\b",
    r"\bfix\b",
    r"\bdeploy\b",
    r"\bconfig\b",
    r"\boptimize\b",
    r"\brefactor\b",
    r"\btest\b",
    r"\brelease\b",
    r"\bupdate\b",
    r"\bupgrade\b",
    r"\bmigrate\b",
    r"\bvalidate\b",
    r"\bconfirm\b",
]

_REQUIRED_SECTIONS = ["概述", "议题", "资源", "问题"]


def _iso_timestamp() -> str:
    """Return current UTC ISO 8601 timestamp."""
    return datetime.now(UTC).isoformat()


# ============================================================
# Completeness scoring (rule_based)
# ============================================================


def _score_completeness(record: RLRecord) -> tuple[float, str]:
    """Score daily report completeness [0, 1].

    Evaluates:
    - messages_summary quality (depth, sender info, topics mentioned)
    - report_sections coverage (expected sections present)
    - state richness (has parsed_context_ids, has active_members)

    Args:
        record: RLRecord to score.

    Returns:
        (score, evidence) tuple.
    """
    score = 0.0
    max_score = 0.0
    reasons: list[str] = []

    state = record.state
    action = record.action

    # 1. Messages summary depth (0-0.35)
    max_score += 0.35
    summary = state.messages_summary
    if summary and len(summary) > 20:
        sub_score = 0.0
        if any(c in summary for c in ["条消息", "messages", "个成员", "members"]):
            sub_score += 0.15
            reasons.append("summary includes message/member counts")
        if any(c in summary for c in ["议题", "topic", "主题"]):
            sub_score += 0.10
            reasons.append("summary mentions topics")
        if len(summary.split("。")) >= 2 or len(summary.split(".")) >= 2:
            sub_score += 0.10
            reasons.append("summary has multi-sentence structure")
        score += sub_score
    elif summary:
        score += 0.10  # Minimal credit for having something
        reasons.append("summary exists but brief")
    else:
        reasons.append("no messages summary")

    # 2. Report sections presence (0-0.25)
    max_score += 0.25
    sections = action.report_sections
    if sections:
        matched = sum(1 for s in _REQUIRED_SECTIONS if any(s in sec for sec in sections))
        sub_score = min(0.25, matched * 0.0625)
        score += sub_score
        if matched >= 3:
            reasons.append(f"report covers {matched}/4 expected sections")
        else:
            reasons.append(f"report covers only {matched}/4 expected sections")
    else:
        reasons.append("no report sections defined")

    # 3. State metadata richness (0-0.25)
    max_score += 0.25
    richness = 0.0
    if state.parsed_context_ids:
        richness += 0.08
    if state.active_members:
        richness += 0.08
    if state.server_ids:
        richness += 0.05
    if state.history_topics:
        richness += 0.04
    score += richness
    if richness >= 0.15:
        reasons.append("rich provenance metadata")
    elif richness > 0:
        reasons.append("partial provenance metadata")

    # 4. Topic coverage (0-0.15)
    max_score += 0.15
    topic_count = record.metadata.affected_topic_count
    if topic_count > 0:
        sub_score = min(0.15, topic_count * 0.03)
        score += sub_score
        reasons.append(f"{topic_count} topics covered")
    else:
        reasons.append("no topics covered")

    evidence = "; ".join(reasons) if reasons else "completeness score is 0"
    return (score, evidence)


# ============================================================
# Conciseness scoring (rule_based)
# ============================================================


def _score_conciseness(record: RLRecord) -> tuple[float, str]:
    """Score conciseness [0, 1] — penalizes verbosity.

    Compares output length (summary + report) to input message count.
    Lower ratio → higher conciseness. Too verbose → low score.

    Formula:
        ideal_chars = message_count * 30  (30 chars/message is efficient)
        actual_chars = len(messages_summary) + report_char_count
        ratio = ideal_chars / max(1, actual_chars)
        score = min(1.0, ratio * 2)  # Scale up (a 2:1 ideal:actual = full score)

    Args:
        record: RLRecord to score.

    Returns:
        (score, evidence) tuple.
    """
    msg_count = max(1, record.state.message_count)
    summary_len = len(record.state.messages_summary)
    report_len = record.metadata.report_char_count
    total_chars = summary_len + report_len

    # Ideal: about 30 chars per message for the summary, 50 for report context
    ideal_chars = msg_count * 80
    actual_chars = max(1, total_chars)

    ratio = ideal_chars / actual_chars
    score = min(1.0, ratio * 2)  # Scale so reasonable verbosity gets high score

    if total_chars == 0:
        score = 0.0
        evidence = "no output text — conciseness scored 0"
    elif ratio >= 0.8:
        evidence = (
            f"very concise: {actual_chars} chars for {msg_count} messages "
            f"(ideal ~{ideal_chars}, ratio={ratio:.2f})"
        )
    elif ratio >= 0.4:
        evidence = (
            f"reasonably concise: {actual_chars} chars for {msg_count} messages (ratio={ratio:.2f})"
        )
    else:
        evidence = (
            f"verbose: {actual_chars} chars for {msg_count} messages "
            f"(ideal ~{ideal_chars}, ratio={ratio:.2f})"
        )

    return (round(score, 4), evidence)


# ============================================================
# Actionability scoring (rule_based)
# ============================================================


def _score_actionability(record: RLRecord) -> tuple[float, str]:
    """Score actionability [0, 1] — presence of actionable keywords.

    Counts how many distinct action keyword categories match in:
    - action.decision text
    - action.report_sections
    - action.status_classifications (if topic_tracking)

    Score = matched_categories / total_categories (capped at 1.0).

    Args:
        record: RLRecord to score.

    Returns:
        (score, evidence) tuple.
    """
    decision = record.action.decision
    sections_text = " ".join(record.action.report_sections)
    status_text = " ".join(
        str(sc.get("new_status", "")) for sc in record.action.status_classifications
    )
    combined = f"{decision} {sections_text} {status_text}"

    # Count distinct keyword category matches
    matched_en = [kw for kw in _ACTION_KEYWORDS_EN if re.search(kw, combined, re.IGNORECASE)]
    matched_cn = [kw for kw in _ACTION_KEYWORDS_CN if kw in combined]

    total_matches = len(matched_en) + len(matched_cn)
    total_keywords = len(_ACTION_KEYWORDS_EN) + len(_ACTION_KEYWORDS_CN)

    score = min(1.0, total_matches / max(1, total_keywords) * 5)
    # Scale: ~5 keyword hits = full score

    if total_matches == 0:
        evidence = "no action keywords found in decision text"
    elif total_matches <= 2:
        evidence = f"few action keywords ({total_matches}): {matched_en + matched_cn}"
    else:
        evidence = (
            f"multiple action keywords ({total_matches}): {matched_en[:3] + matched_cn[:3]}..."
        )

    return (round(score, 4), evidence)


# ============================================================
# Accuracy scoring (auto_comparison, requires legacy output)
# ============================================================


def _score_accuracy(
    record: RLRecord,
    legacy_output: dict | None = None,
) -> tuple[float, str, str]:
    """Score accuracy [0, 1] via text similarity with legacy system output.

    Uses approximate BLEU-like word overlap. Requires T-D5 legacy adapter.

    Args:
        record: RLRecord to score.
        legacy_output: Optional legacy system output dict (from T-D5 adapter).

    Returns:
        (score, evidence, source) tuple. Returns (NaN, reason, "rule_based") if
        legacy_output is None.
    """
    if legacy_output is None:
        return (
            float("nan"),
            "legacy output not available — accuracy scored as NaN",
            "rule_based",
        )

    # Extract legacy report text
    legacy_text = legacy_output.get("report", "") or ""
    legacy_text += " " + (legacy_output.get("summary", "") or "")
    legacy_text += " " + " ".join(legacy_output.get("topic_names", []) or [])

    # Extract our text
    our_text = record.state.messages_summary
    our_text += " " + record.action.decision

    # Simple word-level overlap (character n-gram overlap for CJK)
    def char_ngrams(text: str, n: int = 2) -> set[str]:
        """Generate character n-grams for text comparison."""
        clean = text.replace("\n", " ").replace("  ", " ")
        return {clean[i : i + n] for i in range(len(clean) - n + 1)}

    if len(legacy_text) < 5 or len(our_text) < 5:
        return (
            float("nan"),
            "text too short for accuracy comparison",
            "auto_comparison" if legacy_output else "rule_based",
        )

    legacy_ngrams = char_ngrams(legacy_text, n=2)
    our_ngrams = char_ngrams(our_text, n=2)

    if not legacy_ngrams or not our_ngrams:
        return (
            float("nan"),
            "no overlapping n-grams for comparison",
            "auto_comparison",
        )

    intersection = legacy_ngrams & our_ngrams
    union = legacy_ngrams | our_ngrams

    # Jaccard similarity
    jaccard = len(intersection) / max(1, len(union))

    # BLEU-like precision (2-gram overlap)
    precision = len(intersection) / max(1, len(our_ngrams))

    score = round(min(1.0, (jaccard + precision) / 2), 4)
    evidence = (
        f"text similarity with legacy: jaccard={jaccard:.2f}, "
        f"precision={precision:.2f}, combined={score:.2f}"
    )

    source = "auto_comparison"

    return (score, evidence, source)


# ============================================================
# Main compute_reward
# ============================================================


def compute_reward(
    record: RLRecord,
    legacy_output: dict | None = None,
) -> RLReward:
    """Compute multi-dimensional reward for an RLRecord.

    Combines rule-based dimensions (completeness, conciseness, actionability)
    with optional auto_comparison dimension (accuracy, requires legacy output).

    Args:
        record: RLRecord to evaluate (action and state must be populated).
        legacy_output: Optional legacy system output dict from T-D5 adapter.
            If None, accuracy dimension is scored as NaN and source="rule_based".

    Returns:
        RLReward with populated dimensions, signals, and aggregate score.

    Example:
        >>> record = RLRecord(...)  # From extractor
        >>> reward = compute_reward(record)
        >>> assert 0.0 <= reward.score <= 1.0
        >>> assert reward.source == "rule_based"
        >>> assert reward.dimensions.completeness is not None
        >>> assert reward.dimensions.conciseness is not None
        >>> assert reward.dimensions.actionability is not None
    """
    timestamp = _iso_timestamp()
    signals: list[RLRewardSignal] = []

    # ---- Completeness (rule_based) ----
    comp_score, comp_evidence = _score_completeness(record)
    signals.append(
        RLRewardSignal(
            signal_type="rule_based",
            source="system_check",
            value=comp_score,
            dimension="completeness",
            evidence=comp_evidence,
            timestamp=timestamp,
        )
    )

    # ---- Conciseness (rule_based) ----
    conc_score, conc_evidence = _score_conciseness(record)
    signals.append(
        RLRewardSignal(
            signal_type="rule_based",
            source="system_check",
            value=conc_score,
            dimension="conciseness",
            evidence=conc_evidence,
            timestamp=timestamp,
        )
    )

    # ---- Actionability (rule_based) ----
    act_score, act_evidence = _score_actionability(record)
    signals.append(
        RLRewardSignal(
            signal_type="rule_based",
            source="system_check",
            value=act_score,
            dimension="actionability",
            evidence=act_evidence,
            timestamp=timestamp,
        )
    )

    # ---- Accuracy (auto_comparison or NaN) ----
    acc_score, acc_evidence, acc_source = _score_accuracy(record, legacy_output)
    if not math.isnan(acc_score):
        signals.append(
            RLRewardSignal(
                signal_type=acc_source,
                source="old_system" if legacy_output else "system_check",
                value=acc_score,
                dimension="accuracy",
                evidence=acc_evidence,
                timestamp=timestamp,
            )
        )
        # Source is auto_comparison when legacy output is used
        overall_source = "auto_comparison"
    else:
        overall_source = "rule_based"

    # Aggregate score = mean of all valid (non-NaN) signals
    valid_values = [s.value for s in signals if not math.isnan(s.value)]
    aggregate_score = round(sum(valid_values) / len(valid_values), 4) if valid_values else 0.0

    # Build dimensions object
    dimensions = RLRewardDimension(
        completeness=comp_score,
        accuracy=acc_score if not math.isnan(acc_score) else None,
        conciseness=conc_score,
        actionability=act_score,
    )

    return RLReward(
        source=overall_source,
        score=aggregate_score,
        dimensions=dimensions,
        signals=signals,
    )


# ============================================================
# Convenience: batch reward computation
# ============================================================


def compute_rewards_batch(
    records: list[RLRecord],
    legacy_output: dict | None = None,
) -> list[RLRecord]:
    """Compute rewards for a batch of RLRecords, mutating reward in place.

    Args:
        records: List of RLRecords (reward will be set on each).
        legacy_output: Optional legacy system output dict.

    Returns:
        Same list with reward populated on each record (mutated in place).
    """
    for record in records:
        record.reward = compute_reward(record, legacy_output)
    logger.info("Computed rewards for %d records.", len(records))
    return records


__all__ = [
    "compute_reward",
    "compute_rewards_batch",
]
