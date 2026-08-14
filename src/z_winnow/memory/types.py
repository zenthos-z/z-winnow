"""T-W8-2 / T-W10-E-ba: MemOS memory data types.

Defines the core data structures used across all MemOS adapter
implementations (real, mock, disabled).

T-W10-E-ba: Extended MemOSAdapterProtocol from 3 to 9 methods;
added StructuredMemoryItem dataclass for direct node insertion.
Added delete_memory (MemoryHandler), scheduler_status/wait (SchedulerHandler).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class MemoryResult:
    """A single memory search result from MemOS.

    Attributes:
        id: Unique memory identifier.
        memory: The memory text content.
        metadata: Structured metadata including type, confidence, tags,
            session_id, and status.
        score: Relevance/similarity score (0.0-1.0 range).
    """

    id: str
    memory: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0

    def __post_init__(self) -> None:
        """Ensure metadata has sensible defaults."""
        if self.metadata is None:  # type: ignore[unreachable]
            self.metadata = {}
        # A008: explicit initialization — score defaults to 0.0
        if self.score is None:  # type: ignore[unreachable]
            self.score = 0.0


@dataclass
class TextualMemoryMetadata:
    """GeneralTextMemory metadata fields (MemOS GeneralTextMemory module).

    Simpler than TreeNodeTextualMemoryMetadata — no key/memory_type/status/usage/background.
    Designed for the GeneralTextMemory module which uses vector-based semantic search
    via Qdrant, without Neo4j graph database dependency.

    Field reference: MemOS docs — GeneralTextMemory TextualMemoryMetadata.
    """

    type: str = "event"  # fact | event | opinion | procedure
    memory_time: str = ""  # YYYY-MM-DD, the date this memory refers to
    source: str = "conversation"  # conversation | retrieved | web | file
    confidence: float = 50.0  # 0-100
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    visibility: str = "private"  # private | public | session
    updated_at: str = ""  # ISO 8601 timestamp

    def __post_init__(self) -> None:
        """Ensure list fields have sensible defaults."""
        if self.entities is None:  # type: ignore[unreachable]
            self.entities = []
        if self.tags is None:  # type: ignore[unreachable]
            self.tags = []


@dataclass
class TreeNodeTextualMemoryMetadata:
    """Backward-compatible alias for TreeTextMemory metadata fields.

    Deprecated: Prefer TextualMemoryMetadata for new code.
    Retained for backward compatibility with existing code during migration.
    """

    key: str = ""
    memory_type: str = "LongTermMemory"
    status: str = "activated"
    visibility: str = "private"
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    confidence: float = 50.0
    source: str = ""
    type: str = "topic"
    usage: list[str] = field(default_factory=list)
    background: str = ""

    def __post_init__(self) -> None:
        """Ensure list fields have sensible defaults."""
        if self.entities is None:  # type: ignore[unreachable]
            self.entities = []
        if self.tags is None:  # type: ignore[unreachable]
            self.tags = []
        if self.usage is None:  # type: ignore[unreachable]
            self.usage = []


@dataclass
class StructuredMemoryItem:
    """A structured memory item for direct insertion into GeneralTextMemory.

    Used by add_structured_memory() to store memory content with metadata.
    Metadata is not sent over the REST API (only memory_content is sent),
    but is retained for local type safety and future API expansion.

    Attributes:
        memory: The memory text content (primary payload).
        metadata: TextualMemoryMetadata or plain dict.
    """

    memory: str
    metadata: TextualMemoryMetadata | dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Ensure metadata defaults to empty dict when None is passed."""
        # A008: explicit initialization
        if self.metadata is None:  # type: ignore[unreachable]
            self.metadata = {}


@runtime_checkable
class MemOSAdapterProtocol(Protocol):
    """Protocol defining the MemOS adapter interface.

    All three implementations (Real, Mock, Disabled) conform to this
    protocol.  Consumers depend on this interface via the factory
    function create_memos_adapter() — P002: factory function pattern.

    T-W10-E-ba: Extended from 3 to 9 methods (add_structured_memory,
    get_all_memories, delete_memory, scheduler_status, scheduler_wait,
    health_check).
    """

    # ------------------------------------------------------------------
    # T-W8-2: Original 3 methods
    # ------------------------------------------------------------------

    async def add_memory(
        self, group_id: str, mem_cube_id: str, messages: list[dict[str, str]]
    ) -> dict[str, Any]:
        """Store a memory entry into the specified MemCube.

        Args:
            group_id: Group identifier (maps to MemOS user_id for per-group isolation).
            mem_cube_id: Target MemCube ID (UUID).
            messages: List of message dicts with 'role' and 'content' keys.

        Returns:
            API response dict (or empty dict on error/disabled).
        """
        ...

    async def search_memories(
        self,
        query: str,
        group_id: str,
        readable_cube_ids: list[str],
        top_k: int = 20,
        legacy_group_ids: list[str] | None = None,
        mode: str = "fine",
    ) -> list[MemoryResult]:
        """Search memories across readable cubes.

        Args:
            query: Search query string.
            group_id: Group identifier (maps to MemOS user_id for per-group isolation).
            readable_cube_ids: List of MemCube IDs to search.
            top_k: Maximum number of results to return.
            legacy_group_ids: Optional legacy group_ids for backward compat.

        Returns:
            List of MemoryResult objects (or empty list on error/disabled).
        """
        ...

    async def get_or_create_cube(self, scope: str) -> str:
        """Get or create a MemCube for the given scope.

        Args:
            scope: Scope identifier (e.g. group_name).

        Returns:
            MemCube ID (UUID string).
        """
        ...

    # ------------------------------------------------------------------
    # T-W10-E-ba: Additional methods
    # ------------------------------------------------------------------

    async def add_structured_memory(
        self,
        cube_id: str,
        group_id: str,
        items: list[StructuredMemoryItem],
        async_mode: str = "sync",
    ) -> dict[str, Any]:
        """Store structured memory items directly (bypass auto-extraction).

        Captures written node ids in ``memory_ids`` for caller linkage
        (feedback provenance / dedup).

        Args:
            cube_id: Target MemCube ID.
            group_id: Group identifier (maps to MemOS user_id).
            items: List of StructuredMemoryItem instances to insert.
            async_mode: "sync" (block until processed) or "async" (background).

        Returns:
            ``{"added": N, "total": M, "memory_ids": [...]}`` on success,
            or ``{"status": "disabled"}`` when disabled.
        """
        ...

    async def get_all_memories(
        self,
        cube_id: str,
        group_id: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get all memories from a cube (for persistence / export).

        Returns the full memory payload with structure:
            {"text_mem": [...], "act_mem": [...], "para_mem": [...]}

        Args:
            cube_id: Target MemCube ID.
            group_id: Group identifier (maps to MemOS user_id).
            filters: Optional filter dict (e.g. {"memory_type": "LongTermMemory"}).

        Returns:
            Dict with text_mem/act_mem/para_mem keys (or empty on disabled).
        """
        ...

    async def delete_memory(
        self,
        cube_id: str,
        group_id: str,
        memory_ids: list[str] | None = None,
        file_ids: list[str] | None = None,
        filter: dict[str, Any] | None = None,
    ) -> bool:
        """Delete memories from a MemCube.

        Supports deletion by memory_ids, file_ids, or filter criteria.
        At least one of memory_ids, file_ids, or filter should be provided.

        Maps to POST /product/delete_memory.

        Args:
            cube_id: Target MemCube ID (maps to writable_cube_ids).
            group_id: Group identifier (maps to MemOS user_id).
            memory_ids: Optional list of memory IDs to delete.
            file_ids: Optional list of file IDs to delete.
            filter: Optional filter dict for bulk deletion.

        Returns:
            True if deletion succeeded, False otherwise.
        """
        ...

    # ------------------------------------------------------------------
    # MemFeedback — native correction (MemOS 2.0+) + single-memory get
    # ------------------------------------------------------------------

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
        """Correct memories via native MemFeedback (archive old + write new).

        Maps to POST /product/feedback (MemOS 2.0+). Version-traceable:
        the new memory's working_binding / shared key links back to the
        archived_id; vector cleanup is server-side.

        Args:
            group_id: MemOS user_id (per-group isolation).
            cube_ids: writable cubes — where the corrected memory lands.
            feedback_content: natural-language correction.
            retrieved_memory_ids: target memory ids (from prior RAG turn).
            history: chat history for intent judgement.
            async_mode: "sync" or "async".
            corrected_answer: also return a corrected answer.
            session_id: soft-filter scope.

        Returns:
            ``{"added": [...], "updated": [{id, text, archived_id,
            origin_memory}...], "new_ids": [...], "archived_ids": [...],
            "raw": <record>}`` on success; ``{"status": "disabled"}`` when
            disabled; ``{"status": "error", ...}`` on failure.
        """
        ...

    async def get_memory(
        self, memory_id: str, group_id: str | None = None
    ) -> MemoryResult | None:
        """Fetch a single memory by id (MemOS 2.0+).

        Maps to GET /product/get_memory/{id}. Pairs with node_id captured by
        add_structured_memory / feedback_memory for provenance lookup.

        Returns:
            MemoryResult, or None if not found / disabled.
        """
        ...

    # ------------------------------------------------------------------
    # SchedulerHandler — async job status + wait
    # ------------------------------------------------------------------

    async def scheduler_status(self, user_name: str) -> dict[str, Any]:
        """Check the MemOS scheduler status.

        Maps to GET /product/scheduler/status.

        Args:
            user_name: MemOS user_name (maps to group_id in our system).

        Returns:
            Dict with scheduler status (e.g. pending_count, processing_count).
            Returns {"status": "error", ...} on failure.
        """
        ...

    async def scheduler_wait(
        self,
        user_name: str,
        timeout_seconds: float = 120.0,
        poll_interval: float = 0.2,
    ) -> dict[str, Any]:
        """Wait for MemOS scheduler to complete all pending jobs.

        Maps to POST /product/scheduler/wait.

        Args:
            user_name: MemOS user_name (maps to group_id in our system).
            timeout_seconds: Maximum seconds to wait (server-side).
            poll_interval: Poll interval hint (server-side).

        Returns:
            Dict with completion status.
        """
        ...

    async def health_check(self) -> dict[str, Any]:
        """Check the health of the MemOS backend.

        Returns:
            Dict with at minimum {"status": str}.  When healthy, includes
            latency_ms and other diagnostics.  When disabled, returns
            {"status": "disabled"}.
        """
        ...
