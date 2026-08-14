"""Layer 4 Markdown report output writer — T-W7-5.

Writes composed Markdown reports to the filesystem:
  - Daily reports: reports/daily/{date}.md

Creates parent directories as needed, is idempotent (overwrite on
re-run), and returns the absolute file path written.

Design principles (P011, P013):
  - Simple file I/O, no template rendering (templates are for Layer 3 → 4 rendering)
  - No LLM calls, no database access
  - Thread-safe via atomic write (write to temp + rename)
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def write_daily_report(
    markdown: str,
    date: str,
    output_dir: str = "reports",
    *,
    group_id: str | None = None,
) -> str:
    """Write a daily report Markdown file.

    Creates ``{output_dir}/daily/{group_id}/{date}.md`` if group_id provided,
    otherwise ``{output_dir}/daily/{date}.md``. Parent directories
    are created if they do not exist. Uses atomic write (temp file
    + rename) to avoid partial reads.

    Args:
        markdown:  Complete Markdown report content.
        date:      Date string in YYYYMMDD format (e.g. "20260428").
        output_dir: Base output directory (default "reports").
        group_id:  Optional group identifier for multi-group isolation.

    Returns:
        Absolute path to the written file.

    Raises:
        OSError: If the file cannot be written (disk full, permissions).
    """
    # P011: Simple file I/O — no template rendering, no LLM, no DB.
    daily_dir = Path(output_dir) / "daily" / group_id if group_id else Path(output_dir) / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{date}.md"
    filepath = daily_dir / filename

    # Atomic write: write to temp file in same directory, then rename.
    # This prevents readers from seeing partially written files.
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".md",
            prefix=".tmp_daily_",
            dir=str(daily_dir),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(markdown)
            # Atomic rename (same filesystem → guaranteed atomic on POSIX)
            os.replace(tmp_path, str(filepath))
        except Exception:
            # Clean up temp file on error
            Path(tmp_path).unlink(missing_ok=True)
            raise
    except OSError:
        # Fallback: direct write if tempfile fails (e.g. permissions on temp dir)
        filepath.write_text(markdown, encoding="utf-8")

    logger.info(
        "write_daily_report: written %d chars to %s",
        len(markdown),
        filepath,
    )
    return str(filepath.resolve())
