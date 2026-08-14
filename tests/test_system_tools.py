"""Tests for GET /api/v1/system/tools — lark-cli readiness probe (#8).

Covers:
- The endpoint serializes the check_lark_cli() result (route + schema wiring).
- The not-installed path (shutil.which → None) yields installed=False + guidance.
- The auth-status JSON parsing path: a canned ``lark-cli auth status`` envelope
  is parsed into authed / user_name / base_drive_ok.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

import pytest

# ============================================================
# Endpoint wiring (monkeypatch the service probe)
# ============================================================


def test_system_tools_endpoint_returns_lark_cli_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from z_winnow.web.routes import api_router
    from z_winnow.web.services import system_service

    canned: dict[str, Any] = {
        "installed": True,
        "path": "/fake/lark-cli",
        "version": "lark-cli version abc123",
        "authed": True,
        "user_name": "测试用户",
        "user_status": "ready",
        "base_drive_ok": True,
        "note": "",
    }

    async def _fake() -> dict[str, Any]:
        return canned

    monkeypatch.setattr(system_service, "check_lark_cli", _fake)

    app = FastAPI()
    app.include_router(api_router)
    client = TestClient(app)

    resp = client.get("/api/v1/system/tools")
    assert resp.status_code == 200
    lc = resp.json()["lark_cli"]
    assert lc["installed"] is True
    assert lc["authed"] is True
    assert lc["user_name"] == "测试用户"
    assert lc["base_drive_ok"] is True
    assert set(lc) == {
        "installed",
        "path",
        "version",
        "authed",
        "user_name",
        "user_status",
        "base_drive_ok",
        "note",
    }


# ============================================================
# check_lark_cli logic
# ============================================================


class _FakeProc:
    def __init__(self, *, stdout: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


@pytest.mark.asyncio
async def test_check_lark_cli_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binary missing → installed=False + actionable note, no subprocess calls."""
    from z_winnow.web.services import system_service

    monkeypatch.setattr(shutil, "which", lambda _name: "")

    # Guard: if the binary isn't found, create_subprocess_exec must NOT be called.
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("subprocess must not run when binary is missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

    result = await system_service.check_lark_cli()
    assert result["installed"] is False
    assert result["authed"] is False
    assert result["path"] == ""
    assert "lark-cli" in result["note"]


@pytest.mark.asyncio
async def test_check_lark_cli_parses_auth_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Installed + a real-shaped `auth status` envelope → authed/user_name/base_drive_ok parsed."""
    from z_winnow.web.services import system_service

    monkeypatch.setattr(shutil, "which", lambda _name: "/fake/lark-cli")

    auth_envelope = {
        "appId": "cli_xxx",
        "brand": "feishu",
        "identities": {
            "bot": {"status": "ready", "available": True, "message": "Bot identity: ready"},
            "user": {
                "status": "needs_refresh",
                "available": True,
                "message": "User identity: needs refresh",
                "openId": "ou_xxx",
                "userName": "廖瞻",
                "scope": "base:app:create base:table:create drive:file:upload im:message",
            },
        },
        "identity": "user",
    }

    async def _fake_exec(*args: Any, **_kw: Any) -> _FakeProc:
        if "--version" in args:
            return _FakeProc(stdout=b"lark-cli version 452734f")
        if "auth" in args and "status" in args:
            return _FakeProc(stdout=json.dumps(auth_envelope).encode("utf-8"))
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    result = await system_service.check_lark_cli()
    assert result["installed"] is True
    assert result["path"] == "/fake/lark-cli"
    assert result["version"] == "lark-cli version 452734f"
    assert result["authed"] is True
    assert result["user_name"] == "廖瞻"
    assert result["user_status"] == "needs_refresh"
    assert result["base_drive_ok"] is True
    assert result["note"] == ""  # fully ready → no guidance


@pytest.mark.asyncio
async def test_check_lark_cli_missing_scopes_flags_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authed but scope lacks drive:file:upload → base_drive_ok=False + guidance."""
    from z_winnow.web.services import system_service

    monkeypatch.setattr(shutil, "which", lambda _name: "/fake/lark-cli")

    auth_envelope = {
        "identities": {
            "user": {
                "available": True,
                "userName": "u",
                "status": "ready",
                "scope": "base:app:create base:table:create",  # no drive:file:upload
            }
        }
    }

    async def _fake_exec(*args: Any, **_kw: Any) -> _FakeProc:
        if "auth" in args and "status" in args:
            return _FakeProc(stdout=json.dumps(auth_envelope).encode("utf-8"))
        return _FakeProc(stdout=b"lark-cli version x")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    result = await system_service.check_lark_cli()
    assert result["authed"] is True
    assert result["base_drive_ok"] is False
    assert "auth login --domain base,drive" in result["note"]
