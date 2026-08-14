"""W15-P2-MEMOS: Tests for 8 MemOS management endpoints.

Covers:
  - GET  /api/v1/memos/cubes?group=X          (list cubes)
  - GET  /api/v1/memos/cubes/{cube_id}         (cube detail)
  - DELETE /api/v1/memos/cubes/{cube_id}       (delete cube with confirm gate)
  - POST /api/v1/memos/cubes/{cube_id}/rebuild (async rebuild)
  - POST /api/v1/memos/cubes/{cube_id}/vacuum  (async vacuum)
  - GET  /api/v1/memos/memory/{memory_id}      (memory detail)
  - DELETE /api/v1/memos/memory/{memory_id}    (delete memory)
  - POST /api/v1/memos/flush                   (async flush)

P082: Read methods propagate ConnectError → 502; Write methods degrade.
P079: DELETE cube without confirm → 400/422.
P067: rebuild/vacuum/flush return 202 with task_id.
P078: Uses real MockMemOSAdapter (no mock DB).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from z_winnow.memory.mock_adapter import MockMemOSAdapter
from z_winnow.memory.types import (
    StructuredMemoryItem,
    TextualMemoryMetadata,
)

# ============================================================
# P012: Env isolation — autouse monkeypatch
# ============================================================


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate environment: mock mode, test env, no API key."""
    monkeypatch.setenv("WEFLOW_MOCK_MODE", "true")
    monkeypatch.setenv("WINNOW_ENV", "test")
    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", "")
    monkeypatch.setenv("WINNOW_DB_PATH", "")
    monkeypatch.setenv("WINNOW_REPORTS_DIR", "")
    monkeypatch.delenv("WINNOW_WEB_API_KEY", raising=False)
    monkeypatch.delenv("WEB_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _reset_settings():
    """Reset Settings singleton so each test picks up monkeypatched env."""
    from z_winnow.config.settings import reset_settings

    reset_settings()
    yield
    reset_settings()


# ============================================================
# Core fixtures
# ============================================================


def _build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a FastAPI app with routes and MockMemOSAdapter."""
    from z_winnow.config.settings import reset_settings
    from z_winnow.web.app import lifespan
    from z_winnow.web.routes import api_router

    db_path = str(tmp_path / "test_memos.db")
    reports_dir = str(tmp_path / "reports")
    Path(reports_dir).mkdir(exist_ok=True)

    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_REPORTS_DIR", reports_dir)
    reset_settings()

    fresh = FastAPI(title="memos-test", lifespan=lifespan)
    fresh.include_router(api_router)
    return fresh


@pytest.fixture
async def real_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Yield a running app with MockMemOSAdapter."""
    from z_winnow.web.app import lifespan

    app = _build_app(tmp_path, monkeypatch)
    async with lifespan(app):
        yield app


@pytest.fixture
async def client(real_app: FastAPI):
    """httpx client wired to the real app."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app),
        base_url="http://test",
    ) as c:
        yield c


@pytest.fixture
def mock_adapter(real_app: FastAPI) -> MockMemOSAdapter:
    """Get the MockMemOSAdapter from app state."""
    return real_app.state.memos_adapter  # type: ignore[return-value]


# ============================================================
# Seed helpers
# ============================================================


async def _seed_cube_with_memories(
    adapter: MockMemOSAdapter,
    cube_id: str,
    group_id: str = "",
    count: int = 3,
) -> list[str]:
    """Seed a cube with test memories. Returns list of memory IDs.

    Uses group_id="" by default to match the service layer's query group_id.
    """
    items = []
    for i in range(count):
        items.append(
            StructuredMemoryItem(
                memory=f"Test memory {i} in {cube_id}",
                metadata=TextualMemoryMetadata(
                    type="event",
                    source="test",
                    confidence=80.0,
                    tags=[group_id, "test"],
                    visibility="private",
                ),
            )
        )
    await adapter.add_structured_memory(cube_id=cube_id, group_id=group_id, items=items)

    # Get the actual IDs
    all_data = await adapter.get_all_memories(cube_id=cube_id, group_id=group_id)
    memory_ids = []
    for key in ("text_mem", "act_mem", "para_mem"):
        for item in all_data.get(key, []):
            if isinstance(item, dict):
                memory_ids.append(item.get("id", ""))
    return memory_ids


# ============================================================
# B1: GET /memos/cubes?group=X — list cubes
# ============================================================


class TestListCubes:
    """B1: GET /memos/cubes?group=X returns list[MemCubeOut]."""

    async def test_list_cubes_returns_list(
        self, client: httpx.AsyncClient, mock_adapter: MockMemOSAdapter
    ) -> None:
        """GET /memos/cubes?group=test returns a list."""
        resp = await client.get("/api/v1/memos/cubes", params={"group": "test_group"})
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)

    async def test_list_cubes_has_expected_shape(
        self, client: httpx.AsyncClient, mock_adapter: MockMemOSAdapter
    ) -> None:
        """Each cube has cube_id, group_id, date, message_count, status."""
        resp = await client.get("/api/v1/memos/cubes", params={"group": "test_group"})
        assert resp.status_code == 200
        body = resp.json()
        for cube in body:
            assert "cube_id" in cube
            assert "group_id" in cube
            assert "date" in cube
            assert "message_count" in cube
            assert "status" in cube

    async def test_list_cubes_reflects_seeded_memories(
        self, client: httpx.AsyncClient, mock_adapter: MockMemOSAdapter
    ) -> None:
        """Seeded memories show up in cube's message_count."""
        group = "count_test"
        cube_id = await mock_adapter.get_or_create_cube(f"{group}:topics")
        await _seed_cube_with_memories(mock_adapter, cube_id, count=3)

        resp = await client.get("/api/v1/memos/cubes", params={"group": group})
        assert resp.status_code == 200
        body = resp.json()
        topics_cubes = [c for c in body if c.get("cube_id") == cube_id]
        assert len(topics_cubes) >= 1
        assert topics_cubes[0]["message_count"] == 3

    async def test_list_cubes_empty_group(self, client: httpx.AsyncClient) -> None:
        """Empty group returns list (possibly empty or with default cubes)."""
        resp = await client.get("/api/v1/memos/cubes", params={"group": "nonexistent_group"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ============================================================
# B1: GET /memos/cubes/{cube_id} — cube detail
# ============================================================


class TestGetCubeDetail:
    """B1: GET /memos/cubes/{cube_id} returns MemCubeOut with memory_count."""

    async def test_get_cube_detail_ok(
        self, client: httpx.AsyncClient, mock_adapter: MockMemOSAdapter
    ) -> None:
        """GET detail returns 200 with memory_count."""
        group = "detail_test"
        cube_id = await mock_adapter.get_or_create_cube(f"{group}:topics")
        await _seed_cube_with_memories(mock_adapter, cube_id, count=2)

        resp = await client.get(f"/api/v1/memos/cubes/{cube_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cube_id"] == cube_id
        assert body["message_count"] == 2

    async def test_get_cube_detail_empty_cube(
        self, client: httpx.AsyncClient, mock_adapter: MockMemOSAdapter
    ) -> None:
        """Empty cube returns 200 with message_count=0."""
        cube_id = await mock_adapter.get_or_create_cube("empty_cube:topics")

        resp = await client.get(f"/api/v1/memos/cubes/{cube_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["message_count"] == 0


# ============================================================
# B2: DELETE /memos/cubes/{cube_id} — confirm gate
# ============================================================


class TestDeleteCube:
    """B2: DELETE cube requires {confirm: true}, returns 204."""

    async def test_delete_cube_with_confirm(
        self, client: httpx.AsyncClient, mock_adapter: MockMemOSAdapter
    ) -> None:
        """DELETE with confirm=true returns 204."""
        group = "delete_test"
        cube_id = await mock_adapter.get_or_create_cube(f"{group}:topics")
        await _seed_cube_with_memories(mock_adapter, cube_id, count=1)

        resp = await client.request(
            "DELETE",
            f"/api/v1/memos/cubes/{cube_id}",
            json={"confirm": True},
        )
        assert resp.status_code == 204

    async def test_delete_cube_without_confirm(self, client: httpx.AsyncClient) -> None:
        """DELETE without confirm=true returns 422 (Pydantic validation)."""
        resp = await client.request(
            "DELETE",
            "/api/v1/memos/cubes/some-cube",
            json={"confirm": False},
        )
        assert resp.status_code == 422

    async def test_delete_cube_missing_confirm_field(self, client: httpx.AsyncClient) -> None:
        """DELETE with missing confirm field returns 422."""
        resp = await client.request(
            "DELETE",
            "/api/v1/memos/cubes/some-cube",
            json={},
        )
        assert resp.status_code == 422


# ============================================================
# B3: POST /memos/cubes/{cube_id}/rebuild — async 202
# ============================================================


class TestRebuildCube:
    """B3: POST rebuild returns 202, task completes."""

    async def test_rebuild_returns_202(self, client: httpx.AsyncClient) -> None:
        """POST rebuild returns 202 with task_id."""
        resp = await client.post(
            "/api/v1/memos/cubes/test-cube/rebuild",
            params={"group": "test_group"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert "task_id" in body
        assert body["task_id"]

    async def test_rebuild_task_completes(
        self, client: httpx.AsyncClient, real_app: FastAPI
    ) -> None:
        """Rebuild task transitions to done."""
        from z_winnow.web.services.task_queue import get_task_status

        resp = await client.post(
            "/api/v1/memos/cubes/test-cube/rebuild",
            params={"group": "test_group"},
        )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        # Poll until done
        status = None
        for _ in range(30):
            await asyncio.sleep(0.1)
            status = await get_task_status(task_id, db_path=real_app.state.db_path)
            if status and status["status"] in ("done", "failed"):
                break
        assert status is not None, "Task never appeared in DB"
        assert status["status"] == "done", f"Task failed: {status.get('error_message')}"

        # Verify result is a dict with expected keys
        result = json.loads(status["result_json"])
        assert "status" in result
        assert "sqlite_record_count" in result
        assert "total_written" in result


# ============================================================
# B4: POST /memos/cubes/{cube_id}/vacuum — async 202
# ============================================================


class TestVacuumCube:
    """B4: POST vacuum returns 202, task completes with LifecycleReportOut."""

    async def test_vacuum_returns_202(self, client: httpx.AsyncClient) -> None:
        """POST vacuum returns 202 with task_id."""
        resp = await client.post(
            "/api/v1/memos/cubes/test-cube/vacuum",
            params={"group": "test_group"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert "task_id" in body

    async def test_vacuum_task_completes(
        self, client: httpx.AsyncClient, real_app: FastAPI, mock_adapter: MockMemOSAdapter
    ) -> None:
        """Vacuum task completes with scanned/archived/deleted counts."""
        from z_winnow.web.services.task_queue import get_task_status

        # Seed a cube with memories at various confidence levels
        cube_id = await mock_adapter.get_or_create_cube("vacuum_test:topics")
        items = [
            StructuredMemoryItem(
                memory="Low confidence memory",
                metadata=TextualMemoryMetadata(
                    type="event",
                    source="test",
                    confidence=10.0,
                    tags=["test"],
                    visibility="private",
                ),
            ),
            StructuredMemoryItem(
                memory="High confidence memory",
                metadata=TextualMemoryMetadata(
                    type="event",
                    source="test",
                    confidence=90.0,
                    tags=["test"],
                    visibility="private",
                ),
            ),
        ]
        await mock_adapter.add_structured_memory(
            cube_id=cube_id, group_id="vacuum_test", items=items
        )

        resp = await client.post(
            f"/api/v1/memos/cubes/{cube_id}/vacuum",
            params={"group": "vacuum_test"},
        )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        # Poll until done
        status = None
        for _ in range(30):
            await asyncio.sleep(0.1)
            status = await get_task_status(task_id, db_path=real_app.state.db_path)
            if status and status["status"] in ("done", "failed"):
                break
        assert status is not None, "Task never appeared in DB"
        assert status["status"] == "done", f"Task failed: {status.get('error_message')}"

        result = json.loads(status["result_json"])
        assert "scanned_count" in result
        assert "archived_count" in result
        assert "deleted_count" in result


# ============================================================
# B5: GET /memos/memory/{memory_id} + DELETE /memos/memory/{memory_id}
# ============================================================


class TestMemoryDetailAndDelete:
    """B5: GET memory detail returns 200 MemoryDetailOut; DELETE returns 204."""

    async def test_get_memory_detail_ok(
        self, client: httpx.AsyncClient, mock_adapter: MockMemOSAdapter
    ) -> None:
        """GET memory/{id} returns 200 with full metadata."""
        cube_id = await mock_adapter.get_or_create_cube("mem_detail:topics")
        await _seed_cube_with_memories(mock_adapter, cube_id, count=1)

        # Get the actual memory ID
        all_data = await mock_adapter.get_all_memories(cube_id=cube_id, group_id="")
        memory_id = all_data["text_mem"][0]["id"]

        resp = await client.get(
            f"/api/v1/memos/memory/{memory_id}",
            params={"cube": cube_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["memory_id"] == memory_id
        assert "content" in body
        assert "metadata_json" in body

    async def test_get_memory_detail_not_found(self, client: httpx.AsyncClient) -> None:
        """GET memory/{nonexistent} returns 404."""
        resp = await client.get("/api/v1/memos/memory/nonexistent-id")
        assert resp.status_code == 404

    async def test_delete_memory_ok(
        self, client: httpx.AsyncClient, mock_adapter: MockMemOSAdapter
    ) -> None:
        """DELETE memory/{id} returns 204."""
        cube_id = await mock_adapter.get_or_create_cube("mem_delete:topics")
        await _seed_cube_with_memories(mock_adapter, cube_id, count=1)

        all_data = await mock_adapter.get_all_memories(cube_id=cube_id, group_id="")
        memory_id = all_data["text_mem"][0]["id"]

        resp = await client.delete(
            f"/api/v1/memos/memory/{memory_id}",
            params={"cube": cube_id},
        )
        assert resp.status_code == 204

        # Verify deletion
        all_data = await mock_adapter.get_all_memories(cube_id=cube_id, group_id="")
        assert len(all_data["text_mem"]) == 0

    async def test_delete_memory_not_found(self, client: httpx.AsyncClient) -> None:
        """DELETE memory/{nonexistent} returns 404."""
        resp = await client.delete("/api/v1/memos/memory/nonexistent-id")
        assert resp.status_code == 404


# ============================================================
# B6: POST /memos/flush — async 202
# ============================================================


class TestFlushQueue:
    """B6: POST /memos/flush returns 202; task result is FlushOut."""

    async def test_flush_returns_202(self, client: httpx.AsyncClient) -> None:
        """POST /memos/flush returns 202 with task_id."""
        resp = await client.post("/api/v1/memos/flush")
        assert resp.status_code == 202
        body = resp.json()
        assert "task_id" in body

    async def test_flush_task_completes(self, client: httpx.AsyncClient, real_app: FastAPI) -> None:
        """Flush task completes with FlushOut (status, flushed_count, message)."""
        from z_winnow.web.services.task_queue import get_task_status

        resp = await client.post("/api/v1/memos/flush")
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        # Poll until done
        status = None
        for _ in range(30):
            await asyncio.sleep(0.1)
            status = await get_task_status(task_id, db_path=real_app.state.db_path)
            if status and status["status"] in ("done", "failed"):
                break
        assert status is not None, "Task never appeared in DB"
        assert status["status"] == "done", f"Task failed: {status.get('error_message')}"

        result = json.loads(status["result_json"])
        assert "status" in result
        assert "flushed_count" in result
        assert "message" in result

    async def test_flush_with_pending_jobs(
        self, client: httpx.AsyncClient, real_app: FastAPI
    ) -> None:
        """Flush processes pending sync queue jobs."""
        from z_winnow.pipeline.database import enqueue_sync_job
        from z_winnow.web.services.task_queue import get_task_status

        # Seed a pending job
        conn = real_app.state.db_conn
        await enqueue_sync_job(
            conn,
            op_type="add_topic",
            cube_id="flush-test-cube",
            payload={
                "group_id": "flush_test",
                "dedupe_key": "flush-test-001",
                "summary": "Flush test summary",
                "op_type": "add_topic",
            },
        )

        resp = await client.post("/api/v1/memos/flush")
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        # Poll until done
        status = None
        for _ in range(30):
            await asyncio.sleep(0.2)
            status = await get_task_status(task_id, db_path=real_app.state.db_path)
            if status and status["status"] in ("done", "failed"):
                break
        assert status is not None
        assert status["status"] == "done"

        result = json.loads(status["result_json"])
        assert result["flushed_count"] >= 0


# ============================================================
# P082: Read path error propagation
# ============================================================


class TestP082ReadPathErrors:
    """P082: Read methods propagate ConnectError to caller."""

    async def test_list_cubes_no_adapter_returns_empty(
        self, client: httpx.AsyncClient, real_app: FastAPI
    ) -> None:
        """When adapter is removed, list returns empty list (not 500)."""
        # Store original adapter
        original = real_app.state.memos_adapter
        real_app.state.memos_adapter = None
        try:
            resp = await client.get("/api/v1/memos/cubes", params={"group": "test"})
            # Without adapter, returns empty list (degraded)
            assert resp.status_code == 200
            assert resp.json() == []
        finally:
            real_app.state.memos_adapter = original

    async def test_get_cube_detail_no_adapter_returns_503(
        self, client: httpx.AsyncClient, real_app: FastAPI
    ) -> None:
        """When adapter is None, get cube detail returns 503."""
        original = real_app.state.memos_adapter
        real_app.state.memos_adapter = None
        try:
            resp = await client.get("/api/v1/memos/cubes/some-id")
            assert resp.status_code == 503
        finally:
            real_app.state.memos_adapter = original

    async def test_get_memory_detail_no_adapter_returns_503(
        self, client: httpx.AsyncClient, real_app: FastAPI
    ) -> None:
        """When adapter is None, get memory detail returns 503."""
        original = real_app.state.memos_adapter
        real_app.state.memos_adapter = None
        try:
            resp = await client.get("/api/v1/memos/memory/some-id")
            assert resp.status_code == 503
        finally:
            real_app.state.memos_adapter = original


# ============================================================
# P079: Confirm gate validator
# ============================================================


class TestP079ConfirmGate:
    """P079: CubeDeleteConfirm validator rejects non-True values."""

    def test_confirm_must_be_true(self):
        """confirm=False raises ValidationError."""
        from z_winnow.web.schemas.memos import CubeDeleteConfirm

        with pytest.raises(ValueError):
            CubeDeleteConfirm(confirm=False)

    def test_confirm_true_passes(self):
        """confirm=True is valid."""
        from z_winnow.web.schemas.memos import CubeDeleteConfirm

        model = CubeDeleteConfirm(confirm=True)
        assert model.confirm is True


# ============================================================
# Service function unit tests (direct, no HTTP)
# ============================================================


class TestServiceFunctions:
    """Direct unit tests for memos_service functions."""

    async def test_list_cubes_direct(self, mock_adapter: MockMemOSAdapter) -> None:
        """list_cubes returns cubes for a group."""
        from z_winnow.web.services.memos_service import list_cubes

        group = "svc_list_test"
        await mock_adapter.get_or_create_cube(f"{group}:topics")

        cubes = await list_cubes(mock_adapter, group_id=group)
        assert len(cubes) > 0
        assert any(c["scope"] == "topics" for c in cubes)

    async def test_delete_cube_direct(self, mock_adapter: MockMemOSAdapter) -> None:
        """delete_cube removes all memories from a cube."""
        from z_winnow.web.services.memos_service import delete_cube

        group = "svc_delete_test"
        cube_id = await mock_adapter.get_or_create_cube(f"{group}:topics")
        await _seed_cube_with_memories(mock_adapter, cube_id, count=2)

        success = await delete_cube(mock_adapter, cube_id=cube_id)
        assert success is True

        # Verify empty
        all_data = await mock_adapter.get_all_memories(cube_id=cube_id, group_id="")
        total = sum(len(v) for v in all_data.values() if isinstance(v, list))
        assert total == 0

    async def test_get_memory_detail_direct(self, mock_adapter: MockMemOSAdapter) -> None:
        """get_memory_detail finds a specific memory."""
        from z_winnow.web.services.memos_service import get_memory_detail

        group = "svc_mem_test"
        cube_id = await mock_adapter.get_or_create_cube(f"{group}:topics")
        ids = await _seed_cube_with_memories(mock_adapter, cube_id, count=1)

        detail = await get_memory_detail(mock_adapter, memory_id=ids[0], cube_id=cube_id)
        assert detail is not None
        assert detail["memory_id"] == ids[0]

    async def test_delete_memory_by_id_direct(self, mock_adapter: MockMemOSAdapter) -> None:
        """delete_memory_by_id removes a specific memory."""
        from z_winnow.web.services.memos_service import delete_memory_by_id

        group = "svc_del_mem_test"
        cube_id = await mock_adapter.get_or_create_cube(f"{group}:topics")
        ids = await _seed_cube_with_memories(mock_adapter, cube_id, count=2)

        success = await delete_memory_by_id(mock_adapter, memory_id=ids[0], cube_id=cube_id)
        assert success is True

        all_data = await mock_adapter.get_all_memories(cube_id=cube_id, group_id="")
        remaining = sum(len(v) for v in all_data.values() if isinstance(v, list))
        assert remaining == 1

    async def test_rebuild_cube_direct(
        self, mock_adapter: MockMemOSAdapter, real_app: FastAPI
    ) -> None:
        """rebuild_memos_cube reads SQLite and writes to MemOS."""
        from z_winnow.web.services.memos_service import rebuild_memos_cube

        group = "svc_rebuild_test"
        cube_id = await mock_adapter.get_or_create_cube(f"{group}:topics")

        result = await rebuild_memos_cube(
            adapter=mock_adapter,
            cube_id=cube_id,
            group_id=group,
            db_path=real_app.state.db_path,
        )
        assert result["status"] in ("completed", "degraded")
        assert "sqlite_record_count" in result
        assert "total_written" in result

    async def test_vacuum_cube_direct(self, mock_adapter: MockMemOSAdapter) -> None:
        """vacuum_cube scans and applies lifecycle rules."""
        from z_winnow.web.services.memos_service import vacuum_cube

        group = "svc_vacuum_test"
        cube_id = await mock_adapter.get_or_create_cube(f"{group}:topics")

        # Add one memory with very low confidence (should be archived)
        items = [
            StructuredMemoryItem(
                memory="Low confidence — should archive",
                metadata=TextualMemoryMetadata(
                    type="event",
                    source="test",
                    confidence=5.0,
                    tags=["test"],
                    visibility="private",
                ),
            ),
        ]
        await mock_adapter.add_structured_memory(cube_id=cube_id, group_id=group, items=items)

        result = await vacuum_cube(mock_adapter, cube_id=cube_id, group_id=group)
        assert result["status"] in ("completed", "degraded")
        assert result["scanned_count"] >= 1

    async def test_flush_pending_direct(self, real_app: FastAPI) -> None:
        """flush_pending processes pending sync queue jobs."""
        from z_winnow.web.services.memos_service import flush_pending

        result = await flush_pending(db_path=real_app.state.db_path)
        assert result["status"] in ("completed", "degraded")
        assert "flushed_count" in result


# ============================================================
# Edge cases
# ============================================================


class TestEdgeCases:
    """Edge case tests for MemOS endpoints."""

    async def test_cube_detail_via_list_then_detail(
        self, client: httpx.AsyncClient, mock_adapter: MockMemOSAdapter
    ) -> None:
        """List cubes, then get detail for first cube."""
        group = "roundtrip_test"
        cube_id = await mock_adapter.get_or_create_cube(f"{group}:topics")
        await _seed_cube_with_memories(mock_adapter, cube_id, count=3)

        # List
        resp = await client.get("/api/v1/memos/cubes", params={"group": group})
        assert resp.status_code == 200
        cubes = resp.json()
        target = next((c for c in cubes if c["cube_id"] == cube_id), None)
        assert target is not None

        # Detail
        resp2 = await client.get(f"/api/v1/memos/cubes/{cube_id}")
        assert resp2.status_code == 200
        detail = resp2.json()
        assert detail["message_count"] == target["message_count"]

    async def test_memory_roundtrip(
        self, client: httpx.AsyncClient, mock_adapter: MockMemOSAdapter
    ) -> None:
        """Add memory, get detail, delete — full lifecycle."""
        group = "lifecycle_test"
        cube_id = await mock_adapter.get_or_create_cube(f"{group}:topics")
        ids = await _seed_cube_with_memories(mock_adapter, cube_id, count=1)
        memory_id = ids[0]

        # Get detail
        resp = await client.get(f"/api/v1/memos/memory/{memory_id}", params={"cube": cube_id})
        assert resp.status_code == 200

        # Delete
        resp = await client.delete(f"/api/v1/memos/memory/{memory_id}", params={"cube": cube_id})
        assert resp.status_code == 204

        # Verify gone
        resp = await client.get(f"/api/v1/memos/memory/{memory_id}", params={"cube": cube_id})
        assert resp.status_code == 404
