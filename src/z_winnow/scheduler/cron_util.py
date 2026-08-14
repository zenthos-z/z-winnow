"""Cron expression utilities for the daily-report scheduler.

Thin, defensive wrappers over ``croniter``. Every public helper is total: an
invalid or empty expression never raises — it returns ``False`` / ``None`` /
``[]`` so callers (the tick loop, the status board) can degrade gracefully
instead of crashing one bad group config.

Conventions:
  - All cron math is done on **tz-aware** datetimes (caller passes ``now`` in the
    scheduler's timezone; ``time_to_cron`` / presets are timezone-agnostic).
  - A cron expression is the standard 5-field form ``"m h dom mon dow"``
    (e.g. ``"0 2 * * *"`` = daily 02:00).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from croniter import CroniterError, croniter  # type: ignore[import-untyped]

__all__ = [
    "PRESETS",
    "cron_to_human",
    "is_due",
    "next_fire",
    "next_n",
    "time_to_cron",
    "validate_cron",
]


def _to_dt(value: object) -> datetime:
    """Narrow croniter's untyped return to datetime (always a datetime here)."""
    assert isinstance(value, datetime)
    return value


# Time presets surfaced by the interactive wizard ("凌晨2:00" etc.).
# Maps a human label -> 5-field cron. Kept here so CLI/views/engine share one table.
PRESETS: dict[str, str] = {
    "凌晨 2:00": "0 2 * * *",
    "早 9:00": "0 9 * * *",
    "晚 20:00": "0 20 * * *",
}


def validate_cron(expr: str | None) -> bool:
    """Return True iff ``expr`` is a syntactically valid 5-field cron string.

    ``None`` / empty / whitespace-only → False. Never raises.
    """
    if not expr or not expr.strip():
        return False
    try:
        croniter(expr.strip(), datetime(2000, 1, 1))  # base is irrelevant; only parsing happens
    except CroniterError:
        return False
    except Exception:  # pragma: no cover - croniter shouldn't raise others, but stay total
        return False
    return True


def is_due(expr: str | None, now_minute: datetime) -> bool:
    """True if ``expr`` matches the minute of ``now_minute``.

    ``now_minute`` should be a tz-aware datetime truncated to the minute. Returns
    False on invalid/empty expr (logs nothing here — callers decide logging).
    """
    if not validate_cron(expr):
        return False
    assert expr is not None
    try:
        return bool(croniter.match(expr.strip(), now_minute))  # type: ignore[arg-type]
    except CroniterError:
        return False
    except Exception:  # pragma: no cover
        return False


def next_fire(expr: str | None, after: datetime) -> datetime | None:
    """Next fire time strictly after ``after``, or None if expr is invalid."""
    if not validate_cron(expr):
        return None
    assert expr is not None
    try:
        return _to_dt(croniter(expr.strip(), after).get_next(datetime))
    except CroniterError:
        return None
    except Exception:  # pragma: no cover
        return None


def next_n(expr: str | None, after: datetime, n: int) -> list[datetime]:
    """Next ``n`` fire times strictly after ``after`` (oldest first). [] on invalid."""
    if not validate_cron(expr) or n <= 0:
        return []
    assert expr is not None
    try:
        it = croniter(expr.strip(), after)
        return [_to_dt(it.get_next(datetime)) for _ in range(n)]
    except CroniterError:
        return []
    except Exception:  # pragma: no cover
        return []


def time_to_cron(hhmm: str) -> str | None:
    """Convert ``"HH:MM"`` (24h) to a daily cron ``"M H * * *"``.

    Returns None on malformed input (so the wizard can re-prompt). Accepts
    single-digit hour/minute (``"9:5"``) and tolerates surrounding whitespace.
    """
    s = (hhmm or "").strip()
    if ":" not in s:
        return None
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{m} {h} * * *"


def cron_to_human(expr: str | None) -> str:
    """Best-effort human label for a cron expr, for the status board.

    Handles the preset/daily-HH:MM shapes the wizard produces; falls back to the
    raw expr (or ``—`` when empty/invalid) so the board never shows a blank.
    """
    if not expr or not expr.strip():
        return "—"
    e = expr.strip()
    # Reverse-lookup preset labels first (nicer than "每日 02:00"→preset wording).
    for label, preset in PRESETS.items():
        if e == preset:
            return label
    # "M H * * *" -> 每日 HH:MM
    fields = e.split()
    if len(fields) == 5 and fields[2] == "*" and fields[3] == "*" and fields[4] == "*":
        try:
            m = int(fields[0])
            h = int(fields[1])
            return f"每日 {h:02d}:{m:02d}"
        except ValueError:
            pass
    return e


def yesterday_iso(now: datetime) -> str:
    """Convenience: previous calendar day of ``now`` as ``"YYYY-MM-DD"``."""
    return (now.date() - timedelta(days=1)).isoformat()
