"""T-W8-2 / T-W10-E-a / T-W10-E-bb: Mock MemOS adapter — deterministic data, no HTTP.

Provides the MockMemOSAdapter that returns deterministic, in-memory
storage without requiring a running MemOS service.  Activated when
WINNOW_ENV=test.

T-W10-E-bb: Added 3 new protocol methods (add_structured_memory,
get_all_memories, health_check).

Design:
- P010 Layer 2: Mock adapter with deterministic data (no httpx)
- Implements MemOSAdapterProtocol (9 methods total)
- P016: Dict storage {cube_id: {user_id: [items]}} for cube-per-group isolation
- Convenience methods add() / search() for topic_tracker compatibility
- call_count_* properties for test assertions
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from z_winnow.memory.types import MemoryResult, StructuredMemoryItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deterministic mock data (fallback seeds when store is empty)
# ---------------------------------------------------------------------------

# Pre-seeded memories for different query topics
_MOCK_MEMORY_SEEDS: list[dict[str, Any]] = [
    {
        "id": "mem-mock-001",
        "memory": "讨论决定下周一发布 v2.3.0 版本，需要完成回归测试",
        "metadata": {
            "type": "decision",
            "confidence": 0.95,
            "tags": ["发布", "版本", "v2.3.0"],
            "session_id": "sess-001",
            "status": "active",
        },
        "score": 0.92,
    },
    {
        "id": "mem-mock-002",
        "memory": "张三反馈登录页面在 iOS 17.4 上白屏，需要排查 WebKit 兼容性",
        "metadata": {
            "type": "bug_report",
            "confidence": 0.88,
            "tags": ["iOS", "登录", "白屏", "WebKit"],
            "session_id": "sess-001",
            "status": "open",
        },
        "score": 0.85,
    },
    {
        "id": "mem-mock-003",
        "memory": "架构讨论：考虑将消息队列从 RabbitMQ 迁移到 Redis Stream",
        "metadata": {
            "type": "discussion",
            "confidence": 0.78,
            "tags": ["架构", "消息队列", "Redis"],
            "session_id": "sess-002",
            "status": "active",
        },
        "score": 0.73,
    },
    {
        "id": "mem-mock-004",
        "memory": "李四分享了 API 性能优化方案，P99 延迟降低 40%",
        "metadata": {
            "type": "knowledge",
            "confidence": 0.91,
            "tags": ["性能优化", "API", "P99"],
            "session_id": "sess-002",
            "status": "active",
        },
        "score": 0.81,
    },
    {
        "id": "mem-mock-005",
        "memory": "本周五下午 3 点进行代码评审，范围：用户模块重构代码",
        "metadata": {
            "type": "meeting",
            "confidence": 0.97,
            "tags": ["代码评审", "周五", "用户模块"],
            "session_id": "sess-003",
            "status": "scheduled",
        },
        "score": 0.67,
    },
]

# Pre-defined topic_tracker trend data (for search() convenience method fallback)
_MOCK_TREND_SEEDS: list[dict[str, Any]] = [
    {
        "memory_id": "mock_mem_001",
        "user_id": "mock-group",
        "content": json.dumps(
            {
                "topic_id": "tp_mock001",
                "topic_name": "API 版本迁移",
                "status": "active",
                "weight": 0.75,
                "first_seen": "2026-05-07",
                "last_seen": "2026-05-13",
                "summary": "讨论从 v2 API 迁移到 v3 的计划和时间表",
                "source_server_ids": ["mock_svr_001", "mock_svr_002"],
                "group": "开发与调试工具",
            },
            ensure_ascii=False,
        ),
        "metadata": {
            "topic_id": "tp_mock001",
            "group": "开发与调试工具",
            "date": "2026-05-13",
            "cube": "topic_tracker-mock-group",
        },
        "created_at": "2026-05-13T10:00:00Z",
        "updated_at": "2026-05-13T18:00:00Z",
    },
    {
        "memory_id": "mock_mem_002",
        "user_id": "mock-group",
        "content": json.dumps(
            {
                "topic_id": "tp_mock002",
                "topic_name": "Prompt 注入防护方案",
                "status": "fading",
                "weight": 0.45,
                "first_seen": "2026-05-01",
                "last_seen": "2026-05-10",
                "summary": "讨论如何防止用户输入中的 prompt 注入攻击",
                "source_server_ids": ["mock_svr_010"],
                "group": "安全性",
            },
            ensure_ascii=False,
        ),
        "metadata": {
            "topic_id": "tp_mock002",
            "group": "安全性",
            "date": "2026-05-10",
            "cube": "topic_tracker-mock-group",
        },
        "created_at": "2026-05-10T10:00:00Z",
        "updated_at": "2026-05-10T18:00:00Z",
    },
]


def _make_mock_response() -> dict[str, Any]:
    """Return a deterministic mock API response matching MemOS format.

    Returns:
        A dict mimicking the MemOS /product/search response structure.
    """
    return {
        "code": 0,
        "message": "success",
        "data": {
            "text_mem": [
                {
                    "id": m["id"],
                    "memory": m["memory"],
                    "metadata": m["metadata"],
                    "score": m["score"],
                }
                for m in _MOCK_MEMORY_SEEDS
            ],
        },
    }


# ---------------------------------------------------------------------------
# P016: Unified MockMemOSAdapter with dict storage
# ---------------------------------------------------------------------------


class MockMemOSAdapter:
    """Mock MemOS adapter with in-memory dict storage.

    Implements MemOSAdapterProtocol (add_memory / search_memories /
    get_or_create_cube) plus convenience methods add() / search() for
    topic_tracker compatibility.

    P016: Dict storage {cube_id: {user_id: [items]}} provides
    cube-per-group isolation for topic_tracker's cross-day queries.

    B1: Different cube_ids are isolated — data stored in one cube
    does not appear in search results for another cube.

    Activated when WINNOW_ENV=test.
    """

    def __init__(self) -> None:
        """Initialize the mock adapter."""
        # P014: In-memory cube_id cache — scope → cube_id
        self._cube_cache: dict[str, str] = {}

        # P016: Dict storage — {cube_id: {user_id: [items]}}
        self._store: dict[str, dict[str, list[dict[str, Any]]]] = {}

        # Call counters for test assertions
        self._call_count_search: int = 0
        self._call_count_add: int = 0

        logger.info("MockMemOSAdapter initialized")

    # ------------------------------------------------------------------
    # MemOSAdapterProtocol methods
    # ------------------------------------------------------------------

    async def add_memory(
        self,
        group_id: str,
        mem_cube_id: str,
        messages: list[dict[str, str]],
        async_mode: str = "sync",
    ) -> dict[str, Any]:
        """Store a memory entry into the in-memory dict.

        P016: Items are stored under {cube_id: {user_id: [items]}}.

        Args:
            group_id: The group identifier.
            mem_cube_id: Target MemCube ID.
            messages: Messages to store.

        Returns:
            Mock API response dict with success status.
        """
        self._call_count_add += 1
        logger.debug(
            "Mock add_memory: user=%s cube=%s msgs=%d",
            group_id,
            mem_cube_id,
            len(messages),
        )

        # Ensure nested dict structure
        if mem_cube_id not in self._store:
            self._store[mem_cube_id] = {}
        if group_id not in self._store[mem_cube_id]:
            self._store[mem_cube_id][group_id] = []

        # Store each message as an item with metadata
        ts = time.time()
        for i, msg in enumerate(messages):
            item: dict[str, Any] = {
                "id": f"mem-mock-store-{mem_cube_id}-{self._call_count_add}-{i}",
                "memory": msg.get("content", ""),
                "metadata": {
                    "type": "stored",
                    "confidence": 0.9,
                    "tags": [],
                    "session_id": "mock-session",
                    "status": "active",
                    "role": msg.get("role", "user"),
                    "cube": mem_cube_id,
                },
                "score": 0.9,
                "user_id": group_id,
                "cube_id": mem_cube_id,
                "created_at": ts,
            }
            self._store[mem_cube_id][group_id].append(item)

        return {
            "code": 0,
            "message": "success (mock)",
            "data": {"stored": len(messages)},
        }

    async def search_memories(
        self,
        query: str,
        group_id: str,
        readable_cube_ids: list[str],
        top_k: int = 20,
        legacy_group_ids: list[str] | None = None,
        mode: str = "fine",
    ) -> list[MemoryResult]:
        """Search memories within the in-memory dict.

        Searches items stored under the given cube_ids and group_id.
        Falls back to pre-seeded seeds when the store is empty
        (backward compatibility).

        P016: Searches within {cube_id: {user_id: [items]}} dict.

        Args:
            query: Search query string (mock does simple substring match).
            group_id: The group identifier.
            readable_cube_ids: MemCube IDs to search.
            top_k: Maximum results to return.
            legacy_group_ids: Optional legacy group identifiers for search filtering.

        Returns:
            List of MemoryResult objects.
        """
        self._call_count_search += 1
        logger.debug(
            "Mock search_memories: query=%s cubes=%s top_k=%d",
            query[:80],
            readable_cube_ids,
            top_k,
        )

        results: list[MemoryResult] = []

        # P016: Search store first if not empty
        store_has_data = any(
            cube_id in self._store and self._store[cube_id] for cube_id in readable_cube_ids
        )

        if store_has_data:
            for cube_id in readable_cube_ids:
                if cube_id not in self._store:
                    continue
                cube_data = self._store[cube_id]
                for _uid, items in cube_data.items():
                    # Search across all users in the cube (mock is lenient)
                    for item in items:
                        # Simple substring match on memory content
                        if query and query.lower() not in item.get("memory", "").lower():
                            continue
                        results.append(
                            MemoryResult(
                                id=item.get("id", str(uuid.uuid4())),
                                memory=item.get("memory", ""),
                                metadata=item.get("metadata", {}),
                                score=float(item.get("score", 0.0)),
                            )
                        )
        else:
            # Fallback to pre-seeded data (backward compat)
            raw_items = _MOCK_MEMORY_SEEDS[:top_k]
            results = [
                MemoryResult(
                    id=m["id"],
                    memory=m["memory"],
                    metadata=m["metadata"],
                    score=m["score"],
                )
                for m in raw_items
            ]

        return results[:top_k]

    async def get_or_create_cube(self, scope: str) -> str:
        """Get or create a mock MemCube for the given scope.

        Returns a deterministic UUID derived from the scope string so
        the same scope always returns the same cube_id (test reproducibility).

        Args:
            scope: Scope identifier (e.g. group_name).

        Returns:
            Deterministic UUID string for this scope.
        """
        cached: str | None = self._cube_cache.get(scope)
        if cached is not None:
            return cached

        # P014: Deterministic UUID from scope (namespace DNS + scope name)
        cube_id: str = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"mock.scope.{scope}"))
        self._cube_cache[scope] = cube_id
        logger.debug("Mock cube created: scope=%s cube_id=%s", scope, cube_id)
        return cube_id

    # ------------------------------------------------------------------
    # Convenience methods (topic_tracker compatibility)
    # ------------------------------------------------------------------

    async def add(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Convenience add() — topic_tracker compatible.

        Wraps add_memory() by converting content+metadata into the
        messages format expected by the protocol method.

        Args:
            content: Memory content (typically JSON-serialized topic data).
            metadata: Optional metadata dict (should contain cube key).
            **kwargs: Additional parameters (cube, user_id, etc.).

        Returns:
            Dict with memory_id, status, user_id.
        """
        self._call_count_add += 1
        logger.debug(
            "Mock add() call #%d (content_len=%d)",
            self._call_count_add,
            len(content),
        )

        meta = metadata or {}
        cube_id = kwargs.get("cube", meta.get("cube", "default-cube"))
        uid = kwargs.get("group_id", "mock-group")

        # Store in dict
        if cube_id not in self._store:
            self._store[cube_id] = {}
        if uid not in self._store[cube_id]:
            self._store[cube_id][uid] = []

        memory_id = f"mock_mem_{self._call_count_add:03d}"
        ts = time.time()

        item: dict[str, Any] = {
            "id": memory_id,
            "memory_id": memory_id,
            "user_id": uid,
            "content": content,
            "metadata": {**meta, "cube": cube_id},
            "cube_id": cube_id,
            "created_at": ts,
        }
        self._store[cube_id][uid].append(item)

        return {
            "memory_id": memory_id,
            "status": "ok",
            "user_id": uid,
        }

    async def search(
        self,
        query: str,
        limit: int = 10,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Convenience search() — topic_tracker compatible.

        Searches the in-memory dict for items matching the query.
        Falls back to pre-defined trend seeds when the store is empty.

        Args:
            query: Search query (group name or topic keywords).
            limit: Max results to return.
            **kwargs: Additional parameters (cube, days, etc.).

        Returns:
            List of memory entry dicts with memory_id, user_id, content,
            metadata, etc.
        """
        self._call_count_search += 1
        logger.debug(
            "Mock search(query=%r, limit=%d) call #%d",
            query,
            limit,
            self._call_count_search,
        )

        cube_id = kwargs.get("cube", "")
        results: list[dict[str, Any]] = []

        # P016: Search store first if we have data for this cube
        if cube_id and cube_id in self._store:
            cube_data = self._store[cube_id]
            for uid, items in cube_data.items():
                for item in items:
                    # Simple substring match
                    content = item.get("content", "")
                    if query.lower() in content.lower():
                        results.append(
                            {
                                "memory_id": item.get("memory_id", item.get("id", "")),
                                "user_id": item.get("user_id", uid),  # internal storage dict key
                                "content": content,
                                "metadata": item.get("metadata", {}),
                                "created_at": item.get("created_at", ""),
                                "updated_at": item.get("created_at", ""),
                            }
                        )
        else:
            # Fallback to pre-defined trend seeds (backward compat)
            # Seeds are predefined — return all of them, don't filter by cube
            results = [dict(entry) for entry in _MOCK_TREND_SEEDS]

        return results[:limit]

    # ------------------------------------------------------------------
    # T-W10-E-bb: 3 new protocol methods (Sec.10.1 wave9-memos-design.md)
    # ------------------------------------------------------------------

    async def add_structured_memory(
        self,
        cube_id: str,
        group_id: str,
        items: list[StructuredMemoryItem],
        async_mode: str = "sync",
    ) -> dict[str, Any]:
        """Store structured memory items into the in-memory dict.

        P016: Items stored with metadata serialized as plain dict.
        Supports both TextualMemoryMetadata dataclass and dict metadata.

        Args:
            cube_id: Target MemCube ID.
            group_id: The group identifier.
            items: List of StructuredMemoryItem instances.

        Returns:
            Mock API response dict with count of stored items.
        """
        self._call_count_add += 1
        logger.debug(
            "Mock add_structured_memory: cube=%s user=%s items=%d",
            cube_id,
            group_id,
            len(items),
        )

        # Ensure nested dict structure
        if cube_id not in self._store:
            self._store[cube_id] = {}
        if group_id not in self._store[cube_id]:
            self._store[cube_id][group_id] = []

        ts = time.time()
        stored_count = 0
        memory_ids: list[str] = []
        for item in items:
            mem_id = f"mem-structured-{cube_id}-{stored_count}-{uuid.uuid4().hex[:8]}"
            # Serialize metadata — both dataclass and dict
            if isinstance(item.metadata, dict):
                meta = dict(item.metadata)
            else:
                # Merge fields from both old (TreeNodeTextualMemoryMetadata)
                # and new (TextualMemoryMetadata) for backward compat
                meta = {
                    "type": getattr(item.metadata, "type", "event"),
                    "source": getattr(item.metadata, "source", "conversation"),
                    "visibility": getattr(item.metadata, "visibility", "private"),
                    "entities": getattr(item.metadata, "entities", []),
                    "tags": getattr(item.metadata, "tags", []),
                    "confidence": getattr(item.metadata, "confidence", 50.0),
                }
                # Include TreeNodeTextualMemoryMetadata fields if present
                if hasattr(item.metadata, "key"):
                    meta["key"] = item.metadata.key
                if hasattr(item.metadata, "status"):
                    meta["status"] = item.metadata.status
                if hasattr(item.metadata, "memory_type"):
                    meta["memory_type"] = item.metadata.memory_type
                if hasattr(item.metadata, "memory_time"):
                    meta["memory_time"] = item.metadata.memory_time
            entry: dict[str, Any] = {
                "id": mem_id,
                "memory": item.memory,
                "metadata": meta,
                "score": 1.0,
                "user_id": group_id,
                "cube_id": cube_id,
                "created_at": ts,
            }
            self._store[cube_id][group_id].append(entry)
            memory_ids.append(mem_id)
            stored_count += 1

        return {
            "code": 0,
            "message": "success (mock structured)",
            "data": {"stored": stored_count},
            "added": stored_count,
            "total": len(items),
            "memory_ids": memory_ids,
        }

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
        """Mock MemFeedback: archive retrieved ids + write a corrected entry.

        Simulates the native archive+write so tests exercising the feedback
        flow get a realistic record shape.
        """
        cube_id = cube_ids[0] if cube_ids else "mock_cube"
        if cube_id not in self._store:
            self._store[cube_id] = {}
        if group_id not in self._store[cube_id]:
            self._store[cube_id][group_id] = []

        ts = time.time()
        updated: list[dict[str, Any]] = []
        for cube in self._store.values():
            for entries in cube.values():
                for entry in entries:
                    if entry.get("id") in retrieved_memory_ids:
                        origin = entry.get("memory", "")
                        entry.setdefault("metadata", {})["status"] = "archived"
                        new_id = f"mem-feedback-{uuid.uuid4().hex[:12]}"
                        new_entry: dict[str, Any] = {
                            "id": new_id,
                            "memory": feedback_content,
                            "metadata": {"status": "activated", "working_binding": entry["id"]},
                            "score": 1.0,
                            "user_id": group_id,
                            "cube_id": cube_id,
                            "created_at": ts,
                        }
                        self._store[cube_id][group_id].append(new_entry)
                        updated.append(
                            {
                                "id": new_id,
                                "text": feedback_content,
                                "archived_id": entry["id"],
                                "origin_memory": origin,
                            }
                        )
        new_ids = [u["id"] for u in updated]
        archived_ids = [u["archived_id"] for u in updated]
        logger.debug(
            "Mock feedback_memory: group=%s new=%d archived=%d",
            group_id,
            len(new_ids),
            len(archived_ids),
        )
        return {
            "added": [],
            "updated": updated,
            "new_ids": new_ids,
            "archived_ids": archived_ids,
            "raw": {"add": [], "update": updated},
        }

    async def get_memory(
        self, memory_id: str, group_id: str | None = None
    ) -> MemoryResult | None:
        """Mock single-memory fetch — scan the in-memory store by id."""
        for cube in self._store.values():
            for entries in cube.values():
                for entry in entries:
                    if entry.get("id") == memory_id:
                        return MemoryResult(
                            id=entry.get("id", memory_id),
                            memory=entry.get("memory", ""),
                            metadata=entry.get("metadata", {}) or {},
                            score=float(entry.get("score", 0.0)),
                        )
        return None

    async def delete_memory(
        self,
        cube_id: str,
        group_id: str,
        memory_ids: list[str] | None = None,
        file_ids: list[str] | None = None,
        filter: dict[str, Any] | None = None,
    ) -> bool:
        """Delete memories from the in-memory dict."""
        if cube_id not in self._store or group_id not in self._store[cube_id]:
            return False

        items = self._store[cube_id][group_id]
        to_remove: set[int] = set()

        if memory_ids:
            for i, item in enumerate(items):
                if item.get("id", "") in memory_ids:
                    to_remove.add(i)

        if filter:
            for i, item in enumerate(items):
                if i in to_remove:
                    continue
                meta = item.get("metadata", {})
                if all(meta.get(k) == v for k, v in filter.items()):
                    to_remove.add(i)

        if to_remove:
            self._store[cube_id][group_id] = [
                item for i, item in enumerate(items) if i not in to_remove
            ]
            logger.debug(
                "Mock delete_memory: removed %d items from cube=%s",
                len(to_remove),
                cube_id,
            )
            return True

        return False

    async def get_all_memories(
        self,
        cube_id: str,
        group_id: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get all memories from a cube in the in-memory dict.

        Returns the full memory payload with structure:
            {"text_mem": [...], "act_mem": [...], "para_mem": [...]}

        All items from the store are placed in text_mem by default.
        Optional filters can narrow by memory_type, status, etc.

        Args:
            cube_id: Target MemCube ID.
            group_id: The group identifier.
            filters: Optional filter dict (e.g. {"memory_type": "LongTermMemory"}).

        Returns:
            Dict with text_mem/act_mem/para_mem keys.
        """
        logger.debug(
            "Mock get_all_memories: cube=%s user=%s filters=%s",
            cube_id,
            group_id,
            filters,
        )

        text_mem: list[dict[str, Any]] = []
        act_mem: list[dict[str, Any]] = []
        para_mem: list[dict[str, Any]] = []

        if cube_id in self._store and group_id in self._store[cube_id]:
            for item in self._store[cube_id][group_id]:
                # Apply filters if provided
                if filters:
                    meta = item.get("metadata", {})
                    match = True
                    for fk, fv in filters.items():
                        if meta.get(fk) != fv:
                            match = False
                            break
                    if not match:
                        continue

                mem_type = item.get("metadata", {}).get("memory_type", "LongTermMemory")
                flat_item: dict[str, Any] = {
                    "id": item.get("id", ""),
                    "memory": item.get("memory", "") or item.get("content", ""),
                    "metadata": item.get("metadata", {}),
                    "score": item.get("score", 1.0),
                    "created_at": item.get("created_at", ""),
                    "updated_at": item.get("updated_at", ""),
                }
                if mem_type == "WorkingMemory":
                    act_mem.append(flat_item)
                elif mem_type == "UserMemory":
                    para_mem.append(flat_item)
                else:
                    text_mem.append(flat_item)

        result = {
            "text_mem": text_mem,
            "act_mem": act_mem,
            "para_mem": para_mem,
        }

        logger.debug(
            "Mock get_all_memories: returned t=%d a=%d p=%d",
            len(text_mem),
            len(act_mem),
            len(para_mem),
        )
        return result

    async def scheduler_status(self, user_name: str) -> dict[str, Any]:
        """Return idle scheduler status — mock is always idle."""
        return {"status": "idle", "pending_count": 0, "processing_count": 0}

    async def scheduler_wait(
        self,
        user_name: str,
        timeout_seconds: float = 120.0,
        poll_interval: float = 0.2,
    ) -> dict[str, Any]:
        """Return immediately — mock has no async processing."""
        return {"status": "complete", "waited_seconds": 0}

    async def health_check(self) -> dict[str, Any]:
        """Return mock health status — always healthy.

        Returns:
            {"status": "mock", "latency_ms": 0, "search_status": "ok"}
        """
        logger.debug("Mock health_check: always ok")
        return {"status": "mock", "latency_ms": 0, "search_status": "ok"}

    # ------------------------------------------------------------------
    # Properties (test assertions)
    # ------------------------------------------------------------------

    @property
    def call_count_search(self) -> int:
        """Number of search/search_memories calls made (for test assertions)."""
        return self._call_count_search

    @property
    def call_count_add(self) -> int:
        """Number of add/add_memory calls made (for test assertions)."""
        return self._call_count_add
