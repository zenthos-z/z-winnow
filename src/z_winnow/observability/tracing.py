"""T-K2: LangGraph node-level trace configuration.

Provides the @traced_node decorator that wraps LangGraph node functions
with LangSmith trace spans, automatically recording metadata (date,
node_name, input_size, output_size, errors) at each execution stage.

Also provides trace_llm_call for explicit LLM call child spans within nodes,
TracedGraphConfig for graph-level trace setup, and get_node_run_context for
manual instrumentation within nodes.

Span hierarchy (LangGraph + LangSmith auto-instrumentation):
    graph_run (root — set via TracedGraphConfig)
    ├── data_fetch (node span — @traced_node enhanced)
    ├── orchestrator (node span — @traced_node enhanced)
    │   └── ChatAnthropic.call (LLM span — auto-instrumented by langchain)
    ├── fanout (conditional edge)
    │   ├── daily_reporter (node span — @traced_node enhanced)
    │   │   └── ChatOpenAI.call (LLM span — auto-instrumented)
    │   ├── resource_extractor (node span — @traced_node enhanced)
    │   │   └── ChatOpenAI.call (LLM span — auto-instrumented)
    │   └── engineering_analyzer (node span — @traced_node enhanced)
    │       └── ChatOpenAI.call (LLM span — auto-instrumented)
    ├── output_composer (node span — @traced_node enhanced)
    │   └── ChatAnthropic.call (LLM span — auto-instrumented)
    └── persist (node span — @traced_node enhanced)

Key patterns:
- EXP-004: NodeTraceMetadata uses Pydantic strict(extra='forbid')
- EXP-005: @traced_node compatible with @with_retry + NodeError + QualityResult
- EXP-011: structlog JSON compatibility for trace metadata export
- LangChain chat models (ChatOpenAI, ChatAnthropic) are auto-instrumented as
  child LLM spans when LANGCHAIN_TRACING_V2=true — no explicit trace_llm_call needed.

Usage:
    >>> from z_winnow.observability.tracing import traced_node, TracedGraphConfig
    >>>
    >>> @traced_node("data_fetch")
    ... async def node_data_fetch(state: OverallState) -> dict[str, Any]:
    ...     # Node logic here — automatically traced with metadata
    ...     return {"current_phase": "data_fetch", "messages": [...]}
    >>>
    >>> config = TracedGraphConfig(
    ...     trace_name="daily-report-20260428",
    ...     date="20260428",
    ...     group_name="test-group",
    ... ).to_runnable_config()
    >>> result = await graph.ainvoke(state, config=config)
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Type variable for decorated async node functions
F = TypeVar("F", bound=Callable[..., Any])


# ============================================================
# NodeTraceMetadata — Pydantic model for trace metadata (EXP-004)
# ============================================================


class NodeTraceMetadata(BaseModel, extra="forbid"):
    """Metadata attached to each node-level trace span.

    Stored as metadata on LangSmith spans via set_run_metadata().
    Uses Pydantic strict mode to prevent field schema drift (EXP-004).

    Attributes:
        node_name: LangGraph node name (e.g., "data_fetch", "daily_reporter").
        phase: Execution phase: input_validation | execution | output | error.
        input_size: Approximate size of state input (message count / context chars).
        output_size: Approximate size of state output (returned field count / chars).
        duration_ms: Execution duration in milliseconds.
        date: Target date from state (YYYYMMDD), if available.
        error: Error message if the node failed, None otherwise.
        model_used: Model identifier used by this node, if applicable.
        retry_count: Number of retry attempts (EXP-005 compatibility).
    """

    node_name: str = Field(
        description="LangGraph node name (e.g. data_fetch, daily_reporter)",
    )
    phase: str = Field(
        default="execution",
        description="Execution phase: input_validation | execution | output | error",
    )
    input_size: int = Field(
        default=0,
        ge=0,
        description="Approximate input size (message count or context chars)",
    )
    output_size: int = Field(
        default=0,
        ge=0,
        description="Approximate output size (field count or chars)",
    )
    duration_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Execution duration in milliseconds",
    )
    date: str | None = Field(
        default=None,
        description="Target date from state (YYYYMMDD), if available",
    )
    error: str | None = Field(
        default=None,
        description="Error message if node failed, None otherwise",
    )
    model_used: str | None = Field(
        default=None,
        description="Model identifier used by this node (from subagent output)",
    )
    retry_count: int = Field(
        default=0,
        ge=0,
        description="Number of retry attempts (EXP-005 compatibility)",
    )

    def to_dict(self) -> dict[str, Any]:
        """Export as a JSON-safe dict for LangSmith span metadata.

        Uses exclude_none=True so optional fields not present aren't
        written to the metadata dict — keeps spans clean.

        Returns:
            dict[str, Any]: JSON-safe metadata dict.
        """
        return self.model_dump(exclude_none=True)

    def to_structlog(self) -> dict[str, Any]:
        """Export as structlog-compatible flat dict (EXP-011).

        Converts all values to JSON-safe primitives for structlog
        key-value logging.

        Returns:
            dict[str, Any]: Flat dict suitable for structlog extra= kwarg.
        """
        return self.model_dump(exclude_none=True)


# ============================================================
# Internal helpers — LangSmith SDK access
# ============================================================


def _get_langsmith_traceable() -> Any | None:
    """Get the langsmith.run_helpers.traceable function if available.

    Returns None if langsmith is not installed, LangSmith is disabled,
    or any import error occurs — no crash, graceful fallback.

    Returns:
        Callable | None: The langsmith traceable decorator function, or None.
    """
    try:
        from z_winnow.observability.langsmith_setup import (
            is_langsmith_enabled,
        )

        if not is_langsmith_enabled():
            return None

        from langsmith.run_helpers import traceable

        return traceable
    except ImportError:
        logger.debug("langsmith package not available — node tracing disabled")
        return None
    except Exception as exc:
        logger.debug("Failed to initialize langsmith traceable: %s", exc)
        return None


def _get_current_run_tree() -> Any | None:
    """Get the current LangSmith RunTree from context.

    Returns None if no run is active (e.g., LangSmith disabled or
    called outside a traced context).

    Returns:
        RunTree | None: The current run tree, or None.
    """
    try:
        from langsmith.run_helpers import get_current_run_tree

        return get_current_run_tree()
    except ImportError:
        return None
    except Exception:
        return None


def _set_run_metadata(data: dict[str, Any]) -> bool:
    """Set metadata on the current LangSmith run span.

    Uses langsmith's set_run_metadata() to update the span's metadata
    dict in-place. This is safe to call outside a run context — it
    silently succeeds (no-op) when no run is active.

    Args:
        data: Metadata key-value pairs to set on the current span.

    Returns:
        bool: True if metadata was set, False if no run was active.
    """
    try:
        from langsmith.run_helpers import set_run_metadata

        set_run_metadata(**data)
        return True
    except ImportError:
        return False
    except Exception as exc:
        logger.debug("Failed to set run metadata: %s", exc)
        return False


def _get_langchain_tracer(
    trace_name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, str] | None = None,
) -> Any | None:
    """Get a LangChainTracer callback handler for graph-level tracing.

    This creates a proper LangChain callback handler that LangGraph's
    ainvoke can use to trace all node executions automatically.

    Args:
        trace_name: Human-readable trace name for this run.
        tags: Tags for filtering in LangSmith UI.
        metadata: Metadata dict for the trace root.

    Returns:
        LangChainTracer | None: The tracer callback, or None if unavailable.
    """
    try:
        from z_winnow.observability.langsmith_setup import (
            is_langsmith_enabled,
        )

        if not is_langsmith_enabled():
            return None

        from langchain_core.tracers.langchain import LangChainTracer

        from z_winnow.observability.langsmith_setup import (
            get_langsmith_setup,
        )

        setup = get_langsmith_setup()
        return LangChainTracer(
            project_name=trace_name or setup.project,
            tags=tags,
            metadata=metadata,
        )
    except ImportError:
        logger.debug("LangChainTracer not available — graph tracing disabled")
        return None
    except Exception as exc:
        logger.debug("Failed to create LangChainTracer: %s", exc)
        return None


# ============================================================
# traced_node — Decorator for LangGraph nodes
# ============================================================


def traced_node(
    node_name: str,
    *,
    record_io_size: bool = True,
    extract_date_from_state: bool = True,
    tags: list[str] | None = None,
) -> Callable[[F], F]:
    """Decorate a LangGraph node function with LangSmith trace spans.

    Wraps the node function to automatically:
    1. Create a named LangSmith span for the node execution.
    2. Record input/output size metadata on the span.
    3. Extract the ``date`` field from state for trace correlation.
    4. Measure execution duration and record it.
    5. On error, record the error in span metadata and re-raise.

    The decorator uses ``langsmith.run_helpers.traceable`` directly on the
    wrapper function, which ensures the span is properly created as a child
    of any parent run (e.g., the graph run). Inside the wrapper,
    ``set_run_metadata()`` is called to inject dynamic metadata (duration_ms,
    output_size, model_used) that can only be known after execution.

    When LangSmith is unavailable or disabled, the decorator becomes a no-op
    wrapper that logs timing info via structlog (EXP-011).

    Args:
        node_name: Human-readable node name for the trace span.
        record_io_size: If True, compute approximate input/output sizes
                        from the state dict and return value.
        extract_date_from_state: If True, extract the ``date`` field from
                                 the first positional arg (state) if it's a dict.
        tags: Optional list of tags to attach to the LangSmith span.

    Returns:
        A decorator function that wraps an async node function with tracing.

    Raises:
        The decorator itself never raises — it always returns a wrapped function.
        The wrapped function propagates all original exceptions after recording
        them in the trace span.

    Example:
        >>> @traced_node("data_fetch", tags=["phase-1", "io"])
        ... async def node_data_fetch(state: OverallState) -> dict[str, Any]:
        ...     # Fetch data from data source API
        ...     return {"messages": [...], "current_phase": "data_fetch"}
    """

    def decorator(func: F) -> F:
        # Attempt to get the langsmith traceable decorator
        _traceable = _get_langsmith_traceable()

        if _traceable is not None:
            # ---- LangSmith enabled: full trace instrumentation ----

            @functools.wraps(func)
            @_traceable(
                name=node_name,
                run_type="chain",
                tags=tags,
            )
            async def traced_wrapper(*args: Any, **kwargs: Any) -> Any:
                """Traceable wrapper that sets dynamic metadata on the span."""
                start_time = time.monotonic()
                meta = NodeTraceMetadata(node_name=node_name, phase="execution")

                # --- Pre-execution: extract state metadata ---
                state = args[0] if args else {}
                if extract_date_from_state and isinstance(state, dict):
                    meta.date = state.get("date")  # type: ignore[arg-type]
                if record_io_size and isinstance(state, dict):
                    meta.input_size = _estimate_input_size(state)

                # Set initial metadata on the LangSmith span
                _set_run_metadata(meta.to_dict())

                # --- Execution ---
                try:
                    result = await func(*args, **kwargs)
                    meta.duration_ms = (time.monotonic() - start_time) * 1000.0

                    if record_io_size and isinstance(result, dict):
                        meta.output_size = _estimate_output_size(result)
                        if "model_used" in result:
                            meta.model_used = result["model_used"]

                    # Update span with final metadata
                    _set_run_metadata(meta.to_dict())

                    logger.debug(
                        "Node [%s] completed in %.0fms | input=%d output=%d%s",
                        node_name,
                        meta.duration_ms,
                        meta.input_size,
                        meta.output_size,
                        f" | model={meta.model_used}" if meta.model_used else "",
                        extra={"node_metadata": meta.to_structlog()},
                    )
                    return result

                except Exception as exc:
                    meta.duration_ms = (time.monotonic() - start_time) * 1000.0
                    meta.error = str(exc)
                    meta.phase = "error"

                    # Record error on span before re-raising
                    _set_run_metadata(meta.to_dict())

                    logger.warning(
                        "Node [%s] FAILED in %.0fms: %s",
                        node_name,
                        meta.duration_ms,
                        exc,
                        exc_info=True,
                        extra={"node_metadata": meta.to_structlog()},
                    )
                    raise

        else:
            # ---- LangSmith disabled or unavailable: lightweight logging ----

            @functools.wraps(func)
            async def traced_wrapper(*args: Any, **kwargs: Any) -> Any:
                """Lightweight wrapper logging timing when LangSmith is off."""
                start_time = time.monotonic()
                meta = NodeTraceMetadata(node_name=node_name, phase="execution")

                state = args[0] if args else {}
                if extract_date_from_state and isinstance(state, dict):
                    meta.date = state.get("date")  # type: ignore[arg-type]
                if record_io_size and isinstance(state, dict):
                    meta.input_size = _estimate_input_size(state)

                try:
                    result = await func(*args, **kwargs)
                    meta.duration_ms = (time.monotonic() - start_time) * 1000.0
                    if record_io_size and isinstance(result, dict):
                        meta.output_size = _estimate_output_size(result)

                    logger.debug(
                        "Node [%s] completed in %.0fms (no LangSmith)",
                        node_name,
                        meta.duration_ms,
                    )
                    return result

                except Exception as exc:
                    meta.duration_ms = (time.monotonic() - start_time) * 1000.0
                    meta.error = str(exc)
                    meta.phase = "error"
                    logger.warning(
                        "Node [%s] FAILED in %.0fms: %s",
                        node_name,
                        meta.duration_ms,
                        exc,
                        exc_info=True,
                        extra={"node_metadata": meta.to_structlog()},
                    )
                    raise

        return traced_wrapper  # type: ignore[return-value]

    return decorator


# ============================================================
# trace_llm_call — Async context manager for explicit LLM spans
# ============================================================


@asynccontextmanager
async def trace_llm_call(
    model_name: str,
    *,
    prompt_tokens: int = 0,
    metadata: dict[str, Any] | None = None,
) -> AsyncIterator[Any | None]:
    """Async context manager for explicit LLM call tracing within a node.

    Creates a child span (run_type="llm") under the current node span.
    Use this for LLM calls that do NOT go through LangChain's chat model
    infrastructure (which auto-instruments LLM calls automatically).

    For LangChain-native calls (ChatOpenAI, ChatAnthropic via
    model.ainvoke()), LangSmith auto-instruments child LLM spans — no
    explicit trace_llm_call is needed in that case.

    When called outside a traced context or when LangSmith is disabled,
    this is a no-op and yields None.

    Args:
        model_name: Model identifier (e.g., "claude-sonnet-4-20250514").
        prompt_tokens: Estimated prompt token count for pre-call metadata.
        metadata: Optional additional metadata to attach to the LLM span.

    Yields:
        RunTree | None: The child run tree for the LLM call, or None if
        no parent run is active. The caller can use this to set
        final metadata (completion_tokens, etc.) before the span ends.

    Example:
        >>> # Explicit LLM tracing (non-LangChain HTTP call)
        >>> async with trace_llm_call("my-custom-model", prompt_tokens=1024) as run:
        ...     response = await direct_http_call(messages)
        ...     if run is not None:
        ...         run.add_metadata({"completion_tokens": len(response)})
        ...     return response

    Example (LangChain-native — auto-instrumented, no trace_llm_call needed):
        >>> # ChatOpenAI is auto-instrumented by LangSmith
        >>> response = await model.ainvoke(messages)  # creates LLM span automatically
    """
    child = None

    try:
        run_tree = _get_current_run_tree()
        if run_tree is None:
            yield None
            return

        merged_meta: dict[str, Any] = {
            "model_name": model_name,
            "prompt_tokens": prompt_tokens,
            **(metadata or {}),
        }

        child = run_tree.create_child(
            name=f"LLM:{model_name}",
            run_type="llm",
            inputs={"prompt_tokens": prompt_tokens},
            extra=merged_meta,
        )
        child.add_metadata(merged_meta)

        logger.debug(
            "LLM trace span created: %s (prompt_tokens=%d)",
            model_name,
            prompt_tokens,
        )

        yield child

        # Normal completion: end the child span
        child.end()

    except Exception as exc:
        if child is not None:
            child.add_metadata({"error": str(exc)})
            child.end(error=str(exc))
        raise


# ============================================================
# TracedGraphConfig — RunnableConfig with tracing callbacks
# ============================================================


class TracedGraphConfig(BaseModel, extra="forbid"):
    """Configuration for a traced LangGraph run.

    Builds a LangChain RunnableConfig dict with proper LangSmith
    tracing callbacks and metadata for graph.ainvoke() calls.

    When LangSmith is enabled, includes a LangChainTracer callback handler
    that creates a root trace span. All node-level @traced_node spans
    automatically become children of this root.

    Attributes:
        trace_name: Human-readable trace name for the graph run.
        date: Target date (YYYYMMDD) for trace correlation.
        group_name: Chat group name for trace context.
        report_types: Report types being generated (for tag-based filtering).
        tags: Optional custom tags for trace filtering.
        metadata: Optional additional metadata for the trace root.
    """

    trace_name: str = Field(
        default="winnow-graph-run",
        description="Human-readable trace name for the graph run",
    )
    date: str | None = Field(
        default=None,
        description="Target date (YYYYMMDD) for trace correlation",
    )
    group_name: str | None = Field(
        default=None,
        description="Chat group name for trace context",
    )
    report_types: list[str] = Field(
        default_factory=list,
        description="Report types being generated",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Custom tags for trace filtering",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata for the trace root",
    )

    def to_runnable_config(self) -> dict[str, Any]:
        """Build a RunnableConfig dict for graph.ainvoke().

        Includes LangSmith callbacks (LangChainTracer) and trace metadata
        when LangSmith is enabled. Use this as the ``config`` parameter
        when invoking a compiled LangGraph graph.

        The LangChainTracer callback creates the root graph_run span.
        Node-level @traced_node decorators then create child spans.

        Returns:
            dict: RunnableConfig-compatible dict with callbacks and metadata.

        Example:
            >>> config = TracedGraphConfig(
            ...     trace_name="daily-report-20260428",
            ...     date="20260428",
            ...     group_name="test-group",
            ...     report_types=["daily", "resources", "engineering"],
            ... ).to_runnable_config()
            >>> result = await graph.ainvoke(state, config=config)
        """
        # Build merged metadata
        merged_metadata: dict[str, Any] = {**self.metadata}
        if self.date:
            merged_metadata["date"] = self.date
        if self.group_name:
            merged_metadata["group_name"] = self.group_name
        if self.report_types:
            merged_metadata["report_types"] = self.report_types

        # Build tags list
        all_tags = list(self.tags)
        if self.date:
            all_tags.append(f"date:{self.date}")
        if self.group_name:
            all_tags.append(f"group:{self.group_name}")
        for rt in self.report_types:
            all_tags.append(f"report:{rt}")

        # Build config dict
        config: dict[str, Any] = {
            "metadata": merged_metadata,
            "tags": all_tags,
        }

        # Create LangChainTracer for graph-level tracing
        tracer = _get_langchain_tracer(
            trace_name=self.trace_name,
            tags=all_tags,
            metadata={str(k): str(v) for k, v in merged_metadata.items()},
        )
        if tracer is not None:
            config["callbacks"] = [tracer]

        return config


# ============================================================
# get_node_run_context — Access current RunTree for manual use
# ============================================================


def get_node_run_context() -> Any | None:
    """Get the current LangSmith RunTree for manual instrumentation.

    Use this within a @traced_node function to access the span directly
    for custom metadata injection or to create child spans.

    Returns None when:
    - LangSmith is not installed
    - LangSmith is disabled
    - Called outside of any traced context

    Returns:
        RunTree | None: The current run tree span, or None.

    Example:
        >>> @traced_node("my_node")
        ... async def my_node(state):
        ...     run = get_node_run_context()
        ...     if run is not None:
        ...         run.add_tags(["custom-tag"])
        ...         run.add_metadata({"custom_key": "value"})
        ...     return {"result": "done"}
    """
    return _get_current_run_tree()


# ============================================================
# Input/output size estimation helpers
# ============================================================


def _estimate_input_size(state: dict[str, Any]) -> int:
    """Estimate the input size from a LangGraph state dict.

    Uses the following heuristics in order:
    1. Message count (if ``messages`` or ``raw_messages`` list exists)
    2. Context text length (if ``context_text`` or ``final_report`` exists)
    3. Total non-empty state keys as fallback

    Args:
        state: The LangGraph state dict (OverallState or equivalent).

    Returns:
        int: Approximate input size (message count or character count).
    """
    # 1. Message-based estimation (most accurate for data nodes)
    messages = state.get("messages", []) or state.get("raw_messages", [])
    if isinstance(messages, list) and len(messages) > 0:
        return len(messages)

    # 2. Text-based estimation
    context_text = state.get("context_text", "") or state.get("final_report", "")
    if isinstance(context_text, str) and len(context_text) > 0:
        return len(context_text)

    # 3. Fallback: count non-empty, non-None fields
    return sum(1 for v in state.values() if v is not None and v != "" and v != [] and v != {})


def _estimate_output_size(result: dict[str, Any]) -> int:
    """Estimate the output size from a node's return dict.

    Uses the following heuristics in order:
    1. String length for text fields (final_report, composed_output)
    2. List length for collection fields (daily_reports, messages, etc.)
    3. Non-empty key count as fallback

    Args:
        result: The dict returned by a LangGraph node function.

    Returns:
        int: Approximate output size (character count or item count).
    """
    # 1. Text-based output fields
    for field in (
        "final_report",
        "composed_output",
        "context_text",
    ):
        val = result.get(field)
        if isinstance(val, str) and len(val) > 0:
            return len(val)

    # 2. Collection-based output fields
    for field in (
        "daily_reports",
        "resource_reports",
        "engineering_reports",
        "messages",
        "raw_messages",
        "report_sections",
    ):
        val = result.get(field)
        if isinstance(val, list) and len(val) > 0:
            return len(val)

    # 3. Fallback: count non-empty, non-None fields
    return sum(1 for v in result.values() if v is not None and v != "" and v != [] and v != {})


# ============================================================
# trace_node_func — synchronous convenience wrapper
# ============================================================


def trace_node_func(
    func: Callable[..., Any],
    *,
    node_name: str | None = None,
    tags: list[str] | None = None,
) -> Callable[..., Any]:
    """Convenience function to trace a synchronous node function.

    This is an equivalent of traced_node(node_name)(func) for cases
    where you want to apply tracing to an existing function without
    using the decorator syntax.

    Args:
        func: The synchronous or async node function to wrap.
        node_name: Name for the trace span. Defaults to func.__name__.
        tags: Optional tags for the span.

    Returns:
        The wrapped function with LangSmith tracing applied.

    Example:
        >>> # Apply tracing to an existing function
        >>> traced = trace_node_func(my_existing_node, node_name="custom_name")
        >>> result = await traced(state)
    """
    name = node_name or func.__name__
    return traced_node(name, tags=tags)(func)
