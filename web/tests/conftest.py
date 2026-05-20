"""
Shared fixtures for the web test suite.

The web app reads env vars at module import; tests must set them BEFORE
importing app or oauth. The fixtures below do that under monkeypatch and
reimport the modules per test so module-level state cannot leak.
"""

import importlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _web_env(monkeypatch):
    """Provide a complete env set so module imports succeed.

    Tests that exercise the session-key validation path override
    SESSION_SECRET_KEY explicitly to assert fail-fast on missing/bad values.
    """
    monkeypatch.setenv("COGNITO_SIGNIN_URL", "https://example.invalid/signin")
    monkeypatch.setenv("COGNITO_LOGOUT_URL", "https://example.invalid/logout?client_id=x&response_type=code")
    monkeypatch.setenv("COGNITO_WELL_KNOWN_URL", "https://example.invalid/.well-known/openid-configuration")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("COGNITO_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("OAUTH_CALLBACK_URI", "https://test.example.com/callback")
    monkeypatch.setenv("OAUTH_POST_LOGOUT_URL", "https://test.example.com/chat")
    monkeypatch.setenv("AGENT_ENDPOINT_URL", "https://agent.example.test/chat")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-key-do-not-use-in-prod")
    yield
    for m in ("app", "oauth"):
        sys.modules.pop(m, None)


@pytest.fixture
def oauth_module():
    """Fresh import of web.oauth with env applied. The autouse fixture sets env;
    we pop and reimport here so each test gets a clean module."""
    sys.modules.pop("oauth", None)
    return importlib.import_module("oauth")


@pytest.fixture
def fastapi_app_with_routes(oauth_module):
    """A FastAPI app with the OAuth routes wired and the Authlib client
    patched. Tests drive it via starlette.testclient.TestClient."""
    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-key")

    # Patch the Authlib seams BEFORE registering routes so the route handlers
    # close over the mocked methods.
    oauth_module.oauth.cognito.authorize_redirect = AsyncMock()
    oauth_module.oauth.cognito.authorize_access_token = AsyncMock()

    oauth_module.add_oauth_routes(app)
    return app, oauth_module
