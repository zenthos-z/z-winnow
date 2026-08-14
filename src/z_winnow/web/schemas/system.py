"""System schema models.

Response models for system health checks and configuration.
No direct table mapping — these are runtime-computed values.

Pure Pydantic — no FastAPI dependency.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class HealthCheckOut(BaseModel):
    """Response model for system health check endpoint."""

    model_config = ConfigDict(from_attributes=True)

    status: str = "ok"
    version: str | None = None
    database: str = "unknown"
    uptime_seconds: float | None = None


class ConfigOut(BaseModel):
    """Response model for system configuration endpoint.

    Returns sanitized configuration (no secrets).
    """

    model_config = ConfigDict(from_attributes=True)

    db_path: str | None = None
    web_port: int | None = None
    default_model: str | None = None
    log_level: str | None = None
    features: dict[str, Any] | None = None


class ConfigUpdateIn(BaseModel):
    """Body for PUT /system/config — onboarding wizard「保存并重启」.

    ``values``: {Settings field_name: value}. Omit a key to keep it; "" to clear.
    ``infra``: {compose env var: value} for the memos-api container (.env).
    ``restart``: trigger an app process restart after persisting (default true).
    ``targets``: optional subset to probe on /system/config/test — {"llm","ciphertalk","memos","vision"}.
    """

    model_config = ConfigDict(extra="forbid")
    values: dict[str, Any] = {}
    infra: dict[str, str] = {}
    restart: bool = True
    targets: list[str] | None = None


class ConfigUpdateOut(BaseModel):
    """Response for PUT /system/config. Never echoes secret values."""

    model_config = ConfigDict(from_attributes=True)
    ok: bool = True
    applied_fields: list[str] = []
    infra_written: list[str] = []
    restart: bool = False
    warnings: list[str] = []


class ProbeOut(BaseModel):
    """Response for POST /system/config/test (connectivity probe)."""

    model_config = ConfigDict(from_attributes=True)
    llm: str = "skipped"
    ciphertalk: str = "skipped"
    memos: str = "skipped"
    vision: str = "skipped"


class LarkCliStatusOut(BaseModel):
    """lark-cli readiness (#8) — drives the onboarding step + group-config badge.

    We push to Feishu as the logged-in user (``--as user``), so readiness =
    binary present AND user identity available AND base/drive scopes granted.
    """

    model_config = ConfigDict(from_attributes=True)
    installed: bool = False
    path: str = ""
    version: str = ""
    authed: bool = False  # identities.user.available
    user_name: str = ""
    user_status: str = ""  # "ready" | "needs_refresh" | ""
    base_drive_ok: bool = False  # scope covers base:app:create + drive:file:upload
    note: str = ""  # guidance when not ready


class SystemToolsOut(BaseModel):
    """Response for GET /system/tools — external tool readiness."""

    model_config = ConfigDict(from_attributes=True)
    lark_cli: LarkCliStatusOut
