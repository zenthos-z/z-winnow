"""T-W14-2: Web middleware package — API key auth + unified error handler.

L070: Safe re-exports using conditional imports. This is a PG0 parallel task —
other middleware submodules may not exist yet, so we use try/except to prevent
cascading ImportError.
"""

from __future__ import annotations

__all__ = ["ApiKeyMiddleware", "ErrorHandlerMiddleware"]

# L070: Conditional imports — submodules may not exist in parallel builds
try:
    from z_winnow.web.middleware.auth import ApiKeyMiddleware
except ImportError:
    ApiKeyMiddleware = None  # type: ignore[assignment,misc]

try:
    from z_winnow.web.middleware.error_handler import ErrorHandlerMiddleware
except ImportError:
    ErrorHandlerMiddleware = None  # type: ignore[assignment,misc]
