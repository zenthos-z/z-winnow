"""Graph package — StateGraph construction, error handling, and exports.

Provides the complete winnow LangGraph workflow with Send API fan-out,
plus retry utilities and quality-check functions for robust execution.

Public API:
    Graph construction:
        - build_graph(): Build the StateGraph with Send API fan-out
        - get_graph(): Get or create a compiled graph singleton

    Error handling:
        - NodeError: Exception with node_name, original_error, retry_count context
        - with_retry(): Decorator for retrying async node functions
        - QualityResult: Pydantic model for structured quality assessment
        - check_daily_report_quality(): Validate daily report completeness
"""

from z_winnow.graph.builder import (
    build_graph,
    get_graph,
)
from z_winnow.graph.error_handling import (
    NodeError,
    QualityResult,
    check_daily_report_quality,
    with_retry,
)
from z_winnow.graph.nodes.recovery import (
    with_node_recovery,
    with_node_recovery_no_timeout,
    with_subagent_recovery,
)

__all__ = [
    # Error handling
    "NodeError",
    "QualityResult",
    # Graph construction
    "build_graph",
    "check_daily_report_quality",
    "get_graph",
    # Node recovery
    "with_node_recovery",
    "with_node_recovery_no_timeout",
    "with_retry",
    "with_subagent_recovery",
]
