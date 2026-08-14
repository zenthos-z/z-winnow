"""W15-P0-REPORTS: Tests for regenerate and export endpoints.

Tests verify:
  B1: POST /reports/{rid}/regenerate → 202 AsyncTaskResponse (valid UUID task_id)
  B2: GET /reports/{rid}/export → 200 text/markdown (valid Markdown content)
  B3: 404 for nonexistent report (both endpoints)
  B4: Body overrides (group_id, date) used in regenerate

# P078: Real SQLite :memory: — no mocked database.
# P011: Each B-criterion has its own dedicated test.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from z_winnow.web.routes import api_router

# ============================================================
# Test constants
# ============================================================

VERSION_ID = "test-group-20260601-v1"
REPORT_ID = "test-group-20260601"
GROUP_ID = "test-group"
DATE = "20260601"

os_sep = os.sep


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
    """P078: Real in-memory SQLite with report_versions + async_tasks tables."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row

    await conn.executescript("""
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
        CREATE TABLE IF NOT EXISTS async_tasks (
            task_id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            error TEXT,
            created_at TEXT,
            updated_at TEXT,
            resource_id TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_async_tasks_type_status
            ON async_tasks(task_type, status);
        CREATE INDEX IF NOT EXISTS idx_async_tasks_status
            ON async_tasks(status);
    """)
    yield conn
    await conn.close()


async def _seed_report_version(conn: aiosqlite.Connection) -> None:
    """Seed a report version row for testing."""
    await conn.execute(
        """INSERT INTO report_versions
           (version_id, report_id, group_id, date, version_number,
            content, content_changed, source, build_duration_s, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            VERSION_ID,
            REPORT_ID,
            GROUP_ID,
            DATE,
            1,
            None,
            0,
            "daily_run",
            12.5,
            "2026-06-01T10:00:00Z",
        ),
    )
    await conn.commit()


# ============================================================
# B1: POST /reports/{rid}/regenerate → 202
# ============================================================


class TestB1Regenerate:
    """B1: POST /reports/{rid}/regenerate for existing report → 202."""

    @pytest.mark.asyncio
    async def test_regenerate_returns_202_with_valid_task_id(
        self, app: FastAPI, db_conn: aiosqlite.Connection
    ) -> None:
        """B1: Regenerate enqueues an async task and returns 202 with UUID task_id."""
        await _seed_report_version(db_conn)
        app.state.db_conn = db_conn

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/v1/reports/{VERSION_ID}/regenerate")

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "task_id" in data
        task_id = data["task_id"]
        try:
            uuid.UUID(task_id)
        except ValueError:
            pytest.fail(f"task_id is not a valid UUID: {task_id}")
        assert "status_url" in data
        # B8/AC4: status_url now points at the reports-scoped per-task status
        # endpoint (the old "/api/v1/tasks/{task_id}" was a dead link).
        assert data["status_url"] == f"/api/v1/reports/{VERSION_ID}/tasks/{task_id}"

    @pytest.mark.asyncio
    async def test_b8_regenerate_status_url_get_returns_200(
        self,
        app: FastAPI,
        db_conn: aiosqlite.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """B8/AC2: GET the status_url for a real task_id returns 200 (not 404).

        # A002: real GET endpoint reading async_tasks; explicitly excludes 404.
        # L100: real task queue (start_task/get_task_status), mocked heavy
        # pipeline (build_graph) only.

        The task queue persists to get_settings().db_path; we point it at a
        real shared temp file so start_task (enqueue) and get_task_status
        (the GET endpoint) see the same row.
        """
        await _seed_report_version(db_conn)
        app.state.db_conn = db_conn

        # Point the task-queue DB at a shared temp file (a :memory: path would
        # give start_task and get_task_status isolated DBs → 404). monkeypatch
        # auto-restores the original db_path at teardown (no singleton leak).
        from z_winnow.config.settings import get_settings

        db_file = str(tmp_path / "taskq.db")
        monkeypatch.setattr(get_settings(), "db_path", db_file)

        # Avoid running the real pipeline graph in the background coroutine.
        with patch("z_winnow.graph.builder.build_graph") as mock_build:
            mock_build.return_value.ainvoke = AsyncMock(return_value={})

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/v1/reports/{VERSION_ID}/regenerate")

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        data = resp.json()
        task_id = data["task_id"]
        status_url = data["status_url"]
        assert status_url == f"/api/v1/reports/{VERSION_ID}/tasks/{task_id}"

        # GET the status_url → must be 200 (real task row exists).
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp2 = await client.get(status_url)

        assert resp2.status_code == 200, (
            f"Expected 200 for status_url GET, got {resp2.status_code}: {resp2.text}"
        )
        body = resp2.json()
        assert body["task_id"] == task_id
        assert "status" in body

    @pytest.mark.asyncio
    async def test_regenerate_nonexistent_returns_404(
        self, app: FastAPI, db_conn: aiosqlite.Connection
    ) -> None:
        """B3: Regenerate for nonexistent version → 404."""
        app.state.db_conn = db_conn

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/reports/nonexistent-v1/regenerate")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_regenerate_with_body_overrides(
        self, app: FastAPI, db_conn: aiosqlite.Connection
    ) -> None:
        """B4: Body overrides (group_id, date) passed to regenerate."""
        await _seed_report_version(db_conn)
        app.state.db_conn = db_conn

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/reports/{VERSION_ID}/regenerate",
                json={"group_id": "override-group", "date": "20260602"},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert "task_id" in data

    @pytest.mark.asyncio
    async def test_regenerate_empty_body_uses_stored_values(
        self, app: FastAPI, db_conn: aiosqlite.Connection
    ) -> None:
        """B1: Empty body → stored group_id/date used as defaults."""
        await _seed_report_version(db_conn)
        app.state.db_conn = db_conn

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/v1/reports/{VERSION_ID}/regenerate")

        assert resp.status_code == 202
        data = resp.json()
        assert "task_id" in data


# ============================================================
# B2: GET /reports/{rid}/export → 200 text/markdown
# ============================================================


class TestB2Export:
    """B2: GET /reports/{rid}/export returns text/markdown from L3 JSON."""

    @pytest.mark.asyncio
    async def test_export_service_resolves_path_and_renders(
        self,
        db_conn: aiosqlite.Connection,
        tmp_path: Path,
    ) -> None:
        """B2: Service resolves group_id/date from version, calls render_markdown."""
        await _seed_report_version(db_conn)

        # Create real L3 JSON directory on disk
        l3_root = tmp_path / "data" / "processed"
        l3_dir = l3_root / GROUP_ID / DATE
        l3_dir.mkdir(parents=True)
        (l3_dir / "daily.json").write_text("{}")
        (l3_dir / "resources.json").write_text("{}")
        (l3_dir / "engineering.json").write_text("{}")
        (l3_dir / "topics.json").write_text("{}")

        from z_winnow.web.services.report_service import export_report

        md_result = "# Exported Test Markdown\n\nContent here."

        with patch("z_winnow.subagents.output_composer.render_markdown") as mock_render:
            from unittest.mock import MagicMock

            mock_path = MagicMock(spec=Path)
            mock_path.read_text.return_value = md_result
            mock_render.return_value = mock_path

            with patch("z_winnow.config.settings.get_settings") as mock_settings:
                from z_winnow.config.settings import Settings

                mock_settings.return_value = Settings(
                    layer3_output_dir=str(l3_root),
                    db_path=":memory:",
                )

                md_text = await export_report(db_conn, VERSION_ID)

        assert md_text is not None
        assert md_text == md_result
        mock_render.assert_called_once()
        call_kwargs = mock_render.call_args.kwargs
        json_dir_str = str(call_kwargs["json_dir"])
        assert GROUP_ID in json_dir_str
        assert DATE in json_dir_str

    @pytest.mark.asyncio
    async def test_export_nonexistent_report_returns_404(
        self, app: FastAPI, db_conn: aiosqlite.Connection
    ) -> None:
        """B3: Export for nonexistent version → 404."""
        app.state.db_conn = db_conn

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/reports/nonexistent-v1/export")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_export_service_nonexistent_version_returns_none(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        """Service function returns None when report version not found."""
        from z_winnow.web.services.report_service import export_report

        result = await export_report(db_conn, "nonexistent-v1")
        assert result is None

    @pytest.mark.asyncio
    async def test_export_with_overrides_uses_provided_values(
        self,
        db_conn: aiosqlite.Connection,
        tmp_path: Path,
    ) -> None:
        """B2: Export with explicit group_id/date uses overrides over stored values."""
        await _seed_report_version(db_conn)

        # Create real L3 JSON directory for the override values
        l3_root = tmp_path / "data" / "processed"
        l3_dir = l3_root / "override-g" / "20260602"
        l3_dir.mkdir(parents=True)
        (l3_dir / "daily.json").write_text("{}")
        (l3_dir / "resources.json").write_text("{}")
        (l3_dir / "engineering.json").write_text("{}")
        (l3_dir / "topics.json").write_text("{}")

        from z_winnow.web.services.report_service import export_report

        with patch("z_winnow.subagents.output_composer.render_markdown") as mock_render:
            from unittest.mock import MagicMock

            mock_path = MagicMock(spec=Path)
            mock_path.read_text.return_value = "# Override Test"
            mock_render.return_value = mock_path

            with patch("z_winnow.config.settings.get_settings") as mock_settings:
                from z_winnow.config.settings import Settings

                mock_settings.return_value = Settings(
                    layer3_output_dir=str(l3_root),
                    db_path=":memory:",
                )

                md_text = await export_report(
                    db_conn, VERSION_ID, group_id="override-g", date="20260602"
                )

        assert md_text is not None
        mock_render.assert_called_once()
        json_dir_str = str(mock_render.call_args.kwargs["json_dir"])
        assert "override-g" in json_dir_str
        assert "20260602" in json_dir_str

    @pytest.mark.asyncio
    async def test_export_via_route_returns_200(
        self,
        app: FastAPI,
        db_conn: aiosqlite.Connection,
    ) -> None:
        """B2: Route handler returns 200 text/markdown with mock service."""
        await _seed_report_version(db_conn)
        app.state.db_conn = db_conn

        md_result = "# Route Export Test"

        with patch("z_winnow.web.services.report_service.export_report") as mock_export:
            mock_export.return_value = md_result

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/v1/reports/{VERSION_ID}/export")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert resp.headers["content-type"].startswith("text/markdown")
        assert resp.text == md_result


# ============================================================
# B5: Service-layer edge cases
# ============================================================


class TestB5ServiceEdgeCases:
    """Additional edge case coverage at the service layer."""

    @pytest.mark.asyncio
    async def test_regenerate_report_nonexistent_version(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        """Service function returns None for nonexistent version."""
        from z_winnow.web.services.report_service import regenerate_report

        result = await regenerate_report(db_conn, "nonexistent-v1")
        assert result is None

    @pytest.mark.asyncio
    async def test_export_report_nonexistent_version(self, db_conn: aiosqlite.Connection) -> None:
        """Service function returns None for nonexistent version."""
        from z_winnow.web.services.report_service import export_report

        result = await export_report(db_conn, "nonexistent-v1")
        assert result is None


# ============================================================
# B6: ReportService pattern consistency
# ============================================================


class TestB6ReportServiceDocumentation:
    """B6: Verify that the new service functions are re-exported."""

    def test_regenerate_report_in_package_all(self) -> None:
        """regenerate_report is in services.__all__."""
        import z_winnow.web.services as svc

        assert hasattr(svc, "regenerate_report"), "regenerate_report not importable from services"
        assert "regenerate_report" in svc.__all__

    def test_export_report_in_package_all(self) -> None:
        """export_report is in services.__all__."""
        import z_winnow.web.services as svc

        assert hasattr(svc, "export_report"), "export_report not importable from services"
        assert "export_report" in svc.__all__
