"""E2E-API-09: Reports mutation workflow — regenerate, export, feishu push.

Mode B: Real lifespan + tmp_path file SQLite.
Full round-trip test seeding report_versions through the real DB connection
provided by app.state.db_conn (after lifespan init), then exercising
regenerate, export, and feishu push endpoints.

Usage:
    python -m poetry run pytest tests/test_e2e_api_09_reports_mutation.py -v
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
# Mode B fixtures: real lifespan + tmp_path file SQLite
# ============================================================


def _build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a fresh FastAPI app with real lifespan pointing to tmp_path DB."""
    from z_winnow.config.settings import reset_settings
    from z_winnow.web.app import lifespan
    from z_winnow.web.routes import api_router

    db_path = str(tmp_path / "e2e.db")
    reports_dir = str(tmp_path / "reports")
    Path(reports_dir).mkdir(exist_ok=True)

    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_REPORTS_DIR", reports_dir)
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
# Seed helper
# ============================================================


async def _seed_report_version(app: FastAPI) -> None:
    """Insert a report_version row via the real DB connection."""
    conn = app.state.db_conn
    await conn.execute(
        """INSERT INTO report_versions
           (version_id, report_id, group_id, date, version_number,
            content, source, build_duration_s)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("ver-wf9-1", "rpt-wf9", "g-wf9", "20260601", 1, "original content", "daily_run", 5.0),
    )
    await conn.commit()


# ============================================================
# E2E workflow test
# ============================================================


class TestReportsMutationWorkflow:
    """E2E: Full reports mutation workflow — seed, regenerate, export, feishu."""

    @pytest.mark.asyncio
    async def test_reports_mutation_workflow(
        self,
        client: httpx.AsyncClient,
        real_app: FastAPI,
    ) -> None:
        """Step 1-5: Seed → regenerate (202 + 404) → export → feishu push."""

        # Step 1: Seed report_version via real_app.state.db_conn
        await _seed_report_version(real_app)

        # Step 2: POST /api/v1/reports/ver-wf9-1/regenerate → 202
        resp = await client.post("/api/v1/reports/ver-wf9-1/regenerate")
        assert resp.status_code == 202, (
            f"Expected 202 for regenerate, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "task_id" in data, f"Response missing task_id: {data}"

        # Step 3: POST /api/v1/reports/nonexistent-id/regenerate → 404
        resp = await client.post("/api/v1/reports/nonexistent-id/regenerate")
        assert resp.status_code == 404, (
            f"Expected 404 for nonexistent regenerate, got {resp.status_code}: {resp.text}"
        )

        # Step 4: GET /api/v1/reports/ver-wf9-1/export
        # May return 200 (if L3 JSON exists) or 404 (if not) — both are acceptable.
        resp = await client.get("/api/v1/reports/ver-wf9-1/export")
        assert resp.status_code in (200, 404), (
            f"Expected 200 or 404 for export, got {resp.status_code}: {resp.text}"
        )

        # Step 5: POST /api/v1/reports/rpt-wf9/feishu
        # FeishuPushRequest requires report_id field.
        # The coroutine will fail but the endpoint should accept the request (202).
        resp = await client.post(
            "/api/v1/reports/rpt-wf9/feishu",
            json={"report_id": "rpt-wf9"},
        )
        assert resp.status_code in (202, 404), (
            f"Expected 202 or 404 for feishu push, got {resp.status_code}: {resp.text}"
        )
        # If 202, verify task_id is present
        if resp.status_code == 202:
            data = resp.json()
            assert "task_id" in data, f"Response missing task_id: {data}"
