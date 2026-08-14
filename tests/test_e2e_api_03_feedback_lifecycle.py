"""E2E API: Feedback state machine lifecycle — create, list, consume, rollback.

Full end-to-end workflow test that exercises the feedback state machine:
  create → GET detail → list (unconsumed) → consume → list (filtered out)
  → double-consume (idempotent) → rollback → list (reappears)
  → second feedback + consume → list (only rolled-back first one)

Mode A: bare FastAPI + :memory: SQLite — no full app factory, no middleware.

# P011: 1:1 AC mapping -- single test method covers full lifecycle.
# P078: Real SQLite :memory: for DB-backed tests.
# P054: Route layer tested via real HTTP request/response cycle.
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
    """Provide an in-memory SQLite connection with feedback_events table."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row

    await conn.executescript("""
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
# Test class
# ============================================================


class TestFeedbackLifecycleWorkflow:
    """Full feedback state machine lifecycle test.

    Steps 1-10 exercise create → detail → list → consume → list (filtered)
    → idempotent consume → rollback → list (reappears) → second feedback
    → selective list filtering.
    """

    @pytest.mark.asyncio
    async def test_full_feedback_lifecycle(
        self, client: AsyncClient, db_conn: aiosqlite.Connection
    ) -> None:
        """Steps 1-10: complete feedback lifecycle from create to selective list."""

        # ---- Step 1: POST /api/v1/feedback -> 201 ----
        resp = await client.post(
            "/api/v1/feedback",
            json={
                "group_id": "g-fb",
                "date": "20260601",
                "target_type": "topic",
                "target_id": "sum-001",
                "signal": "correction",
                "severity": "info",
                "correction_note": "test note",
            },
        )
        assert resp.status_code == 201, f"Step 1: Expected 201, got {resp.status_code}: {resp.text}"
        body = resp.json()
        feedback_id = body["feedback_id"]
        assert feedback_id, "Step 1: feedback_id must be non-empty"

        # ---- Step 2: GET /api/v1/feedback/{feedback_id} -> 200, consumed_at is None ----
        resp = await client.get(f"/api/v1/feedback/{feedback_id}")
        assert resp.status_code == 200, f"Step 2: Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["feedback_id"] == feedback_id
        assert body["consumed_at"] is None, "Step 2: consumed_at should be None initially"

        # ---- Step 3: GET /api/v1/feedback?group_id=g-fb&date=20260601 -> 200, contains feedback ----
        resp = await client.get("/api/v1/feedback", params={"group_id": "g-fb", "date": "20260601"})
        assert resp.status_code == 200, f"Step 3: Expected 200, got {resp.status_code}: {resp.text}"
        items = resp.json()
        assert any(fb["feedback_id"] == feedback_id for fb in items), (
            "Step 3: created feedback must appear in list"
        )

        # ---- Step 4: POST /api/v1/feedback/{feedback_id}/consume -> 200, consumed_at not None ----
        resp = await client.post(f"/api/v1/feedback/{feedback_id}/consume")
        assert resp.status_code == 200, f"Step 4: Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["feedback_id"] == feedback_id
        assert body["consumed_at"] is not None, (
            "Step 4: consumed_at must be populated after consume"
        )

        # ---- Step 5: GET /api/v1/feedback?group_id=g-fb&date=20260601 -> items empty (consumed) ----
        resp = await client.get("/api/v1/feedback", params={"group_id": "g-fb", "date": "20260601"})
        assert resp.status_code == 200, f"Step 5: Expected 200, got {resp.status_code}: {resp.text}"
        items = resp.json()
        assert len(items) == 0, (
            f"Step 5: consumed feedback should be filtered out, got {len(items)} items"
        )

        # ---- Step 6: POST /api/v1/feedback/{feedback_id}/consume -> 200 (idempotent) ----
        resp = await client.post(f"/api/v1/feedback/{feedback_id}/consume")
        assert resp.status_code == 200, (
            f"Step 6: double-consume should return 200, got {resp.status_code}: {resp.text}"
        )

        # ---- Step 7: POST /api/v1/feedback/{feedback_id}/rollback -> 200, consumed_at is None ----
        resp = await client.post(f"/api/v1/feedback/{feedback_id}/rollback")
        assert resp.status_code == 200, f"Step 7: Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["feedback_id"] == feedback_id
        assert body["consumed_at"] is None, "Step 7: consumed_at must be None after rollback"

        # ---- Step 8: GET /api/v1/feedback?group_id=g-fb&date=20260601 -> feedback reappears ----
        resp = await client.get("/api/v1/feedback", params={"group_id": "g-fb", "date": "20260601"})
        assert resp.status_code == 200, f"Step 8: Expected 200, got {resp.status_code}: {resp.text}"
        items = resp.json()
        assert any(fb["feedback_id"] == feedback_id for fb in items), (
            "Step 8: rolled-back feedback must reappear in list"
        )

        # ---- Step 9: Create SECOND feedback, consume it ----
        resp2 = await client.post(
            "/api/v1/feedback",
            json={
                "group_id": "g-fb",
                "date": "20260601",
                "target_type": "topic",
                "target_id": "sum-002",
                "signal": "neutral",
                "severity": "info",
            },
        )
        assert resp2.status_code == 201, (
            f"Step 9a: Expected 201, got {resp2.status_code}: {resp2.text}"
        )
        feedback_id_2 = resp2.json()["feedback_id"]

        # Consume the second feedback
        resp_consume = await client.post(f"/api/v1/feedback/{feedback_id_2}/consume")
        assert resp_consume.status_code == 200, (
            f"Step 9b: Expected 200, got {resp_consume.status_code}: {resp_consume.text}"
        )

        # ---- Step 10: GET /api/v1/feedback?group_id=g-fb&date=20260601 -> only first (rolled-back) ----
        resp = await client.get("/api/v1/feedback", params={"group_id": "g-fb", "date": "20260601"})
        assert resp.status_code == 200, (
            f"Step 10: Expected 200, got {resp.status_code}: {resp.text}"
        )
        items = resp.json()
        assert len(items) == 1, (
            f"Step 10: only the rolled-back first feedback should appear, got {len(items)} items"
        )
        assert items[0]["feedback_id"] == feedback_id, (
            "Step 10: the remaining item must be the first (rolled-back) feedback"
        )
