"""Disabled MemOS adapter — no-op implementation (graceful degradation).

Provides DisabledAdapter: a complete no-op MemOS adapter that silently
returns empty results for all queries and ``{"status": "disabled"}`` for
all writes.  Activated when ``MEMOS_ENABLED=false``.

Implements all 9 methods of MemOSAdapterProtocol: add_memory, search_memories,
get_or_create_cube, add_structured_memory, get_all_memories, delete_memory,
scheduler_status, scheduler_wait, health_check — plus 2 convenience methods
(add/search) for backward compatibility.

P016: Graceful degradation — all methods return safe defaults, never
raise exceptions.
"""

from __future__ import annotations

import uuid
from typing import Any

from z_winnow.memory.types import MemoryResult, StructuredMemoryItem


class DisabledAdapter:
    """No-op MemOS adapter — silent empty returns, never throws.

    Implements all 6 methods of MemOSAdapterProtocol plus 2 convenience
    methods (add/search) for topic_tracker backward compatibility.

    P016: Graceful degradation pattern — all query methods return empty
    lists/dicts, all write methods return ``{"status": "disabled"}``.
    This is isomorphic to P016's "parse failure → preserve original data":
    the caller continues without error regardless of adapter state.

    Usage:
        adapter = DisabledAdapter()
        results = await adapter.search_memories(...)  # → []
        status = await adapter.health_check()          # → {"status": "disabled"}
    """

    # ------------------------------------------------------------------
    # T-W8-2: Original 3 protocol methods
    # ------------------------------------------------------------------

    async def add_memory(
        self,
        group_id: str,
        mem_cube_id: str,
        messages: list[dict[str, str]],
        async_mode: str = "sync",
    ) -> dict[str, Any]:
        """Silently return disabled status dict — MemOS is disabled."""
        return {"status": "disabled"}

    async def search_memories(
        self,
        query: str,
        group_id: str,
        readable_cube_ids: list[str],
        top_k: int = 20,
        legacy_group_ids: list[str] | None = None,
        mode: str = "fine",
    ) -> list[MemoryResult]:
        """Silently return empty list — MemOS is disabled."""
        return []

    async def get_or_create_cube(self, scope: str) -> str:
        """Return a deterministic local UUID — MemOS is disabled.

        Uses uuid4 so callers get a valid UUID string even in disabled
        mode, allowing downstream code to proceed without special-casing.
        """
        return str(uuid.uuid4())

    # ------------------------------------------------------------------
    # T-W10-E-ba: 3 new protocol methods (§10.1 wave9-memos-design.md)
    # ------------------------------------------------------------------

    async def add_structured_memory(
        self,
        cube_id: str,
        group_id: str,
        items: list[StructuredMemoryItem],
        async_mode: str = "sync",
    ) -> dict[str, Any]:
        """Silently return disabled status dict — MemOS is disabled.

        P016: Single-point insertion returns safe default so caller
        does not need to branch on adapter state.
        """
        return {"status": "disabled"}

    async def delete_memory(
        self,
        cube_id: str,
        group_id: str,
        memory_ids: list[str] | None = None,
        file_ids: list[str] | None = None,
        filter: dict[str, Any] | None = None,
    ) -> bool:
        """Silently return False — MemOS is disabled."""
        return False

    async def get_all_memories(
        self,
        cube_id: str,
        group_id: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Silently return empty result dict — MemOS is disabled."""
        return {"text_mem": [], "act_mem": [], "para_mem": []}

    async def feedback_memory(
        self,
        group_id: str,
        cube_ids: list[str],
        feedback_content: str,
        retrieved_memory_ids: list[str],
        history: list[dict[str, str]] | None = None,
        async_mode: str = "sync",
        corrected_answer: bool = False,
        session_id: str = "default_session",
    ) -> dict[str, Any]:
        """Silently return disabled status — MemOS is disabled."""
        return {"status": "disabled"}

    async def get_memory(
        self, memory_id: str, group_id: str | None = None
    ) -> MemoryResult | None:
        """Silently return None — MemOS is disabled."""
        return None

    async def scheduler_status(self, user_name: str) -> dict[str, Any]:
        """Return disabled status — MemOS is not available."""
        return {"status": "disabled"}

    async def scheduler_wait(
        self,
        user_name: str,
        timeout_seconds: float = 120.0,
        poll_interval: float = 0.2,
    ) -> dict[str, Any]:
        """Return disabled status — MemOS is not available."""
        return {"status": "disabled"}

    async def health_check(self) -> dict[str, Any]:
        """Return disabled status — MemOS is not available."""
        return {"status": "disabled"}

    # ------------------------------------------------------------------
    # Convenience methods (topic_tracker backward compatibility)
    # ------------------------------------------------------------------

    async def add(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Convenience add() — topic_tracker compatible.  Returns disabled status."""
        return {"memory_id": "", "status": "disabled", "group_id": ""}

    async def search(
        self,
        query: str,
        limit: int = 10,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Convenience search() — topic_tracker compatible.  Returns empty list."""
        return []
