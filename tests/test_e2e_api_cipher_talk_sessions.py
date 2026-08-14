"""E2E-API: CipherTalk sessions endpoint for the「新建群」picker.

Covers GET /api/v1/groups/sessions — exposes real chatrooms (with display_name
from CipherTalk) so the web UI picks a group instead of typing its name.

Mode A: bare FastAPI + :memory: SQLite + AsyncMock cipher_talk_client (P078).

# P054: Route layer tested via HTTP
# P014: Service degrades to available=False instead of raising
# A008: All test data pre-initialized before assertions
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, ConnectError

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
    """Provide an in-memory SQLite connection with the groups table."""
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
def cipher_mock():
    """AsyncMock standing in for the CipherTalk data client."""
    return AsyncMock()


@pytest.fixture
async def client(app: FastAPI, db_conn: aiosqlite.Connection, cipher_mock):
    """Async test client with db_conn + cipher_talk_client on app.state."""
    app.state.db_conn = db_conn
    app.state.db_path = ":memory:"
    app.state.reports_dir = "."
    app.state.cipher_talk_client = cipher_mock

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate environment for each test."""
    monkeypatch.setenv("WEFLOW_MOCK_MODE", "true")
    monkeypatch.delenv("WINNOW_WEB_API_KEY", raising=False)
    monkeypatch.delenv("WEB_API_KEY", raising=False)


# ============================================================
# Tests
# ============================================================


class TestCipherTalkSessions:
    """GET /api/v1/groups/sessions — picker data + degradation."""

    @pytest.mark.asyncio
    async def test_lists_chatrooms_and_marks_registered(
        self, client: AsyncClient, db_conn: aiosqlite.Connection, cipher_mock
    ) -> None:
        """Real chatrooms returned; private chats filtered; registered flagged;
        unregistered sorted first."""
        # Pre-register one group whose chatroom_id matches a mock session
        await db_conn.execute(
            "INSERT INTO groups (group_id, display_name, chatroom_id) VALUES (?, ?, ?)",
            ("g_reg", "Reg Group", "reg@chatroom"),
        )
        await db_conn.commit()

        cipher_mock.get_sessions.return_value = [
            {"username": "reg@chatroom", "displayName": "Reg Group"},  # registered
            {"username": "new@chatroom", "displayName": "New Group"},  # not registered
            {"username": "wxid_priv", "displayName": "Private Person"},  # private chat
        ]

        resp = await client.get("/api/v1/groups/sessions")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["available"] is True
        rooms = body["sessions"]
        # Private chat filtered out -> only 2 chatrooms
        assert len(rooms) == 2
        cids = [r["chatroom_id"] for r in rooms]
        assert set(cids) == {"reg@chatroom", "new@chatroom"}
        assert "wxid_priv" not in cids

        by_cid = {r["chatroom_id"]: r for r in rooms}
        assert by_cid["reg@chatroom"]["is_registered"] is True
        assert by_cid["reg@chatroom"]["display_name"] == "Reg Group"
        assert by_cid["new@chatroom"]["is_registered"] is False
        # Unregistered (False) sorts before registered (True)
        assert rooms[0]["chatroom_id"] == "new@chatroom"

    @pytest.mark.asyncio
    async def test_unreachable_degrades_to_unavailable(
        self, client: AsyncClient, cipher_mock
    ) -> None:
        """CipherTalk failure -> 200 with available=False, empty list (no raise)."""
        cipher_mock.get_sessions.side_effect = ConnectError("boom")

        resp = await client.get("/api/v1/groups/sessions")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is False
        assert body["sessions"] == []

    @pytest.mark.asyncio
    async def test_no_client_attached_is_unavailable(
        self, app: FastAPI, db_conn: aiosqlite.Connection
    ) -> None:
        """Lifespan did not attach a client -> available=False, empty list."""
        app.state.db_conn = db_conn
        app.state.db_path = ":memory:"
        app.state.reports_dir = "."
        # NOTE: cipher_talk_client intentionally NOT set on app.state

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/api/v1/groups/sessions")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is False
        assert body["sessions"] == []

    @pytest.mark.asyncio
    async def test_sessions_route_not_shadowed_by_group_id(
        self, client: AsyncClient, cipher_mock
    ) -> None:
        """GET /groups/sessions must not be captured as /groups/{group_id}."""
        cipher_mock.get_sessions.return_value = []

        resp = await client.get("/api/v1/groups/sessions")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Sessions envelope, not a single GroupOut / 404
        assert "sessions" in body and "available" in body
        assert "group_id" not in body
