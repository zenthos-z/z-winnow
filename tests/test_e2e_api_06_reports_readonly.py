"""E2E API test: Reports version history + diff (read-only).

Mode A (bare FastAPI + :memory: SQLite) -- follows exact patterns from test_web_api.py.

Tests verify report version listing, retrieval, version history enumeration,
diff between versions, and error cases for single-version reports and
non-existent report IDs.

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
"""

_SEED_VERSIONS = """
INSERT INTO report_versions
    (version_id, report_id, group_id, date, version_number, content, content_changed, source, build_duration_s, created_at)
VALUES
    ('ver-wf6-1', 'rpt-wf6', 'g-wf6', '20260601', 1, 'v1 content', 0, 'daily_run', 10.0, '2026-06-01T00:00:00Z'),
    ('ver-wf6-2', 'rpt-wf6', 'g-wf6', '20260601', 2, 'v2 content', 1, 'regenerate', 8.0, '2026-06-01T00:01:00Z'),
    ('ver-wf6-3', 'rpt-wf6', 'g-wf6', '20260601', 3, 'v3 content', 1, 'regenerate', 7.5, '2026-06-01T00:02:00Z'),
    ('ver-single-1', 'rpt-single', 'g-wf6', '20260602', 1, 'only version', 0, 'daily_run', 5.0, '2026-06-02T00:00:00Z');
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
    await conn.executescript(_SEED_VERSIONS)
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
# Test: Reports Version + Diff Workflow
# ============================================================


class TestReportsReadonlyWorkflow:
    """Read-only report version history and diff operations."""

    async def test_reports_version_diff_workflow(self, client: AsyncClient) -> None:
        """Step-by-step workflow covering report listing, version retrieval, and diffs."""

        # --- Step 1: GET /api/v1/reports?group_id=g-wf6 ---
        resp = await client.get(
            "/api/v1/reports",
            params={"group_id": "g-wf6"},
        )
        assert resp.status_code == 200, f"Step 1: Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["total"] >= 3, f"Step 1: Expected total >= 3, got {body['total']}"
        assert len(body["items"]) >= 3, f"Step 1: Expected >= 3 items, got {len(body['items'])}"

        # --- Step 2: GET /api/v1/reports/ver-wf6-1 ---
        resp = await client.get("/api/v1/reports/ver-wf6-1")
        assert resp.status_code == 200, f"Step 2: Expected 200, got {resp.status_code}: {resp.text}"
        version = resp.json()
        assert version["content"] == "v1 content", (
            f"Step 2: Expected content 'v1 content', got {version['content']}"
        )
        assert version["version_id"] == "ver-wf6-1"
        assert version["version_number"] == 1

        # --- Step 3: GET /api/v1/reports/rpt-wf6/versions ---
        resp = await client.get("/api/v1/reports/rpt-wf6/versions")
        assert resp.status_code == 200, f"Step 3: Expected 200, got {resp.status_code}: {resp.text}"
        versions = resp.json()
        assert len(versions) == 3, f"Step 3: Expected 3 versions, got {len(versions)}"
        # Verify sorted by version_number ascending
        assert versions[0]["version_number"] == 1
        assert versions[1]["version_number"] == 2
        assert versions[2]["version_number"] == 3

        # --- Step 4: GET /api/v1/reports/rpt-wf6/diff ---
        resp = await client.get("/api/v1/reports/rpt-wf6/diff")
        assert resp.status_code == 200, f"Step 4: Expected 200, got {resp.status_code}: {resp.text}"
        diff = resp.json()
        assert "report_id" in diff, "Step 4: Missing report_id in diff response"
        assert diff["report_id"] == "rpt-wf6"
        assert "old_version" in diff, "Step 4: Missing old_version in diff response"
        assert "new_version" in diff, "Step 4: Missing new_version in diff response"
        assert "content_changed" in diff, "Step 4: Missing content_changed in diff response"
        # Diff should be between version 2 (old) and version 3 (new)
        assert diff["old_version"] == 2, (
            f"Step 4: Expected old_version=2, got {diff['old_version']}"
        )
        assert diff["new_version"] == 3, (
            f"Step 4: Expected new_version=3, got {diff['new_version']}"
        )
        assert diff["old_content"] == "v2 content"
        assert diff["new_content"] == "v3 content"

        # --- Step 5: GET /api/v1/reports/rpt-single/diff ---
        # Only 1 version, can't diff -- expect 404
        resp = await client.get("/api/v1/reports/rpt-single/diff")
        assert resp.status_code == 404, (
            f"Step 5: Expected 404 (only 1 version), got {resp.status_code}: {resp.text}"
        )

        # --- Step 6: GET /api/v1/reports/nonexistent ---
        resp = await client.get("/api/v1/reports/nonexistent")
        assert resp.status_code == 404, f"Step 6: Expected 404, got {resp.status_code}: {resp.text}"
