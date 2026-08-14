"""T-W9-F-b: Graph node progress tracking — writes to pipeline_runs for SSE push.

Provides the ``with_progress`` wrapper that decorates LangGraph node functions
to record progress information before and after each node execution. The
``pipeline_runs`` table (created by T-A3 / migration 005) is the data source
for SSE-based real-time progress streaming.

Architecture:
    with_progress (outer wrapper, progress to DB)
        └── node function (inner, traced by LangGraph auto-tracing)

    LangGraph auto-tracing (LANGCHAIN_TRACING_V2=true) creates spans for each
    node.  ``with_progress`` handles DB progress updates and does not interact
    with LangSmith.

Node weight table (Design Doc §3.6.1):
    data_fetch: 0.15, content_enrich: 0.15, orchestrator: 0.05,
    unified_reporter: 0.40, output_composer: 0.10, persist: 0.10,
    write_reports: 0.03
    → Defined in settings.py ``NODE_PROGRESS_WEIGHTS`` (P035), not hardcoded.

Usage:
    >>> from z_winnow.graph.progress import with_progress
    >>> builder.add_node("data_fetch", with_progress(node_data_fetch, "data_fetch", 0.15))
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from z_winnow.config import get_settings

# Python 3.11+ has datetime.UTC; use timezone.utc for compatibility
_UTC = getattr(datetime, "UTC", timezone.utc)  # noqa: UP017

logger = logging.getLogger(__name__)

# ============================================================
# Database helpers
# ============================================================


async def update_pipeline_run(run_id: str, **kwargs: Any) -> bool:
    """Update a ``pipeline_runs`` row by ``run_id``.

    Handles three update patterns:
    - ``current_node=...`` → direct SET current_node = ?
    - ``progress_pct=...`` → direct SET progress_pct = ?
    - ``node_history_append=...`` → reads existing JSON array, appends entry,
      writes back as JSON string

    Uses a new connection each call (stateless — no connection pooling needed
    for the low-frequency graph node lifecycle).

    Args:
        run_id: The pipeline run UUID to update.
        **kwargs: Column names and values. Special key ``node_history_append``
                  expects a dict ``{node, ts, ms}`` to append.

    Returns:
        True if the row was updated, False if ``run_id`` not found.
    """
    import aiosqlite

    # A013: read db_path from Settings inside the function body so env /
    # WINNOW_DB_PATH overrides take effect at call time (not import time).
    db_path = get_settings().db_path

    # Ensure directory exists for first-write scenarios
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row

            # Handle direct column updates
            if "current_node" in kwargs:
                await db.execute(
                    "UPDATE pipeline_runs SET current_node = ? WHERE run_id = ?",
                    (kwargs["current_node"], run_id),
                )

            if "progress_pct" in kwargs:
                await db.execute(
                    "UPDATE pipeline_runs SET progress_pct = ? WHERE run_id = ?",
                    (kwargs["progress_pct"], run_id),
                )

            if "message_count" in kwargs:
                await db.execute(
                    "UPDATE pipeline_runs SET message_count = ? WHERE run_id = ?",
                    (kwargs["message_count"], run_id),
                )

            # Handle node_history_append: read → append → write
            if "node_history_append" in kwargs:
                entry = kwargs["node_history_append"]
                if isinstance(entry, dict):
                    # Read current node_history
                    cursor = await db.execute(
                        "SELECT node_history FROM pipeline_runs WHERE run_id = ?",
                        (run_id,),
                    )
                    row = await cursor.fetchone()
                    # A008: data initialized explicitly before use
                    history: list[dict[str, Any]] = []
                    if row and row["node_history"]:
                        try:
                            parsed = json.loads(row["node_history"])
                            if isinstance(parsed, list):
                                history = parsed
                        except json.JSONDecodeError:
                            logger.debug(
                                "update_pipeline_run: node_history JSON parse failed for %s, "
                                "starting fresh",
                                run_id,
                            )

                    history.append(entry)
                    await db.execute(
                        "UPDATE pipeline_runs SET node_history = ? WHERE run_id = ?",
                        (json.dumps(history, ensure_ascii=False), run_id),
                    )

            # Update completed_at if present
            if "status" in kwargs:
                await db.execute(
                    "UPDATE pipeline_runs SET status = ? WHERE run_id = ?",
                    (kwargs["status"], run_id),
                )

            if "completed_at" in kwargs:
                await db.execute(
                    "UPDATE pipeline_runs SET completed_at = ? WHERE run_id = ?",
                    (kwargs["completed_at"], run_id),
                )

            await db.commit()

            logger.debug(
                "update_pipeline_run: run_id=%s updated keys=%s",
                run_id[:8],
                list(kwargs.keys()),
            )
            return True

    except Exception as exc:
        logger.warning(
            "update_pipeline_run: failed for run_id=%s: %s",
            run_id[:8] if run_id else "(missing)",
            exc,
        )
        return False


# ============================================================
# with_progress — node wrapper
# ============================================================


def with_progress(
    node_fn: Callable[..., Any],
    node_name: str,
    weight: float,
) -> Callable[..., Any]:
    """Wrap a LangGraph node function with pipeline_runs progress tracking.

    ``with_progress`` is the **outer** wrapper — the inner node function is
    traced by LangGraph auto-tracing (LANGCHAIN_TRACING_V2=true). ``with_progress``
    handles DB progress updates only. Order: ``with_progress(func)``.

    On each node execution:
    1. **Before**: Updates ``pipeline_runs.current_node`` to this node's name.
    2. **Execute**: Awaits the inner (already-traced) node function.
    3. **After**: Computes new progress percentage, appends to
       ``node_history`` JSON array, updates ``_progress`` in state.

    The ``_progress`` state field accumulates as a float (0.0–1.0) and is
    converted to ``progress_pct`` (0–100) for the database.

    Args:
        node_fn: The async node function.
        node_name: Human-readable node name (matches pipeline_runs.current_node).
        weight: Progress weight (0.0–1.0). Added to cumulative _progress.

    Returns:
        An async wrapper function with the same signature as node_fn.

    Raises:
        Propagates all exceptions from the inner node function (after recording
        the error context in logs). Progress updates are best-effort and
        never block execution.
    """

    async def wrapper(state: dict[str, Any]) -> Any:
        """Progress-tracking wrapper executed on every graph node invocation."""
        run_id: str = state.get("run_id", "")
        if not run_id:
            logger.debug(
                "with_progress: no run_id in state, skipping progress update for %s", node_name
            )
            return await node_fn(state)

        # --- Before execution: set current_node ---
        await update_pipeline_run(run_id, current_node=node_name)

        # --- Execute inner (traced) node ---
        start = time.monotonic()
        try:
            result = await node_fn(state)
        except Exception:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error(
                "with_progress: node [%s] failed after %dms",
                node_name,
                elapsed_ms,
                exc_info=True,
            )
            raise

        elapsed_ms = int((time.monotonic() - start) * 1000)

        # --- After execution: compute progress ---
        # _progress accumulates as float 0.0–1.0
        current_progress: float = float(state.get("_progress", 0.0))
        new_progress: float = min(1.0, current_progress + weight)
        progress_pct: int = int(new_progress * 100)

        now_ts = datetime.now(_UTC).isoformat()

        # Update pipeline_runs
        await update_pipeline_run(
            run_id,
            progress_pct=progress_pct,
            node_history_append={
                "node": node_name,
                "ts": now_ts,
                "ms": elapsed_ms,
            },
        )

        # Accumulate _progress in state for next node.
        # Handle three return types:
        # 1. dict — standard node return, add _progress directly
        # 2. Command — LangGraph routing command, add _progress to update dict
        # 3. Other — wrap in dict with _progress
        try:
            from langgraph.types import Command as _Command

            _has_command = True
        except ImportError:
            _has_command = False

        if _has_command and isinstance(result, _Command):
            # L010: Command.goto is used for routing — must NOT wrap result
            if result.update is not None:
                result.update["_progress"] = new_progress
        elif isinstance(result, dict):
            result["_progress"] = new_progress
        else:
            result = {"_result": result, "_progress": new_progress}

        logger.debug(
            "with_progress: node [%s] done in %dms, progress %.0f%% (%d/%d)",
            node_name,
            elapsed_ms,
            new_progress * 100,
            progress_pct,
            100,
        )

        return result

    # Preserve function metadata for introspection
    wrapper.__name__ = f"progress_{node_name}"  # type: ignore[attr-defined]
    wrapper.__qualname__ = f"with_progress.{node_name}"  # type: ignore[attr-defined]
    wrapper.__doc__ = (
        f"Progress-tracked wrapper for node '{node_name}' (weight={weight}).\n\n"
        f"Original: {node_fn.__name__}"
    )

    return wrapper


# ============================================================
# insert_pipeline_run — initialize a run row before graph execution
# ============================================================


async def insert_pipeline_run(
    run_id: str,
    *,
    component: str = "graph",
    group_id: str = "",
    date: str = "",
    message_count: int = 0,
) -> bool:
    """Insert a new row into ``pipeline_runs`` before graph invocation.

    Called by the graph entry point (orchestrator / CLI) to create the
    initial pipeline_runs row. Subsequent node progress updates are handled
    by ``with_progress`` → ``update_pipeline_run``.

    Args:
        run_id: Unique pipeline run UUID.
        component: Component name (default "graph").
        group_id: Target chat group name.
        date: Target date (YYYYMMDD).
        message_count: Initial message count.

    Returns:
        True on success, False on error.
    """
    import aiosqlite

    # A013: read db_path from Settings inside the function body so env /
    # WINNOW_DB_PATH overrides take effect at call time (not import time).
    db_path = get_settings().db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    now_ts = datetime.now(_UTC).isoformat()

    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO pipeline_runs
                   (run_id, component, status, started_at, message_count,
                    group_id, date, current_node, progress_pct, node_history)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    component,
                    "running",
                    now_ts,
                    message_count,
                    group_id,
                    date,
                    None,  # current_node — will be set by first with_progress wrapper
                    0,  # progress_pct — starts at 0
                    "[]",  # node_history — empty JSON array
                ),
            )
            await db.commit()

        logger.info(
            "insert_pipeline_run: run_id=%s component=%s group=%s date=%s",
            run_id[:8],
            component,
            group_id,
            date,
        )
        return True

    except Exception as exc:
        logger.warning("insert_pipeline_run: failed for run_id=%s: %s", run_id[:8], exc)
        return False
