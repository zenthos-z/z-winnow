"""T-W10-E-f: Feedback-to-MemOS sync — convert + enqueue.

Converts feedback_events rows to MemOS StructuredMemoryItem nodes and
enqueues them into memos_sync_queue for async processing by the sync worker.

P016: Lazy import + single-point insertion — enqueue_feedback_sync is called
from the POST /feedback endpoint with minimal code intrusion.
P022: Storage/Formatting Layer Separation — feedback_to_memos_item does the
MemOS-specific formatting; the sync worker dispatches via sync_ops.

Mapping per wave9-memos-design.md §5.2:
  - metadata.type = "correction"
  - metadata.source = "human_feedback"
  - metadata.confidence = 100.0
  - metadata.memory_type = "UserMemory"
  - metadata.key = f"feedback-{feedback_id}"
  - metadata.tags = [f"group:{group_id}", f"target:{target_type}", "user-correction"]
  - metadata.background = original_text
  - memory content = corrected_text or correction_note or "(only tags)"
  - op_type = "add_feedback"
  - dedupe_key = f"feedback-{feedback_id}"
  - target cube: winnow:{group_id}:feedback（冒号格式，与 topics cube 一致）
"""

from __future__ import annotations

import json as _json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

import aiosqlite

from z_winnow.memory.types import (
    StructuredMemoryItem,
    TreeNodeTextualMemoryMetadata,
)

logger = logging.getLogger(__name__)


# ============================================================
# FeedbackEvent — minimal dataclass for feedback_events row
# ============================================================


@dataclass
class FeedbackEvent:
    """Minimal representation of a feedback_events row for MemOS sync.

    Contains only the fields needed by feedback_to_memos_item() for
    constructing a MemOS StructuredMemoryItem node.
    """

    feedback_id: str
    group_id: str
    target_type: str = ""
    tags: list[str] | None = None  # A008: pre-init to None
    original_text: str | None = None
    corrected_text: str | None = None
    correction_note: str | None = None

    def __post_init__(self) -> None:
        """Ensure mutable fields have sensible defaults."""
        # A008: explicit initialization
        if self.tags is None:
            self.tags = []


# ============================================================
# feedback_to_memos_item — per wave9-memos-design.md §5.2
# ============================================================


def feedback_to_memos_item(fb: FeedbackEvent) -> StructuredMemoryItem:
    """Convert a FeedbackEvent to a MemOS StructuredMemoryItem.

    Mapping strictly follows wave9-memos-design.md §5.2:

    +----------------------------+---------------------------------------+
    | winnow FeedbackEvent     | MemOS UserMemory                      |
    +----------------------------+---------------------------------------+
    | target_type                | tags += [f"target:{target_type}"]      |
    | tags=["fact_error", ...]   | tags += [f"issue:{tag}"] (reserved)   |
    | original_text              | metadata.background =                  |
    | corrected_text             | memory = (node content)                |
    | correction_note            | metadata key=f"feedback-{feedback_id}" |
    +----------------------------+---------------------------------------+

    Args:
        fb: FeedbackEvent dataclass with minimal required fields.

    Returns:
        StructuredMemoryItem ready for MemOS adapter insertion.
    """
    # A008: explicit initialization before building metadata
    tags: list[str] = [
        f"group:{fb.group_id}",
        f"target:{fb.target_type}",
        "user-correction",
    ]

    # Build metadata per design doc §5.2
    # P022: All MemOS formatting happens here, immediately before adapter call
    metadata = TreeNodeTextualMemoryMetadata(
        key=f"feedback-{fb.feedback_id}",
        memory_type="UserMemory",
        status="activated",
        visibility="private",
        type="correction",
        source="human_feedback",
        confidence=100.0,
        tags=tags,
        entities=[fb.group_id, fb.target_type],
        background=fb.original_text or "",
        usage=[],
    )

    # P016: Content is the corrected version; background holds the original
    content: str = fb.corrected_text or fb.correction_note or "(only tags)"

    return StructuredMemoryItem(memory=content, metadata=metadata)


# ============================================================
# enqueue_feedback_sync — read + convert + enqueue
# ============================================================


async def enqueue_feedback_sync(
    conn: aiosqlite.Connection,
    feedback_id: str,
) -> int:
    """Read feedback event from DB, convert, and enqueue into sync queue.

    P016: Single-point insertion — called from POST /feedback handler.
    P022: Formats payload before enqueuing; sync_worker dispatches.

    Steps:
      1. Read feedback_events row by feedback_id
      2. Convert to FeedbackEvent dataclass
      3. Build StructuredMemoryItem via feedback_to_memos_item()
      4. Enqueue into memos_sync_queue with op_type="add_feedback"

    Args:
        conn: aiosqlite database connection (from request.app.state.db_conn).
        feedback_id: The feedback_id to sync (e.g. "fb-2026-05-15-001").

    Returns:
        The auto-incremented queue_id of the new sync queue row.

    Raises:
        ValueError: If feedback_id not found in feedback_events.
    """
    try:
        return await _enqueue_feedback_sync_impl(conn, feedback_id)
    except (sqlite3.ProgrammingError, aiosqlite.Error) as exc:
        # P016: Fire-and-forget safety — when called via asyncio.create_task,
        # the DB connection may be closed before the task executes (e.g. in
        # tests).  Log the error but don't crash the event loop.
        logger.warning(
            "feedback_sync: enqueue failed for feedback_id=%s — DB error: %s",
            feedback_id,
            exc,
        )
        return 0


async def _enqueue_feedback_sync_impl(
    conn: aiosqlite.Connection,
    feedback_id: str,
) -> int:
    """Core implementation of enqueue_feedback_sync — may raise on DB errors."""
    # ---- Step 1: Fetch feedback_events row ----
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM feedback_events WHERE feedback_id = ?",
        (feedback_id,),
    )
    row = await cursor.fetchone()

    if row is None:
        raise ValueError(f"Feedback event not found: {feedback_id}")

    # Convert aiosqlite.Row to dict for .get() access (existing codebase pattern)
    row_dict: dict[str, Any] = dict(row)

    # ---- Step 2: Convert to FeedbackEvent ----
    # A008: Parse tags JSON string → list
    raw_tags: str | None = row_dict.get("tags")  # A008
    tags_list: list[str] = []
    if raw_tags:
        try:
            parsed = _json.loads(raw_tags)
            if isinstance(parsed, list):
                tags_list = [str(t) for t in parsed]
        except (_json.JSONDecodeError, TypeError):
            logger.debug(
                "enqueue_feedback_sync: failed to parse tags=%r for %s",
                raw_tags,
                feedback_id,
            )

    fb = FeedbackEvent(
        feedback_id=row_dict["feedback_id"],
        group_id=row_dict["group_id"],
        target_type=row_dict.get("target_type", ""),  # A008
        tags=tags_list,
        original_text=row_dict.get("original_text"),  # A008
        corrected_text=row_dict.get("corrected_text"),  # A008
        correction_note=row_dict.get("correction_note"),  # A008
    )

    # ---- Step 3: Convert to MemOS StructuredMemoryItem ----
    item: StructuredMemoryItem = feedback_to_memos_item(fb)

    # ---- Step 4: Build sync queue payload ----
    cube_id: str = f"winnow:{fb.group_id}:feedback"

    # A008: Build payload dict with explicit init
    payload: dict[str, Any] = {
        "user_id": fb.group_id,
        "dedupe_key": f"feedback-{feedback_id}",
        "op_type": "add_feedback",
        "type": "correction",
        "source": "human_feedback",
        "confidence": 100.0,
        "memory_type": "UserMemory",
        "key": f"feedback-{feedback_id}",
        "tags": [
            f"group:{fb.group_id}",
            f"target:{fb.target_type}",
            "user-correction",
        ],
        "background": fb.original_text or "",
        "content": item.memory,
        "summary": item.memory,
        "entities": [fb.group_id, fb.target_type, *(fb.tags or [])],
    }

    # P016: Lazy import — only import enqueue_sync_job at call time
    from z_winnow.pipeline.database import enqueue_sync_job

    queue_id: int = await enqueue_sync_job(
        db=conn,
        op_type="add_feedback",
        cube_id=cube_id,
        payload=payload,
        dedupe_key=f"feedback-{feedback_id}",
    )

    logger.info(
        "feedback_sync: enqueued feedback_id=%s → queue_id=%d cube=%s",
        feedback_id,
        queue_id,
        cube_id,
    )

    return queue_id
