"""
Tests for web/app.py — the FastAPI host that mounts the Gradio chat UI.

These cover the auth dependency (check_auth) and the chat proxy that
forwards user messages to the agent Lambda. The Gradio mount itself is
not exercised here; it's a thin wrapper over the chat() function.
"""

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


@pytest.fixture
def app_module():
    """Import web.app with env set; pop after to avoid leakage."""
    sys.modules.pop("app", None)
    return importlib.import_module("app")


def _httpx_response(status=200, body=None):
    """Build a fake httpx.Response shape that chat() reads from."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {"text": "ok"}
    return resp


# ---------------------------------------------------------------------------
# check_auth dependency
# ---------------------------------------------------------------------------

def test_F1_check_auth_missing_session_raises_302_to_login(app_module):
    req = MagicMock()
    req.session = {}  # no access_token, no username
    with pytest.raises(HTTPException) as exc_info:
        app_module.check_auth(req)
    assert exc_info.value.status_code == 302
    assert exc_info.value.headers["Location"] == "/login"


def test_F2_check_auth_valid_session_returns_username(app_module):
    req = MagicMock()
    req.session = {"access_token": "tkn", "username": "alice"}
    assert app_module.check_auth(req) == "alice"


# ---------------------------------------------------------------------------
# chat() proxy — patch httpx.post directly (responses library only patches
# the `requests` library, which web/app.py does not use).
# ---------------------------------------------------------------------------

def _fake_request(token, username="alice"):
    """Build the gr.Request-like object chat() reads from."""
    request = MagicMock()
    request.username = username
    request.request = MagicMock()
    request.request.session = {"access_token": token}
    return request


def test_F3_chat_returns_agent_text_on_200(app_module):
    with patch.object(app_module.httpx, "post", return_value=_httpx_response(
        status=200, body={"text": "Sure, watching that trip"}
    )):
        result = app_module.chat("Track Tokyo", history=[], request=_fake_request("tkn"))
    assert result == "Sure, watching that trip"


def test_F4_chat_returns_relogin_string_on_401(app_module):
    with patch.object(app_module.httpx, "post", return_value=_httpx_response(status=401)):
        result = app_module.chat("anything", history=[], request=_fake_request("tkn"))
    assert "re-login" in result.lower()


def test_F5_chat_returns_generic_failure_on_500(app_module):
    with patch.object(app_module.httpx, "post", return_value=_httpx_response(status=500)):
        result = app_module.chat("anything", history=[], request=_fake_request("tkn"))
    assert "failed to communicate" in result.lower()


def test_F6_chat_forwards_session_access_token_as_bearer(app_module):
    """Multi-tenancy invariant: the Bearer token sent to the agent comes
    from THIS request's session, not from any other state."""
    with patch.object(
        app_module.httpx, "post", return_value=_httpx_response(status=200, body={"text": "ok"})
    ) as mock_post:
        app_module.chat("hi", history=[], request=_fake_request("session-token-abc"))

    # httpx.post was called as positional URL + kwargs (headers, json, timeout)
    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer session-token-abc"


def test_F6b_chat_handles_missing_gradio_request(app_module):
    result = app_module.chat("hi", history=[], request=None)
    assert "session" in result.lower()


def test_F6c_load_handles_missing_gradio_request(app_module):
    label, messages = app_module.on_gradio_app_load(request=None)
    assert label == "Logout (user)"
    assert messages[0].role == "assistant"


# ---------------------------------------------------------------------------
# F7/F8 — session-key bug fix
# ---------------------------------------------------------------------------

def test_F7_session_middleware_key_comes_from_env_not_literal_secret(monkeypatch):
    """SessionMiddleware.secret_key must be SESSION_SECRET_KEY, not 'secret'.
    Find the SessionMiddleware in the user_middleware list and read its
    starlette wrapper's secret_key."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "a-strong-random-key-for-tests")
    sys.modules.pop("app", None)
    import app as fresh_app

    from starlette.middleware.sessions import SessionMiddleware
    middlewares = [m for m in fresh_app.fastapi_app.user_middleware if m.cls is SessionMiddleware]
    assert len(middlewares) == 1
    # Starlette stores middleware kwargs in .kwargs; the key is 'secret_key'.
    assert middlewares[0].kwargs["secret_key"] == "a-strong-random-key-for-tests"


def test_F8_import_fails_fast_when_session_secret_missing(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)
    sys.modules.pop("app", None)
    with pytest.raises(RuntimeError, match="SESSION_SECRET_KEY"):
        importlib.import_module("app")


def test_F8b_import_fails_fast_when_session_secret_is_placeholder(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "secret")
    sys.modules.pop("app", None)
    with pytest.raises(RuntimeError, match="placeholder"):
        importlib.import_module("app")


def test_F8c_import_fails_fast_when_session_secret_is_empty(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "")
    sys.modules.pop("app", None)
    with pytest.raises(RuntimeError, match="SESSION_SECRET_KEY"):
        importlib.import_module("app")
