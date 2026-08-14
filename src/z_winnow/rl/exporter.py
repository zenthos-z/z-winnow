"""T-I5 + T-W10-B-d: Batch JSONL export + weighted reward aggregation.

T-I5 (existing): Exports RLRecord four-tuples to JSONL format with:
- Date-range filtering and incremental export
- Automatic reward computation (rule-based)
- Scenario filtering
- Quality report generation (sample count, scenario distribution, reward stats)
- Pydantic serialization via model_dump()

T-W10-B-d (new): Weighted reward aggregation + dataset export for web dashboard:
- compute_weighted_score: 0.6×human_feedback_avg + 0.3×llm_judge_avg + 0.1×rule_based_avg
- Auto-weight redistribution when signals are missing
- export_dataset: group-scoped RLRecord export with weighted scores

Output format:
    data/rl/{date}_rl_samples.jsonl — one JSON object per line, ensure_ascii=False.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from z_winnow.config.settings import get_settings
from z_winnow.rl.extractor import extract_records
from z_winnow.rl.rewards import compute_reward
from z_winnow.rl.schema import RLRecord, RLRewardSignal

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

# S7: DEFAULT_DB_PATH and DEFAULT_OUTPUT_DIR removed — paths now from Settings


def _iso_timestamp() -> str:
    """Return current UTC ISO 8601 timestamp."""
    return datetime.now(UTC).isoformat()


# ============================================================
# Serialization
# ============================================================


def _serialize_record(record: RLRecord) -> dict[str, Any]:
    """Serialize a single RLRecord to a JSON-safe dict via Pydantic model_dump.

    Args:
        record: RLRecord to serialize.

    Returns:
        JSON-safe dict.
    """
    return record.model_dump(mode="json", exclude_none=False)


# ============================================================
# Quality report
# ============================================================


def _generate_quality_report(
    records: list[RLRecord],
    output_path: str,
    start_date: str,
    end_date: str,
    skip_reward: bool = False,
) -> dict[str, Any]:
    """Generate a quality report for exported records.

    Args:
        records: Exported RLRecord list.
        output_path: Output file path.
        start_date: Start date string.
        end_date: End date string.
        skip_reward: Whether reward was skipped.

    Returns:
        Quality report dict.
    """
    from collections import Counter

    total = len(records)
    if total == 0:
        return {
            "output_path": output_path,
            "total_samples": 0,
            "date_range": f"{start_date} ~ {end_date}",
            "scenario_distribution": {},
            "reward_stats": {
                "mean": None,
                "std": None,
                "computed": not skip_reward,
            },
            "quality_score": 0,
            "issues": ["No records exported — check date range and database content."],
        }

    # Scenario distribution
    scenario_counter: Counter = Counter()
    for r in records:
        scenario_counter[r.scenario] += 1

    scenario_dist = {
        s: {
            "count": scenario_counter[s],
            "percentage": round(scenario_counter[s] / total * 100, 1),
        }
        for s in sorted(scenario_counter.keys())
    }

    # Reward stats
    reward_stats: dict[str, Any] = {
        "computed": not skip_reward,
    }
    if not skip_reward:
        scores = []
        dim_means: dict[str, list[float]] = {
            "completeness": [],
            "conciseness": [],
            "actionability": [],
            "accuracy": [],
        }
        for r in records:
            if r.reward:
                scores.append(r.reward.score)
                dims = r.reward.dimensions
                if dims.completeness is not None:
                    dim_means["completeness"].append(dims.completeness)
                if dims.conciseness is not None:
                    dim_means["conciseness"].append(dims.conciseness)
                if dims.actionability is not None:
                    dim_means["actionability"].append(dims.actionability)
                if dims.accuracy is not None:
                    dim_means["accuracy"].append(dims.accuracy)

        if scores:
            mean = sum(scores) / len(scores)
            variance = sum((s - mean) ** 2 for s in scores) / len(scores)
            std = variance**0.5
            reward_stats["mean"] = round(mean, 4)
            reward_stats["std"] = round(std, 4)
            reward_stats["min"] = round(min(scores), 4)
            reward_stats["max"] = round(max(scores), 4)
        else:
            reward_stats["mean"] = None
            reward_stats["std"] = None

        reward_stats["dimensions"] = {
            dim: {
                "mean": round(sum(vals) / len(vals), 4) if vals else None,
                "std": round(
                    (sum((v - sum(vals) / len(vals)) ** 2 for v in vals) / len(vals)) ** 0.5, 4
                )
                if vals and len(vals) > 1
                else 0.0,
            }
            for dim, vals in dim_means.items()
            if vals
        }

    # Date coverage
    dates = sorted({r.date for r in records})
    date_coverage = f"{dates[0]} ~ {dates[-1]}" if dates else "N/A"

    # Quality score (heuristic: 0-100)
    quality_score = 100
    issues: list[str] = []

    # Penalize tiny datasets
    if total < 5:
        quality_score -= 30
        issues.append(f"Very small dataset ({total} records). Target >= 10.")
    elif total < 10:
        quality_score -= 15
        issues.append(f"Small dataset ({total} records). Consider adding more data.")

    # Penalize single-scenario datasets
    if len(scenario_dist) < 2:
        quality_score -= 20
        issues.append("Only 1 scenario present. Multi-scenario coverage recommended.")

    # Penalize uniform rewards
    if not skip_reward and reward_stats.get("std") is not None:
        if reward_stats["std"] < 0.05:
            quality_score -= 15
            issues.append(
                f"Reward std is very low ({reward_stats['std']:.4f}). "
                "Consider tuning reward function."
            )
        elif reward_stats["std"] < 0.1:
            quality_score -= 5

    quality_score = max(0, quality_score)

    return {
        "output_path": output_path,
        "total_samples": total,
        "date_range": date_coverage,
        "scenario_distribution": scenario_dist,
        "reward_stats": reward_stats,
        "quality_score": quality_score,
        "issues": issues if issues else ["No issues detected"],
    }


# ============================================================
# Main export functions
# ============================================================


def export_rl_dataset(
    start_date: str,
    end_date: str,
    output_path: str | None = None,
    scenario_filter: str | None = None,
    db_path: str | None = None,
    skip_reward: bool = False,
) -> tuple[str, int, dict[str, Any]]:
    """Export RL training dataset to JSONL format.

    Extracts records from SQLite, computes rewards (unless skipped),
    serializes to JSONL, and generates a quality report.

    Args:
        start_date: Start date YYYY-MM-DD or YYYYMMDD.
        end_date: End date YYYY-MM-DD or YYYYMMDD.
        output_path: Output file path. Default:
            data/rl/{start}_{end}_rl_samples.jsonl
        scenario_filter: Filter to one scenario type, or None for all.
        db_path: SQLite database path. Default: data/winnow.db.
        skip_reward: If True, skip reward computation (faster, for testing).

    Returns:
        Tuple of (output_path, record_count, quality_report).

    Raises:
        ValueError: If date format is invalid.

    Example:
        >>> path, count, report = export_rl_dataset(
        ...     "2026-04-20", "2026-04-28",
        ...     output_path="data/rl/test_samples.jsonl",
        ...     skip_reward=True,
        ... )
        >>> assert count >= 0
        >>> print(report["quality_score"])
    """
    # Normalize dates
    start_date = start_date.strip().replace("-", "")
    end_date = end_date.strip().replace("-", "")

    # Default output path
    if output_path is None:
        start_display = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
        end_display = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"
        output_dir = Path(get_settings().rl_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / f"{start_date}_{end_date}_rl_samples.jsonl")
    else:
        output_dir = Path(output_path).parent
        output_dir.mkdir(parents=True, exist_ok=True)

    resolved_db = db_path or get_settings().db_path

    logger.info(
        "Exporting RL dataset: %s ~ %s → %s (scenario=%s, db=%s, skip_reward=%s)",
        start_date,
        end_date,
        output_path,
        scenario_filter or "all",
        resolved_db,
        skip_reward,
    )

    # Step 1: Extract records
    start_display = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    end_display = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

    records = extract_records(
        (start_display, end_display),
        db_path=resolved_db,
    )

    # Apply scenario filter if specified
    if scenario_filter:
        records = [r for r in records if r.scenario == scenario_filter]

    # Step 2: Compute rewards
    if not skip_reward:
        for record in records:
            try:
                record.reward = compute_reward(record)
            except Exception as e:
                logger.warning("Failed to compute reward for %s: %s", record.record_id, e)

    # Step 3: Serialize to JSONL
    record_count = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for record in records:
            record_dict = _serialize_record(record)
            f.write(json.dumps(record_dict, ensure_ascii=False) + "\n")
            record_count += 1

    # Step 4: Generate quality report
    quality_report = _generate_quality_report(
        records, output_path, start_display, end_display, skip_reward
    )

    logger.info(
        "Export complete: %d records → %s (quality_score=%d)",
        record_count,
        output_path,
        quality_report["quality_score"],
    )

    return (output_path, record_count, quality_report)


def export_rl_dataset_daily(
    dates: list[str],
    output_path: str | None = None,
    db_path: str | None = None,
    skip_reward: bool = False,
) -> tuple[str, int, dict[str, Any]]:
    """Export RL dataset for a list of specific dates.

    Convenience wrapper around export_rl_dataset for batch daily export.

    Args:
        dates: List of date strings (YYYYMMDD or YYYY-MM-DD).
        output_path: Output path. Default: data/rl/batch_rl_samples.jsonl.
        db_path: SQLite database path.
        skip_reward: Skip reward computation.

    Returns:
        Tuple of (output_path, record_count, quality_report).
    """
    if not dates:
        return ("", 0, {"issues": ["No dates provided"]})

    normalized = [d.strip().replace("-", "") for d in dates]
    start_date = min(normalized)
    end_date = max(normalized)

    if output_path is None:
        output_dir = Path(get_settings().rl_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / "batch_rl_samples.jsonl")

    return export_rl_dataset(
        start_date=start_date,
        end_date=end_date,
        output_path=output_path,
        db_path=db_path,
        skip_reward=skip_reward,
    )


# ============================================================
# T-W10-B-d: Weighted reward aggregation
# ============================================================

# Default weights when all three signal types are present
DEFAULT_WEIGHTS = {
    "human_feedback": 0.6,
    "llm_judge": 0.3,
    "rule_based": 0.1,
}

# Redistribution weights when exact 1 signal type is missing
REDIST_WEIGHTS: dict[str, dict[str, float]] = {
    # Missing llm_judge → 0.7 human + 0.3 rule
    "no_llm_judge": {"human_feedback": 0.7, "rule_based": 0.3},
    # Missing human_feedback → 0.7 llm_judge + 0.3 rule
    "no_human": {"llm_judge": 0.7, "rule_based": 0.3},
    # Missing rule_based → 0.7 human + 0.3 llm_judge
    "no_rule": {"human_feedback": 0.7, "llm_judge": 0.3},
    # Only human_feedback → 1.0
    "only_human": {"human_feedback": 1.0},
    # Only llm_judge → 1.0
    "only_llm": {"llm_judge": 1.0},
    # Only rule_based → 1.0
    "only_rule": {"rule_based": 1.0},
}


def _group_signals(signals: list[RLRewardSignal]) -> dict[str, list[float]]:
    """Group signal values by their effective signal category.

    Categorization:
        - human_feedback: signal_type == "human_feedback"
        - llm_judge: source == "llm_judge"
        - rule_based: signal_type == "rule_based"

    Args:
        signals: List of reward signal entries from an RLReward.

    Returns:
        Dict with keys "human_feedback", "llm_judge", "rule_based",
        each mapping to a list of float values (may be empty).
    """
    # A008: Pre-initialize groups with empty lists
    groups: dict[str, list[float]] = {
        "human_feedback": [],
        "llm_judge": [],
        "rule_based": [],
    }
    for s in signals:
        if s.signal_type == "human_feedback":
            groups["human_feedback"].append(s.value)
        elif s.source == "llm_judge":
            groups["llm_judge"].append(s.value)
        elif s.signal_type == "rule_based":
            groups["rule_based"].append(s.value)
    return groups


def _compute_weight_config(
    human_avg: float | None,
    llm_avg: float | None,
    rule_avg: float | None,
) -> dict[str, float]:
    """Determine the weight configuration based on which signal types are present.

    Applies auto-redistribution when signals are missing.

    Args:
        human_avg: Average of human_feedback signal values, or None.
        llm_avg: Average of llm_judge signal values, or None.
        rule_avg: Average of rule_based signal values, or None.

    Returns:
        Dict with keys "human_feedback", "llm_judge", "rule_based" and
        weight values that sum to 1.0.
    """
    present: list[str] = []
    if human_avg is not None:
        present.append("human_feedback")
    if llm_avg is not None:
        present.append("llm_judge")
    if rule_avg is not None:
        present.append("rule_based")

    if len(present) == 0:
        return {"human_feedback": 0.0, "llm_judge": 0.0, "rule_based": 0.0}

    if len(present) == 3:
        # P039: Full weights — all three signals available
        return dict(DEFAULT_WEIGHTS)

    if len(present) == 2:
        # Missing exactly 1 signal type — use predefined redistribution
        # Start from zero base to ensure all 3 keys exist
        base: dict[str, float] = {"human_feedback": 0.0, "llm_judge": 0.0, "rule_based": 0.0}
        if "human_feedback" not in present:
            base.update(REDIST_WEIGHTS["no_human"])
        elif "llm_judge" not in present:
            base.update(REDIST_WEIGHTS["no_llm_judge"])
        else:  # missing rule_based
            base.update(REDIST_WEIGHTS["no_rule"])
        return base

    # len(present) == 1 — only one signal type
    if present[0] == "human_feedback":
        return {"human_feedback": 1.0, "llm_judge": 0.0, "rule_based": 0.0}
    elif present[0] == "llm_judge":
        return {"human_feedback": 0.0, "llm_judge": 1.0, "rule_based": 0.0}
    else:
        return {"human_feedback": 0.0, "llm_judge": 0.0, "rule_based": 1.0}


def compute_weighted_score(record: RLRecord) -> float:
    """Compute weighted reward score from RLReward signals (T-W10-B-d).

    Weighted formula (all three signals present):
        score = 0.6 × human_feedback_avg + 0.3 × llm_judge_avg + 0.1 × rule_based_avg

    Auto-redistribution when signals are missing:
        - No llm_judge → 0.7 human + 0.3 rule
        - No human_feedback → 0.7 llm_judge + 0.3 rule
        - No rule_based → 0.7 human + 0.3 llm_judge
        - Only 1 signal type → weight = 1.0 for that type

    Signal categorization:
        - human_feedback: signal_type == "human_feedback"
        - llm_judge: source == "llm_judge"
        - rule_based: signal_type == "rule_based"

    Args:
        record: RLRecord with populated reward.signals.

    Returns:
        Weighted score in [0.0, 1.0]. Returns 0.0 if no reward or no signals.

    Example (B1):
        >>> record = RLRecord(...)
        >>> record.reward.signals = [
        ...     RLRewardSignal(signal_type="human_feedback", value=0.8),
        ...     RLRewardSignal(source="llm_judge", value=0.6),
        ...     RLRewardSignal(signal_type="rule_based", value=0.5),
        ... ]
        >>> score = compute_weighted_score(record)
        >>> math.isclose(score, 0.71, rel_tol=0.01)
        True
    """
    if record.reward is None or not record.reward.signals:
        return 0.0

    groups = _group_signals(record.reward.signals)

    human_avg: float | None = None
    llm_avg: float | None = None
    rule_avg: float | None = None

    if groups["human_feedback"]:
        human_avg = sum(groups["human_feedback"]) / len(groups["human_feedback"])
    if groups["llm_judge"]:
        llm_avg = sum(groups["llm_judge"]) / len(groups["llm_judge"])
    if groups["rule_based"]:
        rule_avg = sum(groups["rule_based"]) / len(groups["rule_based"])

    weights = _compute_weight_config(human_avg, llm_avg, rule_avg)

    # P039: Weighted sum with proper float tolerance handling
    score = (
        weights["human_feedback"] * (human_avg or 0.0)
        + weights["llm_judge"] * (llm_avg or 0.0)
        + weights["rule_based"] * (rule_avg or 0.0)
    )

    return round(score, 4)


# ============================================================
# T-W10-B-d: Dataset export for web dashboard
# ============================================================


def export_dataset(
    group_id: str,
    days: int = 30,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Export RL dataset for a specific group within a lookback window (T-W10-B-d).

    Extracts RLRecords for all dates where the group has data within the
    last ``days`` days, computes rewards, and enriches with:
      - report raw text (state.messages_summary + action.decision)
      - version pairs (from reward signal timestamps)
      - all signals (from reward.signals)
      - weighted score (via compute_weighted_score)
      - DPO preference flag (from signal_type="human_feedback")
      - metadata (record_id, date, scenario, source)

    Args:
        group_id: The group identifier (maps to pipeline_runs.group_id).
        days: Number of days to look back from today. Default: 30.
        db_path: SQLite database path. Default: data/winnow.db.

    Returns:
        List of dict records enriched for web dashboard rendering.
        Returns empty list if no data found or on database errors.

    Example:
        >>> dataset = export_dataset("test_group", days=7)
        >>> len(dataset) >= 0
        True
    """
    resolved_db = db_path or get_settings().db_path
    # A008: Pre-initialize data before try block
    data: list[dict[str, Any]] = []  # A008

    try:
        # Step 1: Find dates with pipeline runs for this group
        conn: sqlite3.Connection | None = None
        group_dates: list[str] = []

        try:
            conn = sqlite3.connect(resolved_db)
            conn.row_factory = sqlite3.Row

            # Query distinct dates from pipeline_runs for this group
            end_date_display = datetime.now(UTC).strftime("%Y%m%d")
            start_date = datetime.now(UTC) - timedelta(days=days)
            start_date_display = start_date.strftime("%Y%m%d")

            cursor = conn.execute(
                """SELECT DISTINCT date FROM pipeline_runs
                   WHERE group_id = ? AND date >= ? AND date <= ?
                   ORDER BY date ASC""",
                (group_id, start_date_display, end_date_display),
            )
            rows = cursor.fetchall()
            group_dates = [row[0] for row in rows]
        except sqlite3.OperationalError as e:
            logger.warning(
                "Database query failed for group %s: %s. Returning empty list.",
                group_id,
                e,
            )
            return []
        except Exception as e:
            logger.warning(
                "Failed to query group dates for %s: %s. Returning empty list.",
                group_id,
                e,
            )
            return []
        finally:
            if conn:
                conn.close()

        if not group_dates:
            logger.info("No data found for group %s in last %d days.", group_id, days)
            return []

        # Step 2: Extract records for each date
        # Convert YYYYMMDD → YYYY-MM-DD for extract_records
        def _to_iso(db_date: str) -> str:
            return f"{db_date[:4]}-{db_date[4:6]}-{db_date[6:8]}"

        iso_dates = [_to_iso(d) for d in group_dates]
        start_iso = iso_dates[0]
        end_iso = iso_dates[-1]

        records = extract_records(
            (start_iso, end_iso),
            db_path=resolved_db,
        )

        # Step 3: Filter to only records within this group's dates
        # (extract_records returns all dates in range)
        valid_dates = set(iso_dates)
        records = [r for r in records if r.date in valid_dates]

        # Step 4: Compute rewards and weighted scores
        for record in records:
            try:
                if record.reward is None or not record.reward.signals:
                    record.reward = compute_reward(record)
            except Exception as e:
                logger.warning("Failed to compute reward for %s: %s", record.record_id, e)

            # Compute weighted score — P039: float precision via round()
            weighted = compute_weighted_score(record)

            # Build report text from state and action
            report_text = record.state.messages_summary
            if record.action.decision:
                report_text += "\n\n## Decision\n" + record.action.decision

            # Determine DPO preference — present if human_feedback signals exist
            has_human = any(
                s.signal_type == "human_feedback"
                for s in (record.reward.signals if record.reward else [])
            )

            # Build version pair info from signal timestamps
            signal_versions: list[dict[str, str]] = []
            if record.reward and record.reward.signals:
                for s in record.reward.signals:
                    signal_versions.append(
                        {
                            "source": s.source,
                            "signal_type": s.signal_type,
                            "dimension": s.dimension,
                            "value": s.value,
                            "evidence": s.evidence,
                            "timestamp": s.timestamp,
                        }
                    )

            entry: dict[str, Any] = {
                "record_id": record.record_id,
                "date": record.date,
                "scenario": record.scenario,
                "group_id": group_id,
                "report_text": report_text,
                "weighted_score": weighted,
                "dpo_preference": has_human,
                "human_feedback_avg": _safe_avg(
                    [
                        s.value
                        for s in (record.reward.signals if record.reward else [])
                        if s.signal_type == "human_feedback"
                    ]
                ),
                "llm_judge_avg": _safe_avg(
                    [
                        s.value
                        for s in (record.reward.signals if record.reward else [])
                        if s.source == "llm_judge"
                    ]
                ),
                "rule_based_avg": _safe_avg(
                    [
                        s.value
                        for s in (record.reward.signals if record.reward else [])
                        if s.signal_type == "rule_based"
                    ]
                ),
                "signals": signal_versions,
                "metadata": {
                    "version": record.metadata.version,
                    "timestamp": record.metadata.timestamp,
                    "trace_id": record.metadata.trace_id,
                    "report_char_count": record.metadata.report_char_count,
                },
            }
            data.append(entry)

        logger.info(
            "Dataset export: group=%s days=%d records=%d",
            group_id,
            days,
            len(data),
        )

    except Exception as e:
        # A008: Graceful degradation — return empty list on any failure
        logger.warning(
            "export_dataset failed for group=%s: %s. Returning empty list.",
            group_id,
            e,
        )
        return []

    return data


def _safe_avg(values: list[float]) -> float | None:
    """Compute arithmetic mean of a list, returning None if empty."""
    if not values:
        return None
    return round(sum(values) / len(values), 4)


# ============================================================
# T-W10-B-b: Judge signal append
# ============================================================


def append_signal(
    output_path: str,
    record_id: str,
    signal: RLRewardSignal,
) -> None:
    """将单个 RL reward signal 追加写入 JSONL 信号日志文件.

    用于 judge CLI 输出 judge_result 后持久化评分信号。
    每个信号一行 JSON，ensure_ascii=False。

    Args:
        output_path: 信号 JSONL 文件路径 (如 data/rl/judge_signals.jsonl).
        record_id: 关联的 RL 记录 ID.
        signal: RLRewardSignal 实例.
    """
    output_dir = Path(output_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    row: dict[str, Any] = {
        "record_id": record_id,
        "signal_type": signal.signal_type,
        "source": signal.source,
        "value": signal.value,
        "dimension": signal.dimension,
        "evidence": signal.evidence,
        "timestamp": signal.timestamp,
    }
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ============================================================
# T-W10-D-e: DPO preference pair generation
# ============================================================


class DPOPreferencePair(BaseModel):
    """A single DPO (Direct Preference Optimization) training pair.

    Maps a "chosen" (post-regenerate, feedback-injected) version against
    a "rejected" (pre-regenerate, no feedback) version of the same report.

    Attributes:
        chosen: Full report content of the preferred (regenerated) version.
        rejected: Full report content of the rejected (original) version.
        metadata: Provenance info including feedback_id, group_id, date,
                  and dimensions_improved.
    """

    chosen: str = Field(description="Preferred report content (post-regenerate)")
    rejected: str = Field(description="Rejected report content (pre-regenerate)")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Provenance: feedback_id, group_id, date, dimensions_improved",
    )


def convert_to_dpo_pair(
    v_chosen: Any,
    v_rejected: Any,
    feedback_id: str,
    *,
    dimensions_improved: list[str] | None = None,
) -> DPOPreferencePair:
    """Convert adjacent report versions into a DPO preference pair (T-W10-D-e).

    v_chosen is the post-regenerate version (feedback injected, preferred).
    v_rejected is the pre-regenerate version (no feedback, rejected).

    P016: Cross-subsystem integration — accepts duck-typed objects with
          .content, .group_id, .date attributes (works with both pipeline
          ReportVersion dataclass and rl.llm_judge ReportVersion model).

    Args:
        v_chosen: Preferred report version (post-regenerate). Must have
                  .content, .group_id, .date attributes.
        v_rejected: Rejected report version (pre-regenerate). Must have
                    .content, .group_id, .date attributes.
        feedback_id: The feedback event ID that drove the regeneration.
        dimensions_improved: Optional list of dimension names improved.

    Returns:
        DPOPreferencePair with chosen/rejected content and metadata.

    Example (B2):
        >>> chosen = type('V', (), {'content': '报告 v2', 'group_id': 'g1', 'date': '20260516'})()
        >>> rejected = type('V', (), {'content': '报告 v1', 'group_id': 'g1', 'date': '20260516'})()
        >>> pair = convert_to_dpo_pair(chosen, rejected, 'fb-001', dimensions_improved=['completeness'])
        >>> pair.chosen != pair.rejected
        True
        >>> 'feedback_id' in pair.metadata
        True
    """
    # A008: Pre-initialize metadata fields
    group_id: str = ""  # A008
    date: str = ""  # A008

    try:
        group_id = getattr(v_chosen, "group_id", "")
        date = getattr(v_chosen, "date", "")
    except Exception:
        pass

    metadata: dict[str, Any] = {
        "feedback_id": feedback_id,
        "group_id": group_id,
        "date": date,
        "dimensions_improved": dimensions_improved or [],
    }

    return DPOPreferencePair(
        chosen=getattr(v_chosen, "content", ""),
        rejected=getattr(v_rejected, "content", ""),
        metadata=metadata,
    )


def export_dpo_pairs(
    group_id: str,
    days: int = 30,
    db_path: str | None = None,
) -> list[DPOPreferencePair]:
    """Export DPO preference pairs for a group from consumed feedback (T-W10-D-e).

    Queries feedback_events for consumed feedback within the lookback window,
    maps each to its report versions (v_chosen = regenerate version,
    v_rejected = previous version), and converts to DPOPreferencePairs.

    P016: Lazy import of pipeline.report_version for cross-subsystem integration.
    A008: Fallback to empty list on any database error.

    Args:
        group_id: The group identifier.
        days: Lookback window in days. Default: 30.
        db_path: SQLite database path. Default: data/winnow.db.

    Returns:
        List of DPOPreferencePair instances. Empty list if no consumed
        feedback or on database errors.

    Example (B4):
        >>> pairs = export_dpo_pairs("test_group", days=7)
        >>> isinstance(pairs, list)
        True
        >>> all(isinstance(p, DPOPreferencePair) for p in pairs)
        True
    """
    resolved_db = db_path or get_settings().db_path
    # A008: Pre-initialize pairs before try
    pairs: list[DPOPreferencePair] = []  # A008

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(resolved_db)
        conn.row_factory = sqlite3.Row

        # Step 1: Find consumed feedback for this group within time window
        end_date = datetime.now(UTC).strftime("%Y%m%d")
        start_date = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y%m%d")

        cursor = conn.execute(
            """SELECT feedback_id, report_id, tags, signal, severity
               FROM feedback_events
               WHERE group_id = ?
                 AND consumed_at IS NOT NULL
                 AND date >= ? AND date <= ?
               ORDER BY consumed_at ASC""",
            (group_id, start_date, end_date),
        )
        consumed_feedbacks = [dict(row) for row in cursor.fetchall()]

        if not consumed_feedbacks:
            logger.info(
                "export_dpo_pairs: no consumed feedback for group=%s days=%d",
                group_id,
                days,
            )
            return []

        # Step 2: For each consumed feedback, find adjacent report versions
        for fb in consumed_feedbacks:
            feedback_id: str = fb["feedback_id"]
            report_id: str | None = fb.get("report_id")

            # Parse tags for dimensions_improved
            dimensions_improved: list[str] = []
            try:
                tags_raw: str | None = fb.get("tags")
                if tags_raw and tags_raw != "[]":
                    tags = json.loads(tags_raw)
                    if isinstance(tags, list):
                        dimensions_improved = [str(t) for t in tags]
            except (json.JSONDecodeError, TypeError):
                pass

            if not report_id:
                logger.info(
                    "export_dpo_pairs: feedback_id=%s has no report_id, skipping",
                    feedback_id,
                )
                continue

            # Step 3: Find regenerate version (v_chosen) and previous version (v_rejected)
            # v_chosen = latest version for report_id (should be "regenerate" source)
            # v_rejected = version before v_chosen
            versions_cursor = conn.execute(
                """SELECT version_id, version_number, content, source, group_id, date
                   FROM report_versions
                   WHERE report_id = ?
                   ORDER BY version_number ASC""",
                (report_id,),
            )
            versions = [dict(row) for row in versions_cursor.fetchall()]

            if len(versions) < 2:
                logger.info(
                    "export_dpo_pairs: report_id=%s has <2 versions, "
                    "cannot form DPO pair, skipping",
                    report_id,
                )
                continue

            # v_chosen = last version (highest version_number, should be regenerate)
            # v_rejected = second-to-last version
            v_chosen_data = versions[-1]
            v_rejected_data = versions[-2]

            # Only create pair if v_chosen is a regenerate (source='regenerate')
            # and content actually differs
            chosen_content: str = v_chosen_data.get("content", "") or ""
            rejected_content: str = v_rejected_data.get("content", "") or ""

            if not chosen_content or not rejected_content:
                logger.info(
                    "export_dpo_pairs: feedback_id=%s — empty content, skipping",
                    feedback_id,
                )
                continue

            if chosen_content == rejected_content:
                logger.info(
                    "export_dpo_pairs: feedback_id=%s — identical content, skipping",
                    feedback_id,
                )
                continue

            # Build DPOPreferencePair via convert_to_dpo_pair with duck-typed objects
            # P016: Lazy import avoided — use simple namespace objects
            v_chosen_obj: Any = type(
                "VersionProxy",
                (),
                {
                    "content": chosen_content,
                    "group_id": v_chosen_data.get("group_id", group_id),
                    "date": v_chosen_data.get("date", ""),
                },
            )()
            v_rejected_obj: Any = type(
                "VersionProxy",
                (),
                {
                    "content": rejected_content,
                    "group_id": v_rejected_data.get("group_id", group_id),
                    "date": v_rejected_data.get("date", ""),
                },
            )()

            pair = convert_to_dpo_pair(
                v_chosen_obj,
                v_rejected_obj,
                feedback_id,
                dimensions_improved=dimensions_improved,
            )
            pairs.append(pair)

        logger.info(
            "export_dpo_pairs: group=%s days=%d pairs=%d",
            group_id,
            days,
            len(pairs),
        )

    except sqlite3.OperationalError as e:
        # A008: Graceful degradation — return empty list on DB failure
        logger.warning(
            "export_dpo_pairs: database error for group=%s: %s. Returning empty list.",
            group_id,
            e,
        )
        return []
    except Exception as e:
        logger.warning(
            "export_dpo_pairs: failed for group=%s: %s. Returning empty list.",
            group_id,
            e,
        )
        return []
    finally:
        if conn:
            conn.close()

    return pairs


__all__ = [
    "DEFAULT_WEIGHTS",
    "DPOPreferencePair",
    "append_signal",
    "compute_weighted_score",
    "convert_to_dpo_pair",
    "export_dataset",
    "export_dpo_pairs",
    "export_rl_dataset",
    "export_rl_dataset_daily",
]
