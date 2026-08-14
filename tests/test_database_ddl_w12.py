"""T-W12-1: DDL batch correction tests (L0-1 + L0-2 + L0-3).

Tests verify:
  B1: report_versions.content is nullable (no NOT NULL in PRAGMA table_info)
  B2: content=NULL can be inserted without error
  B3: report_versions.source DDL comment reflects standard enum values
  B4: idx_pipeline_runs_group_date exists in new database
  R1: Real SQLite migration — no mocks
"""

import aiosqlite
import pytest

from z_winnow.pipeline.database import (
    REPORT_SCHEMA_SQL,
    SCHEMA_SQL,
    init_database_in_conn,
)


@pytest.fixture
async def fresh_db():
    """Create an in-memory SQLite database with full schema initialized."""
    db = await aiosqlite.connect(":memory:")
    await init_database_in_conn(db)
    yield db
    await db.close()


# ============================================================
# B1: content column is nullable (L0-1)
# ============================================================


class TestContentNullable:
    """L0-1: report_versions.content should be TEXT (nullable)."""

    async def test_pragma_shows_content_notnull_zero(self, fresh_db):
        """B1: PRAGMA table_info shows content column has notnull=0."""
        cursor = await fresh_db.execute("PRAGMA table_info(report_versions)")
        rows = await cursor.fetchall()
        content_col = [r for r in rows if r[1] == "content"]
        assert len(content_col) == 1, "content column must exist"
        # r[3] is the notnull flag: 0 = nullable, 1 = NOT NULL
        assert content_col[0][3] == 0, "content column must be nullable (notnull=0)"

    async def test_insert_content_null_succeeds(self, fresh_db):
        """B2: Inserting content=NULL should not raise an error."""
        await fresh_db.execute(
            """INSERT INTO report_versions
               (version_id, report_id, group_id, date, version_number, content, source)
               VALUES (?, ?, ?, ?, ?, NULL, ?)""",
            ("v1", "r1", "g1", "20260520", 1, "daily_run"),
        )
        await fresh_db.commit()

        cursor = await fresh_db.execute(
            "SELECT content FROM report_versions WHERE version_id = ?", ("v1",)
        )
        row = await cursor.fetchone()
        assert row[0] is None, "content should be NULL after insert"

    async def test_insert_content_value_still_works(self, fresh_db):
        """Non-NULL content should still work after nullable change."""
        await fresh_db.execute(
            """INSERT INTO report_versions
               (version_id, report_id, group_id, date, version_number, content, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("v2", "r2", "g1", "20260520", 1, "# Report Content", "daily_run"),
        )
        await fresh_db.commit()

        cursor = await fresh_db.execute(
            "SELECT content FROM report_versions WHERE version_id = ?", ("v2",)
        )
        row = await cursor.fetchone()
        assert row[0] == "# Report Content"


# ============================================================
# B3: source enum comment reflects standard values (L0-2)
# ============================================================


class TestSourceEnumStandard:
    """L0-2: source DDL comment must reflect standard enum values."""

    def test_report_schema_sql_contains_standard_enum(self):
        """B3: REPORT_SCHEMA_SQL comment includes standard enum values."""
        assert "incremental_fix" in REPORT_SCHEMA_SQL, (
            "REPORT_SCHEMA_SQL must reference 'incremental_fix'"
        )
        assert "manual_regen" in REPORT_SCHEMA_SQL, (
            "REPORT_SCHEMA_SQL must reference 'manual_regen'"
        )

    def test_report_schema_sql_no_legacy_enum(self):
        """B3: REPORT_SCHEMA_SQL must not contain legacy enum values."""
        # Check the source line specifically (not general words)
        source_line = [
            line for line in REPORT_SCHEMA_SQL.split("\n") if "source" in line and "TEXT" in line
        ]
        assert len(source_line) == 1, "Expected exactly one source column definition"
        assert "regenerate" not in source_line[0], (
            "Legacy 'regenerate' must be replaced with 'incremental_fix'"
        )
        # 'manual' alone (not 'manual_regen') should not appear
        # We check that the comment has manual_regen, not just "manual"
        assert '"manual"' not in source_line[0], (
            "Legacy '\"manual\"' must be replaced with 'manual_regen'"
        )


# ============================================================
# B4: idx_pipeline_runs_group_date exists in new database (L0-3)
# ============================================================


class TestPipelineRunsIndex:
    """L0-3: idx_pipeline_runs_group_date must exist in new database."""

    async def test_index_exists_in_new_db(self, fresh_db):
        """B4: idx_pipeline_runs_group_date exists in sqlite_master."""
        cursor = await fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_pipeline_runs_group_date",),
        )
        row = await cursor.fetchone()
        assert row is not None, "idx_pipeline_runs_group_date must exist in new database"

    async def test_index_columns_correct(self, fresh_db):
        """Verify the index is on (group_id, date)."""
        cursor = await fresh_db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_pipeline_runs_group_date",),
        )
        row = await cursor.fetchone()
        assert row is not None
        sql: str = row[0]
        assert "group_id" in sql, "Index must cover group_id"
        assert "date" in sql, "Index must cover date"

    def test_schema_sql_contains_index(self):
        """SCHEMA_SQL constant must include the index definition."""
        assert "idx_pipeline_runs_group_date" in SCHEMA_SQL, (
            "SCHEMA_SQL must define idx_pipeline_runs_group_date"
        )


# ============================================================
# R1: Real SQLite migration — full schema verification
# ============================================================


class TestRealSqliteMigration:
    """R1: Verify real SQLite migration creates correct schema."""

    async def test_full_schema_after_init(self, fresh_db):
        """Create new database → verify all DDL corrections applied."""
        # B1: content nullable
        cursor = await fresh_db.execute("PRAGMA table_info(report_versions)")
        rows = await cursor.fetchall()
        content_col = [r for r in rows if r[1] == "content"]
        assert content_col[0][3] == 0, "content must be nullable"

        # B3: source column exists (comment is verified in unit tests above)
        source_col = [r for r in rows if r[1] == "source"]
        assert len(source_col) == 1, "source column must exist"

        # B4: index exists
        cursor = await fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_pipeline_runs_group_date",),
        )
        assert await cursor.fetchone() is not None, "index must exist"

    async def test_migration_idempotent(self):
        """Running init_database_in_conn twice produces same schema."""
        db = await aiosqlite.connect(":memory:")
        await init_database_in_conn(db)
        await init_database_in_conn(db)  # second run — must not fail

        # Verify schema still correct after double-init
        cursor = await db.execute("PRAGMA table_info(report_versions)")
        rows = await cursor.fetchall()
        content_col = [r for r in rows if r[1] == "content"]
        assert content_col[0][3] == 0, "content must remain nullable after re-init"

        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_pipeline_runs_group_date",),
        )
        assert await cursor.fetchone() is not None

        await db.close()
