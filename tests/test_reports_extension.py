"""Tests for W15-P1-REPORTS: version listing, diff, Feishu push endpoints.

# P078: All tests use real SQLite :memory: with full DDL.
# P011: Each AC criterion has its own dedicated test function.
# P013: Class-based organization.
# A018: Real DDL + real INSERT — no mocked connections.
# L100: Real data via create_version from pipeline.report_version.

Usage:
    python -m poetry run pytest tests/test_reports_extension.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI

from z_winnow.pipeline.database import init_database_in_conn
from z_winnow.pipeline.report_version import create_version
from z_winnow.web.schemas.reports import ReportVersionOut

# ============================================================
# P012: Env isolation — autouse monkeypatch
# ============================================================


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """P012: Isolate environment for each test."""
    monkeypatch.setenv("WEFLOW_MOCK_MODE", "true")
    monkeypatch.setenv("WINNOW_ENV", "test")
    monkeypatch.delenv("WINNOW_WEB_API_KEY", raising=False)
    monkeypatch.delenv("WEB_API_KEY", raising=False)
    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", ":memory:")
    monkeypatch.setenv("WINNOW_DB_PATH", ":memory:")


@pytest.fixture(autouse=True)
def _reset_settings():
    """Reset Settings singleton so each test picks up monkeypatched env."""
    from z_winnow.config.settings import reset_settings

    reset_settings()
    yield
    reset_settings()


# ============================================================
# P078: Real in-memory SQLite fixture with full DDL
# ============================================================


@pytest.fixture
async def db():
    """Create an in-memory SQLite database with full schema."""
    async with aiosqlite.connect(":memory:") as conn:
        await init_database_in_conn(conn)
        yield conn


# ============================================================
# FastAPI test app fixture (for route-level tests)
# ============================================================


def _build_test_app(db_conn: aiosqlite.Connection) -> FastAPI:
    """Build a minimal FastAPI app with reports router only, no middleware.

    Uses importlib to avoid triggering routes/__init__.py which has a
    pre-existing NameError in memos.py (_F from an in-progress parallel task).
    Registers the module under its canonical name so lazy imports work.
    """
    import importlib.util
    import sys

    mod_path = (
        Path(__file__).parent.parent
        / "src"
        / "z_winnow"
        / "web"
        / "routes"
        / "reports.py"
    )
    canon = "z_winnow.web.routes.reports"
    spec = importlib.util.spec_from_file_location(canon, str(mod_path.resolve()))
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load reports.py directly")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[canon] = mod
    spec.loader.exec_module(mod)
    router = mod.router

    app = FastAPI()
    app.state.db_conn = db_conn
    app.include_router(router)
    return app


@pytest.fixture
async def client(db: aiosqlite.Connection):
    """httpx client wired to test app with reports router."""
    app = _build_test_app(db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


# ============================================================
# Helper: Seed report versions
# ============================================================


async def _seed_versions(
    db: aiosqlite.Connection,
    report_id: str,
    group_id: str,
    date: str,
    count: int,
    base_content: str = "Report content v",
) -> list[str]:
    """Seed N report versions for a single report_id. Returns version_ids."""
    version_ids: list[str] = []
    for i in range(1, count + 1):
        content = f"{base_content}{i}" if base_content else None
        vid = await create_version(db, report_id, group_id, date, content, "daily_run")
        version_ids.append(vid)
    return version_ids


# ============================================================
# B1: Version listing tests
# ============================================================


class TestReportVersions:
    """B1: GET /reports/{report_id}/versions — version history listing."""

    @pytest.mark.asyncio
    async def test_b1_three_versions_returns_list(
        self, client: httpx.AsyncClient, db: aiosqlite.Connection
    ) -> None:
        """B1: Report with 3 versions → 200 list of length 3."""
        await _seed_versions(db, "rpt-b1", "g_b1", "20260601", 3)

        resp = await client.get("/reports/rpt-b1/versions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 3

        # Verify sorted by version_number ASC (which mirrors created_at ASC)
        version_numbers = [item["version_number"] for item in data]
        assert version_numbers == sorted(version_numbers)

        # All items are ReportVersionOut
        for item in data:
            assert item["report_id"] == "rpt-b1"
            assert item["group_id"] == "g_b1"
            assert "version_id" in item
            assert "version_number" in item
            assert "created_at" in item

    @pytest.mark.asyncio
    async def test_b1_empty_versions(
        self, client: httpx.AsyncClient, db: aiosqlite.Connection
    ) -> None:
        """B1: Report with no versions → 200 empty list (not 404)."""
        resp = await client.get("/reports/rpt-nonexistent/versions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 0

    @pytest.mark.asyncio
    async def test_b1_single_version(
        self, client: httpx.AsyncClient, db: aiosqlite.Connection
    ) -> None:
        """B1: Report with 1 version → 200 list of length 1."""
        await _seed_versions(db, "rpt-single", "g_s", "20260601", 1)

        resp = await client.get("/reports/rpt-single/versions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["version_number"] == 1


# ============================================================
# B2 + B4: Diff tests
# ============================================================


class TestReportDiff:
    """B2/B4: GET /reports/{report_id}/diff — inter-version comparison."""

    @pytest.mark.asyncio
    async def test_b2_two_versions_returns_diff(
        self, client: httpx.AsyncClient, db: aiosqlite.Connection
    ) -> None:
        """B2: Report with 2 different versions → 200 ReportDiffOut."""
        await create_version(db, "rpt-diff", "g_diff", "20260601", "Content A", "daily_run")
        await create_version(db, "rpt-diff", "g_diff", "20260601", "Content B", "daily_run")

        resp = await client.get("/reports/rpt-diff/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["report_id"] == "rpt-diff"
        assert data["group_id"] == "g_diff"
        assert data["old_version"] == 1
        assert data["new_version"] == 2
        assert data["content_changed"] is True
        assert data["old_content"] == "Content A"
        assert data["new_content"] == "Content B"

    @pytest.mark.asyncio
    async def test_b2_same_content_no_change(
        self, client: httpx.AsyncClient, db: aiosqlite.Connection
    ) -> None:
        """B2: Two versions with same content → content_changed=False."""
        await create_version(db, "rpt-same", "g_same", "20260601", "Same content", "daily_run")
        await create_version(db, "rpt-same", "g_same", "20260601", "Same content", "daily_run")

        resp = await client.get("/reports/rpt-same/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["content_changed"] is False

    @pytest.mark.asyncio
    async def test_b4_single_version_returns_404(
        self, client: httpx.AsyncClient, db: aiosqlite.Connection
    ) -> None:
        """B4: Single version → 404 (Need at least 2 versions)."""
        await create_version(db, "rpt-404", "g_404", "20260601", "Only one", "daily_run")

        resp = await client.get("/reports/rpt-404/diff")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_b4_no_versions_returns_404(self, client: httpx.AsyncClient) -> None:
        """B4: Non-existent report → 404."""
        resp = await client.get("/reports/rpt-ghost/diff")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_b2_three_versions_compares_latest_two(
        self, client: httpx.AsyncClient, db: aiosqlite.Connection
    ) -> None:
        """B2: 3 versions → diff compares latest 2 (v2 vs v3)."""
        await create_version(db, "rpt-triple", "g_triple", "20260601", "v1", "daily_run")
        await create_version(db, "rpt-triple", "g_triple", "20260601", "v2", "daily_run")
        await create_version(db, "rpt-triple", "g_triple", "20260601", "v3", "daily_run")

        resp = await client.get("/reports/rpt-triple/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["old_version"] == 2
        assert data["new_version"] == 3
        assert data["old_content"] == "v2"
        assert data["new_content"] == "v3"


# ============================================================
# B3 + B4: Feishu push tests
# ============================================================


class TestFeishuPush:
    """B3/B4: POST /reports/{report_id}/feishu — async Feishu push."""

    @pytest.mark.asyncio
    async def test_b3_push_enqueues_task(
        self, client: httpx.AsyncClient, db: aiosqlite.Connection, tmp_path: Path
    ) -> None:
        """B3: POST /reports/{rid}/feishu for existing report → 202 + task_id.

        Creates L3 JSON on disk so the push coroutine can find it.
        Uses mock to avoid actual Feishu HTTP call.
        """
        # Seed a report version
        report_id = "rpt-push-ok"
        await create_version(db, report_id, "g_push", "20260601", "Test content", "daily_run")

        # Create L3 JSON on disk
        l3_dir = tmp_path / "g_push" / "20260601"
        l3_dir.mkdir(parents=True)
        (l3_dir / "daily.json").write_text(
            json.dumps({"overview": "Test daily", "sections": []}), encoding="utf-8"
        )

        # Ensure settings point to correct paths
        # For the test, we mock the async task to avoid file DB complexity.
        with (
            patch(
                "z_winnow.web.services.report_service._feishu_push_coro",
                new_callable=lambda: _make_mock_feishu_coro(),
            ),
        ):
            resp = await client.post(
                "/reports/rpt-push-ok/feishu",
                json={"report_id": report_id},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert "task_id" in data
        assert "status_url" in data
        # B8/AC4: status_url now points at the reports-scoped per-task status
        # endpoint (the old "/api/v1/tasks/{task_id}" was a dead link).
        assert data["status_url"] == f"/api/v1/reports/{report_id}/tasks/{data['task_id']}"

    @pytest.mark.asyncio
    async def test_b3_push_with_custom_title(
        self, client: httpx.AsyncClient, db: aiosqlite.Connection, tmp_path: Path
    ) -> None:
        """B3: POST with custom doc_title → 202."""
        report_id = "rpt-push-title"
        await create_version(db, report_id, "g_title", "20260601", "Content", "daily_run")

        # Create L3 JSON
        l3_dir = tmp_path / "g_title" / "20260601"
        l3_dir.mkdir(parents=True)
        (l3_dir / "daily.json").write_text(
            json.dumps({"overview": "Test", "sections": []}), encoding="utf-8"
        )

        with patch(
            "z_winnow.web.services.report_service._feishu_push_coro",
            new_callable=lambda: _make_mock_feishu_coro(),
        ):
            resp = await client.post(
                "/reports/rpt-push-title/feishu",
                json={"report_id": report_id, "doc_title": "Custom Title"},
            )

        assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_b4_push_nonexistent_report_returns_404(self, client: httpx.AsyncClient) -> None:
        """B4: POST /reports/nonexistent/feishu → 404."""
        resp = await client.post(
            "/reports/rpt-ghost/feishu",
            json={"report_id": "rpt-ghost"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_b3_push_no_body_defaults(
        self, client: httpx.AsyncClient, db: aiosqlite.Connection, tmp_path: Path
    ) -> None:
        """B3: POST without body → 202 (doc_title defaults to None)."""
        report_id = "rpt-push-nobody"
        await create_version(db, report_id, "g_nobody", "20260601", "Content", "daily_run")

        l3_dir = tmp_path / "g_nobody" / "20260601"
        l3_dir.mkdir(parents=True)
        (l3_dir / "daily.json").write_text(
            json.dumps({"overview": "Test", "sections": []}), encoding="utf-8"
        )

        with patch(
            "z_winnow.web.services.report_service._feishu_push_coro",
            new_callable=lambda: _make_mock_feishu_coro(),
        ):
            resp = await client.post("/reports/rpt-push-nobody/feishu")

        assert resp.status_code == 202


# ============================================================
# Service-level direct tests
# ============================================================


class TestReportServiceExtension:
    """Direct service function tests for W15-P1-REPORTS additions."""

    @pytest.mark.asyncio
    async def test_get_report_versions_returns_sorted(self, db: aiosqlite.Connection) -> None:
        """get_report_versions returns versions sorted by version_number ASC."""
        from z_winnow.web.services.report_service import get_report_versions

        await _seed_versions(db, "rpt-svc", "g_svc", "20260601", 5)

        result = await get_report_versions(db, "rpt-svc")
        assert isinstance(result, list)
        assert len(result) == 5
        for item in result:
            assert isinstance(item, ReportVersionOut)
        version_numbers = [v.version_number for v in result]
        assert version_numbers == sorted(version_numbers)

    @pytest.mark.asyncio
    async def test_push_report_to_feishu_creates_task(
        self, db: aiosqlite.Connection, tmp_path: Path
    ) -> None:
        """push_report_to_feishu enqueues task and returns task_id."""
        from z_winnow.web.services.report_service import push_report_to_feishu

        await create_version(db, "rpt-push-svc", "g_push_svc", "20260601", "Content", "daily_run")

        db_file = tmp_path / "svc.db"

        # Patch the coroutine to avoid Feishu HTTP
        with (
            patch(
                "z_winnow.web.services.report_service._feishu_push_coro",
                new_callable=lambda: _make_mock_feishu_coro(),
            ),
            patch(
                "z_winnow.config.settings.get_settings",
            ) as mock_settings,
        ):
            from z_winnow.config.settings import Settings

            _s = Settings()
            _s.feishu_enabled = True
            _s.layer3_output_dir = str(tmp_path)
            # W16-B2: sqlite_db_path is now a read-only @property mirror of db_path
            # (single source of truth). Assign to db_path, the authoritative field.
            _s.db_path = str(db_file)

            # Create L3 JSON
            l3_dir = tmp_path / "g_push_svc" / "20260601"
            l3_dir.mkdir(parents=True)
            (l3_dir / "daily.json").write_text(
                json.dumps({"overview": "Test", "sections": []}), encoding="utf-8"
            )

            mock_settings.return_value = _s

            task_id = await push_report_to_feishu(db, "rpt-push-svc")

        assert task_id is not None
        assert isinstance(task_id, str)
        assert len(task_id) > 0

    @pytest.mark.asyncio
    async def test_push_report_no_versions_returns_none(self, db: aiosqlite.Connection) -> None:
        """push_report_to_feishu returns None when report has no versions."""
        from z_winnow.web.services.report_service import push_report_to_feishu

        task_id = await push_report_to_feishu(db, "rpt-noexist")
        assert task_id is None


# ============================================================
# W16-B2: Settings SQLite path single-source-of-truth
# ============================================================
# P083: AliasChoices merge + read-only @property mirror.
# L050/A026: db_path is the only authoritative Field; sqlite_db_path mirrors it.
# P012: each setenv/delenv followed by reset_settings() to rebuild the singleton.
# Lives here so H1 (pytest tests/test_reports_extension.py) covers B2/B3 behavior
# and provides verification evidence (curator H1_MISSING guidance).


class TestSettingsDbPathConvergence:
    """B2/B3: db_path authoritative Field, sqlite_db_path read-only mirror."""

    def test_db_path_default_and_mirror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """B2: no DB env -> db_path == 'data/winnow.db', sqlite_db_path mirrors it."""
        from z_winnow.config.settings import get_settings, reset_settings

        for k in (
            "WINNOW_DB_PATH",
            "DB_PATH",
            "WINNOW_SQLITE_DB_PATH",
            "SQLITE_DB_PATH",
        ):
            monkeypatch.delenv(k, raising=False)
        reset_settings()
        s = get_settings()
        assert s.db_path == "data/winnow.db"
        assert s.sqlite_db_path == s.db_path

    def test_sqlite_db_path_is_readonly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """B2: assigning sqlite_db_path raises AttributeError (read-only property)."""
        from z_winnow.config.settings import get_settings, reset_settings

        monkeypatch.delenv("WINNOW_DB_PATH", raising=False)
        reset_settings()
        s = get_settings()
        with pytest.raises(AttributeError):
            s.sqlite_db_path = "should_fail"  # type: ignore[misc]

    def test_conflict_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """B3: both WINNOW_DB_PATH and legacy WINNOW_SQLITE_DB_PATH set ->
        WINNOW_DB_PATH (authoritative, declared first) wins; mirror follows it."""
        from z_winnow.config.settings import get_settings, reset_settings

        monkeypatch.setenv("WINNOW_DB_PATH", "/tmp/a.db")
        monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", "/tmp/b.db")
        reset_settings()
        s = get_settings()
        assert s.db_path == "/tmp/a.db"
        assert s.sqlite_db_path == "/tmp/a.db"

    def test_legacy_alias_drives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """B3: only legacy WINNOW_SQLITE_DB_PATH set (no WINNOW_DB_PATH) ->
        legacy alias still drives the authoritative db_path via merged AliasChoices."""
        from z_winnow.config.settings import get_settings, reset_settings

        monkeypatch.delenv("WINNOW_DB_PATH", raising=False)
        monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", "/tmp/b.db")
        reset_settings()
        s = get_settings()
        assert s.db_path == "/tmp/b.db"
        assert s.sqlite_db_path == "/tmp/b.db"


# ============================================================
# Helpers
# ============================================================


def _make_mock_feishu_coro():
    """Create a mock coroutine factory that returns success."""

    async def _mock_coro(*args, **kwargs):
        return {"status": "uploaded", "rows_count": 1}

    return _mock_coro
