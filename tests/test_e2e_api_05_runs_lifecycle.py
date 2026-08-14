"""E2E API test: Runs lifecycle workflow — create, list, get, cancel, batch, stream.

Covers the full runs lifecycle in one test method:
  1. POST /runs -> 202 (create single run)
  2. GET /runs -> 200 (list runs, non-empty)
  3. GET /runs/{run_id} -> 200 (get run detail)
  4. Seed queued run -> POST cancel -> 200
  5. Seed completed run -> POST cancel -> 409
  6. POST cancel nonexistent -> 404
  7. POST /runs/batch -> 202 (batch create)
  8. GET /runs/stream -> 200 SSE

# P078: Real SQLite via tmp_path — no mocked database.
# P012: autouse monkeypatch env isolation.
# A018: Real DDL + real INSERT — no mocked dicts.
# L100: All tests hit real SQLite via real app lifespan.

Usage:
    poetry run pytest tests/test_e2e_api_05_runs_lifecycle.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

# ============================================================
# P012: Env isolation — autouse monkeypatch
# ============================================================


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch):
    monkeypatch.setenv("WEFLOW_MOCK_MODE", "true")
    monkeypatch.setenv("WINNOW_ENV", "test")
    # POST /runs 现在真跑 orchestrate()：mock LLM + 关闭 MemOS 以隔离 docker 依赖
    monkeypatch.setenv("WINNOW_MOCK_LLM", "true")
    monkeypatch.setenv("WINNOW_MEMOS_ENABLED", "false")
    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", "")
    monkeypatch.setenv("WINNOW_DB_PATH", "")
    monkeypatch.setenv("WINNOW_REPORTS_DIR", "")
    monkeypatch.delenv("WINNOW_WEB_API_KEY", raising=False)
    monkeypatch.delenv("WEB_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _reset_settings():
    from z_winnow.config.settings import reset_settings

    reset_settings()
    yield
    reset_settings()


def _build_app(tmp_path, monkeypatch):
    from z_winnow.config.settings import reset_settings
    from z_winnow.web.app import lifespan
    from z_winnow.web.routes import api_router

    db_path = str(tmp_path / "e2e.db")
    reports_dir = str(tmp_path / "reports")
    Path(reports_dir).mkdir(exist_ok=True)
    monkeypatch.setenv("WINNOW_SQLITE_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_DB_PATH", db_path)
    monkeypatch.setenv("WINNOW_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("WINNOW_REPORTS_DIR", reports_dir)
    monkeypatch.setenv("WINNOW_LAYER3_OUTPUT_DIR", str(tmp_path / "l3"))
    reset_settings()
    fresh = FastAPI(title="e2e-api-test", lifespan=lifespan)
    fresh.include_router(api_router)
    return fresh


@pytest.fixture
async def real_app(tmp_path, monkeypatch):
    from z_winnow.web.app import lifespan

    app = _build_app(tmp_path, monkeypatch)
    async with lifespan(app):
        yield app


@pytest.fixture
async def client(real_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app),
        base_url="http://test",
    ) as c:
        yield c


# ============================================================
# Test: Runs lifecycle workflow
# ============================================================


class TestRunsLifecycleWorkflow:
    """Full lifecycle: create -> list -> get -> cancel (queued/completed/nonexistent) -> batch -> stream."""

    async def test_runs_lifecycle_workflow(self, client, real_app) -> None:
        # ── Step 1: POST /runs → 202 ──
        resp = await client.post(
            "/api/v1/runs",
            json={
                "component": "pipeline",
                "group_id": "g-wf5",
                "date": "2026-06-01",
            },
        )
        assert resp.status_code == 202, f"Step1: Expected 202, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "task_id" in body
        assert len(body["task_id"]) > 0
        # Extract run_id from status_url (format: /api/v1/runs/{run_id})
        status_url = body["status_url"]
        run_id = status_url.rsplit("/", 1)[-1]

        # Wait for background insert_run to complete
        for _ in range(30):
            await asyncio.sleep(0.05)
            resp_list = await client.get("/api/v1/runs")
            if resp_list.status_code == 200 and len(resp_list.json()) > 0:
                break

        # ── Step 2: GET /runs → 200, items non-empty ──
        resp = await client.get("/api/v1/runs")
        assert resp.status_code == 200, f"Step2: Expected 200, got {resp.status_code}: {resp.text}"
        items = resp.json()
        assert isinstance(items, list)
        assert len(items) > 0, "Step2: Expected non-empty runs list"

        # ── Step 3: GET /runs/{run_id} → 200 ──
        resp = await client.get(f"/api/v1/runs/{run_id}")
        assert resp.status_code == 200, f"Step3: Expected 200, got {resp.status_code}: {resp.text}"
        run_detail = resp.json()
        assert run_detail["run_id"] == run_id

        # ── Step 4: Seed a "queued" run and cancel it → 200 ──
        conn = real_app.state.db_conn
        await conn.execute(
            "INSERT INTO pipeline_runs (run_id, component, group_id, date, status) "
            "VALUES ('run-queued-1', 'pipeline', 'g-wf5', '20260601', 'queued')",
        )
        await conn.commit()

        resp = await client.post("/api/v1/runs/run-queued-1/cancel")
        assert resp.status_code == 200, f"Step4: Expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["status"] == "cancelled"

        # ── Step 5: Seed a "completed" run and cancel it → 409 ──
        await conn.execute(
            "INSERT INTO pipeline_runs (run_id, component, group_id, date, status) "
            "VALUES ('run-done-1', 'pipeline', 'g-wf5', '20260601', 'completed')",
        )
        await conn.commit()

        resp = await client.post("/api/v1/runs/run-done-1/cancel")
        assert resp.status_code == 409, f"Step5: Expected 409, got {resp.status_code}: {resp.text}"

        # ── Step 6: Cancel nonexistent run → 404 ──
        resp = await client.post("/api/v1/runs/nonexistent-run/cancel")
        assert resp.status_code == 404, f"Step6: Expected 404, got {resp.status_code}: {resp.text}"

        # ── Step 7: POST /runs/batch → 202 ──
        resp = await client.post(
            "/api/v1/runs/batch",
            json={
                "items": [
                    {"component": "pipeline", "group_id": "g-wf5", "date": "2026-06-02"},
                    {"component": "pipeline", "group_id": "g-wf5", "date": "2026-06-03"},
                ],
            },
        )
        assert resp.status_code == 202, f"Step7: Expected 202, got {resp.status_code}: {resp.text}"
        batch_body = resp.json()
        assert "task_id" in batch_body

        # ── Step 8: GET /runs/stream → 200, SSE headers ──
        # Patch stream_runs to yield only 1 event (avoid 300-iteration loop in test)
        from unittest.mock import patch

        from z_winnow.web.services import run_service as _rs

        _original_stream = _rs.stream_runs

        async def _one_shot_stream(db_path, *, poll_interval_s=0.01, max_iterations=1):
            async for event in _original_stream(db_path, poll_interval_s=0.01, max_iterations=1):
                yield event

        with patch.object(_rs, "stream_runs", _one_shot_stream):
            resp = await client.get("/api/v1/runs/stream")
            assert resp.status_code == 200, (
                f"Step8: Expected 200, got {resp.status_code}: {resp.text}"
            )
            assert "text/event-stream" in resp.headers.get("content-type", "")
