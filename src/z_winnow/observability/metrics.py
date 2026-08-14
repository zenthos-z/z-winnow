"""T-K3: Custom metrics for LangSmith tracing.

Provides per-run metrics collection with structured recording functions
for latency, token usage, quality scores, and subagent coverage. When
LangSmith is enabled, metrics are also pushed as run feedback for
dashboard visibility.

Key metrics tracked:
- Node-level latency: record_latency(node_name, start, end)
- LLM token usage: record_token_usage(llm_response) → input + output tokens
- Quality scores: record_quality_score(score) from T-E4-4 quality check
- Subagent coverage: record_subagent_coverage(completed, total)
- Error rates: record_node_error(node_name, error)

Architecture:
- MetricsCollector: Per-run accumulator (thread-safe via contextvars).
- RunStats: Pydantic model for aggregated stats (EXP-004: strict mode).
- get_run_stats(): Query aggregated stats for a completed run.
- Metrics go to LangSmith as feedback when enabled (run.feedback API).

Patterns applied:
- EXP-004: Pydantic strict(extra='forbid') on RunStats and MetricEntry.
- EXP-005: NodeError integration with error recording.
- EXP-011: Metric output as structlog JSON compatible fields.

Usage:
    >>> from z_winnow.observability.metrics import (
    ...     MetricsCollector, get_current_collector, get_run_stats,
    ... )
    >>> collector = MetricsCollector(run_id="run-001")
    >>> collector.record_latency("data_fetch", 0.0, 1.5)
    >>> collector.record_quality_score(0.85)
    >>> stats = collector.get_stats()
    >>> print(f"P95 latency: {stats.p95_latency_ms:.1f}ms")
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ============================================================
# MetricEntry — Single metric data point (EXP-004: strict)
# ============================================================


class MetricEntry(BaseModel, extra="forbid"):
    """A single metric data point recorded during a graph run.

    Stores the metric value, type, and source context for aggregation.
    Uses Pydantic strict mode (EXP-004).

    Attributes:
        metric_type: Category: latency | token_usage | quality_score |
                     subagent_coverage | node_error.
        node_name: Source node name, if applicable.
        value: Numeric value (milliseconds, token count, score 0-1).
        timestamp: Unix timestamp when the metric was recorded.
        metadata: Optional additional context (model name, error message).
    """

    metric_type: str = Field(
        description="Metric category: latency | token_usage | quality_score | "
        "subagent_coverage | node_error",
    )
    node_name: str | None = Field(
        default=None,
        description="Source node name",
    )
    value: float = Field(
        default=0.0,
        description="Numeric value (ms, token count, score 0-1)",
    )
    timestamp: float = Field(
        default_factory=time.time,
        description="Unix timestamp when recorded",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context (model name, error message, etc.)",
    )


# ============================================================
# RunStats — Aggregated metrics for a complete run (EXP-004)
# ============================================================


class RunStats(BaseModel, extra="forbid"):
    """Aggregated statistics for a single graph run.

    Computed from all MetricEntry data points collected during the run.
    Used as the return type of get_run_stats() and for dashboard panels.

    Attributes:
        run_id: Unique run identifier (UUID or LangSmith run ID).
        total_latency_ms: Total wall-clock time across all nodes.
        avg_latency_ms: Average node latency.
        p95_latency_ms: 95th percentile node latency.
        p99_latency_ms: 99th percentile node latency.
        node_count: Number of nodes executed.
        total_input_tokens: Total input (prompt) tokens across all LLM calls.
        total_output_tokens: Total output (completion) tokens across all LLM calls.
        quality_score: Overall quality score (0.0-1.0), averaged from all quality checks.
        subagent_completed: Number of subagents that completed successfully.
        subagent_total: Total number of subagents attempted.
        error_count: Number of node-level errors.
        node_latencies: Per-node latency breakdown {node_name: latency_ms}.
        model_usage: Per-model token breakdown {model_name: {input: N, output: N}}.
    """

    run_id: str = Field(description="Unique run identifier")
    total_latency_ms: float = Field(default=0.0, ge=0.0, description="Total wall-clock time")
    avg_latency_ms: float = Field(default=0.0, ge=0.0, description="Average node latency")
    p95_latency_ms: float = Field(default=0.0, ge=0.0, description="95th percentile node latency")
    p99_latency_ms: float = Field(default=0.0, ge=0.0, description="99th percentile node latency")
    node_count: int = Field(default=0, ge=0, description="Number of nodes executed")
    total_input_tokens: int = Field(
        default=0, ge=0, description="Total input tokens across all LLM calls"
    )
    total_output_tokens: int = Field(
        default=0, ge=0, description="Total output tokens across all LLM calls"
    )
    quality_score: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Overall quality score 0.0-1.0"
    )
    subagent_completed: int = Field(default=0, ge=0, description="Subagents completed successfully")
    subagent_total: int = Field(default=0, ge=0, description="Total subagents attempted")
    error_count: int = Field(default=0, ge=0, description="Number of node-level errors")
    node_latencies: dict[str, float] = Field(
        default_factory=dict,
        description="Per-node latency breakdown {node_name: latency_ms}",
    )
    model_usage: dict[str, dict[str, int]] = Field(
        default_factory=dict,
        description="Per-model token breakdown {model_name: {input_tokens: N, output_tokens: N}}",
    )

    @property
    def coverage_ratio(self) -> float:
        """Subagent completion ratio: completed / total.

        Returns 1.0 when all subagents completed, 0.0 when none completed.
        Returns 1.0 when no subagents were expected (total=0).
        """
        if self.subagent_total == 0:
            return 1.0
        return self.subagent_completed / self.subagent_total

    @property
    def token_total(self) -> int:
        """Total tokens consumed (input + output)."""
        return self.total_input_tokens + self.total_output_tokens

    @property
    def error_rate(self) -> float:
        """Node error rate (0.0-1.0)."""
        if self.node_count == 0:
            return 0.0
        return min(1.0, self.error_count / self.node_count)

    def to_dict(self) -> dict[str, Any]:
        """Export as JSON-safe dict for dashboards and logging."""
        return self.model_dump()


# ============================================================
# MetricsCollector — Per-run metric accumulator
# ============================================================


class MetricsCollector:
    """Collects metrics for a single graph run.

    Thread-safe: all recording methods are synchronous and atomic.
    Designed to be used as a context manager or manually via start/stop.

    When LangSmith is enabled, metrics are also pushed as run feedback
    via the LangSmith client.feedback API.

    Attributes:
        run_id: Unique identifier for this run.
        entries: All recorded metric data points.
        start_time: Wall-clock time when the collector was started.
        end_time: Wall-clock time when the collector was stopped.
    """

    def __init__(self, run_id: str | None = None) -> None:
        """Initialize a metrics collector for a run.

        Args:
            run_id: Unique run identifier. Auto-generated UUID if None.
        """
        self.run_id: str = run_id or f"run_{uuid.uuid4().hex[:12]}"
        self.entries: list[MetricEntry] = []
        self.start_time: float = time.time()
        self.end_time: float | None = None

    # ── Recording methods ──────────────────────────────────

    def record_latency(
        self,
        node_name: str,
        start_time: float,
        end_time: float,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record node execution latency.

        Args:
            node_name: Name of the LangGraph node.
            start_time: Start timestamp (time.monotonic()).
            end_time: End timestamp (time.monotonic()).
            metadata: Optional additional context.
        """
        duration_ms = (end_time - start_time) * 1000.0
        entry = MetricEntry(
            metric_type="latency",
            node_name=node_name,
            value=duration_ms,
            metadata=metadata or {},
        )
        self.entries.append(entry)
        logger.debug(
            "Metrics: latency[%s] = %.1fms",
            node_name,
            duration_ms,
        )

    def record_token_usage(
        self,
        llm_response: Any,
        *,
        node_name: str | None = None,
        model_name: str | None = None,
    ) -> None:
        """Record token usage from an LLM response.

        Extracts input_tokens and output_tokens from the response object.
        Supports LangChain AIMessage, ChatResult, and raw dict formats.

        Args:
            llm_response: LLM response object with usage_metadata or
                          response_metadata.token_usage.
            node_name: Source node name for attribution.
            model_name: Model identifier for attribution.
        """
        input_tokens = 0
        output_tokens = 0
        resolved_model = model_name or "unknown"

        try:
            # Try LangChain AIMessage format
            if hasattr(llm_response, "usage_metadata"):
                um = llm_response.usage_metadata
                if isinstance(um, dict):
                    input_tokens = int(um.get("input_tokens", 0))
                    output_tokens = int(um.get("output_tokens", 0))
                    resolved_model = model_name or um.get("model_name", "unknown")

            # Try response_metadata (DeepSeek/OpenAI format)
            elif hasattr(llm_response, "response_metadata"):
                rm = llm_response.response_metadata
                if isinstance(rm, dict):
                    tu = rm.get("token_usage", {})
                    if isinstance(tu, dict):
                        input_tokens = int(tu.get("prompt_tokens", 0))
                        output_tokens = int(tu.get("completion_tokens", 0))
                    resolved_model = model_name or rm.get("model_name", "unknown")

            # Try raw dict format
            elif isinstance(llm_response, dict):
                usage = llm_response.get("usage", {})
                input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)))
                output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)))
                resolved_model = model_name or llm_response.get("model", "unknown")

        except (AttributeError, TypeError, ValueError) as exc:
            logger.debug("Failed to extract token usage: %s", exc)

        if input_tokens > 0 or output_tokens > 0:
            entry = MetricEntry(
                metric_type="token_usage",
                node_name=node_name,
                value=float(input_tokens + output_tokens),
                metadata={
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "model_name": resolved_model,
                },
            )
            self.entries.append(entry)
            logger.debug(
                "Metrics: tokens[%s] = %d in + %d out (model=%s)",
                node_name or "?",
                input_tokens,
                output_tokens,
                resolved_model,
            )

    def record_quality_score(
        self,
        score: float,
        *,
        node_name: str = "output_composer",
        issues: list[str] | None = None,
    ) -> None:
        """Record a quality assessment score.

        Aligned with T-E4-4 quality check results. Score is 0.0-1.0
        where 1.0 indicates perfect quality.

        Args:
            score: Quality score 0.0-1.0.
            node_name: Source node (default: output_composer).
            issues: Optional list of quality issues found.
        """
        entry = MetricEntry(
            metric_type="quality_score",
            node_name=node_name,
            value=score,
            metadata={"issues": issues or []},
        )
        self.entries.append(entry)
        logger.debug(
            "Metrics: quality[%s] = %.2f (%d issues)",
            node_name,
            score,
            len(issues or []),
        )

    def record_subagent_coverage(
        self,
        completed: int,
        total: int,
    ) -> None:
        """Record subagent completion coverage.

        Tracks how many subagents completed successfully out of the total
        expected. Used to monitor fan-out reliability.

        Args:
            completed: Number of subagents that completed successfully.
            total: Total number of subagents attempted.
        """
        ratio = completed / total if total > 0 else 1.0
        entry = MetricEntry(
            metric_type="subagent_coverage",
            value=ratio,
            metadata={
                "completed": completed,
                "total": total,
            },
        )
        self.entries.append(entry)
        logger.debug(
            "Metrics: subagent_coverage = %d/%d (%.0f%%)",
            completed,
            total,
            ratio * 100,
        )

    def record_node_error(
        self,
        node_name: str,
        error: str,
    ) -> None:
        """Record a node-level error (EXP-005: NodeError integration).

        Args:
            node_name: Name of the node that encountered the error.
            error: Error message or exception string.
        """
        entry = MetricEntry(
            metric_type="node_error",
            node_name=node_name,
            value=1.0,  # Error count
            metadata={"error": error},
        )
        self.entries.append(entry)
        logger.debug(
            "Metrics: node_error[%s] = %s",
            node_name,
            error[:120],
        )

    # ── Aggregation ────────────────────────────────────────

    def get_stats(self) -> RunStats:
        """Compute aggregated RunStats from all recorded entries.

        Returns:
            RunStats with computed aggregates (latency percentiles,
            token totals, quality scores, coverage).

        Example:
            >>> collector = MetricsCollector("run-001")
            >>> collector.record_latency("data_fetch", 0.0, 1.2)
            >>> collector.record_quality_score(0.9)
            >>> stats = collector.get_stats()
            >>> print(stats.total_latency_ms)
            1200.0
        """
        self.end_time = time.time()

        # Collect per-category entries
        latencies: list[MetricEntry] = []
        token_usages: list[MetricEntry] = []
        quality_scores: list[MetricEntry] = []
        coverages: list[MetricEntry] = []
        errors: list[MetricEntry] = []

        for entry in self.entries:
            if entry.metric_type == "latency":
                latencies.append(entry)
            elif entry.metric_type == "token_usage":
                token_usages.append(entry)
            elif entry.metric_type == "quality_score":
                quality_scores.append(entry)
            elif entry.metric_type == "subagent_coverage":
                coverages.append(entry)
            elif entry.metric_type == "node_error":
                errors.append(entry)

        # Latency stats
        latency_values = sorted(e.value for e in latencies)
        total_latency = sum(latency_values)
        avg_latency = total_latency / len(latency_values) if latency_values else 0.0
        p95_latency = _percentile(latency_values, 95) if latency_values else 0.0
        p99_latency = _percentile(latency_values, 99) if latency_values else 0.0

        # Node latencies breakdown
        node_latencies: dict[str, float] = {}
        for entry in latencies:
            node_name = entry.node_name or "unknown"
            node_latencies[node_name] = entry.value

        # Token stats
        total_input = 0
        total_output = 0
        model_usage: dict[str, dict[str, int]] = {}
        for entry in token_usages:
            inp = entry.metadata.get("input_tokens", 0)
            out = entry.metadata.get("output_tokens", 0)
            total_input += inp
            total_output += out

            model = entry.metadata.get("model_name", "unknown")
            if model not in model_usage:
                model_usage[model] = {"input_tokens": 0, "output_tokens": 0}
            model_usage[model]["input_tokens"] += inp
            model_usage[model]["output_tokens"] += out

        # Quality score (average)
        qs_values = [e.value for e in quality_scores]
        avg_quality = sum(qs_values) / len(qs_values) if qs_values else 1.0

        # Coverage (take latest)
        coverage_completed = 0
        coverage_total = 0
        if coverages:
            latest = coverages[-1]
            coverage_completed = latest.metadata.get("completed", 0)
            coverage_total = latest.metadata.get("total", 0)

        # Node count (unique latency-tracked nodes)
        node_count = len({e.node_name for e in latencies if e.node_name})

        return RunStats(
            run_id=self.run_id,
            total_latency_ms=round(total_latency, 2),
            avg_latency_ms=round(avg_latency, 2),
            p95_latency_ms=round(p95_latency, 2),
            p99_latency_ms=round(p99_latency, 2),
            node_count=node_count,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            quality_score=round(avg_quality, 4),
            subagent_completed=coverage_completed,
            subagent_total=coverage_total,
            error_count=len(errors),
            node_latencies=node_latencies,
            model_usage=model_usage,
        )

    # ── LangSmith feedback ─────────────────────────────────

    def push_to_langsmith(
        self,
        langsmith_run_id: str | None = None,
    ) -> None:
        """Push collected metrics to LangSmith as run feedback.

        Uses the LangSmith client.feedback API to attach metrics to a
        specific run. When LangSmith is not available, this is a no-op.

        Args:
            langsmith_run_id: The LangSmith run ID to attach feedback to.
                              Uses self.run_id as fallback.

        Example:
            >>> collector = MetricsCollector("run-001")
            >>> collector.record_latency("data_fetch", 0.0, 1.2)
            >>> collector.push_to_langsmith()  # Attach to LangSmith run
        """
        target_run_id = langsmith_run_id or self.run_id

        try:
            from z_winnow.observability.langsmith_setup import (
                is_langsmith_enabled,
            )

            if not is_langsmith_enabled():
                logger.debug(
                    "LangSmith disabled, skipping feedback push for run=%s",
                    target_run_id,
                )
                return

            import langsmith
            import requests as _requests  # type: ignore[import-untyped]

            _session = _requests.Session()
            _session.trust_env = False
            client = langsmith.Client(timeout_ms=60_000, session=_session)

            stats = self.get_stats()

            # Push each metric as a separate feedback score
            # Latency metrics converted to seconds to stay well within LangSmith score bounds
            _push_feedback(
                client, target_run_id, "total_latency_s", stats.total_latency_ms / 1000.0
            )
            _push_feedback(client, target_run_id, "p95_latency_s", stats.p95_latency_ms / 1000.0)
            _push_feedback(client, target_run_id, "quality_score", stats.quality_score)
            _push_feedback(client, target_run_id, "total_tokens", float(stats.token_total))
            _push_feedback(client, target_run_id, "subagent_coverage", stats.coverage_ratio)
            _push_feedback(client, target_run_id, "error_rate", stats.error_rate)
            _push_feedback(client, target_run_id, "node_count", float(stats.node_count))

            logger.info(
                "Pushed %d metrics to LangSmith for run=%s",
                7,
                target_run_id,
            )

        except ImportError:
            logger.debug("langsmith package not available for feedback push")
        except Exception as exc:
            logger.warning("Failed to push metrics to LangSmith: %s", exc)

    # ── Context manager protocol ───────────────────────────

    def stop(self) -> RunStats:
        """Stop collecting and return aggregated stats.

        Returns:
            RunStats with all collected metrics aggregated.
        """
        return self.get_stats()

    def __repr__(self) -> str:
        return f"MetricsCollector(run_id={self.run_id!r}, entries={len(self.entries)})"


# ============================================================
# ContextVar — Per-run collector (thread-safe)
# ============================================================

_current_collector: contextvars.ContextVar[MetricsCollector | None] = contextvars.ContextVar(
    "current_metrics_collector", default=None
)


def set_current_collector(collector: MetricsCollector) -> None:
    """Set the current run's metrics collector in the context.

    Called at the start of each graph run to initialize metrics tracking.

    Args:
        collector: The MetricsCollector instance for this run.

    Example:
        >>> collector = MetricsCollector()
        >>> set_current_collector(collector)
    """
    _current_collector.set(collector)


def get_current_collector() -> MetricsCollector | None:
    """Get the current run's metrics collector from the context.

    Returns None if no collector has been set (e.g., called outside
    a graph run context).

    Returns:
        MetricsCollector | None: The current collector, or None.

    Example:
        >>> collector = get_current_collector()
        >>> if collector:
        ...     collector.record_latency("data_fetch", start, end)
    """
    return _current_collector.get(None)


def clear_current_collector() -> None:
    """Clear the current run's metrics collector from the context.

    Called at the end of each graph run to clean up.
    """
    _current_collector.set(None)


# ============================================================
# In-memory metrics store — keyed by run_id
# ============================================================

_store: dict[str, RunStats] = {}


def save_run_stats(run_id: str, stats: RunStats) -> None:
    """Save aggregated RunStats to the in-memory store.

    Args:
        run_id: Unique run identifier.
        stats: Aggregated RunStats to store.
    """
    _store[run_id] = stats
    logger.debug(
        "Saved stats for run=%s (%d nodes, %.0fms total)",
        run_id,
        stats.node_count,
        stats.total_latency_ms,
    )


def get_run_stats(run_id: str) -> RunStats | None:
    """Query aggregated statistics for a completed graph run.

    Args:
        run_id: The unique run identifier.

    Returns:
        RunStats if found, None otherwise.

    Example:
        >>> stats = get_run_stats("run-001")
        >>> if stats:
        ...     print(f"P95 latency: {stats.p95_latency_ms:.1f}ms")
        ...     print(f"Quality: {stats.quality_score:.2f}")
        ...     print(f"Tokens: {stats.token_total}")
    """
    return _store.get(run_id)


def list_run_ids() -> list[str]:
    """List all saved run IDs in the metrics store.

    Returns:
        list[str]: Sorted list of run IDs.
    """
    return sorted(_store.keys())


def clear_store() -> None:
    """Clear the entire metrics store (for testing)."""
    _store.clear()
    logger.debug("Metrics store cleared")


# ============================================================
# Convenience functions (use contextvar collector)
# ============================================================


def record_latency(
    node_name: str,
    start_time: float,
    end_time: float,
    **metadata: Any,
) -> None:
    """Record node latency using the current context collector.

    Convenience wrapper around MetricsCollector.record_latency() that
    uses the contextvar-based current collector. No-op if no collector
    is active.

    Args:
        node_name: LangGraph node name.
        start_time: Start timestamp (time.monotonic()).
        end_time: End timestamp (time.monotonic()).
        **metadata: Additional context fields.

    Example:
        >>> start = time.monotonic()
        >>> # ... node execution ...
        >>> record_latency("data_fetch", start, time.monotonic())
    """
    collector = get_current_collector()
    if collector is not None:
        collector.record_latency(node_name, start_time, end_time, metadata=metadata)


def record_token_usage(
    llm_response: Any,
    *,
    node_name: str | None = None,
    model_name: str | None = None,
) -> None:
    """Record token usage using the current context collector.

    Convenience wrapper for MetricsCollector.record_token_usage().

    Args:
        llm_response: LLM response object.
        node_name: Source node name.
        model_name: Model identifier.
    """
    collector = get_current_collector()
    if collector is not None:
        collector.record_token_usage(llm_response, node_name=node_name, model_name=model_name)


def record_quality_score(
    score: float,
    *,
    node_name: str = "output_composer",
    issues: list[str] | None = None,
) -> None:
    """Record quality score using the current context collector.

    Convenience wrapper for MetricsCollector.record_quality_score().

    Args:
        score: Quality score 0.0-1.0.
        node_name: Source node name.
        issues: Optional list of quality issues found.
    """
    collector = get_current_collector()
    if collector is not None:
        collector.record_quality_score(score, node_name=node_name, issues=issues)


def record_subagent_coverage(completed: int, total: int) -> None:
    """Record subagent coverage using the current context collector.

    Convenience wrapper for MetricsCollector.record_subagent_coverage().

    Args:
        completed: Number of subagents completed successfully.
        total: Total number of subagents attempted.
    """
    collector = get_current_collector()
    if collector is not None:
        collector.record_subagent_coverage(completed, total)


# ============================================================
# Internal helpers
# ============================================================


_LS_SCORE_MAX = 99999.9999


def _push_feedback(
    client: Any,
    run_id: str,
    key: str,
    value: float,
) -> None:
    """Push a single feedback metric to LangSmith.

    Clamps score to LangSmith's accepted range [-99999.9999, 99999.9999].

    Args:
        client: LangSmith client instance.
        run_id: Target run ID.
        key: Metric key name.
        value: Metric numeric value.
    """
    clamped = max(-_LS_SCORE_MAX, min(_LS_SCORE_MAX, value))
    try:
        client.create_feedback(
            run_id=run_id,
            key=key,
            score=clamped,
        )
    except Exception as exc:
        logger.debug("Failed to push feedback '%s' to LangSmith: %s", key, exc)


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Compute a percentile from sorted numeric values.

    Uses the nearest-rank method. Returns 0.0 for empty lists.

    Args:
        sorted_values: Sorted list of values (ascending).
        pct: Percentile to compute (0-100).

    Returns:
        float: The computed percentile value.
    """
    if not sorted_values:
        return 0.0
    k = (pct / 100.0) * (len(sorted_values) - 1)
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    # Linear interpolation between floor and ceil
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return d0 + d1
