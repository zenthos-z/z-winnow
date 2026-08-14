"""E2E-API-10: Reports deletion — DELETE /api/v1/reports/{report_id}.

Mode B: Real lifespan + tmp_path file SQLite.
Seeds a report (2 versions) + an on-disk L3 JSON dir through the real DB
connection, then exercises the delete endpoint and verifies DB rows + L3
files are both removed.

Usage:
    python -m poetry run pytest tests/test_e2e_api_10_reports_delete.py -v
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

# ============================================================
# P012: Env isolation — autouse monkeypatch
# ============================================================


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate environment: mock mode, test env, no API key, blank paths.

    config_overrides.json neutralization is handled suite-wide by conftest's
    ``_neutralize_config_overrides`` autouse fixture (init-kwargs override).
    """
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
# Mode B fixtures: real lifespan + tmp_path file SQLite + tmp L3 dir
# ============================================================


def _build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a fresh FastAPI app with real lifespan pointing to tmp_path DB + L3."""
    from z_winnow.config.settings import reset_settings
    from z_winnow.web.app import lifespan
    from z_winnow.web.routes import api_router

    db_path = str(tmp_path / "e2e.db")
    reports_dir = str(tmp_path / "reports")
    l3_dir = str(tmp_path / "processed")
    Path(reports_dir).mkdir(exist_ok=True)
    Path(l3_dir).mkdir(exist_ok=True)

    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_REPORTS_DIR", reports_dir)
    monkeypatch.setenv("WINNOW_LAYER3_OUTPUT_DIR", l3_dir)
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


# ============================================================
# Constants — report_id == {group_id}-{date} (builder.py convention)
# ============================================================

GROUP_ID = "g-wf10"
REPORT_DATE = "20260601"
REPORT_ID = f"{GROUP_ID}-{REPORT_DATE}"


async def _seed_report(app: FastAPI) -> Path:
    """Seed 2 versions for one report + write an L3 daily.json on disk.

    Returns the L3 date directory path so the test can assert its removal.
    """
    conn = app.state.db_conn
    for vn, vid in ((1, f"{REPORT_ID}-v1"), (2, f"{REPORT_ID}-v2")):
        await conn.execute(
            """INSERT INTO report_versions
               (version_id, report_id, group_id, date, version_number,
                content, source, build_duration_s)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (vid, REPORT_ID, GROUP_ID, REPORT_DATE, vn, f"v{vn} content", "daily_run", 5.0),
        )
    await conn.commit()

    # On-disk L3 JSON — the service must delete this dir too.
    from z_winnow.config.settings import get_settings

    l3_dir = Path(get_settings().layer3_output_dir) / GROUP_ID / REPORT_DATE
    l3_dir.mkdir(parents=True, exist_ok=True)
    (l3_dir / "daily.json").write_text('{"overview":"seed"}', encoding="utf-8")
    return l3_dir


async def _count_versions(app: FastAPI, report_id: str) -> int:
    """Count remaining version rows for a report_id via the real DB connection."""
    conn = app.state.db_conn
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM report_versions WHERE report_id = ?", (report_id,)
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


# ============================================================
# E2E delete workflow test
# ============================================================


class TestReportsDeleteWorkflow:
    """E2E: Full reports delete workflow — seed → delete → verify DB + disk → 404."""

    @pytest.mark.asyncio
    async def test_reports_delete_workflow(
        self,
        client: httpx.AsyncClient,
        real_app: FastAPI,
    ) -> None:
        # Step 1: Seed 2 versions + L3 JSON dir
        l3_dir = await _seed_report(real_app)
        assert await _count_versions(real_app, REPORT_ID) == 2
        assert l3_dir.exists()

        # Step 2: DELETE /api/v1/reports/{report_id} → 204
        resp = await client.delete(f"/api/v1/reports/{REPORT_ID}")
        assert resp.status_code == 204, (
            f"Expected 204 for delete, got {resp.status_code}: {resp.text}"
        )

        # Step 3: DB rows gone (both versions)
        assert await _count_versions(real_app, REPORT_ID) == 0

        # Step 4: L3 JSON dir removed from disk
        assert not l3_dir.exists()

        # Step 5: Listing no longer returns this report
        resp = await client.get(f"/api/v1/reports?group_id={GROUP_ID}")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

        # Step 6: DELETE again → 404 (already gone)
        resp = await client.delete(f"/api/v1/reports/{REPORT_ID}")
        assert resp.status_code == 404, (
            f"Expected 404 for re-delete, got {resp.status_code}: {resp.text}"
        )
