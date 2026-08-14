"""P1-1: GET /api/v1/reports/{report_id}/content endpoint tests.

Covers the transparent L3-JSON passthrough route:
  B1: GET /reports/{id}/content → 200 with the L3 JSON dict (report_type=daily)
  B2: report_type query selects resources.json / engineering.json / topics.json
  B3: 404 for unknown version_id
  B4: 404 when the version exists but no L3 file is on disk

# P078: Real in-memory SQLite via init_database_in_conn — never mock aiosqlite.
# L100: Real L3 JSON files on disk — no mocked file reads.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from z_winnow.pipeline.database import init_database_in_conn
from z_winnow.pipeline.report_version import create_version
from z_winnow.web.routes import api_router

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def app() -> FastAPI:
    """Create a test FastAPI app with the full api_router."""
    _app = FastAPI()
    _app.include_router(api_router)
    return _app


@pytest.fixture
async def db_conn() -> aiosqlite.Connection:
    """In-memory SQLite with full schema."""
    conn = await aiosqlite.connect(":memory:")
    await init_database_in_conn(conn)
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


def _point_l3_at(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Redirect Settings.layer3_output_dir to tmp_path (route has no output_dir arg)."""
    from z_winnow.config.settings import get_settings

    monkeypatch.setattr(get_settings(), "layer3_output_dir", str(tmp_path))


# ============================================================
# B1: GET /reports/{id}/content → 200 transparent daily.json
# ============================================================


@pytest.mark.asyncio
async def test_b1_content_returns_daily_json(
    client: AsyncClient,
    db_conn: aiosqlite.Connection,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1: GET /reports/{id}/content returns the L3 JSON dict transparently."""
    _point_l3_at(monkeypatch, tmp_path)

    vid = await create_version(db_conn, "rpt-c1", "g_c1", "20260601", None, "daily_run")
    l3_dir = tmp_path / "g_c1" / "20260601"
    l3_dir.mkdir(parents=True)
    (l3_dir / "daily.json").write_text(
        json.dumps({"topics": ["a", "b"], "overview": "hello"}), encoding="utf-8"
    )

    resp = await client.get(f"/api/v1/reports/{vid}/content")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # ReportContent.model_dump() shape: report_type / group_id / date / data
    assert body["report_type"] == "daily"
    assert body["group_id"] == "g_c1"
    assert body["date"] == "20260601"
    assert body["data"]["overview"] == "hello"
    assert body["data"]["topics"] == ["a", "b"]


# ============================================================
# B2: report_type query selects the right L3 file
# ============================================================


@pytest.mark.asyncio
async def test_b2_content_report_type_resources(
    client: AsyncClient,
    db_conn: aiosqlite.Connection,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2: report_type=resources returns resources.json content."""
    _point_l3_at(monkeypatch, tmp_path)

    vid = await create_version(db_conn, "rpt-c2", "g_c2", "20260601", None, "daily_run")
    l3_dir = tmp_path / "g_c2" / "20260601"
    l3_dir.mkdir(parents=True)
    (l3_dir / "resources.json").write_text(json.dumps({"total_count": 9}), encoding="utf-8")

    resp = await client.get(f"/api/v1/reports/{vid}/content", params={"report_type": "resources"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["total_count"] == 9


@pytest.mark.asyncio
async def test_b2_content_report_type_engineering(
    client: AsyncClient,
    db_conn: aiosqlite.Connection,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2: report_type=engineering returns engineering.json issues count."""
    _point_l3_at(monkeypatch, tmp_path)

    vid = await create_version(db_conn, "rpt-c3", "g_c3", "20260601", None, "daily_run")
    l3_dir = tmp_path / "g_c3" / "20260601"
    l3_dir.mkdir(parents=True)
    (l3_dir / "engineering.json").write_text(
        json.dumps({"engineering_issues": [{"id": 1}, {"id": 2}, {"id": 3}]}), encoding="utf-8"
    )

    resp = await client.get(f"/api/v1/reports/{vid}/content", params={"report_type": "engineering"})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]["engineering_issues"]) == 3


# ============================================================
# B3: 404 for unknown version_id
# ============================================================


@pytest.mark.asyncio
async def test_b3_content_unknown_version_404(client: AsyncClient) -> None:
    """B3: GET /reports/{unknown}/content → 404."""
    resp = await client.get("/api/v1/reports/does-not-exist/content")
    assert resp.status_code == 404


# ============================================================
# B4: 404 when version exists but no L3 file on disk
# ============================================================


@pytest.mark.asyncio
async def test_b4_content_missing_l3_404(
    client: AsyncClient,
    db_conn: aiosqlite.Connection,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B4: Version exists but no daily.json on disk → 404."""
    _point_l3_at(monkeypatch, tmp_path)

    vid = await create_version(db_conn, "rpt-c4", "g_c4", "20260601", None, "daily_run")
    # No L3 file written
    resp = await client.get(f"/api/v1/reports/{vid}/content")
    assert resp.status_code == 404
