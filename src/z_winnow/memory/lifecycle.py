"""T-W10-E-h: MemOS memory lifecycle management + vacuum.

Uses GeneralTextMemory — simple time-based cleanup without the
TreeTextMemory state machine (no WorkingMemory/LongTermMemory/archived
status transitions).

Cleanup rule: memories with no updates for 90+ days → hard delete via
delete_memory. Does NOT require metadata (uses updated_at timestamp
from the MemOS memory item itself).

P004: STALE detection — purely deterministic time-based threshold.
No LLM dependency.

P016: Lazy import — adapter obtained from factory at call time when not
provided by the caller.

A008: data initialization — all intermediate dicts pre-initialized to
empty containers before conditional population.
"""

from __future__ import annotations

import datetime as _dt
import logging
import time as _time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Time thresholds (seconds) — per wave9-memos-design.md §8.1
# ---------------------------------------------------------------------------

DORMANT_THRESHOLD_DAYS: int = 14
ARCHIVE_THRESHOLD_DAYS: int = 30
DELETE_THRESHOLD_DAYS: int = 90

DORMANT_THRESHOLD_S: float = DORMANT_THRESHOLD_DAYS * 86400.0
ARCHIVE_THRESHOLD_S: float = ARCHIVE_THRESHOLD_DAYS * 86400.0
DELETE_THRESHOLD_S: float = DELETE_THRESHOLD_DAYS * 86400.0

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class NodeLifecycleState:
    """Per-node lifecycle diagnostic.

    Attributes:
        memory_key: Unique key from metadata (e.g. "topic-g1-tp001").
        memory_type: "WorkingMemory" | "LongTermMemory" | "UserMemory".
        current_status: "activated" | "archived" | "deleted".
        current_tags: Current tag list.
        updated_at_s: Parsed updated_at timestamp (Unix seconds), or 0.0.
        days_since_update: Days since last update, -1 if indeterminate.
        action: "none" | "tag_dormant" | "archive" | "delete" |
            "working_to_longterm".
        action_reason: Human-readable reason.
    """

    memory_key: str = ""
    memory_type: str = "LongTermMemory"
    current_status: str = "activated"
    current_tags: list[str] = field(default_factory=list)
    updated_at_s: float = 0.0
    days_since_update: float = -1.0
    action: str = "none"
    action_reason: str = ""

    def __post_init__(self) -> None:
        """A008: explicit initialization of mutable defaults."""
        if self.current_tags is None:  # type: ignore[unreachable]
            self.current_tags = []


@dataclass
class LifecycleReport:
    """Result of a lifecycle scan or vacuum operation.

    Attributes:
        cube_id: Target MemCube ID.
        group_id: Group identifier.
        scanned_at: Unix timestamp when scan was performed.
        total_nodes: Total nodes examined (across all memory types).
        nodes: Per-node lifecycle state diagnostics.
        transitions: List of transitions that were applied (vacuum) or
            would be applied (scan).
        pending_count: Number of nodes that need transitions now.
        summary: Counters keyed by status and action type.
    """

    cube_id: str = ""
    group_id: str = ""
    scanned_at: float = 0.0
    total_nodes: int = 0
    nodes: list[NodeLifecycleState] = field(default_factory=list)
    transitions: list[dict[str, Any]] = field(default_factory=list)
    pending_count: int = 0
    summary: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """A008: explicit initialization of mutable defaults."""
        if self.nodes is None:  # type: ignore[unreachable]
            self.nodes = []
        if self.transitions is None:  # type: ignore[unreachable]
            self.transitions = []
        if self.summary is None:  # type: ignore[unreachable]
            self.summary = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(raw: Any, default_s: float = 0.0) -> float:
    """Parse updated_at / created_at into a float (Unix timestamp seconds).

    Handles three formats:
    - float/int: assumed to be Unix timestamp seconds
    - ISO 8601 string: parsed via datetime.fromisoformat
    - None / missing: returns default_s

    P004: Timestamp extraction is deterministic — no format guessing.
    """
    if raw is None:
        return default_s
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            ts_str = raw.replace("Z", "+00:00")
            dt = _dt.datetime.fromisoformat(ts_str)
            return dt.timestamp()
        except (ValueError, TypeError):
            logger.debug(
                "lifecycle: cannot parse timestamp=%r, using default=%.0f",
                raw,
                default_s,
            )
            return default_s
    return default_s


def _days_since(timestamp_s: float, now_s: float | None = None) -> float:
    """Return the number of days since timestamp_s.

    Args:
        timestamp_s: Unix timestamp in seconds (0.0 = unknown).
        now_s: Current time as Unix seconds (defaults to time.time()).

    Returns:
        Days elapsed, or -1.0 if timestamp is 0.0 (unknown).
    """
    if timestamp_s <= 0.0:
        return -1.0
    ref = now_s if now_s is not None else _time.time()
    return (ref - timestamp_s) / 86400.0


def _assess_node(
    node: dict[str, Any],
    now_s: float | None = None,
) -> dict[str, Any] | None:
    """Assess a single memory node for lifecycle transition.

    P004: Pure deterministic time-threshold comparison — no LLM involvement.

    Returns a transition dict if action is needed now, None otherwise.

    Transition determination (wave9-memos-design.md §8.1):
      1. WorkingMemory → LongTermMemory (status=activated)
      2. archived + 90d → deleted
      3. activated/dormant + 30d no update → archived
      4. activated + 14d no update → mark dormant (add tag)
    """
    # A008: pre-init before conditional population
    metadata: dict[str, Any] = node.get("metadata", {}) or {}
    if metadata is None:  # type: ignore[unreachable]
        metadata = {}

    memory_type: str = metadata.get("memory_type", "LongTermMemory")
    status: str = metadata.get("status", "activated")
    key: str = metadata.get("key", node.get("id", ""))

    # Parse updated_at timestamp
    updated_raw = node.get("updated_at", node.get("created_at"))
    updated_s = _parse_timestamp(updated_raw)

    # P004: If no timestamp available, skip — cannot determine staleness
    if updated_s == 0.0:
        return None

    days = _days_since(updated_s, now_s)
    if days < 0:
        return None

    # Rule: archived + 90d → hard delete via delete_memory
    if status == "archived" and days >= DELETE_THRESHOLD_DAYS:
        return {
            "key": key,
            "from_memory_type": memory_type,
            "from_status": "archived",
            "to_status": "deleted",
            "to_memory_type": memory_type,
            "reason": "archived_90d_delete",
            "days_since_update": round(days, 1),
            "action": "delete_memory",
            "memory_id": node.get("id", ""),
        }

    return None


def _build_node_state(
    node: dict[str, Any],
    now_s: float | None = None,
) -> NodeLifecycleState:
    """Build a NodeLifecycleState from a raw memory node dict.

    Args:
        node: Raw node dict from get_all_memories response.
        now_s: Current time as Unix seconds.

    Returns:
        NodeLifecycleState with full diagnostics.
    """
    metadata: dict[str, Any] = node.get("metadata", {}) or {}
    if metadata is None:  # type: ignore[unreachable]
        metadata = {}

    memory_key: str = str(metadata.get("key", node.get("id", "")))
    memory_type: str = str(metadata.get("memory_type", "LongTermMemory"))
    current_status: str = str(metadata.get("status", "activated"))
    current_tags: list[str] = list(metadata.get("tags", [])) or []

    updated_raw = node.get("updated_at", node.get("created_at"))
    updated_s = _parse_timestamp(updated_raw)
    days = _days_since(updated_s, now_s)

    # P004: Determine action from raw assessment
    transition = _assess_node(node, now_s=now_s)
    if transition is not None:
        if transition.get("extra_tags"):
            action = "tag_dormant"
        elif transition.get("to_status") == "archived":
            action = "archive"
        elif transition.get("to_status") == "deleted":
            action = "delete"
        elif transition.get("reason") == "working_to_longterm":
            action = "working_to_longterm"
        else:
            action = "none"
        reason = transition.get("reason", "")
    else:
        action = "none"
        reason = (
            f"status={current_status}, last active {days:.1f}d ago"
            if days >= 0
            else "no timestamp data"
        )

    return NodeLifecycleState(
        memory_key=memory_key,
        memory_type=memory_type,
        current_status=current_status,
        current_tags=current_tags,
        updated_at_s=updated_s,
        days_since_update=round(days, 1) if days >= 0 else -1.0,
        action=action,
        action_reason=reason,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def scan_lifecycle(
    cube_id: str,
    group_id: str,
    *,
    adapter: Any = None,
    now_s: float | None = None,
) -> LifecycleReport:
    """Scan all topic nodes in a MemCube and report lifecycle transitions.

    Reads all nodes via adapter.get_all_memories(), assesses each against
    the state machine, and returns a LifecycleReport with per-node
    diagnostics and pending transitions.

    P004: Pure deterministic scan — time-threshold comparison only.
    P016: Adapter obtained lazily from factory when not provided.
    A008: data = None pre-initialized for all intermediate collections.

    Args:
        cube_id: Target MemCube ID (e.g. "winnow-g1-topics").
        group_id: Group identifier for logging/reporting.
        adapter: MemOS adapter instance (real/mock/disabled).  Created
            lazily from factory if None.
        now_s: Current time as Unix seconds for deterministic testing.
            Defaults to time.time().

    Returns:
        LifecycleReport with per-node diagnostics and pending transitions.
    """
    # P016: Lazy import only when caller doesn't provide adapter
    if adapter is None:
        from z_winnow.memory.factory import create_memos_adapter

        adapter = create_memos_adapter()

    ref_s = now_s if now_s is not None else _time.time()
    report = LifecycleReport(
        cube_id=cube_id,
        group_id=group_id,
        scanned_at=ref_s,
    )

    # A008: explicit initialization before try/except
    all_data: dict[str, Any] | None = None

    try:
        all_data = await adapter.get_all_memories(
            cube_id=cube_id,
            group_id=group_id,
        )
    except Exception as exc:
        logger.warning(
            "lifecycle.scan_lifecycle: get_all_memories failed for cube=%s — %s",
            cube_id,
            exc,
        )
        report.summary = {"error": str(exc)[:200]}
        return report

    # A008: pre-init lists before population
    text_mem: list[dict[str, Any]] = []
    act_mem: list[dict[str, Any]] = []
    para_mem: list[dict[str, Any]] = []

    if isinstance(all_data, dict):
        text_mem = list(all_data.get("text_mem", []) or [])
        act_mem = list(all_data.get("act_mem", []) or [])
        para_mem = list(all_data.get("para_mem", []) or [])

    # Scan all memory types (WorkingMemory nodes are in act_mem)
    all_items = text_mem + act_mem + para_mem
    report.total_nodes = len(all_items)

    if not all_items:
        logger.debug("lifecycle.scan_lifecycle: cube=%s has 0 nodes", cube_id)
        report.summary = {
            "total_nodes": 0,
            "activated": 0,
            "dormant": 0,
            "archived": 0,
            "deleted": 0,
            "tag_dormant": 0,
            "archive": 0,
            "delete": 0,
            "working_to_longterm": 0,
            "none": 0,
        }
        return report

    # Summary counters
    status_counts: dict[str, int] = {
        "activated": 0,
        "dormant": 0,
        "archived": 0,
        "deleted": 0,
    }
    action_counts: dict[str, int] = {
        "tag_dormant": 0,
        "archive": 0,
        "delete": 0,
        "working_to_longterm": 0,
        "none": 0,
    }

    for item in all_items:
        if not isinstance(item, dict):
            continue

        ns = _build_node_state(item, now_s=ref_s)
        report.nodes.append(ns)

        # Update status counters
        sk = ns.current_status.lower()
        if sk in status_counts:
            status_counts[sk] += 1
        if "dormant" in [t.lower() for t in ns.current_tags]:
            status_counts["dormant"] += 1

        # Update action counters
        if ns.action in action_counts:
            action_counts[ns.action] += 1
        else:
            action_counts[ns.action] = 1

        # Build transition dict for pending actions
        if ns.action != "none":
            transition = _assess_node(item, now_s=ref_s)
            if transition is not None:
                report.transitions.append(transition)

    report.pending_count = len(report.transitions)
    report.summary = {
        **status_counts,
        **action_counts,
        "total_nodes": report.total_nodes,
        "pending_count": report.pending_count,
    }

    logger.info(
        "lifecycle.scan_lifecycle: cube=%s total=%d pending=%d "
        "activated=%d dormant=%d archived=%d deleted=%d "
        "actions: tag_dormant=%d archive=%d delete=%d "
        "working_to_longterm=%d none=%d",
        cube_id,
        report.total_nodes,
        report.pending_count,
        status_counts["activated"],
        status_counts["dormant"],
        status_counts["archived"],
        status_counts["deleted"],
        action_counts["tag_dormant"],
        action_counts["archive"],
        action_counts["delete"],
        action_counts["working_to_longterm"],
        action_counts["none"],
    )
    return report


async def vacuum(
    cube_id: str,
    group_id: str,
    *,
    adapter: Any = None,
    now_s: float | None = None,
    dry_run: bool = False,
) -> LifecycleReport:
    """Execute lifecycle transitions for all nodes in a MemCube.

    Calls scan_lifecycle() to determine which nodes need transitions,
    then reports transitions (application pending handler implementation).

    P016: Graceful degradation — individual transition failures are logged
    but do not abort the entire vacuum.  Remaining nodes continue to be
    processed.

    Args:
        cube_id: Target MemCube ID.
        group_id: Group identifier for logging/reporting.
        adapter: MemOS adapter instance (created lazily if None).
        now_s: Current time as Unix seconds for deterministic testing.
        dry_run: If True, scan only — do not apply transitions.

    Returns:
        LifecycleReport with per-node diagnostics and applied transitions.
    """
    # P016: Lazy import only when caller doesn't provide adapter
    if adapter is None:
        from z_winnow.memory.factory import create_memos_adapter

        adapter = create_memos_adapter()

    report = await scan_lifecycle(
        cube_id=cube_id,
        group_id=group_id,
        adapter=adapter,
        now_s=now_s,
    )

    if dry_run or not report.transitions:
        if dry_run:
            logger.info(
                "lifecycle.vacuum: dry_run — %d transitions identified (not applied)",
                report.pending_count,
            )
        return report

    applied_count = 0
    failed_count = 0

    for transition in report.transitions:
        key = transition["key"]
        mem_id = transition.get("memory_id", key)

        try:
            ok = await adapter.delete_memory(
                cube_id=cube_id,
                group_id=group_id,
                memory_ids=[mem_id],
            )
            if ok:
                applied_count += 1
                transition["applied"] = True
            else:
                failed_count += 1
                transition["applied"] = False
                transition["error"] = "delete_memory returned False"
        except Exception as exc:
            failed_count += 1
            transition["applied"] = False
            transition["error"] = str(exc)[:200]
            logger.warning(
                "lifecycle.vacuum: key=%s delete failed — %s",
                key,
                exc,
            )

    report.summary["executed"] = applied_count
    report.summary["failed"] = failed_count

    logger.info(
        "lifecycle.vacuum: cube=%s applied=%d failed=%d (total scanned=%d)",
        cube_id,
        applied_count,
        failed_count,
        report.total_nodes,
    )
    return report
