"""
Tests for web/oauth.py — the Cognito OAuth flow.

These tests assert the contract of /login, /callback, /logout against a
FastAPI app with the Authlib client patched. Network calls to Cognito are
never made in tests.
"""

from fastapi.responses import RedirectResponse
from starlette.testclient import TestClient
from unittest.mock import AsyncMock


def test_E1_login_redirects_via_authlib(fastapi_app_with_routes):
    app, oauth_module = fastapi_app_with_routes
    # authorize_redirect returns a RedirectResponse in production; mirror that.
    oauth_module.oauth.cognito.authorize_redirect = AsyncMock(
        return_value=RedirectResponse(url="https://cognito.example/authorize?state=xyz")
    )

    client = TestClient(app)
    response = client.get("/login", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://cognito.example/authorize")
    assert oauth_module.oauth.cognito.authorize_redirect.await_count == 1


def test_E2_callback_stores_session_and_redirects_to_chat(fastapi_app_with_routes):
    app, oauth_module = fastapi_app_with_routes
    oauth_module.oauth.cognito.authorize_access_token = AsyncMock(
        return_value={
            "access_token": "tkn-abc",
            "userinfo": {"cognito:username": "alice"},
        }
    )

    client = TestClient(app)
    response = client.get("/callback?code=abc", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/chat"

    # Drive another request to verify the session sticks (TestClient persists cookies).
    # Mount a trivial endpoint to inspect req.session.
    from starlette.requests import Request as StarletteRequest
    @app.get("/_session_dump")
    async def _dump(req: StarletteRequest):
        return dict(req.session)

    inspect = client.get("/_session_dump")
    assert inspect.json() == {"access_token": "tkn-abc", "username": "alice"}


def test_E3_logout_clears_session_and_redirects_with_logout_uri(fastapi_app_with_routes):
    app, oauth_module = fastapi_app_with_routes
    oauth_module.oauth.cognito.authorize_access_token = AsyncMock(
        return_value={"access_token": "tkn-abc", "userinfo": {"cognito:username": "alice"}}
    )
    client = TestClient(app)
    client.get("/callback?code=abc")

    response = client.get("/logout", follow_redirects=False)
    assert response.status_code == 307
    assert "logout_uri=https://test.example.com/chat" in response.headers["location"]


def test_E4_callback_uri_comes_from_env_not_localhost(fastapi_app_with_routes):
    """The redirect_uri passed to authorize_redirect must come from
    OAUTH_CALLBACK_URI, not the hard-coded localhost default."""
    app, oauth_module = fastapi_app_with_routes
    from fastapi.responses import RedirectResponse
    oauth_module.oauth.cognito.authorize_redirect = AsyncMock(
        return_value=RedirectResponse(url="https://x/")
    )

    client = TestClient(app)
    client.get("/login")

    # authorize_redirect is called with (req, callback_uri)
    _, callback_uri = oauth_module.oauth.cognito.authorize_redirect.call_args[0]
    assert callback_uri == "https://test.example.com/callback"
    assert "localhost" not in callback_uri


def test_E5_post_logout_uri_comes_from_env_not_localhost(fastapi_app_with_routes):
    app, oauth_module = fastapi_app_with_routes
    oauth_module.oauth.cognito.authorize_access_token = AsyncMock(
        return_value={"access_token": "t", "userinfo": {"cognito:username": "u"}}
    )
    client = TestClient(app)
    client.get("/callback?code=abc")

    response = client.get("/logout", follow_redirects=False)
    location = response.headers["location"]
    assert "logout_uri=https://test.example.com/chat" in location
    assert "localhost" not in location
