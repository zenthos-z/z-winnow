"""W15-P1-FEEDBACK: Feedback detail, consume, and rollback endpoint tests.

Covers acceptance criteria B1-B4:
  B1: GET /feedback/{id} → 200 FeedbackOut for existing, 404 for nonexistent
  B2: POST /feedback/{id}/consume → 200 idempotent, consumed_at populated
  B3: POST /feedback/{id}/rollback → 200 idempotent, consumed_at cleared
  B4: consume/rollback on nonexistent id → 404

# P054: Route layer parse-validate-delegate — routes tested via HTTP
# P094: Service functions use aiosqlite.Connection (real :memory: DB)
# P078: Real SQLite :memory: for DB-backed tests
# A008: All test data pre-initialized before assertions
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

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
# Helpers
# ============================================================


def _seed_feedback() -> dict:
    """Return a dict of seed data for a feedback event (unconsumed)."""
    return {
        "feedback_id": str(uuid.uuid4()),
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "group_id": "test-group-001",
        "date": "2026-06-05",
        "report_id": "rpt-001",
        "target_type": "topic",
        "target_id": "topic-abc",
        "target_path": "/reports/rpt-001/topics/topic-abc",
        "signal": "correction",
        "severity": "warn",
        "rating": None,
        "tags": '["tag1","tag2"]',
        "correction_mode": "manual",
        "original_text": "original content",
        "corrected_text": "corrected content",
        "correction_note": "fixed typo",
        "reporter": "test-user",
        "consumed_at": None,
        "consumed_by": None,
    }


async def _insert_feedback(db: aiosqlite.Connection, data: dict) -> str:
    """Insert a feedback row and return its feedback_id."""
    await db.execute(
        """INSERT INTO feedback_events
           (feedback_id, created_at, group_id, date, report_id, target_type, target_id,
            target_path, signal, severity, rating, tags, correction_mode,
            original_text, corrected_text, correction_note, reporter,
            consumed_at, consumed_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["feedback_id"],
            data["created_at"],
            data["group_id"],
            data["date"],
            data["report_id"],
            data["target_type"],
            data["target_id"],
            data["target_path"],
            data["signal"],
            data["severity"],
            data["rating"],
            data["tags"],
            data["correction_mode"],
            data["original_text"],
            data["corrected_text"],
            data["correction_note"],
            data["reporter"],
            data["consumed_at"],
            data["consumed_by"],
        ),
    )
    await db.commit()
    return data["feedback_id"]


# ============================================================
# B1: GET /feedback/{id}
# ============================================================


async def test_b1_get_feedback_by_id_200(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B1: GET /feedback/{id} for existing feedback → 200 FeedbackOut matching seeded data."""
    seed = _seed_feedback()
    fid = await _insert_feedback(db_conn, seed)

    resp = await client.get(f"/api/v1/feedback/{fid}")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    body = resp.json()
    assert body["feedback_id"] == fid
    assert body["group_id"] == seed["group_id"]
    assert body["date"] == seed["date"]
    assert body["target_type"] == seed["target_type"]
    assert body["signal"] == seed["signal"]
    assert body["severity"] == seed["severity"]
    assert body["original_text"] == seed["original_text"]
    assert body["corrected_text"] == seed["corrected_text"]
    assert body["correction_note"] == seed["correction_note"]
    assert body["reporter"] == seed["reporter"]
    assert body["consumed_at"] is None
    assert body["consumed_by"] is None


async def test_b1_get_feedback_by_id_404(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B1: GET /feedback/{id} for nonexistent feedback → 404."""
    resp = await client.get("/api/v1/feedback/nonexistent-id")
    assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"


# ============================================================
# B2: POST /feedback/{id}/consume
# ============================================================


async def test_b2_consume_unconsumed_feedback(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B2: POST /feedback/{id}/consume on unconsumed feedback → 200 with consumed_at populated."""
    seed = _seed_feedback()
    fid = await _insert_feedback(db_conn, seed)

    resp = await client.post(f"/api/v1/feedback/{fid}/consume")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    body = resp.json()
    assert body["feedback_id"] == fid
    assert body["consumed_at"] is not None, "consumed_at should be populated after consume"
    assert body["consumed_by"] == "api"


async def test_b2_double_consume_idempotent(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B2: Double-consume → 200 idempotent (same state returned)."""
    seed = _seed_feedback()
    fid = await _insert_feedback(db_conn, seed)

    # First consume
    resp1 = await client.post(f"/api/v1/feedback/{fid}/consume")
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert body1["consumed_at"] is not None

    # Second consume — idempotent
    resp2 = await client.post(f"/api/v1/feedback/{fid}/consume")
    assert resp2.status_code == 200, f"Double consume should return 200, got {resp2.status_code}"
    body2 = resp2.json()
    # Same consumed_at timestamp (no second update since it was already consumed)
    assert body2["consumed_at"] == body1["consumed_at"], (
        "Double consume should not change consumed_at"
    )


# ============================================================
# B3: POST /feedback/{id}/rollback
# ============================================================


async def test_b3_rollback_consumed_feedback(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B3: POST /feedback/{id}/rollback on consumed feedback → 200 with consumed_at cleared."""
    seed = _seed_feedback()
    fid = await _insert_feedback(db_conn, seed)

    # First consume it
    await client.post(f"/api/v1/feedback/{fid}/consume")

    # Then rollback
    resp = await client.post(f"/api/v1/feedback/{fid}/rollback")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    body = resp.json()
    assert body["feedback_id"] == fid
    assert body["consumed_at"] is None, "consumed_at should be cleared after rollback"
    assert body["consumed_by"] is None, "consumed_by should be cleared after rollback"


async def test_b3_rollback_already_unconsumed_idempotent(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B3: Rollback on already-unconsumed feedback → 200 idempotent."""
    seed = _seed_feedback()
    fid = await _insert_feedback(db_conn, seed)

    # Rollback without consuming first — idempotent
    resp = await client.post(f"/api/v1/feedback/{fid}/rollback")
    assert resp.status_code == 200, (
        f"Rollback on unconsumed should return 200, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["feedback_id"] == fid
    assert body["consumed_at"] is None, "consumed_at should remain None"
    assert body["consumed_by"] is None, "consumed_by should remain None"


# ============================================================
# B4: consume/rollback on nonexistent id → 404
# ============================================================


async def test_b4_consume_nonexistent_404(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B4: POST /feedback/{id}/consume on nonexistent id → 404."""
    resp = await client.post("/api/v1/feedback/nonexistent-feedback/consume")
    assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"


async def test_b4_rollback_nonexistent_404(
    client: AsyncClient, db_conn: aiosqlite.Connection
) -> None:
    """B4: POST /feedback/{id}/rollback on nonexistent id → 404."""
    resp = await client.post("/api/v1/feedback/nonexistent-feedback/rollback")
    assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
