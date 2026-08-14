"""E2E-API-10: MemOS full lifecycle workflow — health, search, seed, CRUD, flush.

Mode B: Real lifespan + tmp_path file SQLite.
Full round-trip test exercising the complete MemOS management API:
health check → search → seed cube → list cubes → cube detail → memory
detail → delete memory → delete cube → rebuild → flush.

Usage:
    python -m poetry run pytest tests/test_e2e_api_10_memos_lifecycle.py -v
"""

from __future__ import annotations

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
    """Isolate environment: mock mode, test env, no API key, blank paths."""
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
# Mode B fixtures: real lifespan + tmp_path file SQLite
# ============================================================


def _build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a fresh FastAPI app with real lifespan pointing to tmp_path DB."""
    from z_winnow.config.settings import reset_settings
    from z_winnow.web.app import lifespan
    from z_winnow.web.routes import api_router

    db_path = str(tmp_path / "e2e.db")
    reports_dir = str(tmp_path / "reports")
    Path(reports_dir).mkdir(exist_ok=True)

    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_REPORTS_DIR", reports_dir)
    reset_settings()

    fresh = FastAPI(title="e2e-api-test", lifespan=lifespan)
    fresh.include_router(api_router)
    return fresh


@pytest.fixture
async def real_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Yield a running app with real lifespan (DB + memos worker)."""
    from z_winnow.web.app import lifespan

    app = _build_app(tmp_path, monkeypatch)
    async with lifespan(app):
        yield app


@pytest.fixture
async def client(real_app: FastAPI):
    """httpx AsyncClient wired to the real app."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app),
        base_url="http://test",
    ) as c:
        yield c


@pytest.fixture
def mock_adapter(real_app: FastAPI) -> MockMemOSAdapter:
    """Get the MockMemOSAdapter from app state."""
    return real_app.state.memos_adapter


# ============================================================
# Seed helper
# ============================================================


async def _seed_cube(
    adapter: MockMemOSAdapter,
    cube_id: str,
    group_id: str = "",
    count: int = 3,
) -> list[str]:
    """Seed a cube with test memories. Returns list of memory IDs."""
    items = [
        StructuredMemoryItem(
            memory=f"Test memory {i} for e2e",
            metadata=TextualMemoryMetadata(
                type="event",
                source="test",
                confidence=80.0,
                tags=["test"],
                visibility="private",
            ),
        )
        for i in range(count)
    ]
    await adapter.add_structured_memory(cube_id=cube_id, group_id=group_id, items=items)

    all_data = await adapter.get_all_memories(cube_id=cube_id, group_id=group_id)
    ids: list[str] = []
    for key in ("text_mem", "act_mem", "para_mem"):
        for item in all_data.get(key, []):
            if isinstance(item, dict):
                ids.append(item.get("id", ""))
    return ids


# ============================================================
# E2E lifecycle workflow test
# ============================================================


class TestMemosLifecycleWorkflow:
    """E2E: Full MemOS lifecycle — health, search, seed, CRUD, rebuild, flush."""

    @pytest.mark.asyncio
    async def test_memos_full_lifecycle(
        self,
        client: httpx.AsyncClient,
        real_app: FastAPI,
        mock_adapter: MockMemOSAdapter,
    ) -> None:
        """Steps 1-10: Complete MemOS management API workflow."""

        # Step 1: GET /api/v1/memos/status → 200, has "status" key
        resp = await client.get("/api/v1/memos/status")
        assert resp.status_code == 200, (
            f"Expected 200 for memos status, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "status" in data, f"Response missing 'status' key: {data}"

        # Step 2: POST /api/v1/memos/search → 200, has results or total key
        resp = await client.post(
            "/api/v1/memos/search",
            json={"query": "test"},
        )
        assert resp.status_code == 200, (
            f"Expected 200 for memos search, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "results" in data or "total" in data, (
            f"Response missing 'results' or 'total' key: {data}"
        )

        # Step 3: Seed cube with 3 memories
        # Use get_or_create_cube to get the real cube_id that matches the
        # service layer's scope format ("g-wf10:topics").
        # Use group_id="" for seeding so get_cube_detail (which queries with
        # group_id="") can find the memories.
        actual_cube_id = await mock_adapter.get_or_create_cube("g-wf10:topics")
        memory_ids = await _seed_cube(mock_adapter, actual_cube_id, "", 3)
        assert len(memory_ids) >= 1, (
            f"Expected at least 1 memory ID from seed, got {len(memory_ids)}"
        )
        first_memory_id = memory_ids[0]

        # Step 4: GET /api/v1/memos/cubes?group=g-wf10 → 200, list non-empty
        resp = await client.get("/api/v1/memos/cubes", params={"group": "g-wf10"})
        assert resp.status_code == 200, (
            f"Expected 200 for list cubes, got {resp.status_code}: {resp.text}"
        )
        cubes = resp.json()
        assert isinstance(cubes, list), f"Expected list, got {type(cubes)}"
        assert len(cubes) > 0, "Expected non-empty cubes list for g-wf10"

        # Step 5: GET /api/v1/memos/cubes/{actual_cube_id} → 200, has items
        resp = await client.get(f"/api/v1/memos/cubes/{actual_cube_id}")
        assert resp.status_code == 200, (
            f"Expected 200 for cube detail, got {resp.status_code}: {resp.text}"
        )
        detail = resp.json()
        assert detail["cube_id"] == actual_cube_id
        assert detail["message_count"] >= 1, (
            f"Expected message_count >= 1, got {detail['message_count']}"
        )

        # Step 6: GET /api/v1/memos/memory/{memory_ids[0]} → 200, has memory detail
        resp = await client.get(
            f"/api/v1/memos/memory/{first_memory_id}",
            params={"cube": actual_cube_id},
        )
        assert resp.status_code == 200, (
            f"Expected 200 for memory detail, got {resp.status_code}: {resp.text}"
        )
        mem_detail = resp.json()
        assert mem_detail["memory_id"] == first_memory_id
        assert "content" in mem_detail

        # Step 7: DELETE /api/v1/memos/memory/{memory_ids[0]} → 204
        resp = await client.delete(
            f"/api/v1/memos/memory/{first_memory_id}",
            params={"cube": actual_cube_id},
        )
        assert resp.status_code == 204, (
            f"Expected 204 for memory delete, got {resp.status_code}: {resp.text}"
        )

        # Step 8: DELETE /api/v1/memos/cubes/{actual_cube_id} body={confirm:true} → 204
        resp = await client.request(
            "DELETE",
            f"/api/v1/memos/cubes/{actual_cube_id}",
            json={"confirm": True},
        )
        assert resp.status_code == 204, (
            f"Expected 204 for cube delete, got {resp.status_code}: {resp.text}"
        )

        # Step 9: POST /api/v1/memos/cubes/{actual_cube_id}/rebuild → 202, has task_id
        resp = await client.post(
            f"/api/v1/memos/cubes/{actual_cube_id}/rebuild",
            params={"group": "g-wf10"},
        )
        assert resp.status_code == 202, (
            f"Expected 202 for cube rebuild, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "task_id" in data, f"Response missing task_id: {data}"

        # Step 10: POST /api/v1/memos/flush → 202
        resp = await client.post("/api/v1/memos/flush")
        assert resp.status_code == 202, (
            f"Expected 202 for memos flush, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "task_id" in data, f"Response missing task_id: {data}"
