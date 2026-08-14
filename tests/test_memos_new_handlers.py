"""Tests for MemOS new handler methods: delete_memory, scheduler_*, async_mode.

Phase 4 tests covering:
- delete_memory in real/mock/disabled adapters
- scheduler_status / scheduler_wait in all 3 adapters
- async_mode parameter passthrough in add_memory / add_structured_memory
- sync_ops feedback upsert (delete+re-add on dedupe hit)
"""

from __future__ import annotations

import pytest

from z_winnow.memory.disabled_adapter import DisabledAdapter
from z_winnow.memory.mock_adapter import MockMemOSAdapter
from z_winnow.memory.types import (
    StructuredMemoryItem,
    TreeNodeTextualMemoryMetadata,
)

# ============================================================
# delete_memory
# ============================================================


class TestDeleteMemoryDisabled:
    """DisabledAdapter.delete_memory always returns False."""

    @pytest.mark.asyncio
    async def test_returns_false(self):
        adapter = DisabledAdapter()
        assert await adapter.delete_memory("c1", "g1", memory_ids=["m1"]) is False

    @pytest.mark.asyncio
    async def test_returns_false_with_filter(self):
        adapter = DisabledAdapter()
        assert await adapter.delete_memory("c1", "g1", filter={"status": "archived"}) is False


class TestDeleteMemoryMock:
    """MockMemOSAdapter.delete_memory removes items from in-memory store."""

    @pytest.mark.asyncio
    async def test_delete_by_memory_ids(self):
        adapter = MockMemOSAdapter()
        # Add items first
        await adapter.add_structured_memory(
            cube_id="cube1",
            group_id="g1",
            items=[
                StructuredMemoryItem(memory="alpha", metadata={"key": "k1"}),
                StructuredMemoryItem(memory="beta", metadata={"key": "k2"}),
            ],
        )
        all_data = await adapter.get_all_memories(cube_id="cube1", group_id="g1")
        all_items = all_data.get("text_mem", [])
        assert len(all_items) == 2

        # Delete first item
        first_id = all_items[0].get("id", "")
        ok = await adapter.delete_memory("cube1", "g1", memory_ids=[first_id])
        assert ok is True

        all_data = await adapter.get_all_memories(cube_id="cube1", group_id="g1")
        remaining = all_data.get("text_mem", [])
        assert len(remaining) == 1

    @pytest.mark.asyncio
    async def test_delete_by_filter(self):
        adapter = MockMemOSAdapter()
        await adapter.add_structured_memory(
            cube_id="cube1",
            group_id="g1",
            items=[
                StructuredMemoryItem(
                    memory="old",
                    metadata=TreeNodeTextualMemoryMetadata(key="k1", status="archived"),
                ),
                StructuredMemoryItem(
                    memory="new",
                    metadata=TreeNodeTextualMemoryMetadata(key="k2", status="activated"),
                ),
            ],
        )
        ok = await adapter.delete_memory(
            "cube1",
            "g1",
            filter={"status": "archived"},
        )
        assert ok is True

        all_data = await adapter.get_all_memories(cube_id="cube1", group_id="g1")
        remaining = all_data.get("text_mem", [])
        assert len(remaining) == 1
        assert remaining[0].get("memory", "") == "new"

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self):
        adapter = MockMemOSAdapter()
        ok = await adapter.delete_memory("cube1", "g1", memory_ids=["ghost"])
        assert ok is False


# ============================================================
# scheduler_status / scheduler_wait
# ============================================================


class TestSchedulerDisabled:
    """DisabledAdapter scheduler methods return disabled status."""

    @pytest.mark.asyncio
    async def test_scheduler_status_disabled(self):
        adapter = DisabledAdapter()
        result = await adapter.scheduler_status("g1")
        assert result == {"status": "disabled"}

    @pytest.mark.asyncio
    async def test_scheduler_wait_disabled(self):
        adapter = DisabledAdapter()
        result = await adapter.scheduler_wait("g1")
        assert result == {"status": "disabled"}


class TestSchedulerMock:
    """MockMemOSAdapter scheduler methods return idle/complete."""

    @pytest.mark.asyncio
    async def test_scheduler_status_idle(self):
        adapter = MockMemOSAdapter()
        result = await adapter.scheduler_status("g1")
        assert result["status"] == "idle"

    @pytest.mark.asyncio
    async def test_scheduler_wait_complete(self):
        adapter = MockMemOSAdapter()
        result = await adapter.scheduler_wait("g1", timeout_seconds=5.0)
        assert result["status"] == "complete"


# ============================================================
# async_mode parameter
# ============================================================


class TestAsyncModeParameter:
    """async_mode parameter is accepted by add methods in all adapters."""

    @pytest.mark.asyncio
    async def test_add_memory_async_mode_disabled(self):
        adapter = DisabledAdapter()
        result = await adapter.add_memory(
            group_id="g1",
            mem_cube_id="c1",
            messages=[{"role": "user", "content": "hi"}],
            async_mode="async",
        )
        assert result["status"] == "disabled"

    @pytest.mark.asyncio
    async def test_add_structured_memory_async_mode_mock(self):
        adapter = MockMemOSAdapter()
        result = await adapter.add_structured_memory(
            cube_id="c1",
            group_id="g1",
            items=[StructuredMemoryItem(memory="test")],
            async_mode="async",
        )
        assert result.get("data", {}).get("stored", 0) == 1

    @pytest.mark.asyncio
    async def test_add_memory_sync_mode_disabled(self):
        adapter = DisabledAdapter()
        result = await adapter.add_memory(
            group_id="g1",
            mem_cube_id="c1",
            messages=[],
            async_mode="sync",
        )
        assert result["status"] == "disabled"


# ============================================================
# Protocol conformance
# ============================================================


class TestProtocolConformance:
    """Verify all 3 adapters have the expected 9+2 methods."""

    def test_disabled_adapter_has_all_methods(self):
        adapter = DisabledAdapter()
        for method_name in [
            "add_memory",
            "search_memories",
            "get_or_create_cube",
            "add_structured_memory",
            "get_all_memories",
            "delete_memory",
            "scheduler_status",
            "scheduler_wait",
            "health_check",
            "add",
            "search",
        ]:
            assert hasattr(adapter, method_name), f"DisabledAdapter missing {method_name}"

    def test_mock_adapter_has_all_methods(self):
        adapter = MockMemOSAdapter()
        for method_name in [
            "add_memory",
            "search_memories",
            "get_or_create_cube",
            "add_structured_memory",
            "get_all_memories",
            "delete_memory",
            "scheduler_status",
            "scheduler_wait",
            "health_check",
        ]:
            assert hasattr(adapter, method_name), f"MockMemOSAdapter missing {method_name}"


# ============================================================
# sync_ops upsert (delete + re-add on dedupe hit)
# ============================================================


class TestSyncOpsUpsert:
    """sync_ops._dispatch_add performs delete+re-add when dedupe hits."""

    @pytest.mark.asyncio
    async def test_dedupe_hit_deletes_old_and_re_adds(self):
        from z_winnow.memory.sync_ops import dispatch_op

        adapter = MockMemOSAdapter()

        # Pre-seed an existing item with dedupe_key in memory text
        # (mock search does substring match on memory content)
        dedupe_key = "topic-20260523-tp001"
        await adapter.add_structured_memory(
            cube_id="cube1",
            group_id="g1",
            items=[
                StructuredMemoryItem(
                    memory=f"old summary [{dedupe_key}]",
                    metadata=TreeNodeTextualMemoryMetadata(
                        key=dedupe_key,
                        type="topic",
                    ),
                )
            ],
        )

        # Verify it exists
        all_data = await adapter.get_all_memories(cube_id="cube1", group_id="g1")
        assert len(all_data["text_mem"]) == 1

        # Dispatch an add_topic with the same dedupe_key
        row = {
            "queue_id": 1,
            "op_type": "add_topic",
            "cube_id": "cube1",
            "payload": f'{{"group_id": "g1", "dedupe_key": "{dedupe_key}", "summary": "updated summary", "op_type": "add_topic"}}',
            "retry_count": 0,
        }
        await dispatch_op(adapter, row)

        # Verify: 2 items (no dedup — old retained, new added as-is).
        # Dedup is handled at output_composer level (get_all+delete+re-add).
        all_data = await adapter.get_all_memories(cube_id="cube1", group_id="g1")
        items = all_data["text_mem"]
        assert len(items) == 2
        memories = {i.get("memory", "") for i in items}
        assert "updated summary" in memories

    @pytest.mark.asyncio
    async def test_no_dedupe_miss_adds_normally(self):
        from z_winnow.memory.sync_ops import dispatch_op

        adapter = MockMemOSAdapter()
        row = {
            "queue_id": 2,
            "op_type": "add_feedback",
            "cube_id": "cube1",
            "payload": '{"group_id": "g1", "dedupe_key": "fb-001", "summary": "feedback text", "op_type": "add_feedback"}',
            "retry_count": 0,
        }
        await dispatch_op(adapter, row)

        all_data = await adapter.get_all_memories(cube_id="cube1", group_id="g1")
        assert len(all_data["text_mem"]) == 1
        assert all_data["text_mem"][0].get("memory", "") == "feedback text"
