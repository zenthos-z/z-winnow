"""Overview service -- dashboard stats composed from multiple database queries.

Wraps existing ``pipeline.database`` functions into typed async methods
returning ``OverviewStatsOut`` Pydantic models.

# P022: Pure data retrieval / formatting -- zero LLM calls, zero rendering.
# P014: Every function wraps DB calls in try/except with graceful empty returns.
# A008: Result variables initialized to empty Pydantic model before every try.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiosqlite

from z_winnow.pipeline.database import (
    get_message_count,
    get_message_stats,
    get_sync_queue_stats,
    get_topic_count,
)

if TYPE_CHECKING:
    pass


def _fmt_report_date(date_str: str | None) -> str | None:
    """Format YYYYMMDD report date as YYYY-MM-DD for display.

    Returns None if input is None or empty.
    """
    if not date_str:
        return None
    d = date_str.strip()
    if len(d) == 8:
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d


# L070: Conditional import for schema model
try:
    from z_winnow.web.schemas.overview import OverviewStatsOut
except ImportError:

    class OverviewStatsOut:  # type: ignore[no-redef]
        """Minimal fallback when schemas not yet available."""

        def __init__(
            self,
            total_messages: int = 0,
            total_groups: int = 0,
            total_topics: int = 0,
            total_reports: int = 0,
            total_feedback: int = 0,
            last_sync_at: str | None = None,
            active_runs: int = 0,
            groups: list | None = None,
        ) -> None:
            self.total_messages = total_messages
            self.total_groups = total_groups
            self.total_topics = total_topics
            self.total_reports = total_reports
            self.total_feedback = total_feedback
            self.last_sync_at = last_sync_at
            self.active_runs = active_runs
            self.groups = groups or []


logger = logging.getLogger(__name__)


async def get_overview_stats(
    db: aiosqlite.Connection,
    *,
    group_id: str | None = None,
    date: str | None = None,
) -> OverviewStatsOut:
    """Get overview statistics, optionally filtered by group and/or date.

    # P014: Wraps get_message_stats + get_sync_queue_stats internally.
    # A008: result initialized to empty OverviewStatsOut before try.

    Args:
        db: aiosqlite database connection.
        group_id: Optional group filter.
        date: Optional date filter (YYYYMMDD).

    Returns:
        OverviewStatsOut with aggregated stats. Empty model on failure.
    """
    # A008: explicit initialization before try
    result: OverviewStatsOut = OverviewStatsOut()
    try:
        msg_stats = await get_message_stats(db, date, group_id=group_id)
        topic_count = await get_topic_count(db, date, group_id=group_id)
        sync_stats = await get_sync_queue_stats(db)

        # Count active groups
        if group_id:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM groups WHERE group_id = ? AND is_active = 1",
                (group_id,),
            )
        else:
            cursor = await db.execute("SELECT COUNT(*) FROM groups WHERE is_active = 1")
        row = await cursor.fetchone()
        total_groups: int = row[0] if row else 0

        result = OverviewStatsOut(
            total_messages=msg_stats.get("total", 0),
            total_groups=total_groups,
            total_topics=topic_count,
            active_runs=sync_stats.get("processing", 0),
        )
    except Exception:
        # P014: NEVER raise from service -- log and return empty model
        logger.exception("get_overview_stats failed")
    return result


async def _get_per_group_rows(
    db: aiosqlite.Connection,
    *,
    output_dir: str | None = None,
) -> list[Any]:
    """Build the per-group dashboard rows (P1-2).

    One SQL fetches every active group plus its latest report version
    (version_id/date/created_at) and latest pipeline run status via correlated
    subqueries. For each group that has a report, ``active_member_count`` is read
    from raw_messages (DISTINCT sender on the report's date) and the
    topic/resource/engineering counts from L3 JSON — computed concurrently via
    asyncio.gather (aiosqlite serializes the shared connection internally).

    # P014: Any failure degrades gracefully — counts stay 0, the group is omitted
    only if its row cannot be read at all.

    Args:
        db: aiosqlite database connection.
        output_dir: Optional L3 output root override (defaults to Settings when None).

    Returns:
        List of OverviewGroupItem (raw dicts if the schema cannot be imported).
    """
    # Lazy import avoids a circular dependency at module load time.
    from z_winnow.web.services.report_service import get_report_content

    # A008: explicit initialization
    rows: list[Any] = []
    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            """
            SELECT g.group_id, g.display_name, g.is_active, g.daily_report_enabled,
                   g.engineering_enabled, g.feishu_tables, g.custom_tables,
              (SELECT rv.version_id FROM report_versions rv
                 WHERE rv.group_id = g.group_id
                 ORDER BY rv.created_at DESC LIMIT 1) AS version_id,
              (SELECT rv.date FROM report_versions rv
                 WHERE rv.group_id = g.group_id
                 ORDER BY rv.created_at DESC LIMIT 1) AS report_date,
              (SELECT rv.created_at FROM report_versions rv
                 WHERE rv.group_id = g.group_id
                 ORDER BY rv.created_at DESC LIMIT 1) AS report_at,
              (SELECT rv.feishu_pushed_at FROM report_versions rv
                 WHERE rv.group_id = g.group_id
                 ORDER BY rv.created_at DESC LIMIT 1) AS feishu_pushed_at,
              (SELECT pr.status FROM pipeline_runs pr
                 WHERE pr.group_id = g.group_id
                 ORDER BY pr.created_at DESC LIMIT 1) AS run_status
            FROM groups g
            WHERE g.is_active = 1
            ORDER BY report_at DESC
            """,
        )
        rows = list(await cursor.fetchall())
    except Exception:
        # P014: log and return empty list
        logger.exception("_get_per_group_rows: group query failed")
        rows = []
    finally:
        db.row_factory = original_factory

    async def _counts_for(row: Any) -> dict[str, Any]:
        gid = row["group_id"]
        rdate = row["report_date"]
        active_members = 0
        topic_count = 0
        resource_count = 0
        engineering_count = 0
        world_models_count = 0

        if rdate:
            try:
                cur = await db.execute(
                    "SELECT COUNT(DISTINCT sender) FROM raw_messages "
                    "WHERE group_id = ? AND date = ?",
                    (gid, rdate),
                )
                r = await cur.fetchone()
                active_members = int(r[0]) if r and r[0] is not None else 0
            except Exception:
                logger.debug("active_member_count failed for %s/%s", gid, rdate)

            # L3 counts — aiosqlite serializes the shared connection internally.
            daily, resources, engineering, world_models = await asyncio.gather(
                get_report_content(db, gid, rdate, report_type="daily", output_dir=output_dir),
                get_report_content(db, gid, rdate, report_type="resources", output_dir=output_dir),
                get_report_content(
                    db, gid, rdate, report_type="engineering", output_dir=output_dir
                ),
                get_report_content(
                    db, gid, rdate, report_type="world_models", output_dir=output_dir
                ),
            )
            if daily and isinstance(daily.data.get("topics"), list):
                topic_count = len(daily.data["topics"])
            if resources:
                rc = resources.data.get("total_count")
                if isinstance(rc, int):
                    resource_count = rc
            if engineering and isinstance(
                engineering.data.get("issues") or engineering.data.get("engineering_issues"),
                list,
            ):
                engineering_count = len(
                    engineering.data.get("issues") or engineering.data["engineering_issues"]
                )
            if world_models and isinstance(world_models.data.get("items"), list):
                world_models_count = len(world_models.data["items"])

        # Resolve engineering-enabled from the custom_tables/feishu_tables blobs via
        # the single active_kinds resolver (custom_tables overrides feishu_tables).
        # The raw engineering_enabled column is deprecated and never updated by the
        # UI toggle, so it can't be trusted as the display flag.
        import json as _json

        from z_winnow.pipeline.feishu import schema as _feishu_schema

        def _blob(raw: Any) -> dict[str, Any] | None:
            if not raw:
                return None
            try:
                parsed = _json.loads(raw)
            except (ValueError, TypeError):
                return None
            return parsed if isinstance(parsed, dict) else None

        _ft = _blob(row["feishu_tables"])
        _ct = _blob(row["custom_tables"])
        engineering_enabled = _feishu_schema.kind_enabled_for_report(
            "engineering", _ct, _ft, row["engineering_enabled"]
        )
        world_models_enabled = _feishu_schema.kind_enabled_for_report(
            "world_models", _ct, _ft, False
        )

        return {
            "group_id": gid,
            "display_name": row["display_name"],
            "is_active": bool(row["is_active"]),
            "daily_report_enabled": bool(row["daily_report_enabled"]),
            "engineering_enabled": engineering_enabled,
            "world_models_enabled": world_models_enabled,
            "latest_report_at": _fmt_report_date(row["report_date"]),
            "latest_run_status": row["run_status"],
            "report_version_id": row["version_id"],
            "feishu_pushed_at": row["feishu_pushed_at"],
            "active_member_count": active_members,
            "topic_count": topic_count,
            "resource_count": resource_count,
            "engineering_count": engineering_count,
            "world_models_count": world_models_count,
        }

    dicts = await asyncio.gather(*[_counts_for(r) for r in rows])
    try:
        from z_winnow.web.schemas.overview import OverviewGroupItem

        return [OverviewGroupItem(**d) for d in dicts]
    except Exception:
        # P014: schemas unavailable — return raw dicts
        return list(dicts)


async def get_dashboard_summary(
    db: aiosqlite.Connection,
    *,
    output_dir: str | None = None,
) -> OverviewStatsOut:
    """Get full dashboard summary (unfiltered overall stats).

    Composes OverviewStatsOut from:
    - raw_messages count
    - active groups count
    - topic_summaries count
    - report_versions count
    - feedback_events count
    - pipeline_runs with status='running'
    - per-group rows (P1-2): latest report/run, L3 counts, active members

    Args:
        db: aiosqlite database connection.
        output_dir: Optional L3 output root override (defaults to Settings when None).

    Returns:
        OverviewStatsOut with overall dashboard stats.
    """
    # A008: explicit initialization before try
    result: OverviewStatsOut = OverviewStatsOut()
    try:
        msg_count = await get_message_count(db)
        topic_count = await get_topic_count(db)

        cursor = await db.execute("SELECT COUNT(*) FROM groups WHERE is_active = 1")
        row = await cursor.fetchone()
        total_groups: int = row[0] if row else 0

        cursor = await db.execute("SELECT COUNT(*) FROM report_versions")
        row = await cursor.fetchone()
        total_reports: int = row[0] if row else 0

        cursor = await db.execute("SELECT COUNT(*) FROM feedback_events")
        row = await cursor.fetchone()
        total_feedback: int = row[0] if row else 0

        cursor = await db.execute("SELECT COUNT(*) FROM pipeline_runs WHERE status = 'running'")
        row = await cursor.fetchone()
        active_runs: int = row[0] if row else 0

        # P1-2: per-group rows (latest report/run + L3 counts + active members)
        group_rows = await _get_per_group_rows(db, output_dir=output_dir)

        result = OverviewStatsOut(
            total_messages=msg_count,
            total_groups=total_groups,
            total_topics=topic_count,
            total_reports=total_reports,
            total_feedback=total_feedback,
            active_runs=active_runs,
            groups=group_rows,
        )
    except Exception:
        # P014: NEVER raise from service -- log and return empty model
        logger.exception("get_dashboard_summary failed")
    return result
