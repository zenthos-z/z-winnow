"""Orchestrator module — Graph invocation entry point.

Provides:
- orchestrate(): Main entry point to run the LangGraph workflow.
- ORCHESTRATOR_SYSTEM_PROMPT: System prompt for the orchestrator agent.
- resolve_subagents(): Map report types to required subagent node names.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from z_winnow.graph.builder import build_graph
from z_winnow.observability.langsmith_setup import (
    init_langsmith,
)
from z_winnow.observability.metrics import (
    MetricsCollector,
    clear_current_collector,
    save_run_stats,
    set_current_collector,
)
from z_winnow.orchestrator.orchestrator_loop import (
    ORCHESTRATOR_SYSTEM_PROMPT,
    check_daily_report_quality,
    load_historical_report,
    query_database,
    resolve_subagents,
    validate_report_types,
)
from z_winnow.state import OverallState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-run logging helpers
# ---------------------------------------------------------------------------


def _setup_run_logging(run_id: str, date: str) -> logging.FileHandler | None:
    """为单次 pipeline run 创建专用日志文件。

    日志写入 ``logs/runs/{date}/{run_id}.log``，与全局 logs/winnow.log 并行。
    同时在 structlog context 中绑定 run_id，让此线程内所有后续日志自动带 run_id。

    Returns:
        FileHandler 实例，供 _teardown_run_logging 清理；失败返回 None。
    """
    import os
    import structlog

    try:
        log_dir = os.path.join("logs", "runs", date)
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"{run_id}.log")

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(fmt)

        root = logging.getLogger()
        root.addHandler(handler)

        # 绑定 run_id 到 structlog 上下文（所有 log 行自动带 run_id）
        structlog.contextvars.bind_contextvars(run_id=run_id)

        logger.debug("Per-run logging initialized: %s", log_path)
        return handler
    except Exception:
        logger.debug("Failed to setup per-run logging (non-blocking)", exc_info=True)
        return None


def _teardown_run_logging(handler: logging.FileHandler | None) -> None:
    """移除 per-run 日志 handler 并解绑 structlog 上下文。"""
    import structlog

    try:
        if handler is not None:
            root = logging.getLogger()
            root.removeHandler(handler)
            handler.close()
        structlog.contextvars.unbind_contextvars("run_id")
    except Exception:
        pass


async def orchestrate(
    group_name: str,
    date: str,
    report_types: list[str],
    api_base_url: str = "http://127.0.0.1:5031",
    api_token: str = "",
    # P009: Regenerate mode parameters (optional, default to normal daily_run)
    regenerate: bool = False,
    source: str = "daily_run",
    prior_corrections: list[dict[str, Any]] | None = None,
    cached_messages: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
) -> str:
    """Run the full winnow report generation workflow.

    This is the main entry point for the LangGraph workflow. It:
    1. Constructs the initial OverallState from input parameters
    2. Builds and compiles the StateGraph
    3. Invokes the graph with ainvoke()
    4. Returns the composed final_report Markdown string

    The graph internally handles:
    - Data fetching (mock or real data source API)
    - Orchestrator agent task decomposition
    - Send API fan-out to 3 parallel subagents
    - Subagent result merging via Annotated reducers
    - Output composition to Markdown
    - SQLite three-layer persistence

    Args:
        group_name: Chat group display name (e.g., "测试群聊").
        date: Target date in YYYYMMDD format (e.g., "20260428").
        report_types: Report types to generate, e.g. ["daily", "resources", "engineering"].
        api_base_url: Data source API base URL (default http://127.0.0.1:5031).
        api_token: Data source API authentication token.
        regenerate: P009 — if True, skip data_fetch/content_enrich, reuse cached messages.
        source: P009 — "daily_run" (default) | "regenerate" | "manual".
        prior_corrections: P009 — admin feedback corrections for prompt injection in regenerate.
        cached_messages: P009 — pre-fetched messages to reuse (avoids redundant data source calls).

    Returns:
        str: The complete Markdown report text.

    Raises:
        RuntimeError: If graph compilation or invocation fails.

    Example:
        >>> import asyncio
        >>> report = asyncio.run(orchestrate(
        ...     group_name="测试群聊",
        ...     date="20260428",
        ...     report_types=["daily", "resources", "engineering"],
        ... ))
        >>> assert "# 群日报" in report
    """
    # ── Observability: initialize LangSmith tracing ──────────
    # Graceful degradation: if LANGCHAIN_API_KEY is not configured,
    # init_langsmith() returns a disabled setup and the graph runs
    # without tracing — no crash, no hard error.
    init_langsmith()

    # ── Observability: initialize per-run metrics collector ──
    import uuid

    run_id = run_id or str(uuid.uuid4())
    collector = MetricsCollector(run_id=run_id)
    set_current_collector(collector)
    run_start_time = time.monotonic()

    # ── Per-run logging: dedicated log file + structlog context ──
    _run_log_handler = _setup_run_logging(run_id, date)

    # P0-4: Insert pipeline_runs row for SSE progress tracking
    try:
        from z_winnow.graph.progress import insert_pipeline_run

        await insert_pipeline_run(
            run_id,
            component="graph",
            group_id=group_name,
            date=date,
        )
        logger.info("orchestrate: created pipeline_runs row for run_id=%s", run_id)
    except Exception as _pr_exc:
        logger.debug("orchestrate: insert_pipeline_run failed (non-blocking): %s", _pr_exc)

    logger.info(
        "orchestrate: group=%s, date=%s, types=%s, mode=%s, corrections=%d",
        group_name,
        date,
        report_types,
        "regenerate" if regenerate else "daily_run",
        len(prior_corrections) if prior_corrections else 0,
    )

    # Build initial state
    initial_state: OverallState = {
        "group_name": group_name,
        "date": date,
        "run_id": run_id,  # P0-4: needed by with_progress wrappers
        "report_types": report_types,
        "api_base_url": api_base_url,
        "api_token": api_token,
        "messages": [],
        "image_descriptions": {},
        "link_previews": {},
        "image_analysis_failed": False,
        "content_enrich_enabled": False,
        "subagent_task": "",
        "subagent_type": "",
        "daily_reports": [],
        "resource_reports": [],
        "engineering_reports": [],
        "final_report": "",
        "report_sections": [],
        "raw_message_count": 0,
        "context_count": 0,
        "topic_summary_count": 0,
        "memory_file_path": "",
        "report_file_path": "",
        "errors": [],
        "current_phase": "init",
        # T-W10-D-b: Version tracking — start_time 用于计算 build_duration_s
        "start_time": run_start_time,
        "source": source,  # P009: "daily_run" | "regenerate" | "manual"
        # P009: Regenerate mode cascade — 透传至 data_fetch/content_enrich/persist 节点
        "regenerate": regenerate,
        "prior_corrections": prior_corrections or [],
        "report_id": f"{group_name}-{date}",
        # CT-2: custom_tables initialized as None; node_unified_reporter resolves from DB.
        "custom_tables": None,
    }

    # P009: regenerate mode — pre-populate cached messages to skip data_fetch
    if cached_messages:
        initial_state["messages"] = cached_messages  # type: ignore[index]

    # Build and compile the graph
    graph = build_graph()
    compiled = graph.compile()
    logger.info("Graph compiled successfully with %d nodes", len(compiled.nodes))

    # ── Observability: build TracedGraphConfig for graph-level trace ──
    from z_winnow.observability.tracing import TracedGraphConfig

    trace_config = TracedGraphConfig(
        trace_name=f"winnow-{group_name}-{date}",
        date=date,
        group_name=group_name,
        report_types=report_types,
        tags=["winnow", "graph-run"],
    )
    invoke_config = trace_config.to_runnable_config()

    # Invoke the graph
    _invocation_succeeded = False
    _invocation_exc_msg = ""
    result = {}
    try:
        result: Any = await compiled.ainvoke(initial_state, config=invoke_config)  # type: ignore[call-overload]
        _invocation_succeeded = True
    except Exception as exc:
        _invocation_exc_msg = f"{type(exc).__name__}: {exc}"
        logger.exception("Graph invocation failed: %s", exc)
        raise
    finally:
        # 更新 pipeline_runs 终态（completed / failed）+ 错误详情
        try:
            import datetime

            from z_winnow.graph.progress import update_pipeline_run

            # 收集错误信息：优先用 graph state errors，其次用异常信息
            run_errors: list[str] = []
            if _invocation_succeeded and isinstance(result, dict):
                graph_errors = result.get("errors", [])
                if isinstance(graph_errors, list):
                    run_errors = [str(e) for e in graph_errors]
            elif _invocation_exc_msg:
                run_errors = [_invocation_exc_msg]

            error_message = "; ".join(run_errors) if run_errors else None

            await update_pipeline_run(
                run_id,
                status="completed" if _invocation_succeeded else "failed",
                completed_at=datetime.datetime.now().isoformat(),
                error_message=error_message,
            )
        except Exception:
            logger.debug("update final pipeline status failed (non-blocking)")
        # ── Per-run logging: teardown dedicated handler ──
        _teardown_run_logging(_run_log_handler)
        # ── Observability: finalize metrics collection ───────
        run_end_time = time.monotonic()
        collector.record_latency(
            "orchestrate_total",
            run_start_time,
            run_end_time,
        )
        stats = collector.get_stats()
        save_run_stats(run_id, stats)
        collector.push_to_langsmith()
        clear_current_collector()
        logger.debug(
            "Metrics collected for run=%s: %d entries, %.0fms total",
            run_id,
            len(collector.entries),
            stats.total_latency_ms,
        )

    final_report: str = result.get("final_report", "")
    errors: list[str] = result.get("errors", [])

    if errors:
        logger.warning("Graph completed with %d errors: %s", len(errors), errors)

    logger.info("orchestrate: complete, report length=%d chars", len(final_report))
    return final_report


__all__ = [
    # System prompt
    "ORCHESTRATOR_SYSTEM_PROMPT",
    # Quality check facade
    "check_daily_report_quality",
    "load_historical_report",
    # Entry point
    "orchestrate",
    # Tools
    "query_database",
    "resolve_subagents",
    "validate_report_types",
]
