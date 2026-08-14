"""Auth route — POST /api/v1/auth/login, GET /api/v1/auth/status.

Sets an ``api_key`` cookie (httponly) so subsequent write ops (PUT/POST) pass the
global ApiKeyMiddleware, which already reads ``request.cookies.get('api_key')``.
With ``web_api_key`` unset (dev mode) login is a no-op pass-through.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    api_key: str


@router.get("/status")
async def auth_status() -> dict[str, object]:
    """Tell the wizard whether a login gate is needed (web_api_key configured?)."""
    from z_winnow.config.settings import get_settings

    expected = get_settings().web_api_key
    return {"login_required": bool(expected)}


@router.post("/login")
async def login(body: LoginIn) -> JSONResponse:
    """Validate api_key and set the api_key cookie (7-day expiry)."""
    from z_winnow.config.settings import get_settings

    expected = get_settings().web_api_key
    if not expected:
        # dev mode — no key configured; writes already pass through.
        return JSONResponse(
            {"ok": True, "dev": True, "detail": "dev 模式（未配置 web_api_key），无需登录"}
        )

    if body.api_key != expected:
        raise HTTPException(status_code=401, detail="api_key 不正确")

    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        "api_key",
        body.api_key,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
        path="/",
    )
    return resp
