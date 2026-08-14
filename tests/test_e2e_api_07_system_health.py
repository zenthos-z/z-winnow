"""E2E-API-07: System health, info, config, and overview workflow test.

Covers the full read-only system endpoints lifecycle:
  - GET /api/v1/health
  - GET /api/v1/system/info
  - GET /api/v1/system/config
  - GET /api/v1/overview (empty DB, then seeded)

Mode A: bare FastAPI + :memory: SQLite (P078).
  - _app = FastAPI() + _app.include_router(api_router)
  - aiosqlite.connect(":memory:") with manual DDL
  - app.state.db_conn / db_path / reports_dir
  - AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
  - _env_isolation autouse fixture

# P054: Route layer parse-validate-delegate — routes tested via HTTP
# P078: Real SQLite :memory: for DB-backed tests
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
    """Provide an in-memory SQLite connection with all tables required by
    health, system, and overview endpoints.

    Tables: groups, raw_messages, parsed_contexts, topic_summaries,
    report_versions, feedback_events, pipeline_runs.
    """
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
        CREATE TABLE IF NOT EXISTS raw_messages (
            serverID TEXT PRIMARY KEY,
            date TEXT,
            group_id TEXT,
            sender TEXT,
            content TEXT,
            msg_type TEXT DEFAULT 'text',
            image_path TEXT,
            sanitized INTEGER DEFAULT 0,
            raw_json TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS parsed_contexts (
            context_id TEXT PRIMARY KEY,
            date TEXT,
            group_id TEXT,
            server_ids TEXT,
            context_text TEXT,
            token_count INTEGER,
            source_subagent TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS topic_summaries (
            summary_id TEXT PRIMARY KEY,
            date TEXT,
            group_id TEXT,
            topic_name TEXT,
            topic_id TEXT,
            summary_text TEXT,
            context_ids TEXT,
            source_server_ids TEXT,
            confidence REAL,
            model_used TEXT,
            lifecycle TEXT DEFAULT 'emerging',
            matched_core_topic_id TEXT,
            conclusion TEXT,
            description TEXT,
            participants TEXT,
            trend TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS report_versions (
            version_id TEXT PRIMARY KEY,
            report_id TEXT,
            group_id TEXT,
            date TEXT,
            version_number INTEGER,
            content TEXT,
            content_changed INTEGER DEFAULT 0,
            source TEXT,
            build_duration_s REAL,
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS feedback_events (
            feedback_id TEXT PRIMARY KEY,
            created_at TEXT,
            group_id TEXT,
            date TEXT,
            report_id TEXT,
            target_type TEXT,
            target_id TEXT,
            target_path TEXT,
            target_version_id TEXT,
            target_topic_id TEXT,
            produced_version_id TEXT,
            memos_cube_id TEXT,
            memos_node_id TEXT,
            archived_memos_id TEXT,
            status TEXT DEFAULT 'active',
            rolled_back_at TEXT,
            rolled_back_by TEXT,
            signal TEXT,
            severity TEXT DEFAULT 'info',
            rating TEXT,
            tags TEXT,
            correction_mode TEXT,
            original_text TEXT,
            corrected_text TEXT,
            correction_note TEXT,
            reporter TEXT,
            consumed_at TEXT,
            consumed_by TEXT
        );
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id TEXT PRIMARY KEY,
            group_id TEXT,
            date TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            updated_at TEXT
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
# Seed helpers
# ============================================================


async def _seed_system_data(db: aiosqlite.Connection) -> None:
    """Seed: 3 groups (2 active, 1 inactive), 15 raw_messages for the
    first active group, 2 topic_summaries, and 1 report_version.
    """
    # 3 groups: 2 active, 1 inactive
    await db.execute(
        "INSERT INTO groups (group_id, display_name, chatroom_id, is_active) "
        "VALUES ('g_active_1', 'Active Group One', 'room1@chatroom', 1)"
    )
    await db.execute(
        "INSERT INTO groups (group_id, display_name, chatroom_id, is_active) "
        "VALUES ('g_active_2', 'Active Group Two', 'room2@chatroom', 1)"
    )
    await db.execute(
        "INSERT INTO groups (group_id, display_name, chatroom_id, is_active) "
        "VALUES ('g_inactive', 'Inactive Group', 'room3@chatroom', 0)"
    )

    # 15 raw_messages for g_active_1
    for i in range(15):
        await db.execute(
            "INSERT INTO raw_messages (serverID, date, group_id, sender, content) "
            "VALUES (?, '20260607', 'g_active_1', 'User', ?)",
            (f"sid-msg-{i:03d}", f"Message content {i}"),
        )

    # 2 topic_summaries
    await db.execute(
        "INSERT INTO topic_summaries "
        "(summary_id, date, group_id, topic_name, topic_id, summary_text, "
        " context_ids, source_server_ids, lifecycle) "
        "VALUES ('sum-sys-1', '20260607', 'g_active_1', 'System Topic A', 't-sys-a', "
        " 'summary a', '[]', '[]', 'emerging')"
    )
    await db.execute(
        "INSERT INTO topic_summaries "
        "(summary_id, date, group_id, topic_name, topic_id, summary_text, "
        " context_ids, source_server_ids, lifecycle) "
        "VALUES ('sum-sys-2', '20260607', 'g_active_1', 'System Topic B', 't-sys-b', "
        " 'summary b', '[]', '[]', 'active')"
    )

    # 1 report_version
    await db.execute(
        "INSERT INTO report_versions (version_id, report_id, group_id, date, "
        " version_number, content) "
        "VALUES ('ver-sys-1', 'rpt-sys-1', 'g_active_1', '20260607', 1, 'report content')"
    )

    await db.commit()


# ============================================================
# Tests
# ============================================================


class TestSystemHealthWorkflow:
    """Full workflow: health → system info → config → overview (empty + seeded)."""

    @pytest.mark.asyncio
    async def test_health_overview_system_workflow(
        self, client: AsyncClient, db_conn: aiosqlite.Connection
    ) -> None:
        """Exercise the complete system endpoints workflow end-to-end."""

        # --- Step 1: GET /health → 200, has "status" key ---
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "status" in body, f"Response missing 'status' key: {body}"

        # --- Step 2: GET /system/info → 200 ---
        resp = await client.get("/api/v1/system/info")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "status" in body

        # --- Step 3: GET /system/config → 200, no sensitive keys ---
        resp = await client.get("/api/v1/system/config")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body_text = resp.text.lower()
        for secret_key in ("api_key", "secret", "password"):
            assert secret_key not in body_text, (
                f"Sensitive key '{secret_key}' found in /system/config response"
            )

        # --- Step 4: GET /overview → 200, all counts zero (empty DB) ---
        resp = await client.get("/api/v1/overview")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["total_messages"] == 0, f"Expected 0 messages, got {body['total_messages']}"
        assert body["total_groups"] == 0, f"Expected 0 groups, got {body['total_groups']}"
        assert body["total_topics"] == 0, f"Expected 0 topics, got {body['total_topics']}"
        assert body["total_reports"] == 0, f"Expected 0 reports, got {body['total_reports']}"

        # --- Step 5: Seed data ---
        await _seed_system_data(db_conn)

        # --- Step 6: GET /overview → non-zero counts ---
        resp = await client.get("/api/v1/overview")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["total_messages"] > 0, (
            f"Expected total_messages > 0 after seeding, got {body['total_messages']}"
        )
        assert body["total_groups"] == 2, (
            f"Expected total_groups == 2 (active only), got {body['total_groups']}"
        )
        assert body["total_topics"] > 0, (
            f"Expected total_topics > 0 after seeding, got {body['total_topics']}"
        )
        assert body["total_reports"] > 0, (
            f"Expected total_reports > 0 after seeding, got {body['total_reports']}"
        )
