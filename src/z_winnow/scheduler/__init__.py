"""Daily-report scheduler package.

Standalone (no FastAPI dependency): reads per-group ``daily_schedule_cron`` from
the ``groups`` table, fires ``orchestrate()`` + ``auto_push_after_run()`` on
schedule, and compensates missed days on startup. Exposed via the
``winnow scheduler`` CLI (Rich dashboard + guided wizard).

Heavy submodules (engine / preflight / interactive) are imported lazily via
``__getattr__`` so importing the package is cheap and cron_util stays usable
even before the richer modules exist.
"""

from __future__ import annotations

# Light, dependency-free cron helpers are safe to export eagerly.
from z_winnow.scheduler.cron_util import (
    PRESETS,
    cron_to_human,
    is_due,
    next_fire,
    next_n,
    time_to_cron,
    validate_cron,
)

__all__ = [
    "PRESETS",
    "BackfillResult",
    "CheckResult",
    "DailyScheduler",
    "GroupScheduleRow",
    "PreflightReport",
    "TickResult",
    "check_environment",
    "cron_to_human",
    "is_due",
    "next_fire",
    "next_n",
    "run_menu",
    "time_to_cron",
    "try_auto_start_deps",
    "validate_cron",
]

# Map of lazy attribute -> "module:name". Keeps the heavy imports out of the
# package import path (and lets tests target submodules in isolation).
_LAZY: dict[str, str] = {
    "DailyScheduler": "z_winnow.scheduler.engine:DailyScheduler",
    "GroupScheduleRow": "z_winnow.scheduler.engine:GroupScheduleRow",
    "TickResult": "z_winnow.scheduler.engine:TickResult",
    "BackfillResult": "z_winnow.scheduler.engine:BackfillResult",
    "CheckResult": "z_winnow.scheduler.preflight:CheckResult",
    "PreflightReport": "z_winnow.scheduler.preflight:PreflightReport",
    "check_environment": "z_winnow.scheduler.preflight:check_environment",
    "try_auto_start_deps": "z_winnow.scheduler.preflight:try_auto_start_deps",
    "run_menu": "z_winnow.scheduler.interactive:run_menu",
}


def __getattr__(name: str):  # PEP 562
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_path, _, attr = target.partition(":")
    import importlib

    return getattr(importlib.import_module(mod_path), attr)
