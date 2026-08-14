"""T-W14-2: Tests for web middleware — API key auth + error handler.

P011: Each B-criterion maps to one or more named test functions.
P012: Every test uses monkeypatch for env isolation.
L033: pydantic-settings .env file silently overrides monkeypatch — must set
      model_config["env_file"] = None in test Settings fixture.
A019: B6 exercises real cookie parsing through Starlette HTTP layer.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

# ============================================================
# Fixtures — minimal FastAPI app + settings isolation
# ============================================================


# B1/B2/B3/B6: Minimal app with one GET and one POST endpoint
def _create_app() -> FastAPI:
    """Create a minimal FastAPI app with auth + error handler middleware."""
    from z_winnow.web.middleware.auth import ApiKeyMiddleware
    from z_winnow.web.middleware.error_handler import ErrorHandlerMiddleware

    app = FastAPI()

    # Register error handler first (outermost), then auth (inner)
    app.add_middleware(ErrorHandlerMiddleware)
    app.add_middleware(ApiKeyMiddleware)

    @app.get("/test-read")
    async def read_endpoint():
        return {"ok": True}

    @app.options("/test-options")
    async def options_endpoint():
        return {"ok": True}

    @app.post("/test-write")
    async def write_endpoint():
        return {"written": True}

    # Error handler test routes
    @app.get("/error-value")
    async def error_value():
        raise ValueError("bad value")

    @app.get("/error-permission")
    async def error_permission():
        raise PermissionError("not allowed")

    @app.get("/error-not-found")
    async def error_not_found():
        raise FileNotFoundError("missing file")

    @app.get("/error-generic")
    async def error_generic():
        raise RuntimeError("something broke")

    @app.get("/error-normal")
    async def normal_response():
        return {"ok": True}

    return app


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """P012 + L033: Isolate settings for every test.

    - Set env var via monkeypatch (not .env file)
    - Reset settings singleton so it picks up the new env var
    """
    monkeypatch.setenv("WINNOW_WEB_API_KEY", "test-key-123")
    # L033: Disable .env file so monkeypatch is authoritative
    monkeypatch.setenv("WINNOW_ENV", "test")
    # Reset singleton to pick up new env vars
    from z_winnow.config.settings import reset_settings

    reset_settings()
    yield
    reset_settings()


@pytest.fixture()
def client():
    """TestClient with middleware-mounted app."""
    app = _create_app()
    return TestClient(app)


# ============================================================
# B1: Auth — POST without key returns 401
# ============================================================


def test_auth_post_no_key_returns_401(client: TestClient, monkeypatch):
    """B1: POST with no X-API-Key header and no cookie -> 401 + JSON error body."""
    # Ensure key is configured (already set by autouse fixture, but be explicit)
    monkeypatch.setenv("WINNOW_WEB_API_KEY", "test-key-123")
    from z_winnow.config.settings import reset_settings

    reset_settings()

    app = _create_app()
    c = TestClient(app)

    response = c.post("/test-write")
    assert response.status_code == 401
    body = response.json()
    assert body["error"] == "AuthenticationError"
    assert "detail" in body


# ============================================================
# B2: Auth — POST with valid key passes through
# ============================================================


def test_auth_post_valid_key_header_passes(client: TestClient, monkeypatch):
    """B2: POST with X-API-Key matching WINNOW_WEB_API_KEY -> 200."""
    monkeypatch.setenv("WINNOW_WEB_API_KEY", "test-key-123")
    from z_winnow.config.settings import reset_settings

    reset_settings()

    app = _create_app()
    c = TestClient(app)

    response = c.post("/test-write", headers={"X-API-Key": "test-key-123"})
    assert response.status_code == 200
    assert response.json() == {"written": True}


# ============================================================
# B3: Auth — GET/OPTIONS skip auth entirely
# ============================================================


def test_auth_get_no_key_passes(client: TestClient, monkeypatch):
    """B3: GET with no key header -> 200 (auth bypassed)."""
    monkeypatch.setenv("WINNOW_WEB_API_KEY", "test-key-123")
    from z_winnow.config.settings import reset_settings

    reset_settings()

    app = _create_app()
    c = TestClient(app)

    response = c.get("/test-read")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_auth_options_no_key_passes(client: TestClient, monkeypatch):
    """B3: OPTIONS with no key header -> 200 (auth bypassed)."""
    monkeypatch.setenv("WINNOW_WEB_API_KEY", "test-key-123")
    from z_winnow.config.settings import reset_settings

    reset_settings()

    app = _create_app()
    c = TestClient(app)

    response = c.options("/test-options")
    assert response.status_code == 200


# ============================================================
# B4: Error handler — exception mapping
# ============================================================


def test_error_handler_value_error_returns_422(client: TestClient, monkeypatch):
    """B4: ValueError -> 422 + JSON envelope."""
    monkeypatch.setenv("WINNOW_WEB_API_KEY", "")
    from z_winnow.config.settings import reset_settings

    reset_settings()

    app = _create_app()
    c = TestClient(app)

    response = c.get("/error-value")
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "ValueError"
    assert body["detail"] == "bad value"


def test_error_handler_permission_error_returns_403(client: TestClient, monkeypatch):
    """B4: PermissionError -> 403 + JSON envelope."""
    monkeypatch.setenv("WINNOW_WEB_API_KEY", "")
    from z_winnow.config.settings import reset_settings

    reset_settings()

    app = _create_app()
    c = TestClient(app)

    response = c.get("/error-permission")
    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "PermissionError"
    assert body["detail"] == "not allowed"


def test_error_handler_file_not_found_returns_404(client: TestClient, monkeypatch):
    """B4: FileNotFoundError -> 404 + JSON envelope."""
    monkeypatch.setenv("WINNOW_WEB_API_KEY", "")
    from z_winnow.config.settings import reset_settings

    reset_settings()

    app = _create_app()
    c = TestClient(app)

    response = c.get("/error-not-found")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "FileNotFoundError"
    assert body["detail"] == "missing file"


def test_error_handler_generic_exception_returns_500(client: TestClient, monkeypatch):
    """B4: Unhandled Exception -> 500 + JSON envelope."""
    monkeypatch.setenv("WINNOW_WEB_API_KEY", "")
    from z_winnow.config.settings import reset_settings

    reset_settings()

    app = _create_app()
    c = TestClient(app)

    response = c.get("/error-generic")
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "RuntimeError"
    assert body["detail"] == "something broke"


# ============================================================
# B5: Error handler — non-exception responses pass through
# ============================================================


def test_error_handler_normal_response_passes_through(client: TestClient, monkeypatch):
    """B5: Normal 200 JSON response passes through error handler unchanged."""
    monkeypatch.setenv("WINNOW_WEB_API_KEY", "")
    from z_winnow.config.settings import reset_settings

    reset_settings()

    app = _create_app()
    c = TestClient(app)

    response = c.get("/error-normal")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


# ============================================================
# B6: Cookie auth path works (A019: real Starlette cookie parsing)
# ============================================================


def test_auth_cookie_passes(client: TestClient, monkeypatch):
    """B6: POST with api_key cookie set to valid key -> 200.

    A019: Uses TestClient with cookies parameter — real Starlette HTTP
    cookie parsing, not mocked request.cookies dict.
    """
    monkeypatch.setenv("WINNOW_WEB_API_KEY", "test-key-123")
    from z_winnow.config.settings import reset_settings

    reset_settings()

    app = _create_app()
    c = TestClient(app, cookies={"api_key": "test-key-123"})

    response = c.post("/test-write")
    assert response.status_code == 200
    assert response.json() == {"written": True}


# ============================================================
# Edge case: wrong key returns 401
# ============================================================


def test_auth_wrong_key_returns_401(client: TestClient, monkeypatch):
    """Edge: POST with wrong key -> 401."""
    monkeypatch.setenv("WINNOW_WEB_API_KEY", "test-key-123")
    from z_winnow.config.settings import reset_settings

    reset_settings()

    app = _create_app()
    c = TestClient(app)

    response = c.post("/test-write", headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401
