"""T-F7: Node-level error recovery — graceful subagent failure handling.

Provides the ``with_node_recovery`` decorator that wraps async node functions
with try/except, recording failures to ``state["errors"]`` and returning a
partial result dict instead of propagating exceptions.

Individual subagent failure does NOT block the overall workflow. The
orchestrator checks upstream result completeness and triggers degraded
output when needed.

Architecture reference: plans/tracks/track-f.md T-F7
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


# ============================================================
# Timeout Configuration (P008 / L020 / A009)
# ============================================================
# P008: 超时 = 历史 P95 耗时 × 1.5 — subagent P95 ~60-80s → ×1.5 ≈ 120s.
# L020: 区分派发超时 (harness 等待) 与执行超时 (agent/node 内部完成).
#   - DEFAULT_SUBAGENT_TIMEOUT 是执行超时 (node 内部), 从 settings/env 读取
#   - 派发超时由 Box0/harness 管理, 不在此处控制
# A009: Box0 daemon 300s 硬超时, 子 agent 执行超时 120s < 300s 安全阈.


def _get_default_subagent_timeout() -> float:
    """Read subagent timeout from settings/env.

    P008: 默认 120s = subagent P95 ~60-80s × 1.5.
    L020: 此为执行超时 (node 内部完成), 非派发超时.

    Returns:
        float: Subagent timeout in seconds.
    """
    import os

    timeout_str = os.getenv("SUBAGENT_TIMEOUT_SECONDS", "")
    if not timeout_str:
        timeout_str = os.getenv("WINNOW_SUBAGENT_TIMEOUT_SECONDS", "")
    if timeout_str:
        try:
            return float(timeout_str)
        except (ValueError, TypeError):
            pass
    # Fallback to pydantic-settings default (120)
    try:
        from z_winnow.config.settings import get_settings

        return float(get_settings().subagent_timeout_seconds)
    except Exception:
        return 120.0


DEFAULT_SUBAGENT_TIMEOUT: float = _get_default_subagent_timeout()
"""默认子 agent 执行超时 (秒). P008: 120s = subagent P95 ~60-80s × 1.5.
L020: 此为执行超时, 非派发超时. A009: 120s < 300s Box0 硬杀阈值."""

# Error key for the errors list entry
ERROR_KEY_NODE = "node"
ERROR_KEY_TYPE = "error_type"
ERROR_KEY_MSG = "error_message"


def _make_error_entry(node_name: str, exc: BaseException) -> dict[str, str]:
    """Build a structured error entry dict for state.errors.

    Args:
        node_name: Name of the node where the error occurred.
        exc: The caught exception.

    Returns:
        Dict with node, error_type, error_message keys.
    """
    return {
        ERROR_KEY_NODE: node_name,
        ERROR_KEY_TYPE: type(exc).__name__,
        ERROR_KEY_MSG: str(exc),
    }


def with_node_recovery(
    node_name: str,
    *,
    timeout: float | None = DEFAULT_SUBAGENT_TIMEOUT,
    partial_result: dict[str, Any] | None = None,
) -> Callable[[F], F]:
    """Decorator that wraps an async node function with error recovery.

    When the wrapped node raises an exception:
    1. The exception is caught and logged.
    2. A structured error entry is appended to ``state["errors"]``.
    3. A partial result dict (or empty dict) is returned instead of raising.

    This ensures a single subagent failure does not block the entire graph.
    The orchestrator inspects ``state["errors"]`` to decide whether to
    trigger degraded output.

    Optional timeout: if ``timeout`` is set, the node function is wrapped
    with ``asyncio.wait_for`` and a ``TimeoutError`` is treated the same as
    any other exception.

    Args:
        node_name: Human-readable node name for error logging.
        timeout: Max execution time in seconds (None = no timeout).
        partial_result: Dict to return on failure. Defaults to
            ``{"errors": [error_entry]}``.

    Returns:
        Decorated async function with error recovery.

    Example:
        >>> @with_node_recovery("daily_reporter", timeout=60)
        ... async def daily_reporter_node(state: OverallState) -> dict:
        ...     return await call_subagent(state)
    """
    if partial_result is None:
        partial_result = {}

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                if timeout is not None:
                    result = await asyncio.wait_for(
                        func(*args, **kwargs),
                        timeout=timeout,
                    )
                else:
                    result = await func(*args, **kwargs)
                return result
            except TimeoutError:
                logger.error(
                    "Node '%s' timed out after %s s",
                    node_name,
                    f"{timeout:.1f}" if timeout is not None else "N/A",
                )
                error_entry = _make_error_entry(
                    node_name,
                    TimeoutError(f"Node timed out after {timeout}s"),
                )
                return {
                    **partial_result,
                    "errors": [error_entry],
                }
            except Exception as exc:
                logger.error(
                    "Node '%s' failed: %s: %s",
                    node_name,
                    type(exc).__name__,
                    exc,
                )
                error_entry = _make_error_entry(node_name, exc)
                return {
                    **partial_result,
                    "errors": [error_entry],
                }

        return wrapper  # type: ignore[return-value]

    return decorator


# ============================================================
# Convenience: pre-configured recovery decorators
# ============================================================


def with_subagent_recovery(
    node_name: str,
) -> Callable[[F], F]:
    """Shorthand for subagent node recovery with DEFAULT_SUBAGENT_TIMEOUT.

    Equivalent to ``with_node_recovery(node_name, timeout=DEFAULT_SUBAGENT_TIMEOUT)``.
    P008: DEFAULT_SUBAGENT_TIMEOUT defaults to 120s (subagent P95 ~60-80s x 1.5).
    L020: 此为执行超时 (node 内部), 非派发超时 (harness 等待).

    Args:
        node_name: Subagent node name.

    Returns:
        Configured recovery decorator.
    """
    return with_node_recovery(node_name, timeout=DEFAULT_SUBAGENT_TIMEOUT)


def with_node_recovery_no_timeout(node_name: str) -> Callable[[F], F]:
    """Shorthand for node recovery without timeout.

    Equivalent to ``with_node_recovery(node_name, timeout=None)``.

    Args:
        node_name: Node name for error logging.

    Returns:
        Configured recovery decorator.
    """
    return with_node_recovery(node_name, timeout=None)
