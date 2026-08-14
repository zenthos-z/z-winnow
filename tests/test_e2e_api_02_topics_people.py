"""E2E API test: Core topics + Key people configuration workflow.

Mode A (bare FastAPI + :memory: SQLite) — follows exact patterns from test_web_api.py.

Tests verify full CRUD lifecycle for core_topics and key_people endpoints,
including creation, listing, update, soft-delete, and sender aggregation
from raw_messages.

# P011: 1:1 AC-to-test mapping -- each step documented inline.
# L100: Real FastAPI request/response cycle, not mocked HTTP.
# P078: Real SQLite :memory: for DB-backed tests.
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

_DDL = """
CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    chatroom_id TEXT NOT NULL DEFAULT '',
    output_dir TEXT,
    feishu_enabled INTEGER DEFAULT 0,
    custom_prompt_hints TEXT,
    is_active INTEGER DEFAULT 1,
    daily_report_enabled INTEGER DEFAULT 1,
    daily_schedule_cron TEXT,
    created_at TEXT,
    updated_at TEXT,
    created_by TEXT
);
CREATE TABLE IF NOT EXISTS group_members (
    member_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    wxid TEXT,
    role TEXT DEFAULT 'member',
    weight REAL DEFAULT 1.0,
    note TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS core_topics (
    core_topic_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    description TEXT,
    keywords TEXT,
    priority INTEGER DEFAULT 1,
    is_active INTEGER DEFAULT 1,
    last_matched_date TEXT,
    match_count INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    created_by TEXT
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
"""

_SEED_GROUP = """
INSERT INTO groups (group_id, display_name, chatroom_id, is_active, created_at, updated_at)
VALUES ('g-wf2', 'Workflow Test Group 2', 'room_wf2@chatroom', 1, '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z');
"""

# 9 messages: alice=5, bob=3, carol=1
_SEED_MESSAGES = """
INSERT INTO raw_messages (serverID, date, group_id, sender, content, msg_type, created_at) VALUES
    ('msg-001', '20260601', 'g-wf2', 'alice', 'hello 1', 'text', '2026-06-01T08:00:00Z'),
    ('msg-002', '20260601', 'g-wf2', 'alice', 'hello 2', 'text', '2026-06-01T08:01:00Z'),
    ('msg-003', '20260601', 'g-wf2', 'alice', 'hello 3', 'text', '2026-06-01T08:02:00Z'),
    ('msg-004', '20260601', 'g-wf2', 'alice', 'hello 4', 'text', '2026-06-01T08:03:00Z'),
    ('msg-005', '20260601', 'g-wf2', 'alice', 'hello 5', 'text', '2026-06-01T08:04:00Z'),
    ('msg-006', '20260601', 'g-wf2', 'bob',   'msg b1',  'text', '2026-06-01T09:00:00Z'),
    ('msg-007', '20260601', 'g-wf2', 'bob',   'msg b2',  'text', '2026-06-01T09:01:00Z'),
    ('msg-008', '20260601', 'g-wf2', 'bob',   'msg b3',  'text', '2026-06-01T09:02:00Z'),
    ('msg-009', '20260601', 'g-wf2', 'carol', 'msg c1',  'text', '2026-06-01T10:00:00Z');
"""

# P1-3: list_key_people now reads group_members (LEFT JOIN raw_messages), so the
# three senders must also be registered members. wxid matches the raw senders.
_SEED_MEMBERS = """
INSERT INTO group_members (member_id, group_id, name, wxid, role, is_active, created_at) VALUES
    ('gm-alice', 'g-wf2', 'Alice', 'alice', 'member', 1, '2026-06-01T00:00:00Z'),
    ('gm-bob',   'g-wf2', 'Bob',   'bob',   'member', 1, '2026-06-01T00:00:00Z'),
    ('gm-carol', 'g-wf2', 'Carol', 'carol', 'member', 1, '2026-06-01T00:00:00Z');
"""


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """P012: Isolate environment for each test -- clear API keys, set mock mode."""
    monkeypatch.setenv("WEFLOW_MOCK_MODE", "true")
    monkeypatch.delenv("WINNOW_WEB_API_KEY", raising=False)
    monkeypatch.delenv("WEB_API_KEY", raising=False)


@pytest.fixture
def app() -> FastAPI:
    """Create a test FastAPI app with the api_router included."""
    _app = FastAPI()
    _app.include_router(api_router)
    return _app


@pytest.fixture
async def db_conn():
    """Provide an in-memory SQLite connection with schema + seed data."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_DDL)
    await conn.executescript(_SEED_GROUP)
    await conn.executescript(_SEED_MESSAGES)
    await conn.executescript(_SEED_MEMBERS)
    yield conn
    await conn.close()


@pytest.fixture
async def client(app: FastAPI, db_conn: aiosqlite.Connection):
    """Async test client with db_conn and db_path on app.state."""
    app.state.db_conn = db_conn
    app.state.db_path = ":memory:"
    app.state.reports_dir = "."

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ============================================================
# Test: Topics + People Workflow
# ============================================================


class TestTopicsPeopleWorkflow:
    """Full CRUD workflow for core_topics and key_people endpoints."""

    async def test_topics_and_people_workflow(self, client: AsyncClient) -> None:
        """Step-by-step workflow covering core_topics CRUD + key_people operations."""

        # --- Step 1: GET /api/v1/key-people?group_id=g-wf2&date=20260601 ---
        # Expect 200 with 3 senders sorted by message_count desc (alice=5 first)
        resp = await client.get(
            "/api/v1/key-people",
            params={"group_id": "g-wf2", "date": "20260601"},
        )
        assert resp.status_code == 200, f"Step 1: Expected 200, got {resp.status_code}: {resp.text}"
        people = resp.json()
        assert len(people) == 3, f"Step 1: Expected 3 senders, got {len(people)}"
        assert people[0]["sender"] == "alice", (
            f"Step 1: alice should be first, got {people[0]['sender']}"
        )
        assert people[0]["message_count"] == 5, (
            f"Step 1: alice should have 5 messages, got {people[0]['message_count']}"
        )
        assert people[1]["sender"] == "bob"
        assert people[1]["message_count"] == 3
        assert people[2]["sender"] == "carol"
        assert people[2]["message_count"] == 1

        # --- Step 2: POST /api/v1/core-topics {group_id:"g-wf2", name:"API Design"} ---
        resp = await client.post(
            "/api/v1/core-topics",
            json={"group_id": "g-wf2", "name": "API Design"},
        )
        assert resp.status_code == 201, f"Step 2: Expected 201, got {resp.status_code}: {resp.text}"
        topic1 = resp.json()
        topic_id_1 = topic1["core_topic_id"]
        assert topic1["name"] == "API Design"
        assert topic1["group_id"] == "g-wf2"
        assert topic1["is_active"] == 1

        # --- Step 3: POST /api/v1/core-topics {group_id:"g-wf2", name:"Performance"} ---
        resp = await client.post(
            "/api/v1/core-topics",
            json={"group_id": "g-wf2", "name": "Performance"},
        )
        assert resp.status_code == 201, f"Step 3: Expected 201, got {resp.status_code}: {resp.text}"
        topic2 = resp.json()
        topic_id_2 = topic2["core_topic_id"]
        assert topic2["name"] == "Performance"

        # --- Step 4: GET /api/v1/core-topics?group_id=g-wf2 ---
        resp = await client.get(
            "/api/v1/core-topics",
            params={"group_id": "g-wf2"},
        )
        assert resp.status_code == 200, f"Step 4: Expected 200, got {resp.status_code}: {resp.text}"
        topics = resp.json()
        assert len(topics) == 2, f"Step 4: Expected 2 topics, got {len(topics)}"
        # Both should be active (is_active=True filter by default)
        assert all(t["is_active"] == 1 for t in topics), "Step 4: All topics should be active"

        # --- Step 5: PUT /api/v1/core-topics/{topic_id_1} {name:"API Design v2"} ---
        resp = await client.put(
            f"/api/v1/core-topics/{topic_id_1}",
            json={"name": "API Design v2"},
        )
        assert resp.status_code == 200, f"Step 5: Expected 200, got {resp.status_code}: {resp.text}"
        updated = resp.json()
        assert updated["name"] == "API Design v2", (
            f"Step 5: name should be updated, got {updated['name']}"
        )

        # --- Step 6: DELETE /api/v1/core-topics/{topic_id_2} ---
        resp = await client.delete(f"/api/v1/core-topics/{topic_id_2}")
        assert resp.status_code == 204, f"Step 6: Expected 204, got {resp.status_code}: {resp.text}"

        # --- Step 7: GET /api/v1/core-topics?group_id=g-wf2 (only active) ---
        resp = await client.get(
            "/api/v1/core-topics",
            params={"group_id": "g-wf2"},
        )
        assert resp.status_code == 200, f"Step 7: Expected 200, got {resp.status_code}: {resp.text}"
        topics = resp.json()
        assert len(topics) == 1, f"Step 7: Expected 1 active topic, got {len(topics)}"
        assert topics[0]["core_topic_id"] == topic_id_1

        # --- Step 8: POST /api/v1/key-people?group_id=g-wf2 ---
        # KNOWN BUG: group_id is Query param on POST, not in body
        resp = await client.post(
            "/api/v1/key-people",
            params={"group_id": "g-wf2"},
            json={"sender": "alice", "display_name": "Alice Smith"},
        )
        # Record actual behavior: POST returns 201 because group_id is a required
        # Query param on this endpoint (design quirk, not a body field).
        # KNOWN BUG: group_id is Query param on POST
        assert resp.status_code == 201, (
            f"Step 8: Expected 201 (group_id as Query param), got {resp.status_code}: {resp.text}"
        )

        # --- Step 9: PUT /api/v1/key-people/alice?group_id=g-wf2 ---
        resp = await client.put(
            "/api/v1/key-people/alice",
            params={"group_id": "g-wf2"},
            json={"display_name": "Alice Updated"},
        )
        assert resp.status_code == 200, f"Step 9: Expected 200, got {resp.status_code}: {resp.text}"
        updated_person = resp.json()
        assert updated_person["sender"] == "alice"

        # --- Step 10: DELETE /api/v1/key-people/alice?group_id=g-wf2 ---
        resp = await client.delete(
            "/api/v1/key-people/alice",
            params={"group_id": "g-wf2"},
        )
        assert resp.status_code == 204, (
            f"Step 10: Expected 204, got {resp.status_code}: {resp.text}"
        )
