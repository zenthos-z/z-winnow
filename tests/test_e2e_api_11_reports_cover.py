"""E2E-API-11: Reports cover image workflow — POST /cover (async) + GET /cover.

Mode B: Real lifespan + tmp_path file SQLite.
Seeds a report_version, monkeypatches ``outputs.image_gen.generate_cover`` to write
a fake PNG (avoid real DMX/deepseek), then exercises the cover generate + serve
endpoints end-to-end through the real async task queue.

Usage:
    python -m poetry run pytest tests/test_e2e_api_11_reports_cover.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

# ============================================================
# Env isolation — autouse monkeypatch
# ============================================================


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEFLOW_MOCK_MODE", "true")
    monkeypatch.setenv("WINNOW_ENV", "test")
    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", "")
    monkeypatch.setenv("WINNOW_DB_PATH", "")
    monkeypatch.setenv("WINNOW_REPORTS_DIR", "")


@pytest.fixture(autouse=True)
def _reset_settings():
    from z_winnow.config.settings import reset_settings

    reset_settings()
    yield
    reset_settings()


# ============================================================
# Mode B fixtures
# ============================================================


def _build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from z_winnow.config.settings import reset_settings
    from z_winnow.web.app import lifespan
    from z_winnow.web.routes import api_router

    db_path = str(tmp_path / "e2e_cover.db")
    l3_dir = str(tmp_path / "processed")
    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_LAYER3_OUTPUT_DIR", l3_dir)
    reset_settings()

    fresh = FastAPI(title="e2e-cover-test", lifespan=lifespan)
    fresh.include_router(api_router)
    return fresh


@pytest.fixture
async def real_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from z_winnow.web.app import lifespan

    app = _build_app(tmp_path, monkeypatch)
    async with lifespan(app):
        yield app


@pytest.fixture
async def client(real_app: FastAPI):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app),
        base_url="http://test",
    ) as c:
        yield c


# ============================================================
# Seed + fake generate_cover
# ============================================================

RID = "rpt-cov"
VID = "ver-cov-1"
GROUP = "g-cov"
DATE = "20260701"
FAKE_PNG = b"\x89PNG\r\n\x1a\n fake test cover bytes"


async def _seed_report_version(app: FastAPI) -> None:
    conn = app.state.db_conn
    await conn.execute(
        """INSERT INTO report_versions
           (version_id, report_id, group_id, date, version_number,
            content, source, build_duration_s)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (VID, RID, GROUP, DATE, 1, "cover test content", "daily_run", 1.0),
    )
    await conn.commit()


def _patch_generate_cover_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake generate_cover: write a PNG to the real L3 path + return it."""

    async def _fake(group_id: str, date: str, **kw):
        from z_winnow.config.settings import get_settings

        d = Path(get_settings().layer3_output_dir) / group_id / date
        d.mkdir(parents=True, exist_ok=True)
        p = d / "cover.png"
        p.write_bytes(FAKE_PNG)
        return [p]

    from z_winnow.outputs import image_gen

    monkeypatch.setattr(image_gen, "generate_cover", _fake)


def _patch_generate_cover_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(group_id: str, date: str, **kw):
        raise RuntimeError("simulated DMX failure")

    from z_winnow.outputs import image_gen

    monkeypatch.setattr(image_gen, "generate_cover", _fake)


async def _poll_task(client: httpx.AsyncClient, status_url: str, iterations: int = 100) -> dict:
    """Poll the task status endpoint until terminal."""
    st: dict = {}
    for _ in range(iterations):
        r = await client.get(status_url)
        st = r.json()
        if st.get("status") in ("done", "failed", "cancelled", "error"):
            return st
        await asyncio.sleep(0.05)
    return st


# ============================================================
# Tests
# ============================================================


class TestReportsCoverWorkflow:
    """E2E: POST /reports/{rid}/cover (async) + GET /reports/{rid}/cover."""

    @pytest.mark.asyncio
    async def test_post_cover_generates_and_serves(
        self, client: httpx.AsyncClient, real_app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _seed_report_version(real_app)
        _patch_generate_cover_ok(monkeypatch)

        # POST /cover → 202 + task_id
        resp = await client.post(f"/api/v1/reports/{VID}/cover")
        assert resp.status_code == 202, resp.text
        data = resp.json()
        assert "task_id" in data
        assert data["status_url"].endswith(data["task_id"])

        # 轮询至 done
        st = await _poll_task(client, data["status_url"])
        assert st["status"] == "done", f"task not done: {st}"
        result = st["result"]
        assert isinstance(result, dict)
        assert result.get("files"), f"no files in result: {result}"

        # GET /cover → 200 image/png
        resp = await client.get(f"/api/v1/reports/{VID}/cover")
        assert resp.status_code == 200, resp.text
        assert "image/png" in resp.headers.get("content-type", "")
        assert resp.content == FAKE_PNG

    @pytest.mark.asyncio
    async def test_post_cover_404_when_report_missing(self, client: httpx.AsyncClient) -> None:
        resp = await client.post("/api/v1/reports/nonexistent-report/cover")
        assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_get_cover_404_when_not_generated(
        self, client: httpx.AsyncClient, real_app: FastAPI
    ) -> None:
        await _seed_report_version(real_app)
        # 未生成配图 → 404
        resp = await client.get(f"/api/v1/reports/{VID}/cover")
        assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_post_cover_failure_surfaces_error(
        self, client: httpx.AsyncClient, real_app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _seed_report_version(real_app)
        _patch_generate_cover_fails(monkeypatch)

        resp = await client.post(f"/api/v1/reports/{VID}/cover")
        assert resp.status_code == 202, resp.text
        st = await _poll_task(client, resp.json()["status_url"])
        assert st["status"] == "failed", f"expected failed, got: {st}"
        assert st.get("error"), f"error message empty: {st}"
        # 错误信息含模拟异常类型
        assert "RuntimeError" in st["error"] or "simulated" in st["error"]

    @pytest.mark.asyncio
    async def test_post_cover_accepts_optional_body(
        self, client: httpx.AsyncClient, real_app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CoverRequest 全可选——传 body 覆盖 count/ratio/size 不应 422。"""
        await _seed_report_version(real_app)
        captured: dict = {}

        async def _fake(group_id: str, date: str, *, count=None, ratio=None, size=None, **kw):
            captured.update(count=count, ratio=ratio, size=size)
            from z_winnow.config.settings import get_settings

            d = Path(get_settings().layer3_output_dir) / group_id / date
            d.mkdir(parents=True, exist_ok=True)
            p = d / "cover.png"
            p.write_bytes(FAKE_PNG)
            return [p]

        from z_winnow.outputs import image_gen

        monkeypatch.setattr(image_gen, "generate_cover", _fake)

        resp = await client.post(
            f"/api/v1/reports/{VID}/cover", json={"count": 2, "ratio": "1:1", "size": "1K"}
        )
        assert resp.status_code == 202, resp.text
        st = await _poll_task(client, resp.json()["status_url"])
        assert st["status"] == "done", st
        assert captured == {"count": 2, "ratio": "1:1", "size": "1K"}, captured
