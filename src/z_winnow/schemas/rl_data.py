"""RL training data schema — Pydantic v2 models for Wave 3 RL pipeline.

Defines the data format standards for reinforcement learning training
samples, reward signals, and data quality checks.

Architecture:
    RLTrainingSample ──► 1:many ──► RewardSignal
                         aggregates raw_messages / parsed_contexts / topic_summaries
    DataQualityCheck ───► validates RLTrainingSample for completeness,
                          consistency, and timeliness

Design principles:
    1. Four-tuple orientation: (state, action, reward, metadata) unified format
    2. Multi-scenario support: preference_alignment, routing_decision, topic_tracking
    3. Full-chain provenance: every record traceable to source data (serverID → SQLite)
    4. Versioned evolution: v1 schema with extensible metadata

Reference:
    plans/tracks/track-i-schema.md — RL Schema 前置定义
    docs/architecture-detail.md — Overall architecture
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# ============================================================
# Enums
# ============================================================


class SignalType(StrEnum):
    """Type of reward signal — maps to the reward source strategy.

    Priorities (from track-i-schema.md T-I-S1):
        - rule_based: 优先 — deterministic checks (completeness, format)
        - auto_comparison: 次优先 — compare with old system output
        - human_feedback: 长期 — manual annotation / scoring
    """

    RULE_BASED = "rule_based"
    """Deterministic quality checks (e.g., all sections present, format valid)."""

    AUTO_COMPARISON = "auto_comparison"
    """Automated comparison with prior system output or reference."""

    HUMAN_FEEDBACK = "human_feedback"
    """Manual annotation or explicit user rating."""


class RewardSource(StrEnum):
    """Origin of the reward signal — where the feedback came from."""

    SYSTEM_CHECK = "system_check"
    """Automated validation rule (e.g., structure completeness)."""

    LLM_JUDGE = "llm_judge"
    """LLM-as-judge evaluation comparing outputs."""

    OLD_SYSTEM = "old_system"
    """Comparison with output from legacy winnow system."""

    HUMAN_ANNOTATOR = "human_annotator"
    """Manual annotation by human reviewer."""

    USER_FEEDBACK = "user_feedback"
    """End-user feedback (e.g., report accepted/modified/rejected)."""


class RLScenario(StrEnum):
    """RL training scenario type — three canonical use cases.

    Maps to the four-tuple (state, action, reward, metadata) with
    scenario-specific state/action constraints.
    """

    PREFERENCE_ALIGNMENT = "preference_alignment"
    """Which daily reports are better?
    state = day context, action = generated report, reward = human/system annotation.
    """

    ROUTING_DECISION = "routing_decision"
    """Which subagent should be invoked for which context?
    state = day context, action = orchestrator routing decision, reward = feedback.
    """

    TOPIC_TRACKING = "topic_tracking"
    """Topic status judgment accuracy.
    state = message context + history topics, action = status classification, reward = validation.
    """


# ============================================================
# Core Models
# ============================================================


class RewardSignal(BaseModel):
    """A single reward/feedback signal attached to a training sample.

    Each signal represents one dimension of quality feedback. Multiple
    signals can be attached to a single RLTrainingSample to capture
    multi-dimensional rewards (completeness, accuracy, conciseness, actionability).

    Attributes:
        signal_type: Type of reward signal (rule_based, auto_comparison, human_feedback).
        source: Origin of the feedback (system_check, llm_judge, old_system, human_annotator, user_feedback).
        value: Reward value, normalized to [0.0, 1.0] range.
        evidence: Human-readable evidence / justification for the reward value.
        dimension: Optional quality dimension label (e.g., "completeness", "accuracy").
        timestamp: ISO 8601 timestamp when the signal was generated.
    """

    signal_type: SignalType = Field(
        description="Type of reward signal: rule_based | auto_comparison | human_feedback"
    )
    source: RewardSource = Field(
        description="Origin of the feedback: system_check | llm_judge | old_system | human_annotator | user_feedback"
    )
    value: float = Field(
        ge=0.0,
        le=1.0,
        description="Normalized reward value in [0.0, 1.0].",
    )
    evidence: str = Field(
        default="",
        description="Human-readable justification or evidence for the reward value.",
    )
    dimension: str = Field(
        default="",
        description="Quality dimension label (e.g., 'completeness', 'accuracy', 'conciseness', 'actionability').",
    )
    timestamp: str = Field(
        default="",
        description="ISO 8601 timestamp of signal generation (e.g., '2026-04-28T10:30:00').",
    )

    @field_validator("evidence")
    @classmethod
    def evidence_required_for_non_rule(cls, v: str, info: Any) -> str:
        """Ensure evidence is provided for non-rule-based signals."""
        signal_type = info.data.get("signal_type")
        if signal_type in (SignalType.AUTO_COMPARISON, SignalType.HUMAN_FEEDBACK) and not v.strip():
            raise ValueError(
                f"Evidence is required for signal_type={signal_type}. "
                f"Provide justification for the reward value."
            )
        return v


class RLTrainingSample(BaseModel):
    """A single RL training sample linking raw data to reward signals.

    Central data structure for the RL training pipeline. Each sample
    bundles raw message IDs, parsed context IDs, topic summary IDs, and
    their associated reward signals into a single training record.

    This maps to the four-tuple concept:
        state  = (raw_messages, parsed_contexts, topic_summaries)
        action = implicitly captured via reward_signals dimension
        reward = reward_signals
        metadata = (date, scenario, model_used, trace_id)

    Attributes:
        sample_id: Unique sample identifier, format "rl-{date}-{seq}".
        date: Target date in YYYYMMDD format.
        scenario: RL training scenario type.
        raw_messages: List of source serverIDs (Layer 1 — raw_messages table).
        parsed_contexts: List of context IDs (Layer 2 — parsed_contexts table).
        topic_summaries: List of summary IDs (Layer 3 — topic_summaries table).
        reward_signals: One or more reward signals attached to this sample.
        model_used: Model identifier used to generate the action (e.g., "claude-sonnet-4-20250514").
        trace_id: Optional LangSmith trace ID for observability.
        metadata: Arbitrary extensible metadata dict for future evolution.
    """

    sample_id: str = Field(
        description="Unique sample identifier, format 'rl-{YYYYMMDD}-{seq}'.",
        pattern=r"^rl-\d{8}-\d{3,}$",
    )
    date: str = Field(
        description="Target date in YYYYMMDD format.",
        pattern=r"^\d{8}$",
    )
    scenario: RLScenario = Field(
        description="RL training scenario type.",
    )
    raw_messages: list[str] = Field(
        default_factory=list,
        description="Source serverIDs — references Layer 1 raw_messages table.",
    )
    parsed_contexts: list[str] = Field(
        default_factory=list,
        description="Context IDs — references Layer 2 parsed_contexts table.",
    )
    topic_summaries: list[str] = Field(
        default_factory=list,
        description="Summary IDs — references Layer 3 topic_summaries table.",
    )
    reward_signals: list[RewardSignal] = Field(
        default_factory=list,
        description="Reward / feedback signals for this training sample.",
    )
    model_used: str = Field(
        default="",
        description="Model identifier used to generate the action.",
    )
    trace_id: str = Field(
        default="",
        description="LangSmith trace ID for full observability.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Extensible metadata for future evolution (v2+ fields).",
    )

    @field_validator("reward_signals")
    @classmethod
    def at_least_one_signal(cls, v: list[RewardSignal]) -> list[RewardSignal]:
        """For now, signals are optional (exploration phase). In v2, may require ≥1."""
        return v

    @field_validator("raw_messages")
    @classmethod
    def server_ids_not_empty_strings(cls, v: list[str]) -> list[str]:
        """Reject empty strings in server ID list."""
        for item in v:
            if not item.strip():
                raise ValueError(f"raw_messages contains empty string: {v}")
        return v

    @model_validator(mode="after")
    def validate_cross_field_consistency(self) -> RLTrainingSample:
        """Cross-field validation: sample_id date prefix matches date field.

        Runs after all individual field validators, so both sample_id and date
        are guaranteed to be populated. In v1 exploration phase, provenance
        emptiness is not enforced.
        """
        date_value = self.date
        expected_prefix = f"rl-{date_value}-"
        if not self.sample_id.startswith(expected_prefix):
            raise ValueError(
                f"sample_id '{self.sample_id}' date prefix does not match date field "
                f"'{date_value}'. Expected prefix: '{expected_prefix}'."
            )
        return self

    @property
    def total_reward(self) -> float:
        """Compute aggregate reward as mean of all signal values.

        Returns 0.0 if no signals are attached.
        """
        if not self.reward_signals:
            return 0.0
        return sum(s.value for s in self.reward_signals) / len(self.reward_signals)

    @property
    def provenance_depth(self) -> int:
        """Count of provenance IDs across all three layers.

        Higher values indicate richer traceability; useful for filtering
        high-quality training samples.
        """
        return len(self.raw_messages) + len(self.parsed_contexts) + len(self.topic_summaries)

    @property
    def has_human_feedback(self) -> bool:
        """Whether this sample has at least one human-feedback reward signal."""
        return any(s.signal_type == SignalType.HUMAN_FEEDBACK for s in self.reward_signals)


# ============================================================
# Data Quality Check Models
# ============================================================


class QualityCheckResult(BaseModel):
    """Result of a single data quality dimension check.

    Attributes:
        passed: Whether the check passed.
        dimension: Quality dimension name (完整性/一致性/时效性).
        details: Human-readable description of the check result.
        score: Normalized quality score [0.0, 1.0] for this dimension.
        issues: Specific issues found, if any.
    """

    passed: bool = Field(description="Whether this quality dimension check passed.")
    dimension: str = Field(description="Quality dimension: 完整性 | 一致性 | 时效性")
    details: str = Field(default="", description="Human-readable check result description.")
    score: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Normalized quality score for this dimension.",
    )
    issues: list[str] = Field(
        default_factory=list,
        description="Specific issues found during the check.",
    )


class DataQualityCheck(BaseModel):
    """Data quality checker for RLTrainingSample records.

    Performs three dimension checks:
        1. 完整性 (Completeness): Are all required provenance IDs present?
        2. 一致性 (Consistency): Are the linked IDs internally consistent?
        3. 时效性 (Timeliness): Is the data within the expected date range?

    Attributes:
        sample: The RLTrainingSample to validate.
        max_age_days: Maximum age in days for timeliness check (default 7).
        reference_date: Reference date for timeliness (defaults to today).
    """

    sample: RLTrainingSample = Field(description="Training sample to validate.")
    max_age_days: int = Field(
        default=7,
        ge=1,
        description="Maximum allowed age in days for timeliness check.",
    )
    reference_date: str = Field(
        default="",
        description="Reference date for timeliness in YYYYMMDD. Defaults to today.",
    )

    def check_completeness(self) -> QualityCheckResult:
        """Check 完整性 (Completeness): required fields and provenance IDs.

        In v1 exploration phase, a sample is considered sufficiently complete
        if it has at least one reward signal and a valid sample_id/date/scenario.
        v2 may enforce non-empty provenance lists.
        """
        issues: list[str] = []

        if not self.sample.sample_id:
            issues.append("Missing sample_id")
        if not self.sample.date:
            issues.append("Missing date")
        if not self.sample.reward_signals:
            issues.append("No reward signals attached (exploration phase: warning only)")

        # Check for overly sparse provenance (all three layers empty)
        if (
            not self.sample.raw_messages
            and not self.sample.parsed_contexts
            and not self.sample.topic_summaries
        ):
            issues.append(
                "All provenance fields (raw_messages, parsed_contexts, topic_summaries) "
                "are empty — sample may lack traceability"
            )

        score = max(0.0, 1.0 - len(issues) * 0.2)
        return QualityCheckResult(
            passed=len(issues) == 0,
            dimension="完整性",
            details=f"Checked required fields and provenance; found {len(issues)} issue(s).",
            score=score,
            issues=issues,
        )

    def check_consistency(self) -> QualityCheckResult:
        """Check 一致性 (Consistency): internal ID format and cross-field consistency.

        Validates:
            - sample_id matches date field
            - All server IDs follow expected format (non-empty)
            - scenario is a valid RLScenario
            - Reward signal values are in [0, 1]
        """
        issues: list[str] = []

        # sample_id format: rl-YYYYMMDD-NNN
        expected_prefix = f"rl-{self.sample.date}-"
        if not self.sample.sample_id.startswith(expected_prefix):
            issues.append(
                f"sample_id '{self.sample.sample_id}' does not match date "
                f"'{self.sample.date}'; expected prefix '{expected_prefix}'"
            )

        # Check for duplicate server IDs across lists
        all_ids = (
            self.sample.raw_messages + self.sample.parsed_contexts + self.sample.topic_summaries
        )
        if len(all_ids) != len(set(all_ids)):
            issues.append("Duplicate IDs found across provenance fields")

        # Reward signal values sanity check
        for i, signal in enumerate(self.sample.reward_signals):
            if signal.value < 0.0 or signal.value > 1.0:
                issues.append(f"Reward signal {i}: value {signal.value} out of [0, 1] range")
            if not signal.dimension.strip():
                issues.append(f"Reward signal {i}: missing quality dimension label")

        score = max(0.0, 1.0 - len(issues) * 0.15)
        return QualityCheckResult(
            passed=len(issues) == 0,
            dimension="一致性",
            details=f"Checked ID formats, cross-field consistency; found {len(issues)} issue(s).",
            score=score,
            issues=issues,
        )

    def check_timeliness(self) -> QualityCheckResult:
        """Check 时效性 (Timeliness): whether the data is within acceptable age.

        Compares the sample date against the reference date. Data older than
        max_age_days is flagged as stale but may still be usable for training.

        Returns:
            QualityCheckResult with score decreasing as data ages.
        """
        issues: list[str] = []

        # Determine reference date
        ref_date_str = self.reference_date or date.today().strftime("%Y%m%d")
        try:
            ref_date = date(
                year=int(ref_date_str[:4]),
                month=int(ref_date_str[4:6]),
                day=int(ref_date_str[6:8]),
            )
            sample_date = date(
                year=int(self.sample.date[:4]),
                month=int(self.sample.date[4:6]),
                day=int(self.sample.date[6:8]),
            )
        except (ValueError, IndexError):
            return QualityCheckResult(
                passed=False,
                dimension="时效性",
                details=f"Invalid date format in sample.date='{self.sample.date}' "
                f"or reference_date='{ref_date_str}'.",
                score=0.0,
                issues=["Unparseable date format"],
            )

        age_days = (ref_date - sample_date).days

        if age_days < 0:
            issues.append(
                f"Sample date {self.sample.date} is in the future (reference: {ref_date_str})"
            )
            score = 0.0
        elif age_days <= 1:
            score = 1.0  # Fresh — today or yesterday
        elif age_days <= self.max_age_days:
            # Linear decay: 1.0 at day 1 → 0.3 at max_age_days
            ratio = age_days / self.max_age_days
            score = round(1.0 - 0.7 * ratio, 2)
        else:
            issues.append(
                f"Sample date {self.sample.date} is {age_days} days old "
                f"(max allowed: {self.max_age_days} days)"
            )
            score = 0.1

        return QualityCheckResult(
            passed=len(issues) == 0,
            dimension="时效性",
            details=f"Age: {age_days} day(s) (ref: {ref_date_str}, max: {self.max_age_days} days).",
            score=score,
            issues=issues,
        )

    def check_all(self) -> list[QualityCheckResult]:
        """Run all three quality checks and return results.

        Returns:
            List of three QualityCheckResult for 完整性, 一致性, 时效性.
        """
        return [
            self.check_completeness(),
            self.check_consistency(),
            self.check_timeliness(),
        ]

    @property
    def overall_score(self) -> float:
        """Compute overall quality score as mean of all three dimensions."""
        results = self.check_all()
        if not results:
            return 0.0
        return round(sum(r.score for r in results) / len(results), 2)

    @property
    def all_passed(self) -> bool:
        """Whether all quality checks passed."""
        return all(r.passed for r in self.check_all())


# ============================================================
# Re-exports from rl.schema — Four-Tuple Models
# ============================================================
# These models originate from rl/schema.py (T-I-schema output).
# Re-exported here to support the canonical import path:
#   from z_winnow.schemas.rl_data import RLRecord, ...
# The extractor and reward engine use these models directly.

from z_winnow.rl.schema import (  # noqa: E402
    RLAction,
    RLMetadata,
    RLProvenance,
    RLRecord,
    RLReward,
    RLState,
)

# Alias for compatibility with task description expectations
Provenance = RLProvenance

__all__ = [
    # Original models from this module
    "DataQualityCheck",
    # Re-exported four-tuple models from rl.schema
    "Provenance",
    "QualityCheckResult",
    "RLAction",
    "RLMetadata",
    "RLProvenance",
    "RLRecord",
    "RLReward",
    "RLScenario",
    "RLState",
    "RLTrainingSample",
    "RewardSignal",
    "RewardSource",
    "SignalType",
]
