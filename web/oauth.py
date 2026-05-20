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

    # -----------------------------------------------------------------------
    # Route: /login
    # Problem solved: the user is not authenticated and needs to prove identity.
    # Solution: generate a Cognito-hosted login URL (including a random `state`
    # parameter to prevent CSRF) and redirect the browser there. The user
    # enters credentials on Cognito's page — never on our server.
    # -----------------------------------------------------------------------
    @fastapi_app.get("/login")
    async def login(req: Request):
        return await oauth.cognito.authorize_redirect(req, OAUTH_CALLBACK_URI)

    # -----------------------------------------------------------------------
    # Route: /callback
    # Problem solved: after the user logs in, Cognito redirects back here with
    # a one-time authorization code. This route exchanges that code for tokens
    # (access + ID) using the client secret, then stores what it needs in the
    # server-side session so subsequent requests don't require re-authentication.
    #
    # Why store the access_token? The chat() function in app.py forwards it as
    # a Bearer token when calling the agent Lambda, which validates it against
    # Cognito's JWKS to confirm the caller's identity.
    # -----------------------------------------------------------------------
    @fastapi_app.get("/callback")
    async def callback(req: Request):
        # Exchange the authorization code for tokens; Authlib also validates
        # the ID token signature and nonce to prevent replay attacks.
        tokens = await oauth.cognito.authorize_access_token(req)
        access_token = tokens["access_token"]
        # cognito:username is the Cognito-specific claim for the user's login name
        username = tokens["userinfo"]["cognito:username"]
        # Persist both values in the signed session cookie (see app.py for
        # how SessionMiddleware protects this cookie from tampering).
        req.session["access_token"] = access_token
        req.session["username"] = username
        return RedirectResponse(url="/chat")

    # -----------------------------------------------------------------------
    # Route: /logout
    # Problem solved: clearing the local session is not enough — the user
    # would still have an active SSO session with Cognito and could return
    # to /login and be logged straight back in without entering credentials.
    # Solution: clear the local session AND redirect to Cognito's logout
    # endpoint so both sessions are terminated together.
    # -----------------------------------------------------------------------
    @fastapi_app.get("/logout")
    async def logout(req: Request):
        # Invalidate the local session (removes access_token and username
        # from the signed cookie, so check_auth will redirect to /login
        # on next visit)
        req.session.clear()
        # Build the Cognito logout URL with the post-logout redirect URI so
        # Cognito knows where to send the browser after it ends the SSO session.
        logout_url = f"{COGNITO_LOGOUT_URL}&logout_uri={OAUTH_POST_LOGOUT_URL}"
        return RedirectResponse(url=logout_url)
