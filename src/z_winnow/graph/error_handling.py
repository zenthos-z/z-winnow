"""Error handling utilities — retry decorator, NodeError, quality checks.

Provides:
- NodeError: Exception class with node context for graceful error propagation
- with_retry: Decorator for async node functions with configurable retries
- QualityResult: Pydantic model for structured quality assessment
- check_daily_report_quality: Structural quality checker for daily reports

Architecture reference: docs/architecture-detail.md §3.5-3.6
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Type variable for decorated async functions
F = TypeVar("F", bound=Callable[..., Any])


# ============================================================
# NodeError — Exception with node execution context
# ============================================================


class NodeError(Exception):
    """Exception raised when a graph node encounters an error.

    Wraps the original exception with node-level context for better
    observability and debugging. Raised by the @with_retry decorator
    when all retry attempts are exhausted.

    Attributes:
        node_name: Name of the node where the error occurred.
        original_error: The original exception that was caught.
        retry_count: Number of retries attempted before this error.
    """

    def __init__(
        self,
        node_name: str,
        original_error: BaseException,
        retry_count: int = 0,
    ) -> None:
        self.node_name: str = node_name
        self.original_error: BaseException = original_error
        self.retry_count: int = retry_count
        super().__init__(f"Node '{node_name}' failed after {retry_count} retries: {original_error}")


# ============================================================
# QualityResult — Structured quality assessment
# ============================================================


class QualityResult(BaseModel):
    """Structured result of a daily report quality check.

    Used by check_daily_report_quality() to return a standardized
    assessment of report completeness and correctness.

    Attributes:
        score: Quality score from 0.0 to 1.0, where 1.0 is perfect.
               Each structural issue reduces the score by 0.25.
        issues: Human-readable list of issues found.
    """

    score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Quality score 0.0-1.0, where 1.0 is perfect",
    )
    issues: list[str] = Field(
        default_factory=list,
        description="List of human-readable issues found",
    )

    @property
    def passed(self) -> bool:
        """Whether the quality check passed (no issues found)."""
        return len(self.issues) == 0

    @property
    def requires_retry(self) -> bool:
        """Whether the quality is low enough to warrant a retry.

        Returns True when score < 0.5, indicating the report likely
        needs regeneration with quality feedback.
        """
        return self.score < 0.5


# ============================================================
# Quality Check — Structural validation for daily reports
# ============================================================


def check_daily_report_quality(report: dict[str, Any]) -> QualityResult:
    """Check daily report quality based on structural completeness.

    Evaluates the following quality dimensions:
    1. **Structure**: overview + topics + trend_analysis must be present
    2. **Overview length**: at least 10 words for a meaningful summary
    3. **Topics**: at least one topic must be identified
    4. **Trend analysis**: non-empty string (>=10 chars)
    5. **Trend summary**: one-line summary of topic evolution
    6. **Highlights**: at least one highlight recommended (soft check)

    Each issue reduces the quality score by 0.25. The score floors at 0.0.

    Args:
        report: Report dict with keys:
            - overview (str): Daily summary paragraph (3-5 sentences).
            - important_notice (str | None): Important notice text, if any.
            - topics (list[dict]): Unified topic list, each with topic_id,
              topic_name, lifecycle, status, weight, background, process, conclusion, etc.
            - trend_analysis (str): Free-text trend analysis paragraph.
            - trend_summary (str): One-line topic evolution summary.
            - highlights (list[str]): Key quotes or insights from the day.

    Returns:
        QualityResult with numeric score and issues list.

    Example:
        >>> result = check_daily_report_quality({
        ...     "overview": "Today the team discussed architecture decisions.",
        ...     "topics": [{"topic_name": "Architecture review", "lifecycle": "emerging"}],
        ...     "trend_analysis": "Team moved from design to implementation phase.",
        ...     "trend_summary": "Architecture discussion emerged.",
        ...     "highlights": ["Good discussion about patterns"],
        ... })
        >>> result.passed
        True
        >>> result.score
        1.0
    """
    issues: list[str] = []

    # 1. Structure completeness — mandatory sections
    if not report.get("overview"):
        issues.append("缺少日报概览 (overview)")
    if not report.get("topics"):
        issues.append("缺少价值议题表 (topics)")
    if not report.get("trend_analysis"):
        issues.append("缺少讨论趋势分析 (trend_analysis)")

    # 2. Overview length — at least 10 words for meaningful content
    overview: str = report.get("overview", "")
    if overview:
        word_count = len(overview.split())
        if word_count < 10:
            issues.append(f"日报概览过短 ({word_count} 词，需要至少 10 词)")
    elif "overview" in report:
        # Key exists but value is empty/falsy
        issues.append("日报概览为空字符串")

    # 3. Topic sections — at least one topic identified
    topics = report.get("topics", [])
    if isinstance(topics, list) and len(topics) == 0 and "topics" in report:
        issues.append("价值议题表为空，未识别到任何议题")

    # 4. Trend analysis — required sub-fields
    trend_analysis = report.get("trend_analysis", "")
    if isinstance(trend_analysis, str) and len(trend_analysis.strip()) < 10:
        issues.append("讨论趋势分析过短（需要至少 10 字符）")
    elif not trend_analysis:
        issues.append("讨论趋势分析为空")

    trend_summary = report.get("trend_summary", "")
    if not trend_summary:
        issues.append("缺少议题演变汇总 (trend_summary)")

    # 5. Highlights — soft check (logged but not scored)
    highlights = report.get("highlights", [])
    if isinstance(highlights, list) and len(highlights) == 0:
        logger.debug("Highlights list is empty (optional, not scored)")

    # Calculate score: each issue reduces 0.25, floor at 0.0
    score = max(0.0, 1.0 - len(issues) * 0.25)

    return QualityResult(score=score, issues=issues)


# ============================================================
# with_retry — Async node retry decorator
# ============================================================


def with_retry(
    max_retries: int = 2,
    *,
    retry_delay: float = 1.0,
    backoff_factor: float = 2.0,
    retryable_exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[F], F]:
    """Decorator that retries an async function on failure.

    Implements exponential backoff with configurable max retries.
    When all retries are exhausted, raises :class:`NodeError` with
    the node name, original exception, and retry count context.

    Args:
        max_retries: Maximum number of retry attempts (default 2).
        retry_delay: Initial delay in seconds between retries (default 1.0).
        backoff_factor: Multiplier for exponential backoff — each subsequent
            retry waits ``retry_delay * backoff_factor^attempt`` seconds
            (default 2.0).
        retryable_exceptions: Exception types eligible for retry. Default is
            ``(Exception,)`` which retries on all exceptions.

    Returns:
        Decorated async function with retry logic. The wrapper preserves
        the original function's signature, name, and docstring.

    Raises:
        NodeError: When all retries are exhausted. Contains node_name,
            original_error, and retry_count attributes.

    Example:
        >>> @with_retry(max_retries=2, retry_delay=1.0)
        ... async def node_daily_reporter(state: OverallState) -> dict:
        ...     # May fail transiently due to API rate limits
        ...     return await call_llm_api(state)
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            node_name = getattr(func, "__name__", "unknown")
            last_error: BaseException | None = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_error = exc
                    if attempt < max_retries:
                        delay = retry_delay * (backoff_factor**attempt)
                        logger.warning(
                            "Retry %d/%d for node '%s' after %.1fs: %s",
                            attempt + 1,
                            max_retries,
                            node_name,
                            delay,
                            exc,
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            "Node '%s' failed after %d retries: %s",
                            node_name,
                            max_retries,
                            exc,
                        )

            # All retries exhausted — raise contextual NodeError
            raise NodeError(
                node_name=node_name,
                original_error=last_error or RuntimeError("Unknown error in node"),
                retry_count=max_retries,
            )

        return wrapper  # type: ignore[return-value]

    return decorator
