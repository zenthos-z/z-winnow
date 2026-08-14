"""Web control panel for z-winnow.

Provides a FastAPI REST API backend (``/api/v1``) with static SPA frontend
serving at ``/ui/``. Includes API key authentication middleware and unified
error handling.

Usage:
    poetry run winnow web
    # or: uvicorn z_winnow.web.app:app --port 8100

Default port: 8100 (configurable via WEB_PORT env var).
"""
