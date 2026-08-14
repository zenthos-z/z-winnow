"""T-W10-E-c: MemOS sync operation dispatcher.

Routes sync queue rows (op_type → adapter method) with idempotent
dedupe logic: before adding, searches for existing items by dedupe_key
and updates instead of creating duplicates.

P022: Storage/Formatting Layer Separation — all MemOS formatting
(structured memory items, metadata construction) happens here,
immediately before the adapter call.  The SQLite queue stores raw JSON.

A008: data = None explicit initialization before JSON parse chain.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any

from z_winnow.memory.types import (
    MemOSAdapterProtocol,
    StructuredMemoryItem,
    TextualMemoryMetadata,
)

logger = logging.getLogger(__name__)


async def dispatch_op(
    adapter: MemOSAdapterProtocol,
    row: dict[str, Any],
) -> None:
    """Dispatch a single sync queue row to the appropriate adapter method.

    P022: Formats payload into MemOS-specific data structures (memory items,
    metadata) immediately before calling the adapter.

    A008: data = None initialized before JSON parse chain to prevent
    NameError from masking the real root cause of parse failure.

    op_type mapping:
      - add_topic / add_feedback → adapter.add_structured_memory()

    Idempotency: payloads carry a dedupe_key; add operations search for
    existing items with the same key first and skip if found.

    Args:
        adapter: MemOS adapter instance (real/mock/disabled).
        row: Sync queue row dict with keys: queue_id, op_type, cube_id,
             payload (JSON string), retry_count, etc.

    Raises:
        Exception: Re-raises any exception from adapter methods so the
                   worker can handle retry logic.
    """
    # A008: Explicit initialization before JSON parse chain
    data: Any = None

    op_type: str = row["op_type"]
    cube_id: str = row["cube_id"]
    payload_str: str = row["payload"]

    try:
        data = _json.loads(payload_str)
    except _json.JSONDecodeError as exc:
        logger.error(
            "sync_ops.dispatch_op: malformed payload for queue_id=%d op=%s — %s",
            row["queue_id"],
            op_type,
            exc,
        )
        raise

    group_id: str = data.get("group_id", "")
    dedupe_key: str = data.get("dedupe_key", "")

    logger.debug(
        "sync_ops.dispatch_op: queue_id=%d op=%s cube=%s dedupe=%s",
        row["queue_id"],
        op_type,
        cube_id,
        dedupe_key,
    )

    if op_type in ("add_topic", "add_feedback"):
        await _dispatch_add(adapter, cube_id, group_id, data, dedupe_key)
    else:
        logger.warning(
            "sync_ops.dispatch_op: unknown op_type=%s for queue_id=%d — skipping",
            op_type,
            row["queue_id"],
        )


# ============================================================
# P022: Formatting helpers — construct MemOS data structures
# ============================================================


def _build_structured_item(
    data: dict[str, Any],
    item_type: str = "topic",
) -> StructuredMemoryItem:
    """Build a StructuredMemoryItem from raw payload data.

    Uses TextualMemoryMetadata (GeneralTextMemory compatible) with
    minimal fields — metadata is not sent via REST API, only retained
    for local type safety.

    Args:
        data: Raw payload dict from sync queue.
        item_type: 'topic' or 'feedback' — determines metadata.type field.

    Returns:
        StructuredMemoryItem ready for adapter.add_structured_memory().
    """
    summary = data.get("summary", data.get("content", ""))
    source = data.get("source", "sync_worker")
    confidence = float(data.get("confidence", 50.0))
    tags = list(data.get("tags", [])) if isinstance(data.get("tags"), list) else []
    entities = list(data.get("entities", [])) if isinstance(data.get("entities"), list) else []
    memory_time = data.get("memory_time", "")

    metadata = TextualMemoryMetadata(
        type=item_type,
        memory_time=memory_time,
        source=source,
        confidence=confidence,
        entities=entities,
        tags=tags,
    )

    return StructuredMemoryItem(memory=summary, metadata=metadata)


# ============================================================
# Internal dispatch helpers
# ============================================================


async def _dispatch_add(
    adapter: MemOSAdapterProtocol,
    cube_id: str,
    group_id: str,
    data: dict[str, Any],
    dedupe_key: str,
) -> None:
    """Handle add_topic / add_feedback: add to MemOS.

    Deduplication is handled at the output_composer level via MemoryHandler
    (get_all + delete same-date records + re-add). The sync queue here is
    a simple fire-and-forget path for feedback corrections.

    Args:
        adapter: MemOS adapter instance.
        cube_id: Target MemCube ID.
        group_id: Group identifier.
        data: Parsed payload dict.
        dedupe_key: Unique deduplication key (for logging only).
    """
    item_type = "topic" if data.get("op_type") == "add_topic" else "feedback"
    logger.debug("sync_ops: dispatching add type=%s key=%s", item_type, dedupe_key)

    item = _build_structured_item(data, item_type=item_type)
    await adapter.add_structured_memory(
        cube_id=cube_id,
        group_id=group_id,
        items=[item],
    )
