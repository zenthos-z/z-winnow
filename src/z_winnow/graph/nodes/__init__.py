"""Orchestrator graph nodes.

T-W12-11: data_fetch_node removed — Track F implementation deleted.
Main graph uses builder.py's inline node_data_fetch instead.

Provides:
- with_node_recovery: Error recovery decorator
"""

from z_winnow.graph.nodes.recovery import with_node_recovery

__all__ = [
    "with_node_recovery",
]
