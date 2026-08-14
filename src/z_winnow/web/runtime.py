"""Restart orchestration for the onboarding wizard's「保存并重启」.

Decouples the web layer (PUT /system/config endpoint) from the CLI layer
(``winnow web``):

1. ``cli._cmd_web`` builds a ``uvicorn.Server``, calls :func:`register_server`,
   then ``server.run()`` (blocks).
2. The PUT /system/config endpoint calls :func:`request_restart` after persisting
   the config; a Starlette ``BackgroundTask`` then calls :func:`trigger_shutdown`
   (flips ``server.should_exit``) **after** the HTTP 202 response is flushed.
3. uvicorn releases the listening socket during its shutdown (before
   ``server.run()`` returns), then :func:`take_restart_flag` is checked and the
   CLI ``os.execv``-replaces the process — port is free, child rebinds cleanly.

Kept as a tiny standalone module to avoid a cli ↔ web import cycle.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass  # uvicorn.Server typed loosely as Any to avoid an import-time dependency

_state: dict[str, Any] = {"server": None, "restart_requested": False}
_lock = threading.Lock()


def register_server(server: Any) -> None:
    """Register the running uvicorn.Server (called from cli._cmd_web)."""
    with _lock:
        _state["server"] = server


def get_server() -> Any | None:
    return _state.get("server")


def request_restart() -> bool:
    """Arm the restart flag. Returns True if a server is registered.

    Does NOT flip ``should_exit`` itself — the caller wraps :func:`trigger_shutdown`
    in a BackgroundTask so the HTTP response is sent first.
    """
    with _lock:
        if _state.get("server") is None:
            return False
        _state["restart_requested"] = True
        return True


async def trigger_shutdown() -> None:
    """BackgroundTask: flip ``server.should_exit`` so uvicorn begins shutdown.

    Runs after the PUT response is flushed (Starlette BackgroundTask guarantee),
    so the client receives its 202 before the port goes down.
    """
    srv = get_server()
    if srv is not None:
        srv.should_exit = True  # type: ignore[attr-defined]


def take_restart_flag() -> bool:
    """Read + clear the flag (called from cli after ``server.run()`` returns)."""
    with _lock:
        flag = bool(_state.get("restart_requested"))
        _state["restart_requested"] = False
        return flag


__all__ = [
    "get_server",
    "register_server",
    "request_restart",
    "take_restart_flag",
    "trigger_shutdown",
]
