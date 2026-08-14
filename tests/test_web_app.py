"""T-W14-7: Web app structure tests.

Tests verify (P011: 1:1 AC mapping):
  B1: [import structure] No HTMX page routes remain; only /api/v1/ paths present
  B2: [StaticFiles mount] /ui mount exists serving static files
  B3: [APIRouter prefix] api_router has prefix=/api/v1 and >= 11 sub-routers
  B4: [root redirect] GET / returns 307 redirect to /ui/
  B5: [lifespan preserved] lifespan sets all 5 app.state attributes with real aiosqlite

P076: 4-quadrant contract — test both old behavior removed and new behavior added.
A018: B5 uses real aiosqlite, not mocked DB connection (L100 compliant).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

# ============================================================
# B1: Import structure — no HTMX page routes
# ============================================================


class TestImportStructure:
    """B1: app imports cleanly with zero HTMX page route remnants."""

    def test_import_succeeds(self):
        """from z_winnow.web.app import app succeeds."""
        from z_winnow.web.app import app

        assert app is not None

    def test_no_htmx_page_routes(self):
        """App has no routes matching HTMX page patterns from web/pages/*."""
        from z_winnow.web.app import app

        # A002: Verify zero residual page paths
        htmx_page_paths = {
            "/overview",
            "/data-explorer",
            "/feedback",
            "/groups",
            "/report-viewer",
            "/reports",
            "/rl-dataset",
            "/run-control",
            "/runs",
            "/settings",
        }

        route_paths = {r.path for r in app.routes if hasattr(r, "path")}
        for page_path in htmx_page_paths:
            assert page_path not in route_paths, f"HTMX page route {page_path} still registered"

    def test_api_v1_routes_present(self):
        """App routes contain /api/v1/ prefixed paths."""
        from z_winnow.web.app import app

        route_paths = [r.path for r in app.routes if hasattr(r, "path")]
        api_paths = [p for p in route_paths if "/api/v1" in p]
        assert len(api_paths) > 0, "No /api/v1 routes found"


# ============================================================
# B2: StaticFiles mount at /ui
# ============================================================


class TestStaticFilesMount:
    """B2: /ui mount exists serving static files."""

    def test_ui_mount_exists(self):
        """app.routes includes a mounted sub-application at path /ui."""
        from z_winnow.web.app import app

        mounts = [r for r in app.routes if isinstance(r, Mount) and r.path == "/ui"]
        assert len(mounts) > 0, "No /ui mount found in app.routes"

    def test_ui_mount_name(self):
        """The /ui mount has name='ui'."""
        from z_winnow.web.app import app

        mounts = [r for r in app.routes if isinstance(r, Mount) and r.path == "/ui"]
        assert mounts[0].name == "ui"


# ============================================================
# B3: APIRouter prefix
# ============================================================


class TestAPIRouterPrefix:
    """B3: api_router has prefix=/api/v1 and aggregates all route modules."""

    def test_api_router_prefix(self):
        """api_router.prefix == '/api/v1'."""
        from z_winnow.web.routes import api_router

        assert api_router.prefix == "/api/v1"

    def test_api_router_has_sub_routes(self):
        """api_router.routes >= 11 (one per route module)."""
        from z_winnow.web.routes import api_router

        assert len(api_router.routes) >= 11, (
            f"Expected >= 11 sub-routers, got {len(api_router.routes)}"
        )


# ============================================================
# B4: Root redirect
# ============================================================


class TestRootRedirect:
    """B4: GET / returns 307/308 redirect to /ui/."""

    def test_root_redirects_to_ui(self):
        """GET / -> 307 redirect to /ui/."""
        from z_winnow.web.app import app

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (307, 308), f"Expected 307/308, got {resp.status_code}"
        assert resp.headers["location"] == "/ui/"


# ============================================================
# B5: Lifespan preserved — real aiosqlite (L100 compliant)
# ============================================================


class TestLifespanPreserved:
    """B5: Lifespan sets all 5 app.state attributes with real aiosqlite."""

    @pytest.mark.asyncio
    async def test_lifespan_sets_state_attributes(self, tmp_path: Path):
        """Lifespan initializes db_conn, db_path, reports_dir, memos_stop_event, memos_worker_task."""
        db_path = str(tmp_path / "test_lifespan.db")
        reports_dir = str(tmp_path / "reports")

        # P078/L100: Real SQLite, not mocked
        with (
            patch(
                "z_winnow.web.app._get_db_path",
                return_value=db_path,
            ),
            patch("z_winnow.config.settings.get_settings") as mock_settings,
        ):
            # Minimal settings mock for lifespan
            # W16-B2: sqlite_db_path is now a read-only @property mirror of db_path;
            # set db_path (the authoritative field) on the mock.
            mock_settings.return_value.db_path = db_path
            mock_settings.return_value.reports_dir = reports_dir
            mock_settings.return_value.web_port = 8100

            from z_winnow.web.app import app, lifespan

            async with lifespan(app):
                # Verify all 5 state attributes
                assert hasattr(app.state, "db_conn"), "Missing app.state.db_conn"
                assert hasattr(app.state, "db_path"), "Missing app.state.db_path"
                assert hasattr(app.state, "reports_dir"), "Missing app.state.reports_dir"
                assert hasattr(app.state, "memos_stop_event"), "Missing app.state.memos_stop_event"
                assert hasattr(app.state, "memos_worker_task"), (
                    "Missing app.state.memos_worker_task"
                )

                # Verify types
                assert isinstance(app.state.db_conn, aiosqlite.Connection)
                assert app.state.db_path == db_path
                assert app.state.reports_dir == reports_dir
                assert isinstance(app.state.memos_stop_event, asyncio.Event)
                assert isinstance(app.state.memos_worker_task, asyncio.Task)
