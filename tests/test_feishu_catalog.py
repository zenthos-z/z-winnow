"""Tests for the pluggable Feishu table catalog (#9.4).

Covers:
- ``TABLE_CATALOG`` integrity (every kind has fields + a callable mapper; the
  mandatory spine is exactly summary/topics/resources).
- ``active_kinds`` / ``table_cfg`` / ``default_tables_config`` selection logic.
- The ``migrate_groups_add_feishu_tables_blob`` backfill: a group persisted
  before the blob column gets its blob synthesized from the legacy 4 columns.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from z_winnow.pipeline.feishu import schema

# ============================================================
# Catalog integrity
# ============================================================


def test_catalog_has_expected_kinds() -> None:
    assert set(schema.TABLE_CATALOG) == {
        "summary",
        "topics",
        "resources",
        "engineering",
        "world_models",
    }


def test_mandatory_spine_is_summary_topics_resources() -> None:
    """User-confirmed core: 议题 + 资源 + 日报汇总 are mandatory for every group."""
    assert frozenset({"summary", "topics", "resources"}) == schema.MANDATORY_KINDS


def test_every_table_def_is_well_formed() -> None:
    for kind, tdef in schema.TABLE_CATALOG.items():
        assert tdef.kind == kind
        assert tdef.display_name, f"{kind} missing display_name"
        assert isinstance(tdef.fields, list) and tdef.fields, f"{kind} missing fields"
        assert callable(tdef.source.mapper), f"{kind} mapper not callable"
        assert isinstance(tdef.source.l3_key, str) and tdef.source.l3_key
        # Mandatory kinds can't also declare themselves optional-default-off.
        if tdef.mandatory:
            assert tdef.kind in schema.MANDATORY_KINDS


def test_attachment_hooks_declared_on_summary_and_resources() -> None:
    """The uploader dispatches attachments per TableDef.attachments (no if-kind)."""
    assert "daily_md" in schema.TABLE_CATALOG["summary"].attachments
    assert "cover" in schema.TABLE_CATALOG["summary"].attachments
    assert "resource_files" in schema.TABLE_CATALOG["resources"].attachments
    # Topics/engineering have no attachment hooks.
    assert schema.TABLE_CATALOG["topics"].attachments == ()
    assert schema.TABLE_CATALOG["engineering"].attachments == ()


def test_each_mapper_returns_column_oriented_shape() -> None:
    """Every mapper returns (columns: list[str], rows: list[list]) — even on empty."""
    sample = {
        "daily": {"date": "20260709", "topics": []},
        "resources": {"resources": []},
        "engineering": {"issues": []},
        "world_models": {"items": []},
    }
    for _kind, tdef in schema.TABLE_CATALOG.items():
        cols, rows = tdef.source.mapper(sample[tdef.source.l3_key], "20260709")
        assert isinstance(cols, list) and all(isinstance(c, str) for c in cols)
        assert isinstance(rows, list)


# ============================================================
# Selection logic
# ============================================================


def test_active_kinds_empty_config_is_mandatory_only() -> None:
    assert schema.active_kinds({}) == ["summary", "topics", "resources"]
    assert schema.active_kinds(None) == ["summary", "topics", "resources"]


def test_active_kinds_engineering_toggles() -> None:
    assert "engineering" not in schema.active_kinds({"engineering": {"enabled": False}})
    assert "engineering" in schema.active_kinds({"engineering": {"enabled": True}})


def test_active_kinds_ignores_unknown_kinds() -> None:
    """A future/unknown kind in the blob doesn't crash older code."""
    active = schema.active_kinds({"academic": {"enabled": True}, "engineering": {"enabled": True}})
    assert "academic" not in active
    assert set(active) == {"summary", "topics", "resources", "engineering"}


def test_active_kinds_preserves_catalog_order() -> None:
    """Order matters: first kind is created inline with base_create."""
    assert schema.active_kinds({"engineering": {"enabled": True}}) == [
        "summary",
        "topics",
        "resources",
        "engineering",
    ]


def test_table_cfg_defaults() -> None:
    # Mandatory kind defaults to enabled even when absent from the blob.
    assert schema.table_cfg({}, "summary") == {"enabled": True, "table_id": ""}
    # Optional kind defaults to disabled.
    assert schema.table_cfg({}, "engineering") == {"enabled": False, "table_id": ""}
    # Present entry is read through.
    cfg = {"engineering": {"enabled": True, "table_id": "tblX"}}
    assert schema.table_cfg(cfg, "engineering") == {"enabled": True, "table_id": "tblX"}


def test_default_tables_config() -> None:
    blob = schema.default_tables_config()
    assert set(blob) == set(schema.TABLE_CATALOG)
    # Mandatory on, engineering off by default (default_enabled=False, #7.1).
    for kind in schema.MANDATORY_KINDS:
        assert blob[kind]["enabled"] is True
    assert blob["engineering"]["enabled"] is False
    assert all(v["table_id"] == "" for v in blob.values())


# ============================================================
# Backfill migration (legacy 4 columns → blob)
# ============================================================


@pytest.mark.asyncio
async def test_migrate_backfills_blob_from_legacy_columns(tmp_path: Path) -> None:
    """A group with the old 4 columns + engineering_enabled but no blob gets a
    synthesized blob after migrate_groups_add_feishu_tables_blob."""
    from z_winnow.pipeline.database import (
        init_database_in_conn,
        migrate_groups_add_feishu_tables_blob,
    )

    db = await aiosqlite.connect(str(tmp_path / "t.db"))
    await init_database_in_conn(db)
    # init_database already ran the blob migration once (no-op backfill on empty).
    # Insert a group with legacy columns populated (simulating a pre-#9.9 group).
    await db.execute(
        """INSERT INTO groups
           (group_id, display_name, chatroom_id,
            feishu_table_summary, feishu_table_topics, feishu_table_resources,
            feishu_table_engineering, feishu_engineering_enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "g_legacy",
            "旧群",
            "r@x",
            "tblSum",
            "tblTop",
            "tblRes",
            "tblEng",
            0,  # engineering disabled
        ),
    )
    await db.commit()

    # Run the migration again — it backfills rows where feishu_tables is NULL.
    await migrate_groups_add_feishu_tables_blob(db)

    cur = await db.execute("SELECT feishu_tables FROM groups WHERE group_id = ?", ("g_legacy",))
    row = await cur.fetchone()
    await db.close()

    assert row is not None and row[0]
    blob = json.loads(row[0])
    assert blob["summary"] == {"enabled": True, "table_id": "tblSum"}
    assert blob["topics"] == {"enabled": True, "table_id": "tblTop"}
    assert blob["resources"] == {"enabled": True, "table_id": "tblRes"}
    assert blob["engineering"] == {"enabled": False, "table_id": "tblEng"}


@pytest.mark.asyncio
async def test_migrate_is_idempotent(tmp_path: Path) -> None:
    """Running the migration twice doesn't clobber an existing blob."""
    from z_winnow.pipeline.database import (
        init_database_in_conn,
        migrate_groups_add_feishu_tables_blob,
    )

    db = await aiosqlite.connect(str(tmp_path / "t2.db"))
    await init_database_in_conn(db)
    pre = json.dumps(
        {"summary": {"enabled": True, "table_id": "keep"}, "engineering": {"enabled": False}}
    )
    await db.execute(
        "INSERT INTO groups(group_id, display_name, chatroom_id, feishu_tables) VALUES (?, ?, ?, ?)",
        ("g", "d", "r", pre),
    )
    await db.commit()

    await migrate_groups_add_feishu_tables_blob(db)  # should NOT overwrite the existing blob

    cur = await db.execute("SELECT feishu_tables FROM groups WHERE group_id = ?", ("g",))
    row = await cur.fetchone()
    await db.close()
    assert row is not None
    assert json.loads(row[0])["summary"]["table_id"] == "keep"


# ============================================================
# HTTP endpoint (GET /feishu/catalog)
# ============================================================


def test_feishu_catalog_endpoint_returns_kinds() -> None:
    """GET /api/v1/feishu/catalog returns the catalog the UI renders its checklist from."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from z_winnow.web.routes import api_router

    app = FastAPI()
    app.include_router(api_router)
    client = TestClient(app)

    resp = client.get("/api/v1/feishu/catalog")
    assert resp.status_code == 200
    kinds = resp.json()["kinds"]
    by_kind = {k["kind"]: k for k in kinds}
    assert set(by_kind) == {
        "summary",
        "topics",
        "resources",
        "engineering",
        "world_models",
    }
    # mandatory spine
    for k in ("summary", "topics", "resources"):
        assert by_kind[k]["mandatory"] is True
    assert by_kind["engineering"]["mandatory"] is False
    assert by_kind["world_models"]["mandatory"] is False
    # every entry carries the fields the frontend needs
    for k in kinds:
        assert {"kind", "display_name", "mandatory", "default_enabled", "field_count"} <= set(k)
