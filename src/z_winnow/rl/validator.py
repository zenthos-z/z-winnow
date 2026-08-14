"""T-I4: Dataset quality validator and statistics reporter.

Validates JSONL RL training datasets against the Pydantic RLRecord schema
and performs data quality checks:

- Schema integrity: Pydantic validation per line
- Reward distribution: Warn if > 90% scores identical
- Scenario distribution: Warn if any scenario < 5%
- Missing value rate: Per-field null/empty statistics
- Anomaly detection: latency_ms < 0, score out of [0,1]

Usage:
    from z_winnow.rl.validator import validate_dataset
    report = validate_dataset("data/rl_dataset.jsonl")
    print(report["status"])  # "PASSED" or "FAILED"
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from z_winnow.rl.schema import RLRecord

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

VALID_SCENARIOS = {
    "preference_alignment",
    "routing_decision",
    "topic_tracking",
}

REQUIRED_TOP_KEYS = {
    "record_id",
    "date",
    "scenario",
    "state",
    "action",
    "reward",
    "metadata",
}

REQUIRED_STATE_KEYS = {
    "messages_summary",
    "context_token_count",
    "history_topics",
    "server_ids",
    "parsed_context_ids",
    "active_members",
    "message_count",
}

REQUIRED_ACTION_KEYS = {
    "agent",
    "decision",
    "model_used",
    "temperature",
    "latency_ms",
}

# ============================================================
# Validation logic
# ============================================================


def _validate_schema(lines: list[dict]) -> list[dict]:
    """Validate each JSONL line against the RLRecord Pydantic model.

    Args:
        lines: Parsed JSON dicts from the JSONL file.

    Returns:
        List of issue dicts for schema validation failures.
    """
    issues: list[dict] = []
    for i, record in enumerate(lines):
        # Pydantic validation
        try:
            RLRecord.model_validate(record)
        except Exception as e:
            issues.append(
                {
                    "line": i + 1,
                    "type": "schema_validation",
                    "severity": "error",
                    "detail": str(e)[:300],
                }
            )
    return issues


def _validate_required_keys(lines: list[dict]) -> list[dict]:
    """Check top-level required keys exist.

    Args:
        lines: Parsed JSON dicts.

    Returns:
        List of issue dicts for missing keys.
    """
    issues: list[dict] = []
    for i, record in enumerate(lines):
        missing = REQUIRED_TOP_KEYS - set(record.keys())
        if missing:
            issues.append(
                {
                    "line": i + 1,
                    "type": "missing_keys",
                    "severity": "error",
                    "detail": f"Missing top-level keys: {sorted(missing)}",
                }
            )
    return issues


def _validate_scenario_distribution(lines: list[dict]) -> dict:
    """Check scenario distribution for imbalance.

    Args:
        lines: Parsed JSON dicts.

    Returns:
        Dict with counts, percentages, and issues if < 5% threshold.
    """
    total = len(lines)
    if total == 0:
        return {"counts": {}, "percentages": {}, "issues": []}

    counter: Counter = Counter()
    for record in lines:
        scenario = record.get("scenario", "unknown")
        counter[scenario] += 1

    counts = dict(counter)
    percentages = {k: round(v / total * 100, 1) for k, v in counts.items()}

    issues: list[dict] = []
    for scenario, pct in percentages.items():
        if pct < 5.0:
            issues.append(
                {
                    "type": "scenario_imbalance",
                    "severity": "warning",
                    "detail": (
                        f"Scenario '{scenario}' at {pct}% (< 5% threshold). "
                        f"Consider adding more samples."
                    ),
                }
            )

    return {"counts": counts, "percentages": percentages, "issues": issues}


def _validate_reward_distribution(lines: list[dict]) -> dict:
    """Check reward distribution for uniformity.

    Warns if > 90% of records have the same reward score (or NaN).

    Args:
        lines: Parsed JSON dicts.

    Returns:
        Dict with stats and issues.
    """
    scores: list[float] = []
    for record in lines:
        reward = record.get("reward")
        if reward and isinstance(reward, dict):
            score = reward.get("score")
            if isinstance(score, int | float):
                scores.append(float(score))

    if not scores:
        # Check if all have null reward
        null_count = sum(1 for r in lines if r.get("reward") is None)
        return {
            "mean": None,
            "std": None,
            "min": None,
            "max": None,
            "count": 0,
            "null_count": null_count,
            "dimensions": {},
            "issues": [
                {
                    "type": "no_rewards",
                    "severity": "warning",
                    "detail": f"No reward scores found ({null_count}/{len(lines)} are null).",
                }
            ],
        }

    mean = sum(scores) / len(scores)
    variance = sum((s - mean) ** 2 for s in scores) / len(scores)
    std = variance**0.5

    # Check if > 90% have the same score
    rounded = [round(s, 2) for s in scores]
    most_common = Counter(rounded).most_common(1)[0]
    pct_same = most_common[1] / len(scores) * 100 if scores else 0

    issues: list[dict] = []
    if pct_same > 90:
        issues.append(
            {
                "type": "uniform_rewards",
                "severity": "warning",
                "detail": (
                    f"{pct_same:.0f}% of rewards are the same value "
                    f"({most_common[0]:.2f}). Consider calibrating reward function."
                ),
            }
        )

    # Dimension breakdown
    dim_stats: dict[str, dict] = {}
    dim_names = ["completeness", "accuracy", "conciseness", "actionability"]
    for dim in dim_names:
        dim_values: list[float] = []
        for record in lines:
            reward = record.get("reward")
            if reward and isinstance(reward, dict):
                dims = reward.get("dimensions") or reward.get("dimensions", {})
                if not isinstance(dims, dict):
                    continue
                val = dims.get(dim)
                if isinstance(val, int | float):
                    dim_values.append(float(val))
        if dim_values:
            d_mean = sum(dim_values) / len(dim_values)
            d_var = sum((v - d_mean) ** 2 for v in dim_values) / len(dim_values)
            dim_stats[dim] = {
                "mean": round(d_mean, 4),
                "std": round(d_var**0.5, 4),
                "count": len(dim_values),
            }

    return {
        "mean": round(mean, 4),
        "std": round(std, 4),
        "min": round(min(scores), 4),
        "max": round(max(scores), 4),
        "count": len(scores),
        "null_count": sum(1 for r in lines if r.get("reward") is None),
        "dimensions": dim_stats,
        "issues": issues,
    }


def _validate_missing_values(lines: list[dict]) -> dict:
    """Check for missing/null values across key fields.

    Args:
        lines: Parsed JSON dicts.

    Returns:
        Dict with field-level null rates and issues.
    """
    total = len(lines)
    if total == 0:
        return {"null_rates": {}, "issues": []}

    null_rates: dict[str, float] = {}
    issues: list[dict] = []

    # Top-level fields
    for field in ["record_id", "date", "scenario"]:
        null_count = sum(1 for r in lines if not r.get(field))
        rate = null_count / total * 100
        if rate > 0:
            null_rates[field] = round(rate, 1)
            issues.append(
                {
                    "type": "missing_values",
                    "severity": "error" if rate > 10 else "warning",
                    "detail": f"Field '{field}' missing in {null_count}/{total} records ({rate:.1f}%).",
                }
            )

    # Nested fields
    for record in lines:
        state = record.get("state")
        if state and isinstance(state, dict):
            for field in ["messages_summary", "server_ids"]:
                val = state.get(field)
                if not val and field not in null_rates:
                    count = sum(
                        1
                        for r in lines
                        if r.get("state")
                        and isinstance(r.get("state"), dict)
                        and not r["state"].get(field)
                    )
                    rate = count / total * 100
                    if rate > 5:
                        null_rates[f"state.{field}"] = round(rate, 1)
                        issues.append(
                            {
                                "type": "missing_values",
                                "severity": "warning",
                                "detail": (
                                    f"Field 'state.{field}' empty in {count}/{total} records "
                                    f"({rate:.1f}%)."
                                ),
                            }
                        )

        break  # Only check the first record for nested field type

    return {"null_rates": null_rates, "issues": issues}


def _validate_anomalies(lines: list[dict]) -> list[dict]:
    """Detect anomalies in data values.

    Checks:
    - latency_ms < 0
    - score not in [0, 1]
    - temperature not in [0, 2]
    - message_count < 0
    - context_token_count < 0

    Args:
        lines: Parsed JSON dicts.

    Returns:
        List of anomaly issue dicts.
    """
    issues: list[dict] = []

    for i, record in enumerate(lines):
        line_no = i + 1

        # Check latency_ms
        action = record.get("action")
        if action and isinstance(action, dict):
            latency = action.get("latency_ms")
            if isinstance(latency, int | float) and latency < 0:
                issues.append(
                    {
                        "line": line_no,
                        "type": "anomaly",
                        "severity": "error",
                        "detail": f"latency_ms = {latency} < 0",
                    }
                )

            temp = action.get("temperature")
            if isinstance(temp, int | float) and (temp < 0 or temp > 2.0):
                issues.append(
                    {
                        "line": line_no,
                        "type": "anomaly",
                        "severity": "warning",
                        "detail": f"temperature = {temp} not in [0, 2]",
                    }
                )

        # Check score
        reward = record.get("reward")
        if reward and isinstance(reward, dict):
            score = reward.get("score")
            if isinstance(score, int | float) and (score < 0.0 or score > 1.0):
                issues.append(
                    {
                        "line": line_no,
                        "type": "anomaly",
                        "severity": "error",
                        "detail": f"reward.score = {score} not in [0, 1]",
                    }
                )

        # Check message_count
        state = record.get("state")
        if state and isinstance(state, dict):
            msg_count = state.get("message_count")
            if isinstance(msg_count, int | float) and msg_count < 0:
                issues.append(
                    {
                        "line": line_no,
                        "type": "anomaly",
                        "severity": "error",
                        "detail": f"state.message_count = {msg_count} < 0",
                    }
                )

            token_count = state.get("context_token_count")
            if isinstance(token_count, int | float) and token_count < 0:
                issues.append(
                    {
                        "line": line_no,
                        "type": "anomaly",
                        "severity": "warning",
                        "detail": f"state.context_token_count = {token_count} < 0",
                    }
                )

    return issues


# ============================================================
# Main validation entry point
# ============================================================


def validate_dataset(file_path: str) -> dict[str, Any]:
    """Validate a JSONL RL training dataset.

    Performs schema validation, distribution checks, missing value analysis,
    and anomaly detection. Returns a structured report dict.

    Args:
        file_path: Path to the JSONL file.

    Returns:
        Report dict with keys:
            - dataset: str — file path
            - total_records: int
            - date_range: str — min/max dates
            - scenario_distribution: dict
            - reward_stats: dict
            - issues: list[dict]
            - issue_count: int
            - status: str — "PASSED" or "FAILED"
            - message: str

    Example:
        >>> report = validate_dataset("data/rl_dataset.jsonl")
        >>> assert report["status"] in ("PASSED", "FAILED")
        >>> print(f"Issues: {report['issue_count']}")
    """
    fp = Path(file_path)
    if not fp.exists():
        return {
            "dataset": str(file_path),
            "total_records": 0,
            "date_range": "N/A",
            "scenario_distribution": {},
            "reward_stats": {},
            "issues": [
                {
                    "type": "file_not_found",
                    "severity": "error",
                    "detail": f"File not found: {file_path}",
                }
            ],
            "issue_count": 1,
            "status": "FAILED",
            "message": f"Dataset file not found: {file_path}",
        }

    # Parse JSONL
    lines: list[dict] = []
    parse_errors: list[dict] = []
    with open(file_path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    lines.append(obj)
                else:
                    parse_errors.append(
                        {
                            "line": i,
                            "type": "parse_error",
                            "severity": "error",
                            "detail": f"Line {i} is not a JSON object (got {type(obj).__name__})",
                        }
                    )
            except json.JSONDecodeError as e:
                parse_errors.append(
                    {
                        "line": i,
                        "type": "parse_error",
                        "severity": "error",
                        "detail": f"Line {i}: {str(e)[:200]}",
                    }
                )

    total = len(lines) + len(parse_errors)

    # Run all validation checks
    all_issues: list[dict] = list(parse_errors)
    all_issues.extend(_validate_schema(lines))
    all_issues.extend(_validate_required_keys(lines))
    all_issues.extend(_validate_anomalies(lines))

    # Scenario distribution
    scenario_result = _validate_scenario_distribution(lines)
    all_issues.extend(scenario_result.get("issues", []))

    # Reward distribution
    reward_result = _validate_reward_distribution(lines)
    all_issues.extend(reward_result.get("issues", []))

    # Missing values
    missing_result = _validate_missing_values(lines)
    all_issues.extend(missing_result.get("issues", []))

    # Date range
    dates = sorted(r["date"] for r in lines if r.get("date"))
    date_range = f"{dates[0]} ~ {dates[-1]}" if dates else "N/A"

    # Status determination
    errors = [i for i in all_issues if i.get("severity") == "error"]
    status = "PASSED" if len(errors) == 0 else "FAILED"

    report = {
        "dataset": str(file_path),
        "total_records": total,
        "date_range": date_range,
        "scenario_distribution": {
            "counts": scenario_result.get("counts", {}),
            "percentages": scenario_result.get("percentages", {}),
        },
        "reward_stats": {
            **reward_result,
            "issues": reward_result.get("issues", []),
        },
        "issues": all_issues,
        "issue_count": len(all_issues),
        "status": status,
        "message": (
            f"Validation {status}: {len(all_issues)} issue(s) found "
            f"({len(errors)} error(s), "
            f"{len(all_issues) - len(errors)} warning(s))"
        ),
    }

    logger.info("Dataset validation %s: %s", status, report["message"])
    return report


__all__ = [
    "validate_dataset",
]
