"""E2E-API-01: Groups CRUD full lifecycle test.

Covers the complete create → list → get → update → delete workflow
for the /api/v1/groups endpoint family.

Mode A: bare FastAPI + :memory: SQLite (P078).
  - _app = FastAPI() + _app.include_router(api_router)
  - aiosqlite.connect(":memory:") with manual DDL
  - app.state.db_conn / db_path / reports_dir
  - AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
  - _env_isolation autouse fixture

# P054: Route layer parse-validate-delegate — routes tested via HTTP
# P078: Real SQLite :memory: for DB-backed tests
# P050: Parameterized SQL — tested indirectly via service layer
# A008: All test data pre-initialized before assertions
"""

from __future__ import annotations

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from z_winnow.web.routes import api_router

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def app() -> FastAPI:
    """Create a test FastAPI app with the api_router included."""
    _app = FastAPI()
    _app.include_router(api_router)
    return _app


@pytest.fixture
async def db_conn():
    """Provide an in-memory SQLite connection with groups table."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row

    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id              TEXT PRIMARY KEY,
            display_name          TEXT NOT NULL,
            chatroom_id           TEXT NOT NULL,
            output_dir            TEXT,
            feishu_enabled        INTEGER DEFAULT 0,
            custom_prompt_hints   TEXT,
            is_active             INTEGER DEFAULT 1,
            daily_report_enabled  INTEGER DEFAULT 1,
            daily_schedule_cron   TEXT,
            created_at            TEXT DEFAULT (datetime('now')),
            updated_at            TEXT DEFAULT (datetime('now')),
            created_by            TEXT
        );
    """)
    yield conn
    await conn.close()


@pytest.fixture
async def client(app: FastAPI, db_conn: aiosqlite.Connection):
    """Async test client with db_conn on app.state."""
    app.state.db_conn = db_conn
    app.state.db_path = ":memory:"
    app.state.reports_dir = "."

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# P012: Env isolation
@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """P012: Isolate environment for each test."""
    monkeypatch.setenv("WEFLOW_MOCK_MODE", "true")
    monkeypatch.delenv("WINNOW_WEB_API_KEY", raising=False)
    monkeypatch.delenv("WEB_API_KEY", raising=False)


# ============================================================
# Tests
# ============================================================


class TestGroupsCRUDWorkflow:
    """Full lifecycle: create → list → get → update → delete for groups."""

    @pytest.mark.asyncio
    async def test_full_crud_lifecycle(
        self, client: AsyncClient, db_conn: aiosqlite.Connection
    ) -> None:
        """Exercise the complete groups CRUD workflow end-to-end."""

        # --- Step 1: Create Group A → 201 ---
        resp = await client.post(
            "/api/v1/groups",
            json={"display_name": "Workflow Group A", "chatroom_id": "wf-a@chatroom"},
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        group_a = resp.json()
        group_id = group_a["group_id"]
        assert group_a["display_name"] == "Workflow Group A"
        assert group_a["chatroom_id"] == "wf-a@chatroom"
        assert group_a["is_active"] == 1

        # --- Step 2: Create Group B → 201 ---
        resp = await client.post(
            "/api/v1/groups",
            json={"display_name": "Workflow Group B", "chatroom_id": "wf-b@chatroom"},
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        group_b = resp.json()
        assert group_b["display_name"] == "Workflow Group B"

        # --- Step 3: List groups → 200, total >= 2 ---
        resp = await client.get("/api/v1/groups")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["total"] >= 2, f"Expected total >= 2, got {body['total']}"
        assert len(body["items"]) >= 2

        # --- Step 4: Search groups → total >= 2 ---
        resp = await client.get("/api/v1/groups", params={"search": "Workflow"})
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["total"] >= 2, f"Expected search total >= 2, got {body['total']}"

        # --- Step 5: Get group by ID → 200, display_name correct ---
        resp = await client.get(f"/api/v1/groups/{group_id}")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["group_id"] == group_id
        assert body["display_name"] == "Workflow Group A"

        # --- Step 6: Update group → 200 ---
        resp = await client.put(
            f"/api/v1/groups/{group_id}",
            json={"display_name": "Renamed Group"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["group_id"] == group_id
        assert body["display_name"] == "Renamed Group"

        # --- Step 7: Verify update persisted ---
        resp = await client.get(f"/api/v1/groups/{group_id}")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["display_name"] == "Renamed Group"

        # --- Step 8: Delete group → 204 ---
        resp = await client.delete(f"/api/v1/groups/{group_id}")
        assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text}"

        # --- Step 9: Get deleted group → 404 ---
        resp = await client.get(f"/api/v1/groups/{group_id}")
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"

        # --- Step 10: Delete again → 404 ---
        resp = await client.delete(f"/api/v1/groups/{group_id}")
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
