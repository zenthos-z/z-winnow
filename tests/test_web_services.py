"""Tests for web service modules (T-W14-3).

# P078: All tests use in-memory SQLite with real DDL via init_database_in_conn.
#       Never mock aiosqlite. Seed data via real INSERT statements.
# P011: Each B-criterion has its own dedicated test function.
# P013: Class-based organization grouping by service module.
# P012: autouse monkeypatch env isolation
# A018: Real DDL + real INSERT — no mocked connections, no hardcoded dicts.
# L100: 100% non-mock data — all tests hit real SQLite :memory: engine.

Usage:
    python -m poetry run pytest tests/test_web_services.py -v
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import pytest

from z_winnow.pipeline.database import init_database_in_conn
from z_winnow.pipeline.report_version import create_version
from z_winnow.web.schemas.core_topics import CoreTopicOut
from z_winnow.web.schemas.data import L3SummaryOut
from z_winnow.web.schemas.groups import GroupMemberOut, GroupOut
from z_winnow.web.schemas.key_people import KeyPeopleOut
from z_winnow.web.schemas.overview import OverviewStatsOut
from z_winnow.web.schemas.reports import ReportVersionOut
from z_winnow.web.services import PaginatedResult
from z_winnow.web.services.group_service import (
    get_group_detail,
    list_core_topics,
    list_groups,
    list_members,
)
from z_winnow.web.services.key_people_service import list_key_people
from z_winnow.web.services.overview_service import (
    get_dashboard_summary,
    get_overview_stats,
)
from z_winnow.web.services.report_service import (
    ReportContent,
    get_report_content,
    get_report_diff,
    get_report_version,
    list_report_versions,
)
from z_winnow.web.services.topic_service import (
    get_topic_detail,
    list_topics,
)

# ============================================================
# P012: Env isolation — autouse monkeypatch
# ============================================================


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """P012: Isolate environment for each test."""
    monkeypatch.setenv("WEFLOW_MOCK_MODE", "true")
    monkeypatch.delenv("WINNOW_WEB_API_KEY", raising=False)
    monkeypatch.delenv("WEB_API_KEY", raising=False)
    # Neutralize the wizard's config_overrides.json injection so get_settings() sees
    # only env/defaults — otherwise persisted overrides (e.g. db_path) leak in and
    # beat the test's monkeypatched env vars (kwargs win).
    monkeypatch.setattr("z_winnow.config.settings._load_overrides", lambda: {})


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
# Helper: seed raw_messages
# ============================================================


async def _seed_raw_messages(
    db: aiosqlite.Connection, count: int, group_id: str = "", date: str = "20260601"
) -> None:
    """Seed raw_messages table with deterministic test data."""
    for i in range(count):
        sender = f"sender_{i % 3}"  # 3 distinct senders
        await db.execute(
            """INSERT INTO raw_messages
               (serverID, date, group_id, sender, content, msg_type, raw_json)
               VALUES (?, ?, ?, ?, ?, 'text', '{}')""",
            (f"srv_{i:04d}", date, group_id, sender, f"Message {i}"),
        )
    await db.commit()


async def _seed_groups(db: aiosqlite.Connection) -> None:
    """Seed groups table: 3 active, 2 inactive."""
    groups_data = [
        ("g_active_1", "Test Group Alpha", "room1@chatroom", 1),
        ("g_active_2", "Development Team", "room2@chatroom", 1),
        ("g_active_3", "Test Beta Group", "room3@chatroom", 1),
        ("g_inactive_1", "Archived Group", "room4@chatroom", 0),
        ("g_inactive_2", "Old Team", "room5@chatroom", 0),
    ]
    for gid, name, chatroom, active in groups_data:
        await db.execute(
            """INSERT INTO groups
               (group_id, display_name, chatroom_id, is_active)
               VALUES (?, ?, ?, ?)""",
            (gid, name, chatroom, active),
        )
    await db.commit()


async def _seed_members(db: aiosqlite.Connection) -> None:
    """Seed group_members for g_active_1."""
    members_data = [
        ("gm-1", "g_active_1", "Alice", "admin", 1),
        ("gm-2", "g_active_1", "Bob", "member", 1),
        ("gm-3", "g_active_1", "Charlie", "viewer", 0),  # inactive
    ]
    for mid, gid, name, role, active in members_data:
        await db.execute(
            """INSERT INTO group_members
               (member_id, group_id, name, role, is_active)
               VALUES (?, ?, ?, ?, ?)""",
            (mid, gid, name, role, active),
        )
    await db.commit()


async def _seed_core_topics(db: aiosqlite.Connection) -> None:
    """Seed core_topics for g_active_1."""
    topics_data = [
        ("core-1", "g_active_1", "API Design", 1, 1),
        ("core-2", "g_active_1", "Performance", 2, 1),
        ("core-3", "g_active_1", "Legacy Topic", 1, 0),  # inactive
    ]
    for tid, gid, name, priority, active in topics_data:
        await db.execute(
            """INSERT INTO core_topics
               (core_topic_id, group_id, name, priority, is_active)
               VALUES (?, ?, ?, ?, ?)""",
            (tid, gid, name, priority, active),
        )
    await db.commit()


async def _seed_topic_summaries(db: aiosqlite.Connection) -> None:
    """Seed topic_summaries with different lifecycle values."""
    topics = [
        ("sum_001", "20260601", "g_test", "Topic A", "emerging"),
        ("sum_002", "20260601", "g_test", "Topic B", "active"),
        ("sum_003", "20260601", "g_test", "Topic C", "declining"),
        ("sum_004", "20260602", "g_test", "Topic D", "active"),
        ("sum_005", "20260601", "g_other", "Topic E", "active"),
    ]
    for sid, date, gid, name, lifecycle in topics:
        await db.execute(
            """INSERT INTO topic_summaries
               (summary_id, date, group_id, topic_name, summary_text,
                context_ids, source_server_ids, lifecycle)
               VALUES (?, ?, ?, ?, ?, '[]', '[]', ?)""",
            (sid, date, gid, name, f"Summary for {name}", lifecycle),
        )
    await db.commit()


# ============================================================
# B1: Overview Service Tests
# ============================================================


class TestOverviewService:
    """P013: Group tests by service module."""

    @pytest.mark.asyncio
    async def test_b1_overview_stats(self, db: aiosqlite.Connection) -> None:
        """B1: get_overview_stats returns typed stats from real database functions.

        Seeds an in-memory SQLite with raw_messages rows via init_database_in_conn,
        calls get_overview_stats, asserts result.total_messages == seeded_count
        and isinstance(result, OverviewStatsOut).
        """
        # Seed 15 messages for group g_test on date 20260601
        await _seed_raw_messages(db, count=15, group_id="g_test", date="20260601")

        result = await get_overview_stats(db, group_id="g_test", date="20260601")

        assert isinstance(result, OverviewStatsOut)
        assert result.total_messages == 15

    @pytest.mark.asyncio
    async def test_b1_overview_stats_empty_db(self, db: aiosqlite.Connection) -> None:
        """B1: Empty database returns zeroed OverviewStatsOut."""
        result = await get_overview_stats(db)

        assert isinstance(result, OverviewStatsOut)
        assert result.total_messages == 0
        assert result.total_topics == 0

    @pytest.mark.asyncio
    async def test_b1_dashboard_summary(self, db: aiosqlite.Connection) -> None:
        """B1: get_dashboard_summary returns overall stats."""
        await _seed_raw_messages(db, count=5)
        await _seed_groups(db)

        result = await get_dashboard_summary(db)

        assert isinstance(result, OverviewStatsOut)
        assert result.total_messages == 5
        assert result.total_groups == 3  # Only active groups

    @pytest.mark.asyncio
    async def test_b1_dashboard_per_group_rows(
        self, db: aiosqlite.Connection, tmp_path: Path
    ) -> None:
        """P1-2: get_dashboard_summary returns groups[] with L3 counts.

        Seeds 3 active groups + a latest report version + raw_messages + L3 JSON
        for g_active_1. Asserts groups[] has one row per active group, the group
        with a report carries populated counts, and groups without reports stay 0.
        """
        await _seed_groups(db)  # 3 active (g_active_1/2/3), 2 inactive
        await create_version(db, "rpt-p12", "g_active_1", "20260601", None, "daily_run")
        await _seed_raw_messages(db, count=5, group_id="g_active_1", date="20260601")

        l3_dir = tmp_path / "g_active_1" / "20260601"
        l3_dir.mkdir(parents=True)
        (l3_dir / "daily.json").write_text(
            json.dumps({"topics": ["t1", "t2", "t3"], "overview": "x"}),
            encoding="utf-8",
        )
        (l3_dir / "resources.json").write_text(json.dumps({"total_count": 7}), encoding="utf-8")
        (l3_dir / "engineering.json").write_text(
            json.dumps({"engineering_issues": [{"id": 1}, {"id": 2}]}), encoding="utf-8"
        )

        result = await get_dashboard_summary(db, output_dir=str(tmp_path))

        assert isinstance(result, OverviewStatsOut)
        assert len(result.groups) == 3  # only active groups

        by_id = {g.group_id: g for g in result.groups}
        # g_active_1 has a report → counts populated
        g1 = by_id["g_active_1"]
        assert g1.report_version_id is not None
        assert g1.active_member_count == 3  # sender_0/1/2 distinct
        assert g1.topic_count == 3
        assert g1.resource_count == 7
        assert g1.engineering_count == 2
        assert g1.display_name == "Test Group Alpha"
        # groups without reports → zero counts, no version
        g2 = by_id["g_active_2"]
        assert g2.report_version_id is None
        assert g2.topic_count == 0


# ============================================================
# B2: Group Service Tests
# ============================================================


class TestGroupService:
    """P013: Group service test class."""

    @pytest.mark.asyncio
    async def test_b2_group_pagination(self, db: aiosqlite.Connection) -> None:
        """B2: list_groups paginates groups with search filtering.

        Seeds 5 groups (3 active, 2 inactive).
        - list_groups(page=1, page_size=2) -> 2 items, total=3 (active only)
        - list_groups(search="test") -> only matching groups
        - list_groups(is_active=None) -> all 5 groups
        """
        await _seed_groups(db)

        # Test 1: pagination with active filter
        result = await list_groups(db, page=1, page_size=2)
        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 2
        assert result.total == 3  # Only active groups
        assert result.page == 1
        assert result.page_size == 2

        # Verify return types
        for item in result.items:
            assert isinstance(item, GroupOut)

        # Test 2: search filter — "test" matches "Test Group Alpha" and "Test Beta Group"
        result_search = await list_groups(db, search="test")
        assert result_search.total == 2  # "Test Group Alpha" + "Test Beta Group"
        for item in result_search.items:
            assert "test" in item.display_name.lower()

        # Test 3: is_active=None returns all groups
        result_all = await list_groups(db, is_active=None)
        assert result_all.total == 5

    @pytest.mark.asyncio
    async def test_b2_get_group_detail(self, db: aiosqlite.Connection) -> None:
        """B2: get_group_detail returns GroupOut or None."""
        await _seed_groups(db)

        found = await get_group_detail(db, "g_active_1")
        assert found is not None
        assert isinstance(found, GroupOut)
        assert found.display_name == "Test Group Alpha"

        missing = await get_group_detail(db, "nonexistent")
        assert missing is None

    @pytest.mark.asyncio
    async def test_b2_list_members(self, db: aiosqlite.Connection) -> None:
        """B2: list_members returns member list filtered by active status."""
        await _seed_groups(db)
        await _seed_members(db)

        # Active only (default)
        active = await list_members(db, "g_active_1")
        assert len(active) == 2  # Alice + Bob
        for m in active:
            assert isinstance(m, GroupMemberOut)

        # All members
        all_members = await list_members(db, "g_active_1", is_active=False)
        assert len(all_members) == 3  # Alice + Bob + Charlie (inactive)

    @pytest.mark.asyncio
    async def test_b2_list_core_topics(self, db: aiosqlite.Connection) -> None:
        """B2: list_core_topics returns topics filtered by active status."""
        await _seed_groups(db)
        await _seed_core_topics(db)

        # Active only (default)
        active = await list_core_topics(db, "g_active_1")
        assert len(active) == 2  # API Design + Performance
        for t in active:
            assert isinstance(t, CoreTopicOut)

        # All topics
        all_topics = await list_core_topics(db, "g_active_1", is_active=False)
        assert len(all_topics) == 3


# ============================================================
# B3: Topic Service Tests
# ============================================================


class TestTopicService:
    """P013: Topic service test class."""

    @pytest.mark.asyncio
    async def test_b3_topic_filters(self, db: aiosqlite.Connection) -> None:
        """B3: list_topics queries topic_summaries with date/group/lifecycle filters.

        Seeds topic_summaries with lifecycle values: emerging, active, declining.
        - list_topics(lifecycle="active") -> only matching rows
        - list_topics(group_id="g_test", date="20260601") -> filtered correctly
        """
        await _seed_topic_summaries(db)

        # Test 1: lifecycle filter — 3 "active" topics
        result = await list_topics(db, lifecycle="active")
        assert isinstance(result, PaginatedResult)
        assert result.total == 3  # sum_002, sum_004, sum_005
        for item in result.items:
            assert isinstance(item, L3SummaryOut)
            assert item.lifecycle == "active"

        # Test 2: group + date filter
        result_gd = await list_topics(db, group_id="g_test", date="20260601")
        assert result_gd.total == 3  # sum_001, sum_002, sum_003

        # Test 3: group filter only
        result_g = await list_topics(db, group_id="g_other")
        assert result_g.total == 1  # sum_005

    @pytest.mark.asyncio
    async def test_b3_get_topic_detail(self, db: aiosqlite.Connection) -> None:
        """B3: get_topic_detail returns single topic or None."""
        await _seed_topic_summaries(db)

        found = await get_topic_detail(db, "sum_001")
        assert found is not None
        assert isinstance(found, L3SummaryOut)
        assert found.topic_name == "Topic A"

        missing = await get_topic_detail(db, "nonexistent")
        assert missing is None


# ============================================================
# B4: Report Service Tests
# ============================================================


class TestReportService:
    """P013: Report service test class."""

    @pytest.mark.asyncio
    async def test_b4_report_versions(self, db: aiosqlite.Connection) -> None:
        """B4: report_service reads report versions.

        Seeds report_versions via create_version.
        - list_report_versions(group_id="g_test") -> version returned
        - get_report_version(version_id) -> ReportVersionDetail returned
        """
        vid = await create_version(db, "rpt-001", "g_test", "20260601", None, "daily_run")

        # Test listing
        result = await list_report_versions(db, group_id="g_test")
        assert isinstance(result, PaginatedResult)
        assert result.total >= 1
        for item in result.items:
            assert isinstance(item, ReportVersionOut)
        assert result.items[0].report_id == "rpt-001"

        # Test get detail
        detail = await get_report_version(db, vid)
        assert detail is not None
        assert isinstance(detail, ReportVersionOut)
        assert detail.version_id == vid
        assert detail.group_id == "g_test"

    @pytest.mark.asyncio
    async def test_b4_report_content(self, db: aiosqlite.Connection, tmp_path) -> None:
        """B4: get_report_content reads L3 JSON files.

        Uses tmp_path to create daily.json in L3 directory structure.
        """
        # Create L3 directory structure: {group_id}/{date}/daily.json
        l3_dir = tmp_path / "g_test" / "20260601"
        l3_dir.mkdir(parents=True)
        report_data = {"overview": "Test daily report", "topic_sections": []}
        (l3_dir / "daily.json").write_text(
            json.dumps(report_data, ensure_ascii=False), encoding="utf-8"
        )

        content = await get_report_content(db, "g_test", "20260601", output_dir=str(tmp_path))
        assert content is not None
        assert isinstance(content, ReportContent)
        assert content.data.get("overview") == "Test daily report"
        assert content.report_type == "daily"

        # Non-existent report returns None
        missing = await get_report_content(db, "g_test", "20260602", output_dir=str(tmp_path))
        assert missing is None

    @pytest.mark.asyncio
    async def test_b4_report_diff(self, db: aiosqlite.Connection) -> None:
        """B4: get_report_diff compares two versions."""
        # Create two versions
        await create_version(db, "rpt-diff", "g_diff", "20260601", "Version 1 content", "daily_run")
        await create_version(db, "rpt-diff", "g_diff", "20260601", "Version 2 content", "daily_run")

        diff = await get_report_diff(db, "rpt-diff")
        assert diff is not None
        assert diff.old_version == 1
        assert diff.new_version == 2
        assert diff.content_changed is True

        # Single version returns None
        await create_version(db, "rpt-single", "g_s", "20260601", "Only version", "daily_run")
        no_diff = await get_report_diff(db, "rpt-single")
        assert no_diff is None


# ============================================================
# B5: Key People Service Tests
# ============================================================


async def _seed_key_people_members(db: aiosqlite.Connection, group_id: str = "g_test") -> None:
    """Seed group_members whose wxid matches the raw senders in _seed_raw_messages.

    P1-3: list_key_people now reads from group_members (LEFT JOIN raw_messages),
    so raw_messages alone no longer surfaces anyone — members must be registered.
    Inserts the parent groups row first (group_members.group_id is a FK to it).
    """
    await db.execute(
        "INSERT OR IGNORE INTO groups (group_id, display_name, chatroom_id) VALUES (?, ?, ?)",
        (group_id, f"Group {group_id}", f"room_{group_id}@chatroom"),
    )
    members = [
        ("kp-0", group_id, "Zero", "sender_0", "admin", "note-0", 1),
        ("kp-1", group_id, "One", "sender_1", "member", None, 1),
        ("kp-2", group_id, "Two", "sender_2", "member", None, 1),
    ]
    for mid, gid, name, wxid, role, note, active in members:
        await db.execute(
            """INSERT INTO group_members
               (member_id, group_id, name, wxid, role, note, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (mid, gid, name, wxid, role, note, active),
        )
    await db.commit()


class TestKeyPeopleService:
    """P013: Key people service test class."""

    @pytest.mark.asyncio
    async def test_b5_key_people(self, db: aiosqlite.Connection) -> None:
        """B5: list_key_people returns registered members enriched with raw_messages stats.

        Seeds 3 group_members (wxid sender_0/1/2) + 10 raw_messages
        (sender_0: 4, sender_1: 3, sender_2: 3). Returns members sorted by
        message_count descending, carrying display_name/role/notes.
        """
        await _seed_key_people_members(db)
        await _seed_raw_messages(db, count=10, group_id="g_test", date="20260601")

        result = await list_key_people(db, "g_test", "20260601", limit=3)

        assert isinstance(result, list)
        assert len(result) == 3

        # Verify return type
        for person in result:
            assert isinstance(person, KeyPeopleOut)
            assert person.sender != ""
            assert person.display_name is not None

        # Verify descending order by message_count
        counts = [p.message_count for p in result]
        assert counts == sorted(counts, reverse=True)

        # sender_0 has 4 messages (most) + carries its registered metadata
        assert result[0].sender == "sender_0"
        assert result[0].message_count == 4
        assert result[0].display_name == "Zero"
        assert result[0].role == "admin"
        assert result[0].notes == "note-0"

    @pytest.mark.asyncio
    async def test_b5_key_people_empty(self, db: aiosqlite.Connection) -> None:
        """B5: No registered members returns empty list."""
        result = await list_key_people(db, "g_empty", "20260601")
        assert isinstance(result, list)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_b5_key_people_limit(self, db: aiosqlite.Connection) -> None:
        """B5: Limit parameter caps the result count."""
        await _seed_key_people_members(db)
        await _seed_raw_messages(db, count=10, group_id="g_test", date="20260601")

        result = await list_key_people(db, "g_test", "20260601", limit=2)
        assert len(result) == 2  # Capped at limit=2

    @pytest.mark.asyncio
    async def test_b5_key_people_no_date_spans_all(self, db: aiosqlite.Connection) -> None:
        """B5: date=None aggregates message stats across all dates."""
        await _seed_key_people_members(db)
        # sender_0: 2 msgs on 20260601 + 2 on 20260602 (distinct serverIDs)
        span_msgs = [
            ("srv_span_0", "20260601", "sender_0"),
            ("srv_span_1", "20260601", "sender_0"),
            ("srv_span_2", "20260602", "sender_0"),
            ("srv_span_3", "20260602", "sender_0"),
        ]
        for sid, dt, sender in span_msgs:
            await db.execute(
                "INSERT INTO raw_messages (serverID, date, group_id, sender, content, raw_json) "
                "VALUES (?, ?, 'g_test', ?, ?, '{}')",
                (sid, dt, sender, f"msg {sid}"),
            )
        await db.commit()

        with_date = await list_key_people(db, "g_test", "20260601")
        no_date = await list_key_people(db, "g_test")  # spans both dates

        by_no_date = {p.sender: p for p in no_date}
        assert by_no_date["sender_0"].message_count == 4  # 2 + 2 across dates

        by_date = {p.sender: p for p in with_date}
        assert by_date["sender_0"].message_count == 2  # scoped to one date

    @pytest.mark.asyncio
    async def test_b5_key_people_member_without_messages(self, db: aiosqlite.Connection) -> None:
        """B5: A registered member with no raw_messages appears with count=0."""
        # Parent group first (group_members.group_id is a FK to groups)
        await db.execute(
            "INSERT OR IGNORE INTO groups (group_id, display_name, chatroom_id) "
            "VALUES ('g_nomsg', 'NoMsg Group', 'room_nomsg@chatroom')"
        )
        await db.execute(
            """INSERT INTO group_members
               (member_id, group_id, name, wxid, role, note, is_active)
               VALUES ('kp-x', 'g_nomsg', 'Lurker', 'lurker_1', 'member', NULL, 1)"""
        )
        await db.commit()

        result = await list_key_people(db, "g_nomsg")
        assert len(result) == 1
        assert result[0].sender == "lurker_1"
        assert result[0].display_name == "Lurker"
        assert result[0].message_count == 0  # COALESCE(m.message_count, 0)


# ============================================================
# T-W14-4: New service module tests
#   B1: Data service provenance trace (data_service)
#   B2: Feedback state machine (feedback_service)
#   B3: Run service SSE generator (run_service)
#   B4: MemOS graceful degradation P082 (memos_service)
#   B5: Zero FastAPI imports in service modules
#
# P078: B1/B2/B3 use real in-memory SQLite with seeded data.
# L100: Only B4 uses mock adapter for httpx.ConnectError simulation.
# ============================================================


# ---------------------------------------------------------------
# Seed helpers for T-W14-4 test data
# ---------------------------------------------------------------


async def _seed_l1_l2_l3_chain(db: aiosqlite.Connection) -> None:
    """Seed a full L1 -> L2 -> L3 provenance chain for trace tests."""
    # L1: raw messages
    msgs = [
        ("sid-100", "20260601", "grp-chain", "Alice", "Hello world", "{}"),
        ("sid-101", "20260601", "grp-chain", "Bob", "Good morning", "{}"),
        ("sid-102", "20260601", "grp-chain", "Charlie", "Hi there", "{}"),
        ("sid-103", "20260602", "grp-chain", "Dave", "Bye", "{}"),
    ]
    for args in msgs:
        await db.execute(
            "INSERT INTO raw_messages (serverID, date, group_id, sender, content, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            args,
        )

    # L2: parsed contexts referencing sid-100 and sid-101
    await db.execute(
        "INSERT INTO parsed_contexts (context_id, date, group_id, server_ids, context_text) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "ctx-100",
            "20260601",
            "grp-chain",
            json.dumps(["sid-100", "sid-101"]),
            "Alice and Bob discussed greetings.",
        ),
    )

    # L3: topic summaries referencing sid-100 and sid-101
    await db.execute(
        "INSERT INTO topic_summaries "
        "(summary_id, date, group_id, topic_name, summary_text, context_ids, "
        "source_server_ids, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ts-100",
            "20260601",
            "grp-chain",
            "Greetings",
            "Morning greetings exchanged.",
            json.dumps(["ctx-100"]),
            json.dumps(["sid-100", "sid-101"]),
            0.95,
        ),
    )
    await db.commit()


async def _seed_feedback_events(db: aiosqlite.Connection) -> None:
    """Seed feedback_events for state machine tests."""
    await db.execute(
        "INSERT INTO feedback_events "
        "(feedback_id, group_id, date, target_type, signal, reporter) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("fb-chain-001", "grp-chain", "20260601", "topic", "positive", "admin"),
    )
    await db.commit()


async def _seed_pipeline_runs(db: aiosqlite.Connection) -> None:
    """Seed pipeline_runs for list/stream tests."""
    await db.execute(
        "INSERT INTO pipeline_runs (run_id, component, status, group_id, date) "
        "VALUES (?, ?, ?, ?, ?)",
        ("run-chain-001", "pipeline", "completed", "grp-chain", "20260601"),
    )
    await db.commit()


# ---------------------------------------------------------------
# MockMemOSAdapter for B4 (P010)
# ---------------------------------------------------------------


class MockMemOSAdapter:
    """Mock adapter for memos_service tests.

    Simulates httpx.ConnectError when connect_error=True.
    Only used for B4 memos tests — all other tests use real SQLite.
    """

    def __init__(self, *, connect_error: bool = False):
        self._connect_error = connect_error
        self._memories = [
            {"id": "mem-001", "memory": "test memory", "metadata": {}, "score": 0.9},
        ]

    async def add_memory(self, group_id, mem_cube_id, messages):
        if self._connect_error:
            raise httpx.ConnectError("Connection refused")
        return {"id": "mem-new"}

    async def search_memories(
        self, query, group_id, readable_cube_ids, top_k=20, legacy_group_ids=None
    ):
        if self._connect_error:
            raise httpx.ConnectError("Connection refused")
        from z_winnow.memory.types import MemoryResult

        return [MemoryResult(id="mem-001", memory="test memory", metadata={}, score=0.9)]

    async def get_or_create_cube(self, scope):
        return "cube-001"

    async def add_structured_memory(self, cube_id, group_id, items, async_mode="sync"):
        if self._connect_error:
            raise httpx.ConnectError("Connection refused")
        return {"id": "mem-new-structured"}

    async def get_all_memories(self, cube_id, group_id, filters=None):
        if self._connect_error:
            raise httpx.ConnectError("Connection refused")
        return {"text_mem": self._memories, "act_mem": [], "para_mem": []}

    async def delete_memory(self, cube_id, group_id, memory_ids=None, file_ids=None, filter=None):
        if self._connect_error:
            raise httpx.ConnectError("Connection refused")
        return True

    async def scheduler_status(self, user_name):
        return {"status": "idle", "pending_count": 0}

    async def scheduler_wait(self, user_name, timeout_seconds=120.0, poll_interval=0.2):
        return {"status": "completed"}

    async def health_check(self):
        if self._connect_error:
            raise httpx.ConnectError("Connection refused")
        return {"status": "ok", "latency_ms": 42.0}


# ============================================================
# B1 (T-W14-4): Data Service — provenance trace
# ============================================================


class TestDataService:
    """T-W14-4 data_service: L1/L2/L3 queries + provenance tracing."""

    @pytest.mark.asyncio
    async def test_get_l1_messages_basic(self, db: aiosqlite.Connection) -> None:
        """L1 messages can be fetched by date with pagination."""
        from z_winnow.web.services.data_service import get_l1_messages

        await _seed_l1_l2_l3_chain(db)

        result = await get_l1_messages(db, "20260601")
        assert result["total"] == 3  # sid-100, 101, 102 (not 103 which is date 0602)
        assert result["page"] == 1
        assert len(result["items"]) == 3

    @pytest.mark.asyncio
    async def test_get_l1_messages_group_filter(self, db: aiosqlite.Connection) -> None:
        """L1 messages can be filtered by group_id."""
        from z_winnow.web.services.data_service import get_l1_messages

        await _seed_l1_l2_l3_chain(db)

        result = await get_l1_messages(db, "20260601", group_id="grp-chain")
        assert result["total"] == 3

    @pytest.mark.asyncio
    async def test_get_l1_messages_pagination(self, db: aiosqlite.Connection) -> None:
        """L1 messages pagination splits results correctly."""
        from z_winnow.web.services.data_service import get_l1_messages

        await _seed_l1_l2_l3_chain(db)

        page1 = await get_l1_messages(db, "20260601", page=1, page_size=2)
        assert page1["total"] == 3
        assert len(page1["items"]) == 2

        page2 = await get_l1_messages(db, "20260601", page=2, page_size=2)
        assert len(page2["items"]) == 1

    @pytest.mark.asyncio
    async def test_get_l1_messages_empty_date(self, db: aiosqlite.Connection) -> None:
        """L1 messages returns empty for nonexistent date."""
        from z_winnow.web.services.data_service import get_l1_messages

        result = await get_l1_messages(db, "99999999")
        assert result["total"] == 0
        assert result["items"] == []

    @pytest.mark.asyncio
    async def test_get_l2_contexts_by_server_ids(self, db: aiosqlite.Connection) -> None:
        """L2 contexts found by server_ids via JSON LIKE match."""
        from z_winnow.web.services.data_service import (
            get_l2_contexts_by_server_ids,
        )

        await _seed_l1_l2_l3_chain(db)

        results = await get_l2_contexts_by_server_ids(db, ["sid-100"])
        assert len(results) == 1
        assert results[0]["context_id"] == "ctx-100"

    @pytest.mark.asyncio
    async def test_get_l3_topics(self, db: aiosqlite.Connection) -> None:
        """L3 topics fetched by date."""
        from z_winnow.web.services.data_service import get_l3_topics

        await _seed_l1_l2_l3_chain(db)

        results = await get_l3_topics(db, "20260601")
        assert len(results) == 1
        assert results[0]["topic_name"] == "Greetings"

    @pytest.mark.asyncio
    async def test_get_l3_topics_with_group_filter(self, db: aiosqlite.Connection) -> None:
        """L3 topics filtered by group_id."""
        from z_winnow.web.services.data_service import get_l3_topics

        await _seed_l1_l2_l3_chain(db)

        results = await get_l3_topics(db, "20260601", group_id="nonexistent")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_trace_message_to_topics(self, db: aiosqlite.Connection) -> None:
        """B1: Forward trace from message to topics returns full provenance chain."""
        from z_winnow.web.services.data_service import (
            trace_message_to_topics,
        )

        await _seed_l1_l2_l3_chain(db)

        result = await trace_message_to_topics(db, "sid-100")
        assert result["server_id"] == "sid-100"
        assert result["message"] is not None
        assert result["message"]["sender"] == "Alice"
        assert "parsed_content" in result["message"]
        assert "topics" in result
        assert len(result["topics"]) >= 1
        assert result["topics"][0]["topic_name"] == "Greetings"

    @pytest.mark.asyncio
    async def test_trace_message_to_topics_not_found(self, db: aiosqlite.Connection) -> None:
        """Trace returns empty for nonexistent server_id."""
        from z_winnow.web.services.data_service import (
            trace_message_to_topics,
        )

        result = await trace_message_to_topics(db, "nonexistent")
        assert result["message"] is None
        assert result["topics"] == []

    @pytest.mark.asyncio
    async def test_trace_topic_to_messages(self, db: aiosqlite.Connection) -> None:
        """Reverse trace from topic to messages returns full chain."""
        from z_winnow.web.services.data_service import (
            trace_topic_to_messages,
        )

        await _seed_l1_l2_l3_chain(db)

        result = await trace_topic_to_messages(db, "Greetings")
        assert result["topic_name"] == "Greetings"
        assert len(result["summaries"]) == 1
        assert len(result["raw_messages"]) == 2  # sid-100, sid-101
        assert len(result["contexts"]) == 1  # ctx-100

    @pytest.mark.asyncio
    async def test_trace_topic_to_messages_not_found(self, db: aiosqlite.Connection) -> None:
        """Reverse trace returns empty for nonexistent topic."""
        from z_winnow.web.services.data_service import (
            trace_topic_to_messages,
        )

        result = await trace_topic_to_messages(db, "NonexistentTopic")
        assert result["summaries"] == []
        assert result["raw_messages"] == []
        assert result["contexts"] == []

    @pytest.mark.asyncio
    async def test_find_server_id_page(self, db: aiosqlite.Connection) -> None:
        """find_server_id_page returns correct page number."""
        from z_winnow.web.services.data_service import (
            find_server_id_page,
        )

        await _seed_l1_l2_l3_chain(db)

        page = await find_server_id_page(db, "20260601", "sid-100", page_size=2)
        assert page == 1

        page2 = await find_server_id_page(db, "20260601", "sid-102", page_size=2)
        assert page2 == 2

    @pytest.mark.asyncio
    async def test_find_server_id_page_not_found(self, db: aiosqlite.Connection) -> None:
        """find_server_id_page returns None for nonexistent server_id."""
        from z_winnow.web.services.data_service import (
            find_server_id_page,
        )

        page = await find_server_id_page(db, "20260601", "nonexistent")
        assert page is None


# ============================================================
# B2 (T-W14-4): Feedback Service — state machine
# ============================================================


class TestFeedbackService:
    """T-W14-4 feedback_service: unconsumed/consumed/rollback cycle."""

    @pytest.mark.asyncio
    async def test_list_unconsumed(self, db: aiosqlite.Connection) -> None:
        """List returns unconsumed feedback events."""
        from z_winnow.web.services.feedback_service import (
            list_unconsumed_feedback,
        )

        await _seed_feedback_events(db)

        results = await list_unconsumed_feedback(db, "grp-chain", "20260601")
        assert len(results) == 1
        assert results[0]["feedback_id"] == "fb-chain-001"
        assert results[0]["consumed_at"] is None

    @pytest.mark.asyncio
    async def test_consume_feedback(self, db: aiosqlite.Connection) -> None:
        """B2: consume_feedback sets consumed_at and returns True."""
        from z_winnow.web.services.feedback_service import (
            consume_feedback,
        )

        await _seed_feedback_events(db)

        success = await consume_feedback(db, "fb-chain-001", consumed_by="test")
        assert success is True

        # Verify consumed_at is set (use index access — db fixture has no row_factory)
        cursor = await db.execute(
            "SELECT consumed_at, consumed_by FROM feedback_events WHERE feedback_id = ?",
            ("fb-chain-001",),
        )
        row = await cursor.fetchone()
        assert row[0] is not None  # consumed_at
        assert row[1] == "test"  # consumed_by

    @pytest.mark.asyncio
    async def test_consume_already_consumed(self, db: aiosqlite.Connection) -> None:
        """Consuming an already consumed feedback returns False."""
        from z_winnow.web.services.feedback_service import (
            consume_feedback,
        )

        await _seed_feedback_events(db)
        await consume_feedback(db, "fb-chain-001")

        success = await consume_feedback(db, "fb-chain-001")
        assert success is False

    @pytest.mark.asyncio
    async def test_rollback_feedback(self, db: aiosqlite.Connection) -> None:
        """B2: rollback_feedback clears consumed_at and returns True."""
        from z_winnow.web.services.feedback_service import (
            consume_feedback,
            rollback_feedback,
        )

        await _seed_feedback_events(db)
        await consume_feedback(db, "fb-chain-001")

        success = await rollback_feedback(db, "fb-chain-001")
        assert success is True

        cursor = await db.execute(
            "SELECT consumed_at FROM feedback_events WHERE feedback_id = ?",
            ("fb-chain-001",),
        )
        row = await cursor.fetchone()
        assert row[0] is None  # consumed_at

    @pytest.mark.asyncio
    async def test_rollback_unconsumed_fails(self, db: aiosqlite.Connection) -> None:
        """Rolling back an unconsumed feedback returns False."""
        from z_winnow.web.services.feedback_service import (
            rollback_feedback,
        )

        await _seed_feedback_events(db)

        success = await rollback_feedback(db, "fb-chain-001")
        assert success is False

    @pytest.mark.asyncio
    async def test_full_state_machine_cycle(self, db: aiosqlite.Connection) -> None:
        """B2: Full cycle: unconsumed -> consumed -> rolled back -> unconsumed."""
        from z_winnow.web.services.feedback_service import (
            consume_feedback,
            list_unconsumed_feedback,
            rollback_feedback,
        )

        await _seed_feedback_events(db)

        # Initially unconsumed
        unconsumed = await list_unconsumed_feedback(db, "grp-chain", "20260601")
        assert len(unconsumed) == 1

        # Consume
        assert await consume_feedback(db, "fb-chain-001") is True
        unconsumed = await list_unconsumed_feedback(db, "grp-chain", "20260601")
        assert len(unconsumed) == 0

        # Rollback restores unconsumed state
        assert await rollback_feedback(db, "fb-chain-001") is True
        unconsumed = await list_unconsumed_feedback(db, "grp-chain", "20260601")
        assert len(unconsumed) == 1

    @pytest.mark.asyncio
    async def test_consume_nonexistent(self, db: aiosqlite.Connection) -> None:
        """Consuming nonexistent feedback_id returns False."""
        from z_winnow.web.services.feedback_service import (
            consume_feedback,
        )

        success = await consume_feedback(db, "nonexistent-id")
        assert success is False


# ============================================================
# B3 (T-W14-4): Run Service — list + SSE generator
# ============================================================


class TestRunService:
    """T-W14-4 run_service: pipeline_runs CRUD + SSE stream."""

    @pytest.mark.asyncio
    async def test_list_runs(self, db: aiosqlite.Connection) -> None:
        """list_runs returns pipeline run records."""
        from z_winnow.web.services.run_service import list_runs

        await _seed_pipeline_runs(db)

        results = await list_runs(db)
        assert len(results) == 1
        assert results[0]["run_id"] == "run-chain-001"

    @pytest.mark.asyncio
    async def test_list_runs_with_filters(self, db: aiosqlite.Connection) -> None:
        """list_runs supports group_id and date filters."""
        from z_winnow.web.services.run_service import list_runs

        await _seed_pipeline_runs(db)

        results = await list_runs(db, group_id="grp-chain")
        assert len(results) == 1

        results_empty = await list_runs(db, group_id="nonexistent")
        assert len(results_empty) == 0

    @pytest.mark.asyncio
    async def test_stream_runs_yields_sse(self) -> None:
        """B3: stream_runs yields SSE-formatted strings with data: prefix."""
        from z_winnow.web.services.run_service import stream_runs

        # Use a temp file DB since :memory: is per-connection
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_stream.db")
            async with aiosqlite.connect(db_path) as setup_conn:
                await setup_conn.execute(
                    """CREATE TABLE IF NOT EXISTS pipeline_runs (
                        run_id TEXT PRIMARY KEY,
                        component TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'unknown',
                        started_at TEXT,
                        completed_at TEXT,
                        message_count INTEGER DEFAULT 0,
                        error_message TEXT,
                        current_node TEXT,
                        progress_pct INTEGER,
                        node_history TEXT,
                        group_id TEXT,
                        date TEXT,
                        created_at TEXT DEFAULT (datetime('now'))
                    )"""
                )
                await setup_conn.execute(
                    "INSERT INTO pipeline_runs (run_id, component, status) VALUES (?, ?, ?)",
                    ("run-stream-1", "pipeline", "running"),
                )
                await setup_conn.commit()

            events: list[str] = []
            async for event in stream_runs(db_path, poll_interval_s=0.05, max_iterations=3):
                events.append(event)

            assert len(events) >= 2
            for event in events:
                assert event.startswith("data: ")
                assert event.endswith("\n\n")
                payload = event[len("data: ") : -2]
                data = json.loads(payload)
                assert "runs" in data

    @pytest.mark.asyncio
    async def test_b3_insert_update_stream_same_source(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B3 (W16-B3): write path (insert_run/update_run) and SSE read path
        (stream_runs) resolve to the SAME physical DB via get_settings().db_path.

        Fixture chain (L100: real SQLite file, A018: no mocks):
          reset_settings() → setenv WINNOW_DB_PATH=tmp file → get_settings()
          rebuild → init_database creates schema at settings.db_path → insert_run
          + update_run write run_x → stream_runs (given the same settings.db_path
          string) reads run_x back.

        L028/L045: stream_runs is bounded by max_iterations=1 (single yield) —
        no infinite SSE stream, no curl. reset_settings() in finally prevents the
        rebuilt singleton from leaking into later tests.
        """
        from z_winnow.config import get_settings, reset_settings
        from z_winnow.pipeline.database import init_database
        from z_winnow.web.services.run_service import (
            insert_run,
            stream_runs,
            update_run,
        )

        db_file = str(tmp_path / "w16b3_same_source.db")
        reset_settings()
        monkeypatch.setenv("WINNOW_DB_PATH", db_file)
        try:
            settings = get_settings()
            # A013: override must take effect at call time (not import time)
            assert settings.db_path == db_file, (
                f"settings.db_path must honor WINNOW_DB_PATH override; got {settings.db_path!r}"
            )

            # --- Pipeline write path: schema + insert_run/update_run ---
            # Both read settings.db_path inside the function body.
            await init_database(settings.db_path)
            ok = await insert_run("run_x", group_id="g", date="20260101")
            assert ok is True, "insert_run must succeed when schema exists at settings.db_path"
            updated = await update_run("run_x", status="completed", message_count=7)
            assert updated is True, "update_run must touch the row written by insert_run"

            # Verify the row landed in the exact file insert_run resolved to.
            async with aiosqlite.connect(settings.db_path) as verify:
                verify.row_factory = aiosqlite.Row
                cur = await verify.execute(
                    "SELECT run_id, group_id, date, status, message_count "
                    "FROM pipeline_runs WHERE run_id = ?",
                    ("run_x",),
                )
                row = await cur.fetchone()
            assert row is not None, "run_x must be visible in the DB insert_run wrote to"
            assert row["group_id"] == "g"
            assert row["date"] == "20260101"
            assert row["status"] == "completed"
            assert row["message_count"] == 7

            # --- SSE read path: stream_runs given the SAME settings.db_path string ---
            events: list[str] = []
            async for event in stream_runs(settings.db_path, poll_interval_s=0.0, max_iterations=1):
                events.append(event)
            assert events, "stream_runs must yield at least one SSE event"
            payload = events[0][len("data: ") : -2]
            data = json.loads(payload)
            run_ids = {r["run_id"] for r in data["runs"]}
            assert "run_x" in run_ids, (
                "stream_runs (read path) must see run_x written by insert_run "
                "→ proves both resolve to the same physical DB via settings.db_path"
            )
        finally:
            # Restore singleton so later tests rebuild from clean env (monkeypatch
            # undoes WINNOW_DB_PATH at fixture teardown, after this finally).
            reset_settings()


# ============================================================
# B4 (T-W14-4): Memos Service — P082 graceful degradation
# ============================================================


class TestMemosService:
    """T-W14-4 memos_service: P082 asymmetric fault tolerance."""

    @pytest.mark.asyncio
    async def test_search_memos_success(self) -> None:
        """search_memos returns results on success."""
        from z_winnow.web.services.memos_service import search_memos

        adapter = MockMemOSAdapter()
        results = await search_memos(adapter, "cube-001", "test query")
        assert len(results) == 1
        assert results[0]["id"] == "mem-001"

    @pytest.mark.asyncio
    async def test_search_memos_propagates_error(self) -> None:
        """B4: search_memos propagates httpx.ConnectError (read method, P082)."""
        from z_winnow.web.services.memos_service import search_memos

        adapter = MockMemOSAdapter(connect_error=True)
        with pytest.raises(httpx.ConnectError, match="Connection refused"):
            await search_memos(adapter, "cube-001", "test query")

    @pytest.mark.asyncio
    async def test_get_all_memos_success(self) -> None:
        """get_all_memos returns memories on success."""
        from z_winnow.web.services.memos_service import get_all_memos

        adapter = MockMemOSAdapter()
        results = await get_all_memos(adapter, "cube-001")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_get_all_memos_propagates_error(self) -> None:
        """B4: get_all_memos propagates httpx.ConnectError (read method, P082)."""
        from z_winnow.web.services.memos_service import get_all_memos

        adapter = MockMemOSAdapter(connect_error=True)
        with pytest.raises(httpx.ConnectError, match="Connection refused"):
            await get_all_memos(adapter, "cube-001")

    @pytest.mark.asyncio
    async def test_add_memo_success(self) -> None:
        """add_memo returns memory ID on success."""
        from z_winnow.web.services.memos_service import add_memo

        adapter = MockMemOSAdapter()
        result = await add_memo(adapter, "cube-001", "test content")
        assert result is not None

    @pytest.mark.asyncio
    async def test_add_memo_returns_none_on_error(self) -> None:
        """B4: add_memo catches ConnectError and returns None (write method, P082)."""
        from z_winnow.web.services.memos_service import add_memo

        adapter = MockMemOSAdapter(connect_error=True)
        result = await add_memo(adapter, "cube-001", "test content")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_memo_success(self) -> None:
        """delete_memo returns True on success."""
        from z_winnow.web.services.memos_service import delete_memo

        adapter = MockMemOSAdapter()
        result = await delete_memo(adapter, "cube-001", "mem-001")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_memo_returns_false_on_error(self) -> None:
        """B4: delete_memo catches ConnectError and returns False (write method, P082)."""
        from z_winnow.web.services.memos_service import delete_memo

        adapter = MockMemOSAdapter(connect_error=True)
        result = await delete_memo(adapter, "cube-001", "mem-001")
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_ok(self) -> None:
        """health_check returns ok status on success."""
        from z_winnow.web.services.memos_service import health_check

        adapter = MockMemOSAdapter()
        result = await health_check(adapter)
        assert result["status"] == "ok"
        assert result.get("latency_ms") == 42.0

    @pytest.mark.asyncio
    async def test_health_check_degraded(self) -> None:
        """B4: health_check returns degraded on ConnectError (P082)."""
        from z_winnow.web.services.memos_service import health_check

        adapter = MockMemOSAdapter(connect_error=True)
        result = await health_check(adapter)
        assert result["status"] == "degraded"
        assert "error" in result
        assert "Connection refused" in result["error"]

    @pytest.mark.asyncio
    async def test_start_stop_sync_worker(self) -> None:
        """start/stop_sync_worker are idempotent."""
        from z_winnow.web.services.memos_service import (
            start_sync_worker,
            stop_sync_worker,
        )

        await start_sync_worker()
        await start_sync_worker()  # idempotent
        await stop_sync_worker()
        await stop_sync_worker()  # safe


# ============================================================
# B5 (T-W14-4): Zero FastAPI imports in service modules
# ============================================================


class TestNoFastAPIImports:
    """B5: Verify that T-W14-4 service modules have zero FastAPI imports."""

    @pytest.mark.parametrize(
        "module_name",
        [
            "z_winnow.web.services.data_service",
            "z_winnow.web.services.feedback_service",
            "z_winnow.web.services.run_service",
            "z_winnow.web.services.memos_service",
        ],
    )
    def test_no_fastapi_imports(self, module_name: str) -> None:
        """B5: Service module has no 'from fastapi' or 'import fastapi'."""
        mod = importlib.import_module(module_name)
        source = inspect.getsource(mod)
        assert "from fastapi" not in source
        assert "import fastapi" not in source


# ============================================================
# T-W14-5: task_queue, judge_service, export_service, system_service
# ============================================================
# P010: 3-layer mock architecture (tmp_path SQLite + AsyncMock + orchestration)
# P011: AC 1:1 mapping — B1..B5 each have dedicated test functions
# P078: Real-SQLite DDL verification for task_queue
# A019/L100: B4 uses real temporary SQLite (non-mock data source)
# A024: Shared tmp_path DB path across background tasks
# A031/A032: Status verified via get_task_status, not coroutine assumption
# ============================================================


async def _init_test_db_for_w14_5(db_path: str) -> None:
    """Create a minimal test database with required tables for T-W14-5."""
    from z_winnow.pipeline.database import init_database

    await init_database(db_path)


# ---------- B1: task_queue lifecycle ----------


class TestTaskQueueLifecycle:
    """B1: start_task returns UUID; status transitions queued->running->done."""

    @pytest.mark.asyncio
    async def test_b1_lifecycle_queued_to_done(self, tmp_path: Path) -> None:
        """B1: start_task returns UUID; status goes queued->running->done."""
        from z_winnow.web.services.task_queue import (
            get_task_status,
            start_task,
        )

        db_path = str(tmp_path / "test_b1.db")

        async def trivial_coro() -> dict[str, Any]:
            await asyncio.sleep(0.01)
            return {"ok": True}

        task_id = await start_task(
            task_type="test",
            resource_id="res-1",
            coro_factory=trivial_coro,
            db_path=db_path,
        )

        # task_id must be a UUID string
        assert isinstance(task_id, str)
        assert len(task_id) == 36

        # Immediately after: should be queued, running, or already done
        status = await get_task_status(task_id, db_path=db_path)
        assert status is not None
        assert status["task_id"] == task_id
        assert status["status"] in ("queued", "running", "done")

        # Wait for completion
        await asyncio.sleep(0.3)

        status = await get_task_status(task_id, db_path=db_path)
        assert status is not None
        assert status["status"] == "done"
        assert status["result_json"] is not None
        result = json.loads(status["result_json"])
        assert result == {"ok": True}


# ---------- B2: task_queue failure path ----------


class TestTaskQueueFailure:
    """B2: When coroutine raises, status='failed' with non-empty error_message."""

    @pytest.mark.asyncio
    async def test_b2_failure_status_and_error(self, tmp_path: Path) -> None:
        """B2: Failed coroutine -> status='failed', error_message contains 'boom'."""
        from z_winnow.web.services.task_queue import (
            get_task_status,
            start_task,
        )

        db_path = str(tmp_path / "test_b2.db")

        async def failing_coro() -> None:
            raise ValueError("boom")

        task_id = await start_task(
            task_type="test",
            resource_id="res-fail",
            coro_factory=failing_coro,
            db_path=db_path,
        )

        await asyncio.sleep(0.3)

        status = await get_task_status(task_id, db_path=db_path)
        assert status is not None
        assert status["status"] == "failed"
        # A032: error_message must be non-empty
        assert status["error_message"] is not None
        assert "boom" in status["error_message"]


# ---------- B3: judge_service wraps correctly ----------


class TestJudgeService:
    """B3: run_judge calls start_task; mock judge_report returns JudgeResult fields."""

    @pytest.mark.asyncio
    async def test_b3_judge_service_wraps(self, tmp_path: Path) -> None:
        """B3: run_judge spawns task; mock judge_report returns JudgeResult fields."""
        from z_winnow.rl.llm_judge import (
            DimensionScore,
            JudgeResult,
        )

        mock_result = JudgeResult(
            completeness=DimensionScore(score=0.8, evidence="test"),
            accuracy=DimensionScore(score=0.9, evidence="test"),
            conciseness=DimensionScore(score=0.7, evidence="test"),
            actionability=DimensionScore(score=0.6, evidence="test"),
            overall=0.75,
            model_used="test-model",
            judge_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        db_path = str(tmp_path / "test_b3.db")

        with patch(
            "z_winnow.web.services.judge_service.judge_report",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            from z_winnow.web.services.judge_service import (
                get_judge_result,
                run_judge,
            )

            task_id = await run_judge("g_test", "20260501", db_path=db_path)
            assert isinstance(task_id, str)

            await asyncio.sleep(0.3)

            result = await get_judge_result(task_id, db_path=db_path)
            assert result is not None
            assert result["status"] == "done"
            assert result["parsed_result"] is not None

            parsed = result["parsed_result"]
            assert "overall" in parsed
            assert "model_used" in parsed
            assert "judge_at" in parsed
            assert parsed["overall"] == 0.75


# ---------- B4: export_service wraps correctly (with real SQLite) ----------


class TestExportService:
    """B4: run_export/run_rl_dataset_export spawn background tasks.

    A019/L100: At least one test uses real temporary SQLite database.
    """

    @pytest.mark.asyncio
    async def test_b4_export_with_real_sqlite(self, tmp_path: Path) -> None:
        """B4: run_export with real SQLite (A019/L100)."""
        db_path = str(tmp_path / "test_b4.db")
        await _init_test_db_for_w14_5(db_path)

        # Insert real L3 data for export
        async with aiosqlite.connect(db_path) as conn:
            from z_winnow.pipeline.database import init_database_in_conn

            await init_database_in_conn(conn)
            await conn.execute(
                """INSERT INTO topic_summaries
                   (summary_id, date, topic_name, summary_text, context_ids,
                    source_server_ids, confidence, model_used, group_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "sum_test_001",
                    "20260501",
                    "Test Topic",
                    '{"overview": "test summary"}',
                    '["ctx_001"]',
                    '["srv_001"]',
                    0.85,
                    "test-model",
                    "g_test",
                ),
            )
            await conn.commit()

        from z_winnow.web.services.export_service import run_export

        task_id = await run_export("g_test", "20260501", "20260502", db_path=db_path)
        assert isinstance(task_id, str)

        await asyncio.sleep(0.3)

        from z_winnow.web.services.task_queue import get_task_status

        status = await get_task_status(task_id, db_path=db_path)
        assert status is not None
        assert status["status"] == "done"
        assert status["result_json"] is not None

        result = json.loads(status["result_json"])
        assert "row_count" in result
        assert result["row_count"] >= 0

    @pytest.mark.asyncio
    async def test_b4_rl_dataset_export(self, tmp_path: Path) -> None:
        """B4: run_rl_dataset_export wraps export_dataset via to_thread."""
        db_path = str(tmp_path / "test_b4_rl.db")

        mock_dataset = [
            {"record_id": "r1", "date": "2026-05-01", "weighted_score": 0.8},
        ]

        with patch(
            "z_winnow.rl.exporter.export_dataset",
            return_value=mock_dataset,
        ):
            from z_winnow.web.services.export_service import (
                run_rl_dataset_export,
            )

            task_id = await run_rl_dataset_export("g_test", days=7, db_path=db_path)
            assert isinstance(task_id, str)

            await asyncio.sleep(0.3)

            from z_winnow.web.services.task_queue import get_task_status

            status = await get_task_status(task_id, db_path=db_path)
            assert status is not None
            assert status["status"] == "done"

            result = json.loads(status["result_json"])
            assert isinstance(result, list)
            assert len(result) == 1
            assert result[0]["record_id"] == "r1"


# ---------- B5: system_service config masking + stats ----------


class TestSystemService:
    """B5: Config masking and stats aggregation."""

    @pytest.mark.asyncio
    async def test_b5_config_masking(self) -> None:
        """B5: get_system_config() returns db_path + mock_mode, NOT sensitive keys."""
        from z_winnow.web.services.system_service import get_system_config

        config = await get_system_config()

        # Must contain these keys
        assert "db_path" in config
        assert "mock_mode" in config

        # Must NOT contain sensitive keys
        assert "anthropic_api_key" not in config
        assert "deepseek_api_key" not in config
        assert "openai_api_key" not in config

    @pytest.mark.asyncio
    async def test_b5_stats_shape(self, tmp_path: Path) -> None:
        """B5: get_system_stats() returns message_count, pipeline_runs, queue_stats."""
        from z_winnow.web.services.system_service import get_system_stats

        db_path = str(tmp_path / "test_b5.db")
        await _init_test_db_for_w14_5(db_path)

        stats = await get_system_stats(db_path=db_path)

        assert "message_count" in stats
        assert "pipeline_runs" in stats
        assert "queue_stats" in stats

        assert "total" in stats["pipeline_runs"]
        assert isinstance(stats["pipeline_runs"]["total"], int)

        qs = stats["queue_stats"]
        for key in ("pending", "processing", "done", "failed", "total"):
            assert key in qs


# ---------- P078: DDL verification ----------


class TestTaskQueueDDL:
    """P078: _ensure_async_tasks_table DDL tested with real SQLite."""

    @pytest.mark.asyncio
    async def test_p078_ddl_round_trip(self, tmp_path: Path) -> None:
        """P078: PRAGMA table_info + INSERT round-trip on real SQLite."""
        from z_winnow.web.services.task_queue import (
            _ensure_async_tasks_table,
        )

        db_path = str(tmp_path / "test_ddl.db")

        async with aiosqlite.connect(db_path) as conn:
            await _ensure_async_tasks_table(conn)

            cursor = await conn.execute("PRAGMA table_info(async_tasks)")
            columns = {row[1] for row in await cursor.fetchall()}

            expected_cols = {
                "task_id",
                "task_type",
                "resource_id",
                "status",
                "result",
                "error",
                "created_at",
                "updated_at",
                "started_at",
                "finished_at",
            }
            assert expected_cols.issubset(columns), f"Missing: {expected_cols - columns}"

            # INSERT round-trip
            await conn.execute(
                """INSERT INTO async_tasks
                   (task_id, task_type, resource_id, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    "test-id-1",
                    "judge",
                    "res-1",
                    "queued",
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                ),
            )
            await conn.commit()

            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM async_tasks WHERE task_id = ?", ("test-id-1",)
            )
            row = await cursor.fetchone()
            assert row is not None
            assert dict(row)["task_id"] == "test-id-1"
            assert dict(row)["status"] == "queued"


# ---------- Additional coverage: cancel + list ----------


class TestTaskQueueCancelAndList:
    """cancel_task and list_tasks coverage."""

    @pytest.mark.asyncio
    async def test_cancel_queued_task(self, tmp_path: Path) -> None:
        """cancel_task on queued task returns True; on done returns False."""
        from z_winnow.web.services.task_queue import (
            cancel_task,
            get_task_status,
            start_task,
        )

        db_path = str(tmp_path / "test_cancel.db")

        task_id = await start_task("test", "res-1", db_path=db_path)

        ok = await cancel_task(task_id, db_path=db_path)
        assert ok is True

        status = await get_task_status(task_id, db_path=db_path)
        assert status["status"] == "cancelled"

        ok2 = await cancel_task(task_id, db_path=db_path)
        assert ok2 is False

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self, tmp_path: Path) -> None:
        """cancel_task on nonexistent task returns False."""
        from z_winnow.web.services.task_queue import cancel_task

        db_path = str(tmp_path / "test_cancel_ne.db")
        ok = await cancel_task("nonexistent-uuid", db_path=db_path)
        assert ok is False

    @pytest.mark.asyncio
    async def test_list_tasks_filters(self, tmp_path: Path) -> None:
        """list_tasks with type/status filters and limit."""
        from z_winnow.web.services.task_queue import (
            list_tasks,
            start_task,
        )

        db_path = str(tmp_path / "test_list.db")

        await start_task("judge", "res-1", db_path=db_path)
        await start_task("export", "res-2", db_path=db_path)
        await start_task("judge", "res-3", db_path=db_path)

        all_tasks = await list_tasks(db_path=db_path)
        assert len(all_tasks) == 3

        judge_tasks = await list_tasks(task_type="judge", db_path=db_path)
        assert len(judge_tasks) == 2

        queued_tasks = await list_tasks(status="queued", db_path=db_path)
        assert len(queued_tasks) == 3

        limited = await list_tasks(limit=1, db_path=db_path)
        assert len(limited) == 1
