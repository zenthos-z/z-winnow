"""Topic service -- topic summary queries with lifecycle filtering.

Wraps existing ``Storage.get_topic_summary`` and adds new SQL for
lifecycle filtering and pagination not covered by existing functions.

# P050: Parameterized SQL for new queries.
# P022: Pure data retrieval -- zero LLM calls.
# P009: All filter params default to None and cascade transparently.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from z_winnow.web.services import PaginatedResult

# L070: Conditional import
try:
    from z_winnow.web.schemas.data import L3SummaryOut as TopicDetail
    from z_winnow.web.schemas.data import L3SummaryOut as TopicSummary
except ImportError:

    class TopicSummary:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    TopicDetail = TopicSummary  # type: ignore[misc]


logger = logging.getLogger(__name__)


async def list_topics(
    db: aiosqlite.Connection,
    *,
    group_id: str | None = None,
    date: str | None = None,
    lifecycle: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PaginatedResult:
    """List topic summaries with filtering and pagination.

    Uses existing ``database.get_topics_by_date`` where applicable;
    new SQL for lifecycle filter and combined filter scenarios.

    Args:
        db: aiosqlite database connection.
        group_id: Optional group filter.
        date: Optional date filter (YYYYMMDD).
        lifecycle: Optional lifecycle filter (emerging|active|declining|closed).
        page: Page number (1-based).
        page_size: Items per page.

    Returns:
        PaginatedResult of TopicSummary items.
    """
    # A008: explicit initialization
    result: PaginatedResult = PaginatedResult(items=[], total=0, page=page, page_size=page_size)

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        conditions: list[str] = []
        params: list[str] = []

        if group_id is not None:
            conditions.append("group_id = ?")
            params.append(group_id)

        if date is not None:
            conditions.append("date = ?")
            params.append(date)

        if lifecycle is not None:
            conditions.append("lifecycle = ?")
            params.append(lifecycle)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # Count total matching rows
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM topic_summaries {where}",
            tuple(params),
        )
        row = await cursor.fetchone()
        total: int = row[0] if row else 0

        # Fetch page
        offset = (page - 1) * page_size
        cursor = await db.execute(
            f"SELECT * FROM topic_summaries {where} ORDER BY date DESC, summary_id LIMIT ? OFFSET ?",
            (*tuple(params), page_size, offset),
        )
        rows = await cursor.fetchall()

        items = [TopicSummary.model_validate(dict(r)) for r in rows]
        result = PaginatedResult(items=items, total=total, page=page, page_size=page_size)
    except Exception:
        # P014: log and return empty result
        logger.exception("list_topics failed")
        result = PaginatedResult(items=[], total=0, page=page, page_size=page_size)
    finally:
        db.row_factory = original_factory

    return result


async def get_topic_detail(
    db: aiosqlite.Connection,
    summary_id: str,
) -> TopicDetail | None:
    """Get a single topic summary by ID.

    Args:
        db: aiosqlite database connection.
        summary_id: Topic summary identifier.

    Returns:
        TopicDetail or None if not found.
    """
    # A008: explicit initialization
    result: TopicDetail | None = None

    original_factory = db.row_factory
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            "SELECT * FROM topic_summaries WHERE summary_id = ?",
            (summary_id,),
        )
        row = await cursor.fetchone()
        if row is not None:
            result = TopicDetail.model_validate(dict(row))
    except Exception:
        logger.exception("get_topic_detail failed for summary_id=%s", summary_id)
        result = None
    finally:
        db.row_factory = original_factory

    return result
