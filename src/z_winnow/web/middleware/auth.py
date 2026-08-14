"""T-W14-2: API key authentication middleware for write operations.

P082 (read-write asymmetric fault tolerance): Auth applies ONLY to non-GET/OPTIONS
methods. GET and OPTIONS pass through without any key inspection.

P035 (bidirectional env-attr mapping): API key read from get_settings().web_api_key,
not raw os.getenv() — pydantic-settings single source of truth.

A013: Key read inside dispatch() via get_settings(), never at module level.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Methods that bypass authentication entirely (P082)
_READ_METHODS = {"GET", "OPTIONS"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that enforces API key authentication on write operations.

    - GET and OPTIONS requests pass through without any key inspection.
    - All other methods (POST, PUT, PATCH, DELETE) require a valid API key.
    - Key accepted via ``X-API-Key`` header OR ``api_key`` cookie (cookie first).
    - Returns 401 JSON on missing or invalid key.

    A013: API key read from get_settings() at call time, not module level.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # P082: Read methods bypass auth entirely
        if request.method in _READ_METHODS:
            return await call_next(request)

        # A013: Read settings at call time, not module level
        from z_winnow.config.settings import get_settings

        settings = get_settings()
        expected_key: str = settings.web_api_key  # type: ignore[union-attr]

        # A008: Pre-initialize variables
        provided_key: str | None = None

        # Check cookie first, then header
        provided_key = request.cookies.get("api_key")
        if not provided_key:
            provided_key = request.headers.get("X-API-Key")

        if not expected_key:
            # No key configured — allow all (development mode)
            logger.warning("web_api_key not configured, allowing unauthenticated write request")
            return await call_next(request)

        if not provided_key or provided_key != expected_key:
            logger.warning(
                "Authentication failed for %s %s — invalid or missing API key",
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=401,
                content={
                    "error": "AuthenticationError",
                    "detail": "Missing or invalid API key",
                },
            )

        return await call_next(request)
