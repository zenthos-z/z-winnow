"""E2E API: Multi-layer data explorer + provenance chain workflow.

Full end-to-end workflow test that exercises all data explorer endpoints:
  seed data → stats (global + filtered) → L1/L2/L3 browse → provenance
  → L1 detail → invalid layer 400

Mode A: bare FastAPI + :memory: SQLite — no full app factory, no middleware.

# P011: 1:1 AC mapping -- single test method covers full data exploration workflow.
# P078: Real SQLite :memory: for DB-backed tests.
# P054: Route layer tested via real HTTP request/response cycle.
# P032: Multi-layer data explorer pattern.
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
    """Provide an in-memory SQLite connection with all required tables."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row

    await conn.executescript("""
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


async def _seed_full_chain(db: aiosqlite.Connection) -> None:
    """Insert a full provenance chain: 3 raw_messages, 1 context, 1 topic_summary.

    All entries use group_id='grp-wf4', date='20260601'.
    """
    # 3 raw messages
    await db.execute(
        "INSERT INTO raw_messages (serverID, date, group_id, sender, content) "
        "VALUES ('sid-001', '20260601', 'grp-wf4', 'Alice', 'First message')"
    )
    await db.execute(
        "INSERT INTO raw_messages (serverID, date, group_id, sender, content) "
        "VALUES ('sid-002', '20260601', 'grp-wf4', 'Bob', 'Second message')"
    )
    await db.execute(
        "INSERT INTO raw_messages (serverID, date, group_id, sender, content) "
        "VALUES ('sid-003', '20260601', 'grp-wf4', 'Carol', 'Third message')"
    )

    # 1 parsed context referencing sid-001 and sid-002
    await db.execute(
        "INSERT INTO parsed_contexts "
        "(context_id, date, group_id, server_ids, context_text) "
        "VALUES ('ctx-wf4', '20260601', 'grp-wf4', "
        "'[\"sid-001\", \"sid-002\"]', 'Context block for workflow test')"
    )

    # 1 topic_summary referencing ctx-wf4, sourced from sid-001 and sid-002
    await db.execute(
        "INSERT INTO topic_summaries "
        "(summary_id, date, group_id, topic_name, topic_id, summary_text, "
        " context_ids, source_server_ids, lifecycle) "
        "VALUES ('sum-wf4', '20260601', 'grp-wf4', 'Workflow Topic', 't-wf4', "
        "'Summary for workflow test', '[\"ctx-wf4\"]', "
        "'[\"sid-001\", \"sid-002\"]', 'emerging')"
    )

    await db.commit()


# ============================================================
# Test class
# ============================================================


class TestDataExplorerWorkflow:
    """Full multi-layer data explorer + provenance chain workflow.

    Steps 1-10 exercise stats, layer browsing, provenance, L1 detail,
    and invalid layer rejection.
    """

    @pytest.mark.asyncio
    async def test_full_data_exploration_workflow(
        self, client: AsyncClient, db_conn: aiosqlite.Connection
    ) -> None:
        """Steps 1-10: complete data exploration from stats to invalid layer."""

        # ---- Step 1: Seed full chain data ----
        await _seed_full_chain(db_conn)

        # ---- Step 2: GET /api/v1/data/stats -> 200, verify counts ----
        resp = await client.get("/api/v1/data/stats")
        assert resp.status_code == 200, f"Step 2: Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["total_messages"] >= 3, (
            f"Step 2: total_messages >= 3 expected, got {body['total_messages']}"
        )
        assert body["total_topics"] >= 1, (
            f"Step 2: total_topics >= 1 expected, got {body['total_topics']}"
        )

        # ---- Step 3: GET /api/v1/data/stats?group_id=grp-wf4 -> filtered counts ----
        resp = await client.get("/api/v1/data/stats", params={"group_id": "grp-wf4"})
        assert resp.status_code == 200, f"Step 3: Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["total_messages"] >= 3, (
            f"Step 3: filtered total_messages >= 3 expected, got {body['total_messages']}"
        )
        assert body["total_topics"] >= 1, (
            f"Step 3: filtered total_topics >= 1 expected, got {body['total_topics']}"
        )

        # ---- Step 4: GET /api/v1/data/l1/grp-wf4/20260601 -> items >= 3 ----
        resp = await client.get("/api/v1/data/l1/grp-wf4/20260601")
        assert resp.status_code == 200, f"Step 4: Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        items = body.get("items", [])
        assert len(items) >= 3, f"Step 4: L1 items >= 3 expected, got {len(items)}"

        # ---- Step 5: GET /api/v1/data/l2/grp-wf4/20260601 -> items >= 1 ----
        resp = await client.get("/api/v1/data/l2/grp-wf4/20260601")
        assert resp.status_code == 200, f"Step 5: Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        items = body.get("items", [])
        assert len(items) >= 1, f"Step 5: L2 items >= 1 expected, got {len(items)}"

        # ---- Step 6: GET /api/v1/data/l3/grp-wf4/20260601 -> items >= 1 ----
        resp = await client.get("/api/v1/data/l3/grp-wf4/20260601")
        assert resp.status_code == 200, f"Step 6: Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        items = body.get("items", [])
        assert len(items) >= 1, f"Step 6: L3 items >= 1 expected, got {len(items)}"

        # ---- Step 7: GET /api/v1/data/provenance/sid-001 -> 200, has message + topics ----
        resp = await client.get("/api/v1/data/provenance/sid-001")
        assert resp.status_code == 200, f"Step 7: Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "message" in body, "Step 7: response must have 'message' key"
        assert "topics" in body, "Step 7: response must have 'topics' key"
        assert isinstance(body["topics"], list), "Step 7: topics must be a list"
        assert len(body["topics"]) > 0, (
            "Step 7: topics must be non-empty for sid-001 (linked to sum-wf4)"
        )

        # ---- Step 8: GET /api/v1/data/provenance/nonexistent_sid -> 404 ----
        resp = await client.get("/api/v1/data/provenance/nonexistent_sid")
        assert resp.status_code == 404, f"Step 8: Expected 404, got {resp.status_code}: {resp.text}"

        # ---- Step 9: GET /api/v1/data/l1/grp-wf4/20260601/detail/sid-001 -> 200 ----
        resp = await client.get("/api/v1/data/l1/grp-wf4/20260601/detail/sid-001")
        assert resp.status_code == 200, f"Step 9: Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "contexts" in body, "Step 9: response must have 'contexts' array"
        assert "summaries" in body, "Step 9: response must have 'summaries' array"
        assert isinstance(body["contexts"], list), "Step 9: contexts must be a list"
        assert isinstance(body["summaries"], list), "Step 9: summaries must be a list"

        # ---- Step 10: GET /api/v1/data/l4/grp-wf4/20260601 -> 400 (invalid layer) ----
        resp = await client.get("/api/v1/data/l4/grp-wf4/20260601")
        assert resp.status_code == 400, (
            f"Step 10: Expected 400 for invalid layer, got {resp.status_code}: {resp.text}"
        )
