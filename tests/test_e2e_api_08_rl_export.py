"""E2E API test: RL export workflow — seed data, export, poll, validate, error cases.

Covers the full RL export lifecycle in one test method:
  1. Seed raw_messages + topic_summaries for 3 dates
  2. POST /rl/export → 202 (async task)
  3. Poll GET /rl/export/{task_id} until done
  4. Verify result has output_path + record_count
  5. POST with reversed dates → 422
  6. GET with random UUID → 404

# P078: Real SQLite via tmp_path — no mocked database.
# P012: autouse monkeypatch env isolation.
# A018: Real DDL + real INSERT — no mocked dicts.
# L100: All tests hit real SQLite via real app lifespan.

Usage:
    poetry run pytest tests/test_e2e_api_08_rl_export.py -v
"""

from __future__ import annotations

import asyncio
import uuid
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
# Test: RL export workflow
# ============================================================


class TestRLExportWorkflow:
    """Full RL export lifecycle: seed → export → poll → verify → error cases."""

    async def test_rl_export_workflow(self, client, real_app) -> None:
        import json

        conn = real_app.state.db_conn

        # ── Step 1: Seed raw_messages and topic_summaries for 3 dates ──
        dates = ["20260601", "20260602", "20260603"]
        for date_val in dates:
            # 5 raw_messages per date
            for i in range(5):
                server_id = f"srv-{date_val}-{i:03d}"
                await conn.execute(
                    "INSERT INTO raw_messages "
                    "(serverID, date, group_id, sender, content, msg_type, raw_json) "
                    "VALUES (?, ?, 'g-wf8', ?, ?, 'text', ?)",
                    (
                        server_id,
                        date_val,
                        f"sender_{i}",
                        f"Test message {i} for date {date_val}",
                        json.dumps({"content": f"msg {i}"}),
                    ),
                )
            # 1 topic_summary per date
            summary_id = f"sum-{date_val}-001"
            await conn.execute(
                "INSERT INTO topic_summaries "
                "(summary_id, date, group_id, topic_name, summary_text, context_ids, "
                "source_server_ids, confidence, lifecycle) "
                "VALUES (?, ?, 'g-wf8', ?, ?, ?, ?, 0.85, 'active')",
                (
                    summary_id,
                    date_val,
                    f"Topic for {date_val}",
                    f"Summary text for date {date_val}",
                    "[]",
                    json.dumps([f"srv-{date_val}-{j:03d}" for j in range(5)]),
                ),
            )
        await conn.commit()

        # ── Step 2: POST /rl/export → 202 ──
        resp = await client.post(
            "/api/v1/rl/export",
            json={
                "group_id": "g-wf8",
                "start_date": "2026-06-01",
                "end_date": "2026-06-03",
            },
        )
        assert resp.status_code == 202, f"Step2: Expected 202, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "task_id" in body
        task_id = body["task_id"]

        # ── Step 3: Poll GET /rl/export/{task_id} until done ──
        final_status = None
        for _ in range(30):
            await asyncio.sleep(0.1)
            poll_resp = await client.get(f"/api/v1/rl/export/{task_id}")
            if poll_resp.status_code == 200:
                data = poll_resp.json()
                if data["status"] in ("done", "failed"):
                    final_status = data
                    break

        assert final_status is not None, "Step3: Export task never completed within poll window"
        assert final_status["status"] == "done", (
            f"Step3: Export task failed: {final_status.get('error')}"
        )

        # ── Step 4: Verify result has output_path and record_count ──
        result = final_status.get("result")
        assert result is not None, "Step4: Result is None for completed export"
        assert "output_path" in result, f"Step4: Missing output_path in result: {result}"
        assert "record_count" in result, f"Step4: Missing record_count in result: {result}"

        # ── Step 5: POST with reversed dates → 422 ──
        resp = await client.post(
            "/api/v1/rl/export",
            json={
                "group_id": "g-wf8",
                "start_date": "2026-06-05",
                "end_date": "2026-06-01",
            },
        )
        assert resp.status_code == 422, f"Step5: Expected 422, got {resp.status_code}: {resp.text}"

        # ── Step 6: GET with random UUID → 404 ──
        fake_id = str(uuid.uuid4())
        resp = await client.get(f"/api/v1/rl/export/{fake_id}")
        assert resp.status_code == 404, f"Step6: Expected 404, got {resp.status_code}: {resp.text}"
