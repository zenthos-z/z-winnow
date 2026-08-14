"""W16-INT integration reconciliation (P0 three-axis tail card).

Mock-mode, in-process TestClient reconciliation of the P0 three axes
(schema single source / config db_path single source / dead-code cleanup)
and the seven P0 root-cause bugs they cover: B1, B2, B4, B7, B8, B9, B10.

This is a mock-mode test (FastAPI in-process client + monkeypatch,
WINNOW_REAL_LLM off). It deliberately carries NO integration/e2e/slow
markers so it runs inside the not-integration gate — a test excluded from
that gate would let the end-to-end assertions silently no-op (A010).

Assertion matrix (AC B3 coverage, one area per class):
  - B7  : empty/None Topic renders without TypeError across A1(schema default)
          -> A2(composer tolerance) -> A4(Jinja2 renderer line 27 t.trend[:80]).
  - B8  : AsyncTaskResponse.status_url resolves to a real per-report task
          route returning 200 (both regenerate and feishu paths).
  - B9  : judge route resolves report_id -> (group_id,date,version_id) and
          reads body.dimensions (documented downstream noop) WITHOUT
          fabricating a run_judge dimensions parameter.
  - B4  : export endpoint returns a bare text/markdown string (no JSON
          response_model wrap), consumable via .text().
  - B1  : Topic schema has no `sections` field (flat-field source of truth).
  - B2  : output_composer self-loop (compose_final_report) removed.
  - B10 : frontend index.html has no dead settings.html / groups.html links.
  - schema axis : Topic/Resource/EngineeringIssue strongly-typed, extra='allow',
                  all-default (root-out-None).
  - db_path axis: sqlite_db_path is a read-only @property mirror of db_path;
                  the four cross-cut files carry no bare SQLITE_DB_PATH getenv.
  - dead-code axis: layer3_storage / OrchestratorState / orchestrator_plan /
                  image_gen / compat / deepagents dead symbols all removed.

# P046: post-split cross-module aggregation verification — this is the tail card,
#        it does not re-run module-level single-point checks (those are A4/A2/C1),
#        it exercises cross-module contracts (render chain, status_url resolution,
#        judge passthrough, export contract).
# L039: integration surfaces cross-module dependency bugs -> we exercise the
#        empty-Topic + dimensions-passthrough + status_url-resolution combinations.
# A010/A021: every assertion truly executes the business path — real Jinja2
#            render, real GET on the returned status_url, real spy on run_judge,
#            real export through the route. No import-only / default-path shortcuts.
# L013/L044/L028/L045: mock TestClient + monkeypatch; no real SSE / external calls.
# A018/L100: where data is involved we use the real composer/renderer and real
#            in-memory SQLite (P078), not a mocked database.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
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

GROUP_ID = "w16int-group"
DATE = "20260601"
REPORT_ID = "w16int-group-20260601"
VERSION_ID = "w16int-group-20260601-v1"


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def app() -> FastAPI:
    """Create a test FastAPI app with the api_router mounted (prefix /api/v1)."""
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
    """Seed one report_versions row resolvable by version_id and report_id."""
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
            "w16_int_seed",
            1.0,
            "2026-06-01T10:00:00Z",
        ),
    )
    await conn.commit()


def _client_ctx(app: FastAPI):
    """Build an in-process AsyncClient context manager over the ASGI app."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ============================================================
# B7: empty/None Topic renders without TypeError (A1 -> A2 -> A4)
# ============================================================


class TestB7EmptyTopicRender:
    """B7: rendering a report whose Topic has empty/None fields must not raise.

    Root cause: daily_report.j2:27 ``{{ t.trend[:80] }}`` raised TypeError when
    ``t.trend`` was None. A1 makes every Topic field default to "" (root-out
    None); A2's composer tolerates a legacy None-trend element and normalises
    it via per-item fallback (L037); the Jinja2 renderer then slices "" safely.
    """

    @pytest.mark.asyncio
    async def test_b7_none_trend_topic_renders_without_typeerror(self) -> None:
        """A010/A021: real composer + real Jinja2 render of an empty Topic."""
        from z_winnow.subagents.output_composer import (
            _dict_to_composed,
            render_composed,
        )
        from z_winnow.subagents.unified_reporter.models import Topic

        # A1: a Topic with nothing supplied has trend == "" (root-out-None).
        default_dump = Topic().model_dump()
        assert default_dump["trend"] == "", "Topic.trend must default to '' not None"

        # A2: composer tolerates a legacy topic carrying trend=None (the original
        # B7 input shape) plus a topic missing trend entirely. Per-item isolation
        # (L037) + A1 defaults normalise both so None never leaks.
        unified = {
            "overview": "B7 integration overview",
            "trend_analysis": "B7 trend analysis",
            "topics": [
                {"topic_id": "t-empty", "topic_name": "EmptyTopic"},
                {"topic_id": "t-null", "topic_name": "NullTrendTopic", "trend": None},
            ],
        }
        composed = _dict_to_composed(unified, date=DATE)
        assert len(composed.topics) == 2, "composer must emit both topics"
        for topic in composed.topics:
            # None must NOT leak through to the renderer (B7 root cause).
            assert isinstance(topic.get("trend"), str), (
                "trend leaked as non-str after composer normalisation"
            )

        # A4: actually render via the Jinja2 daily_report template. Before the A1
        # fix a None trend raised TypeError on line 27 ``t.trend[:80]``. The
        # EmptyTopic (A1 default path) keeps its name; the NullTrendTopic was
        # normalised to a default instance by the composer fallback (L037).
        markdown = render_composed(composed)
        assert isinstance(markdown, str) and markdown, "render produced no output"
        assert "EmptyTopic" in markdown, "default-path topic must render with its name"


# ============================================================
# B8: status_url resolves to a real per-report task route (200)
# ============================================================


class TestB8StatusUrlRealRoute:
    """B8: AsyncTaskResponse.status_url points at a route that really exists.

    The previous status link pointed at a non-existent tasks route. The fix
    (global decision, reports self-contained per-task route) emits
    ``/api/v1/reports/{rid}/tasks/{task_id}`` which really reads async_tasks.
    We GET the returned status_url and assert 200 (not 404).
    """

    @pytest.mark.asyncio
    async def test_b8_regenerate_status_url_get_returns_200(
        self,
        app: FastAPI,
        db_conn: aiosqlite.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """B8: regenerate -> GET its status_url -> 200."""
        import asyncio

        from z_winnow.config.settings import get_settings

        await _seed_report_version(db_conn)
        app.state.db_conn = db_conn

        # task_queue persists to settings.db_path; point both start_task (insert)
        # and the GET endpoint (read) at the same shared temp file (monkeypatch
        # restores the original at teardown — no singleton leak).
        db_file = str(tmp_path / "taskq_regenerate.db")
        monkeypatch.setattr(get_settings(), "db_path", db_file)

        # Keep the background regenerate coro from building the real pipeline
        # graph (mock-only heavy work; the row insert is what we assert on).
        with patch("z_winnow.graph.builder.build_graph") as mock_build:
            mock_build.return_value.ainvoke = AsyncMock(return_value={})
            async with _client_ctx(app) as client:
                resp = await client.post(f"/api/v1/reports/{VERSION_ID}/regenerate")

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        data = resp.json()
        task_id = data["task_id"]
        # validate UUID shape (real start_task output)
        uuid.UUID(task_id)
        status_url = data["status_url"]
        assert status_url.endswith(f"/tasks/{task_id}"), (
            f"status_url must point at the per-task route, got {status_url}"
        )

        # drain the (mocked) background task so it does not linger past teardown
        await asyncio.sleep(0.1)

        # A010: actually GET the status_url returned by the endpoint -> must be 200
        async with _client_ctx(app) as client:
            resp2 = await client.get(status_url)

        assert resp2.status_code == 200, (
            f"status_url GET must be 200 (route exists), got {resp2.status_code}: {resp2.text}"
        )
        body = resp2.json()
        assert body["task_id"] == task_id
        assert "status" in body

    @pytest.mark.asyncio
    async def test_b8_feishu_status_url_get_returns_200(
        self,
        app: FastAPI,
        db_conn: aiosqlite.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """B8: feishu push -> GET its status_url -> 200 (feishu disabled = no network)."""
        import asyncio

        from z_winnow.config.settings import get_settings

        await _seed_report_version(db_conn)
        app.state.db_conn = db_conn

        db_file = str(tmp_path / "taskq_feishu.db")
        monkeypatch.setattr(get_settings(), "db_path", db_file)
        # feishu disabled -> background coro returns early, no network call
        monkeypatch.setattr(get_settings(), "feishu_enabled", False)

        async with _client_ctx(app) as client:
            resp = await client.post(f"/api/v1/reports/{REPORT_ID}/feishu")

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        data = resp.json()
        task_id = data["task_id"]
        uuid.UUID(task_id)
        status_url = data["status_url"]
        assert status_url.endswith(f"/tasks/{task_id}"), (
            f"status_url must point at the per-task route, got {status_url}"
        )

        await asyncio.sleep(0.1)

        async with _client_ctx(app) as client:
            resp2 = await client.get(status_url)

        assert resp2.status_code == 200, (
            f"status_url GET must be 200 (route exists), got {resp2.status_code}: {resp2.text}"
        )
        assert resp2.json()["task_id"] == task_id


# ============================================================
# B9 / B1b: judge resolves coords + dimensions read (no run_judge signature lock-in)
# ============================================================


class TestB9JudgePassthrough:
    """B9: judge route resolves report_id -> (group_id,date,version_id) and reads
    body.dimensions (documented downstream noop), without fabricating a run_judge
    dimensions parameter (run_judge has no such parameter on disk).
    """

    @pytest.mark.asyncio
    async def test_b9_resolves_coords_and_reads_dimensions(
        self,
        app: FastAPI,
        db_conn: aiosqlite.Connection,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A010/A021: real POST /judge with a spy on judge_service.run_judge."""
        await _seed_report_version(db_conn)
        app.state.db_conn = db_conn

        # judge.py imports run_judge *inside* the handler, so patching the module
        # attribute is picked up at call time. Real run_judge has the signature
        # run_judge(group_id, date, version_id=..., db_path=...) — NO dimensions.
        with patch(
            "z_winnow.web.services.judge_service.run_judge",
            new=AsyncMock(return_value="judge-task-id"),
        ) as mock_run:
            caplog.set_level(logging.INFO)
            async with _client_ctx(app) as client:
                resp = await client.post(
                    "/api/v1/judge",
                    json={"report_id": VERSION_ID, "dimensions": ["clarity", "accuracy"]},
                )

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"

        # B9: run_judge received non-empty, correctly-resolved coordinates.
        mock_run.assert_awaited_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("group_id") == GROUP_ID, (
            f"group_id must resolve to seeded value, got {kwargs.get('group_id')!r}"
        )
        assert kwargs.get("date") == DATE, (
            f"date must resolve to seeded value, got {kwargs.get('date')!r}"
        )
        assert kwargs.get("version_id") == VERSION_ID, (
            f"version_id must resolve correctly, got {kwargs.get('version_id')!r}"
        )
        # B1b: the route must NOT fabricate a dimensions parameter for run_judge
        # (its real signature has none).
        assert "dimensions" not in kwargs, (
            "route must not fabricate a run_judge dimensions parameter"
        )

        # B1b: body.dimensions must be READ by the route (documented as downstream
        # noop via logging), not silently dropped. This proves the route no longer
        # discards dimensions at the route layer.
        dimension_messages = [r.getMessage() for r in caplog.records if "clarity" in r.getMessage()]
        assert dimension_messages, (
            "route must surface body.dimensions (documented noop), not drop it silently"
        )


# ============================================================
# B4: export returns bare text/markdown (no JSON wrap)
# ============================================================


class TestB4ExportBareMarkdown:
    """B4: export endpoint returns a bare text/markdown string, not a JSON-wrapped
    dict. Frontend ``.text()`` consumes it (``.json()`` would have failed 100%)."""

    @pytest.mark.asyncio
    async def test_b4_export_returns_bare_text_markdown(
        self,
        app: FastAPI,
        db_conn: aiosqlite.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A010: real L3 JSON on disk -> real Jinja2 render -> real route GET."""
        from z_winnow.config.settings import get_settings

        await _seed_report_version(db_conn)
        app.state.db_conn = db_conn

        # real L3 JSON so export_report renders via render_markdown (no service mock).
        # The topic dict is emitted via Topic().model_dump() so it carries every key
        # the daily_report.j2 template slices (conclusion/trend/participants/...).
        # This is the schema single-source contract: on-disk JSON is a complete,
        # Topic-shaped dict -> render-safe (no UndefinedError on missing keys).
        from z_winnow.subagents.unified_reporter.models import Topic

        topic_dict = Topic(topic_id="t1", topic_name="ExportTopic").model_dump()

        l3_root = tmp_path / "data" / "processed"
        l3_dir = l3_root / GROUP_ID / DATE
        l3_dir.mkdir(parents=True)
        (l3_dir / "daily.json").write_text(
            json.dumps(
                {
                    "date": DATE,
                    "overview": "B4 export overview",
                    "important_notice": "",
                    "topics": [topic_dict],
                    "trend_analysis": "B4 trend analysis",
                    "trend_summary": "",
                    "highlights": [],
                }
            ),
            encoding="utf-8",
        )
        (l3_dir / "resources.json").write_text(
            json.dumps({"date": DATE, "resources": [], "count_by_type": {}, "total_count": 0}),
            encoding="utf-8",
        )
        (l3_dir / "engineering.json").write_text(
            json.dumps({"date": DATE, "engineering_issues": [], "group_summary": {}, "model_used": ""}),
            encoding="utf-8",
        )
        (l3_dir / "topics.json").write_text(
            json.dumps({"date": DATE, "topics": [], "trend_summary": "", "lifecycle_counts": {}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(get_settings(), "layer3_output_dir", str(l3_root))

        async with _client_ctx(app) as client:
            resp = await client.get(f"/api/v1/reports/{VERSION_ID}/export")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        # B4: media_type is text/markdown (bare markdown response)
        assert resp.headers["content-type"].startswith("text/markdown"), (
            f"Content-Type must be text/markdown, got {resp.headers['content-type']!r}"
        )

        # B4: body is a bare string, NOT a JSON-wrapped dict. Before the contract
        # fix the frontend parsed this via .json() and failed 100%. A bare markdown
        # string must not parse as a JSON object.
        text = resp.text
        assert isinstance(text, str) and text, "export body must be a non-empty string"
        stripped = text.lstrip()
        assert not stripped.startswith("{"), (
            "export body must be bare markdown, not a JSON object wrapper"
        )
        # .text() is consumable: the real render produced markdown with the topic
        assert "ExportTopic" in text, "real render output must contain the seeded topic"


# ============================================================
# Schema axis + B1 + B2 (W16-A1 / W16-A2 single source of truth)
# ============================================================


class TestAxisSchemaSingleSource:
    """Schema axis: Topic/Resource/EngineeringIssue are the single field-definition
    source of truth, strongly-typed with all defaults (root-out-None)."""

    def test_b1_topic_has_no_sections_field(self) -> None:
        """B1: Topic has no `sections` field — the flat-field path is the source
        of truth (board r1-W16-A1-0: a sections sub-model would be an A026 zombie)."""
        from z_winnow.subagents.unified_reporter.models import Topic

        assert "sections" not in Topic.model_fields, (
            "Topic must not define a sections field (flat-field contract)"
        )

    def test_schema_models_root_out_none_and_allow_extra(self) -> None:
        """A1: the three L3-record models default every field (no None) and allow
        legacy extra fields (P045 backward-compatible tolerance)."""
        from z_winnow.subagents.unified_reporter.models import (
            EngineeringIssue,
            Resource,
            Topic,
        )

        for model in (Topic, Resource, EngineeringIssue):
            assert model.model_config.get("extra") == "allow", (
                f"{model.__name__} must allow extra fields (P045 tolerance)"
            )

        # Topic: every declared str field defaults to "" (B7 root-out-None).
        topic_dump = Topic().model_dump()
        for key in (
            "topic_name",
            "lifecycle",
            "status",
            "conclusion",
            "description",
            "trend",
            "first_seen",
            "last_seen",
        ):
            assert topic_dump[key] == "", f"Topic.{key} must default to '' not None"
        assert topic_dump["participants"] == []
        assert topic_dump["weight"] == 0.0

        # Resource / EngineeringIssue: their str fields default to "" too.
        resource_dump = Resource().model_dump()
        for key in ("resource_type", "resource_title", "summary", "content"):
            assert resource_dump[key] == "", f"Resource.{key} must default to ''"
        issue_dump = EngineeringIssue().model_dump()
        for key in ("group", "description", "solution", "status"):
            assert issue_dump[key] == "", f"EngineeringIssue.{key} must default to ''"

    def test_b2_output_composer_self_loop_removed(self) -> None:
        """B2: the output_composer self-loop entry point (compose_final_report)
        and its degraded-rendering submodule were removed as dead code (A2).
        L070: importing the package still succeeds; only the dead symbols are gone."""
        import z_winnow.subagents.output_composer as composer

        assert not hasattr(composer, "compose_final_report"), (
            "compose_final_report self-loop must be removed (B2 dead code)"
        )
        # the degraded submodule (the other half of the removed self-loop) is gone
        assert (
            importlib.util.find_spec("z_winnow.subagents.output_composer.degraded")
            is None
        ), "degraded submodule must be removed (B2 dead code)"
        # the package itself still imports cleanly (L070)
        assert hasattr(composer, "compose_json") and hasattr(composer, "render_markdown")


# ============================================================
# Config axis (W16-B2 / W16-B3): db_path single source of truth
# ============================================================


class TestAxisConfigDbPath:
    """Config axis: settings.db_path is the single source of truth; sqlite_db_path
    is a read-only @property mirror, and the cross-cut callers no longer read a
    bare SQLITE_DB_PATH env var (converged onto get_settings().db_path)."""

    def test_sqlite_db_path_is_property_mirror_of_db_path(self) -> None:
        """W16-B2: sqlite_db_path is a read-only @property, not an independent Field."""
        from z_winnow.config.settings import Settings, get_settings

        # A @property on the class (not a pydantic Field)
        attr = inspect.getattr_static(Settings, "sqlite_db_path")
        assert isinstance(attr, property), (
            "sqlite_db_path must be a read-only @property mirror, not a Field"
        )
        # mirror invariant: same value as db_path
        settings = get_settings()
        assert settings.sqlite_db_path == settings.db_path, (
            "sqlite_db_path must mirror db_path (single source of truth)"
        )

    def test_b3_cross_cut_files_have_no_bare_sqlite_db_path_getenv(self) -> None:
        """W16-B3: the four cross-cut files no longer read a bare SQLITE_DB_PATH
        env var — they go through get_settings().db_path (A013)."""
        root = Path(__file__).resolve().parent.parent / "src" / "z_winnow"
        cross_cut = [
            "graph/progress.py",
            "memory/sync_worker.py",
            "orchestrator/orchestrator_loop.py",
            "storage.py",
        ]
        for rel in cross_cut:
            text = (root / rel).read_text(encoding="utf-8")
            assert "SQLITE_DB_PATH" not in text, (
                f"{rel} must not reference SQLITE_DB_PATH (converged onto db_path)"
            )


# ============================================================
# Dead-code axis (W16-C1) + B10 frontend
# ============================================================


class TestAxisDeadCodeAndFrontend:
    """Dead-code axis: the C1 removals (layer3_storage / OrchestratorState /
    orchestrator_plan / image_gen / compat / deepagents dead symbols) are gone.
    B10: the frontend index.html no longer links to dead settings/groups pages."""

    def test_c1_dead_modules_removed(self) -> None:
        """C1: deleted submodules are no longer importable.

        Note: ``outputs.image_gen`` was removed in W16-C1 but revived by #9.2
        (daily cover-image generation via DMX/Gemini) — no longer dead.
        """
        for modname in (
            "z_winnow.pipeline.layer3_storage",
            "z_winnow.graph.nodes.orchestrator_plan",
            "z_winnow.compat",
        ):
            assert importlib.util.find_spec(modname) is None, (
                f"{modname} must be removed (W16-C1 dead code)"
            )

    def test_c1_dead_symbols_removed(self) -> None:
        """C1: OrchestratorState and the deepagents orchestration dead symbols
        are gone from their (still-active) parent modules."""
        import z_winnow.orchestrator.orchestrator_loop as orchestrator_loop
        import z_winnow.state as state

        assert not hasattr(state, "OrchestratorState"), (
            "state.OrchestratorState must be removed (W16-C1)"
        )
        for symbol in (
            "create_orchestrator_agent",
            "OrchestratorMiddleware",
            "OrchestratorToolFilterMiddleware",
        ):
            assert not hasattr(orchestrator_loop, symbol), (
                f"orchestrator_loop.{symbol} must be removed (W16-C1 dead code)"
            )

    def test_b10_frontend_has_no_dead_links(self) -> None:
        """B10: every local *.html link in index.html resolves to an existing static page.

        Originally asserted index.html had no settings/groups links (those were dead
        pages during W16 cleanup). settings.html is now a real, linked page, so the
        check is generalized: any ``href="page.html[?...]"`` must point to a file in
        static/. Keeps the intent (no dead links) without going stale when pages are
        added/renamed.
        """
        import re

        static_dir = (
            Path(__file__).resolve().parent.parent / "src" / "z_winnow" / "web" / "static"
        )
        index_html = (static_dir / "index.html").read_text(encoding="utf-8")

        targets = re.findall(r'href="([A-Za-z0-9_\-]+\.html)(?:\?[^"]*)?"', index_html)
        assert targets, "index.html should link to at least one local .html page"

        missing = sorted({t for t in targets if not (static_dir / t).exists()})
        assert not missing, f"index.html links to non-existent static pages: {missing}"
