"""T-W14-2: Unified exception-to-JSON error handler middleware.

Maps Python exception types to deterministic HTTP status codes with a consistent
JSON envelope: ``{"error": "<ExceptionTypeName>", "detail": "<message>"}``.

Exception mapping:
  - ValueError -> 422
  - PermissionError -> 403
  - FileNotFoundError -> 404
  - unhandled Exception -> 500

P054: Three-phase pipeline (parse -> validate -> business logic) applies to
the request/response flow through this middleware.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Exception type -> HTTP status code mapping
_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    ValueError: 422,
    PermissionError: 403,
    FileNotFoundError: 404,
}


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that catches exceptions and returns structured JSON errors.

    Catches exceptions raised by route handlers and maps them to appropriate
    HTTP status codes. All error responses use a consistent JSON envelope:
    ``{"error": "<ExceptionTypeName>", "detail": "<message>"}``.

    Non-exception responses (normal 200 etc.) pass through unchanged.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            # Look up exception type in our mapping; default to 500
            status_code: int = _EXCEPTION_STATUS_MAP.get(type(exc), 500)
            error_name: str = type(exc).__name__
            detail: str = str(exc)

            logger.error(
                "Unhandled exception in %s %s: %s: %s",
                request.method,
                request.url.path,
                error_name,
                detail,
                exc_info=True,
            )

            return JSONResponse(
                status_code=status_code,
                content={
                    "error": error_name,
                    "detail": detail,
                },
            )
