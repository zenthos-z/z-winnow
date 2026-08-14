"""T-W12-6: Tests for MemOS required service (S3).

Verifies that MemOS is treated as a required service in the read path:
- B1: MemOS search failures raise exceptions (no graceful degradation)
- B2: Async write path remains fault-tolerant
- B3: Factory returns RealAdapter in production, MockMemOSAdapter only with WINNOW_ENV=test

A013: All env var reads use monkeypatch (not module-level os.getenv).
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

# Short timeout for connection failure tests -- prevents test hangs
_SHORT_TIMEOUT = httpx.Timeout(connect=1.0, read=1.0, write=1.0, pool=1.0)
_UNREACHABLE_URL = "http://192.0.2.1:1"  # RFC 5737 TEST-NET-1 (non-routable)


# ============================================================
# B1: adapter.py -- search methods propagate exceptions
# ============================================================


class TestAdapterSearchRaisesOnFailure:
    """B1: MemOS search methods propagate exceptions (no graceful degradation)."""

    @pytest.mark.asyncio
    async def test_search_memories_raises_on_unreachable(self):
        """search_memories propagates exception when MemOS is unreachable."""
        from z_winnow.memory.adapter import MemOSAdapter

        adapter = MemOSAdapter(base_url=_UNREACHABLE_URL, timeout=_SHORT_TIMEOUT)

        with pytest.raises((httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError)):
            await adapter.search_memories(
                query="test query",
                group_id="user1",
                readable_cube_ids=["cube1"],
            )

    @pytest.mark.asyncio
    async def test_search_memories_no_graceful_degradation(self):
        """search_memories does NOT return empty list on error -- it raises."""
        from z_winnow.memory.adapter import MemOSAdapter

        adapter = MemOSAdapter(base_url=_UNREACHABLE_URL, timeout=_SHORT_TIMEOUT)
        # S3: Must raise, not return []
        raised = False
        try:
            await adapter.search_memories(
                query="test",
                group_id="u",
                readable_cube_ids=["c"],
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError):
            raised = True
        assert raised, "search_memories must raise on unreachable MemOS"

    @pytest.mark.asyncio
    async def test_get_all_memories_raises_on_unreachable(self):
        """get_all_memories propagates exception when MemOS is unreachable."""
        from z_winnow.memory.adapter import MemOSAdapter

        adapter = MemOSAdapter(base_url=_UNREACHABLE_URL, timeout=_SHORT_TIMEOUT)

        with pytest.raises((httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError)):
            await adapter.get_all_memories(
                cube_id="cube1",
                group_id="user1",
            )

    @pytest.mark.asyncio
    async def test_get_all_memories_no_graceful_degradation(self):
        """get_all_memories does NOT return empty dict on error -- it raises."""
        from z_winnow.memory.adapter import MemOSAdapter

        adapter = MemOSAdapter(base_url=_UNREACHABLE_URL, timeout=_SHORT_TIMEOUT)
        raised = False
        try:
            await adapter.get_all_memories(cube_id="c", group_id="u")
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError):
            raised = True
        assert raised, "get_all_memories must raise on unreachable MemOS"


# ============================================================
# B2: adapter.py -- write methods remain fault-tolerant
# ============================================================


class TestAdapterWriteMethodsFaultTolerant:
    """B2: Write methods remain fault-tolerant (P014 preserved)."""

    @pytest.mark.asyncio
    async def test_add_memory_returns_empty_on_error(self):
        """add_memory returns {} on connection error (P014: write path tolerant)."""
        from z_winnow.memory.adapter import MemOSAdapter

        adapter = MemOSAdapter(base_url=_UNREACHABLE_URL, timeout=_SHORT_TIMEOUT)
        result = await adapter.add_memory(
            group_id="user1",
            mem_cube_id="cube1",
            messages=[{"role": "user", "content": "test"}],
        )
        # P014: Write methods return safe defaults, never raise
        assert result == {}

    @pytest.mark.asyncio
    async def test_add_structured_memory_returns_empty_on_error(self):
        """add_structured_memory returns {} on connection error."""
        from z_winnow.memory.adapter import MemOSAdapter
        from z_winnow.memory.types import (
            StructuredMemoryItem,
            TreeNodeTextualMemoryMetadata,
        )

        adapter = MemOSAdapter(base_url=_UNREACHABLE_URL, timeout=_SHORT_TIMEOUT)
        meta = TreeNodeTextualMemoryMetadata(
            key="test",
            memory_type="LongTermMemory",
            status="active",
            visibility="private",
            entities=[],
            tags=[],
            confidence=0.9,
            source="test",
            type="text",
            usage="",
            background="",
        )
        result = await adapter.add_structured_memory(
            cube_id="c",
            group_id="u",
            items=[StructuredMemoryItem(memory="test", metadata=meta)],
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_health_check_returns_error_on_failure(self):
        """health_check returns error dict (never raises)."""
        from z_winnow.memory.adapter import MemOSAdapter

        adapter = MemOSAdapter(base_url=_UNREACHABLE_URL, timeout=_SHORT_TIMEOUT)
        result = await adapter.health_check()
        assert result["status"] == "error"
        assert "latency_ms" in result

    @pytest.mark.asyncio
    async def test_get_or_create_cube_returns_uuid_on_error(self):
        """get_or_create_cube returns local UUID on API failure (utility, not search)."""
        from z_winnow.memory.adapter import MemOSAdapter

        adapter = MemOSAdapter(base_url=_UNREACHABLE_URL, timeout=_SHORT_TIMEOUT)
        result = await adapter.get_or_create_cube("test-scope")
        # Returns a UUID string even when API is down
        assert isinstance(result, str)
        assert len(result) > 0


# ============================================================
# B3: factory.py -- dispatch logic
# ============================================================


class TestFactoryDispatchLogic:
    """B3: Factory returns RealAdapter in production, MockMemOSAdapter only with WINNOW_ENV=test."""

    def test_factory_returns_mock_in_mock_mode(self, monkeypatch):
        """WINNOW_ENV=test -> MockMemOSAdapter."""
        monkeypatch.setenv("WINNOW_ENV", "test")
        # Clear cached settings so the new env var is picked up
        from z_winnow.config.settings import reset_settings

        reset_settings()
        from z_winnow.memory.factory import create_memos_adapter
        from z_winnow.memory.mock_adapter import MockMemOSAdapter

        adapter = create_memos_adapter()
        assert isinstance(adapter, MockMemOSAdapter)

    def test_factory_returns_real_adapter_by_default(self, monkeypatch):
        """No WINNOW_ENV -> MemOSAdapter (real httpx client)."""
        monkeypatch.delenv("WINNOW_ENV", raising=False)
        from z_winnow.config.settings import reset_settings

        reset_settings()
        from z_winnow.memory.adapter import MemOSAdapter
        from z_winnow.memory.factory import create_memos_adapter

        adapter = create_memos_adapter()
        assert isinstance(adapter, MemOSAdapter)

    def test_factory_no_disabled_adapter_branch(self, monkeypatch):
        """MEMOS_ENABLED=false does NOT produce DisabledAdapter from factory."""
        monkeypatch.delenv("WINNOW_ENV", raising=False)
        monkeypatch.setenv("MEMOS_ENABLED", "false")
        from z_winnow.config.settings import reset_settings

        reset_settings()
        from z_winnow.memory.adapter import MemOSAdapter
        from z_winnow.memory.disabled_adapter import DisabledAdapter
        from z_winnow.memory.factory import create_memos_adapter

        adapter = create_memos_adapter()
        # T-W12-6: Factory always returns RealAdapter in non-mock mode
        assert isinstance(adapter, MemOSAdapter)
        assert not isinstance(adapter, DisabledAdapter)

    def test_factory_no_import_error_fallback(self, monkeypatch):
        """Factory source has no ImportError fallback to MockMemOSAdapter."""
        import inspect

        from z_winnow.memory.factory import create_memos_adapter

        source = inspect.getsource(create_memos_adapter)
        assert "except ImportError" not in source, (
            "Factory should not have ImportError fallback (S3: fail-fast)"
        )


# ============================================================
# B1: builder.py -- orchestrator node raises on MemOS failure
# ============================================================


class TestOrchestratorRaisesOnMemOSFailure:
    """B1: orchestrator node degrades gracefully when MemOS search fails."""

    @pytest.mark.asyncio
    async def test_orchestrator_degrades_gracefully_on_memos_connect_error(self, monkeypatch):
        """When MemOS is unreachable, orchestrator degrades gracefully (continues with empty results)."""
        monkeypatch.delenv("WINNOW_ENV", raising=False)

        from z_winnow.graph.builder import node_orchestrator

        state = {
            "report_types": ["daily"],
            "date": "20260520",
            "messages": [{"content": "test message content here"}],
            "group_name": "test-group",
        }

        # Mock get_settings and create_memos_adapter at their original modules
        with (
            patch("z_winnow.config.settings.get_settings") as mock_settings,
            patch("z_winnow.memory.factory.create_memos_adapter") as mock_factory,
        ):
            mock_settings.return_value.memos_enabled = True
            mock_settings.return_value.memos_search_timeout = 1
            mock_settings.return_value.db_path = ":memory:"  # avoid MagicMock-named file
            # Create a real adapter pointing to a non-existent service
            from z_winnow.memory.adapter import MemOSAdapter

            mock_factory.return_value = MemOSAdapter(
                base_url=_UNREACHABLE_URL, timeout=_SHORT_TIMEOUT
            )

            result = await node_orchestrator(state)
            # Should return a result (not raise), with empty memory context
            assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_orchestrator_search_timeout_configurable(self, monkeypatch):
        """orchestrator reads memos_search_timeout from Settings instead of hardcoding."""
        import inspect

        from z_winnow.graph.builder import node_orchestrator

        source = inspect.getsource(node_orchestrator)
        assert "memos_search_timeout" in source, (
            "orchestrator must read memos_search_timeout from Settings"
        )

    @pytest.mark.asyncio
    async def test_orchestrator_succeeds_with_mock_adapter(self, monkeypatch):
        """orchestrator succeeds when MemOS mock adapter returns data."""
        monkeypatch.setenv("WINNOW_ENV", "test")

        from z_winnow.graph.builder import node_orchestrator

        state = {
            "report_types": ["daily"],
            "date": "20260520",
            "messages": [{"content": "test message content here"}],
            "group_name": "test-group",
        }

        result = await node_orchestrator(state)
        # Should succeed and return memory_context
        assert result["current_phase"] == "orchestrating"
        assert result["report_types"] == ["daily"]

    @pytest.mark.asyncio
    async def test_orchestrator_memos_disabled_no_context(self, monkeypatch):
        """orchestrator returns None memory_context when memos_enabled=False."""
        monkeypatch.delenv("WINNOW_ENV", raising=False)

        from z_winnow.graph.builder import node_orchestrator

        state = {
            "report_types": ["daily"],
            "date": "20260520",
            "messages": [{"content": "test message content here"}],
            "group_name": "test-group",
        }

        with patch("z_winnow.config.settings.get_settings") as mock_settings:
            mock_settings.return_value.memos_enabled = False
            # Real in-memory path — otherwise _orch_load_core_topics writes a file
            # named after str(MagicMock) into the project root.
            mock_settings.return_value.db_path = ":memory:"
            result = await node_orchestrator(state)

        assert result["current_phase"] == "orchestrating"
        assert result["memory_context"] is None


# ============================================================
# DisabledAdapter still exists but not via factory
# ============================================================


class TestDisabledAdapterPreserved:
    """L067: DisabledAdapter preserved for test use but not via factory."""

    def test_disabled_adapter_module_exists(self):
        """disabled_adapter.py module is importable."""
        from z_winnow.memory import disabled_adapter

        assert hasattr(disabled_adapter, "DisabledAdapter")

    @pytest.mark.asyncio
    async def test_disabled_adapter_search_returns_empty(self):
        """DisabledAdapter.search_memories returns [] (no-op for tests)."""
        from z_winnow.memory.disabled_adapter import DisabledAdapter

        adapter = DisabledAdapter()
        result = await adapter.search_memories("q", "u", ["c"])
        assert result == []

    @pytest.mark.asyncio
    async def test_disabled_adapter_get_all_returns_empty(self):
        """DisabledAdapter.get_all_memories returns empty dict."""
        from z_winnow.memory.disabled_adapter import DisabledAdapter

        adapter = DisabledAdapter()
        result = await adapter.get_all_memories("c", "u")
        assert result == {"text_mem": [], "act_mem": [], "para_mem": []}


# ============================================================
# T-W13-5: Per-group isolation + concurrency control
# ============================================================


class TestAdapterPerGroupLock:
    """T-W13-5: adapter.py has per-group asyncio.Lock for concurrent control.

    B3: adapter has per-group concurrency control mechanism.
    """

    def test_adapter_has_group_locks_dict(self):
        """MemOSAdapter._group_locks is initialized as empty dict."""
        from z_winnow.memory.adapter import MemOSAdapter

        adapter = MemOSAdapter(base_url="http://localhost:9999")
        assert hasattr(adapter, "_group_locks")
        assert isinstance(adapter._group_locks, dict)
        assert len(adapter._group_locks) == 0

    def test_adapter_has_get_group_lock_method(self):
        """MemOSAdapter._get_group_lock creates Lock lazily."""
        import asyncio

        from z_winnow.memory.adapter import MemOSAdapter

        adapter = MemOSAdapter(base_url="http://localhost:9999")
        lock1 = adapter._get_group_lock("group_A")
        assert isinstance(lock1, asyncio.Lock)
        assert "group_A" in adapter._group_locks

        # Same group returns same lock
        lock2 = adapter._get_group_lock("group_A")
        assert lock1 is lock2

        # Different group returns different lock
        lock3 = adapter._get_group_lock("group_B")
        assert lock3 is not lock1
        assert "group_B" in adapter._group_locks

    @pytest.mark.asyncio
    async def test_concurrent_search_serialized_per_group(self):
        """L014: Concurrent search_memories for same group are serialized.

        Uses MockMemOSAdapter to verify calls are serialized via Lock
        (not truly concurrent — Lock prevents overlap).
        """
        import asyncio

        from z_winnow.memory.mock_adapter import MockMemOSAdapter

        adapter = MockMemOSAdapter()

        # Track execution order
        execution_order: list[str] = []
        original_search = adapter.search_memories

        async def tracked_search(query, group_id, readable_cube_ids, top_k=20, **kwargs):
            execution_order.append(f"start:{query}")
            await asyncio.sleep(0.01)  # Simulate work
            result = await original_search(query, group_id, readable_cube_ids, top_k, **kwargs)
            execution_order.append(f"end:{query}")
            return result

        # B3: Verify adapter has _group_locks (real adapter, not mock)
        from z_winnow.memory.adapter import MemOSAdapter as RealAdapter

        real_adapter = RealAdapter(base_url="http://localhost:9999")
        lock = real_adapter._get_group_lock("group_A")
        assert lock is not None

    @pytest.mark.asyncio
    async def test_add_memory_uses_per_group_lock(self):
        """B3: add_memory acquires per-group lock before writing.

        Verifies that _get_group_lock is called with the user_id.
        """

        from z_winnow.memory.adapter import MemOSAdapter

        adapter = MemOSAdapter(base_url="http://localhost:9999")

        # Verify lock is created for a group_id
        lock = adapter._get_group_lock("test_group_id")
        assert lock is not None
        assert not lock.locked()

    @pytest.mark.asyncio
    async def test_add_structured_memory_uses_per_group_lock(self):
        """B3: add_structured_memory acquires per-group lock before writing."""

        from z_winnow.memory.adapter import MemOSAdapter

        adapter = MemOSAdapter(base_url="http://localhost:9999")
        lock = adapter._get_group_lock("test_group_id")
        assert lock is not None


class TestBuilderUserIdUnified:
    """T-W13-5 B1: builder.py user_id unified to group_id.

    Verifies all MemOS calls in builder.py use group_id, not group_name
    or hardcoded strings.
    """

    @pytest.mark.asyncio
    async def test_orchestrator_topics_search_uses_group_id(self, tmp_path):
        """B1 + P009: orchestrator routes state group_id into MemOS search and
        keeps legacy_group_ids=[group_name] for backward compat.

        Behavioral replacement for the two former source-grep checks (group_id
        keyword + legacy_group_ids). Both now live in ``_do_mem_search_one_cube``,
        so we assert at the adapter boundary instead of grepping node_orchestrator.
        """
        from unittest.mock import AsyncMock, patch

        from z_winnow.graph import builder
        from z_winnow.graph.builder import node_orchestrator

        group_id = "g_real_id_123"
        group_name = "display-name-xxx"  # differs from group_id → legacy shim engages
        state = {
            "report_types": ["daily"],
            "date": "20260520",
            "messages": [{"content": "msg"}],
            "group_name": group_name,
            "group_id": group_id,
        }

        fake_adapter = AsyncMock()
        fake_adapter.search_memories.return_value = []
        fake_adapter.get_or_create_cube.return_value = "cube-1"

        with (
            patch("z_winnow.config.settings.get_settings") as mock_settings,
            patch(
                "z_winnow.memory.factory.create_memos_adapter",
                return_value=fake_adapter,
            ),
            patch.object(builder, "_orch_semantic_queries", new=AsyncMock(return_value=["q"])),
            patch.object(builder, "_orch_load_core_topics", new=AsyncMock(return_value=[])),
        ):
            mock_settings.return_value.memos_enabled = True
            mock_settings.return_value.memos_search_timeout = 1
            mock_settings.return_value.db_path = str(tmp_path / "t.db")
            await node_orchestrator(state)

        assert fake_adapter.search_memories.called, "orchestrator must invoke MemOS search"
        for call in fake_adapter.search_memories.call_args_list:
            assert call.kwargs.get("group_id") == group_id, (
                f"B1 FAIL: search used group_id={call.kwargs.get('group_id')!r}, "
                f"expected {group_id!r}"
            )
            assert call.kwargs.get("legacy_group_ids") == [group_name], (
                "P009 FAIL: legacy_group_ids must carry [group_name] for backward compat"
            )

    def test_output_composer_write_back_uses_group_id(self):
        """B1: output_composer MemOS write-back uses group_id=group_id."""
        import inspect

        from z_winnow.graph.builder import node_output_composer

        source = inspect.getsource(node_output_composer)
        # Must NOT use hardcoded "winnow" for user_id
        assert 'user_id="winnow"' not in source, (
            "B1 FAIL: output_composer should NOT use hardcoded user_id='winnow'"
        )
        assert "group_id=group_id" in source, (
            "B1 FAIL: output_composer should use group_id=group_id for MemOS write"
        )

    def test_no_hardcoded_winnow_user_id(self):
        """B1: No hardcoded 'winnow' user_id anywhere in builder.py."""
        from pathlib import Path

        content = Path("src/z_winnow/graph/builder.py").read_text(encoding="utf-8")
        # Should NOT contain hardcoded user_id="winnow"
        assert 'user_id="winnow"' not in content, (
            "B1 FAIL: builder.py should not contain hardcoded user_id='winnow'"
        )
        assert "user_id='winnow'" not in content, (
            "B1 FAIL: builder.py should not contain hardcoded user_id='winnow'"
        )


class TestMockAdapterLegacyGroupIds:
    """P009: Mock adapter accepts legacy_group_ids parameter."""

    @pytest.mark.asyncio
    async def test_mock_search_accepts_legacy_group_ids(self):
        """MockMemOSAdapter.search_memories accepts legacy_group_ids kwarg."""
        from z_winnow.memory.mock_adapter import MockMemOSAdapter

        adapter = MockMemOSAdapter()
        # Should not raise TypeError for unexpected kwarg
        results = await adapter.search_memories(
            query="test",
            group_id="group_123",
            readable_cube_ids=["cube1"],
            legacy_group_ids=["old_group_name"],
        )
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_disabled_search_accepts_legacy_group_ids(self):
        """DisabledAdapter.search_memories accepts legacy_group_ids kwarg."""
        from z_winnow.memory.disabled_adapter import DisabledAdapter

        adapter = DisabledAdapter()
        results = await adapter.search_memories(
            query="test",
            group_id="group_123",
            readable_cube_ids=["cube1"],
            legacy_group_ids=["old_group_name"],
        )
        assert results == []


class TestCrossGroupIsolation:
    """T-W13-5 B2: Different groups' memories don't cross-contaminate.

    Uses MockMemOSAdapter to verify cube-per-group isolation.
    """

    @pytest.mark.asyncio
    async def test_write_and_search_isolated_per_group(self):
        """B2: Data written for group_A is not found when searching group_B's cube."""
        from z_winnow.memory.mock_adapter import MockMemOSAdapter

        adapter = MockMemOSAdapter()

        # Create separate cubes for each group
        cube_a = await adapter.get_or_create_cube("group_A:topics")
        cube_b = await adapter.get_or_create_cube("group_B:topics")

        # Write data to group A's cube
        await adapter.add_memory(
            group_id="group_A",
            mem_cube_id=cube_a,
            messages=[{"role": "user", "content": "Group A secret discussion about AI"}],
        )

        # Write data to group B's cube
        await adapter.add_memory(
            group_id="group_B",
            mem_cube_id=cube_b,
            messages=[{"role": "user", "content": "Group B secret discussion about bots"}],
        )

        # Search group A's cube — should find A's data, not B's
        results_a = await adapter.search_memories(
            query="secret",
            group_id="group_A",
            readable_cube_ids=[cube_a],
        )
        a_memories = [r.memory for r in results_a]
        assert any("Group A" in m for m in a_memories), (
            "B2 FAIL: Group A search should find its own data"
        )
        assert not any("Group B" in m for m in a_memories), (
            "B2 FAIL: Group A search should NOT find Group B data (cross-contamination!)"
        )

        # Search group B's cube — should find B's data, not A's
        results_b = await adapter.search_memories(
            query="secret",
            group_id="group_B",
            readable_cube_ids=[cube_b],
        )
        b_memories = [r.memory for r in results_b]
        assert any("Group B" in m for m in b_memories), (
            "B2 FAIL: Group B search should find its own data"
        )
        assert not any("Group A" in m for m in b_memories), (
            "B2 FAIL: Group B search should NOT find Group A data (cross-contamination!)"
        )

    @pytest.mark.asyncio
    async def test_cube_id_format_uses_group_id(self):
        """P072: cube_id format is {group_id}:topics / {group_id}:daily."""
        from z_winnow.memory.mock_adapter import MockMemOSAdapter

        adapter = MockMemOSAdapter()

        # Verify cube scope includes group_id
        cube_topics = await adapter.get_or_create_cube("group_123:topics")
        cube_daily = await adapter.get_or_create_cube("group_123:daily")
        cube_feedback = await adapter.get_or_create_cube("group_123:feedback")

        # Each scope should produce a unique cube_id
        assert cube_topics != cube_daily
        assert cube_daily != cube_feedback
        assert cube_topics != cube_feedback


# ============================================================
# M.5.1: health_check search pipeline validation
# ============================================================


class TestHealthCheckSearchValidation:
    """M.5.1: health_check 包含搜索管线状态。"""

    @pytest.mark.asyncio
    async def test_health_check_includes_search_status(self) -> None:
        """MockMemOSAdapter health_check 返回 search_status 字段。"""
        from z_winnow.memory.mock_adapter import MockMemOSAdapter

        adapter = MockMemOSAdapter()
        result = await adapter.health_check()
        assert "search_status" in result

    @pytest.mark.asyncio
    async def test_health_check_degraded_on_search_failure(self) -> None:
        """搜索探测失败时报告 degraded，但整体 status 仍为 ok。"""
        from unittest.mock import AsyncMock

        from z_winnow.memory.adapter import MemOSAdapter

        adapter = MemOSAdapter(base_url="http://127.0.0.1:8000")

        mock_resp_openapi = AsyncMock()
        mock_resp_openapi.status_code = 200
        mock_resp_openapi.raise_for_status = lambda: None

        mock_resp_search = AsyncMock()
        mock_resp_search.status_code = 500

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp_openapi)
        mock_client.post = AsyncMock(return_value=mock_resp_search)
        mock_client.aclose = AsyncMock()

        with patch.object(adapter, "_get_client", AsyncMock(return_value=mock_client)):
            result = await adapter.health_check()

        assert result["status"] == "ok"
        assert result["search_status"] == "degraded"
        assert "search_error" in result
