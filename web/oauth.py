# This module solves the problem of authenticating users before they can access
# the travel-agent chat UI. Rather than building a custom login system, it
# delegates authentication entirely to Amazon Cognito using the standard
# OAuth 2.0 Authorization Code flow. The browser never handles passwords directly;
# Cognito issues short-lived tokens that the app uses to verify identity and
# authorise calls to the agent Lambda.

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth
import os

# Configuration — all values come from environment variables so the same
# image works in every environment.
COGNITO_SIGNIN_URL = os.getenv("COGNITO_SIGNIN_URL")
COGNITO_LOGOUT_URL = os.getenv("COGNITO_LOGOUT_URL")
COGNITO_WELL_KNOWN_ENDPOINT_URL = os.getenv("COGNITO_WELL_KNOWN_URL")
COGNITO_CLIENT_ID = os.getenv("COGNITO_CLIENT_ID")
COGNITO_CLIENT_SECRET = os.getenv("COGNITO_CLIENT_SECRET")

# OAuth callback and post-logout URLs come from env so the deployed image
# can point at a real host instead of localhost. Tests stub these via
# monkeypatch.setenv before importing this module.
OAUTH_CALLBACK_URI = os.getenv("OAUTH_CALLBACK_URI", "http://localhost:8000/callback")
OAUTH_POST_LOGOUT_URL = os.getenv("OAUTH_POST_LOGOUT_URL", "http://localhost:8000/chat")

# OAuth client hoisted to module scope so tests can patch
# `web.oauth.oauth.cognito.authorize_redirect` and `.authorize_access_token`.
# Authlib reads server_metadata_url lazily, so constructing here does not
# make a network call at import time.
oauth = OAuth()
oauth.register(
    name="cognito",
    client_id=COGNITO_CLIENT_ID,
    client_secret=COGNITO_CLIENT_SECRET,
    client_kwargs={"scope": "openid email profile"},
    server_metadata_url=COGNITO_WELL_KNOWN_ENDPOINT_URL,
    redirect_uri=OAUTH_CALLBACK_URI,
)


def add_oauth_routes(fastapi_app: FastAPI):
    """Register /login, /callback, /logout on the FastAPI app."""

    @fastapi_app.get("/login")
    async def login(req: Request):
        return await oauth.cognito.authorize_redirect(req, OAUTH_CALLBACK_URI)

    @fastapi_app.get("/callback")
    async def callback(req: Request):
        tokens = await oauth.cognito.authorize_access_token(req)
        access_token = tokens["access_token"]
        username = tokens["userinfo"]["cognito:username"]
        req.session["access_token"] = access_token
        req.session["username"] = username
        return RedirectResponse(url="/chat")

    @fastapi_app.get("/logout")
    async def logout(req: Request):
        req.session.clear()
        logout_url = f"{COGNITO_LOGOUT_URL}&logout_uri={OAUTH_POST_LOGOUT_URL}"
        return RedirectResponse(url=logout_url)
