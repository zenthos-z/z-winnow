"""Observability module for z-winnow.

Provides LangSmith tracing integration, LangGraph node-level trace
configuration, and custom metrics (latency, token usage, quality scores,
subagent coverage).

Submodules:
- langsmith_setup: LangSmith SDK initialization and trace context management
- tracing: @traced_node decorator for LangGraph node-level spans
- metrics: Custom metrics recording and querying

Usage:
    >>> from z_winnow.observability import init_langsmith
    >>> setup = init_langsmith()  # One-time initialization
    >>>
    >>> from z_winnow.observability.tracing import traced_node
    >>> @traced_node("data_fetch")
    ... async def my_node(state):
    ...     return {"current_phase": "done"}
"""

from z_winnow.observability.langsmith_setup import (
    LangSmithSetup,
    get_langsmith_callbacks,
    get_langsmith_setup,
    init_langsmith,
    is_langsmith_enabled,
    reset_langsmith,
)
from z_winnow.observability.metrics import (
    MetricEntry,
    MetricsCollector,
    RunStats,
    clear_current_collector,
    clear_store,
    get_current_collector,
    get_run_stats,
    list_run_ids,
    record_latency,
    record_quality_score,
    record_subagent_coverage,
    record_token_usage,
    save_run_stats,
    set_current_collector,
)
from z_winnow.observability.tracing import (
    NodeTraceMetadata,
    TracedGraphConfig,
    get_node_run_context,
    trace_llm_call,
    trace_node_func,
    traced_node,
)

__all__ = [
    "LangSmithSetup",
    "MetricEntry",
    "MetricsCollector",
    "NodeTraceMetadata",
    "RunStats",
    "TracedGraphConfig",
    "clear_current_collector",
    "clear_store",
    "get_current_collector",
    "get_langsmith_callbacks",
    "get_langsmith_setup",
    "get_node_run_context",
    "get_run_stats",
    "init_langsmith",
    "is_langsmith_enabled",
    "list_run_ids",
    "record_latency",
    "record_quality_score",
    "record_subagent_coverage",
    "record_token_usage",
    "reset_langsmith",
    "save_run_stats",
    "set_current_collector",
    "trace_llm_call",
    "trace_node_func",
    "traced_node",
]
