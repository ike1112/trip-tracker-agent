# This module solves the problem of authenticating users before they can access
# the travel-agent chat UI. Rather than building a custom login system, it
# delegates authentication entirely to Amazon Cognito using the standard
# OAuth 2.0 Authorization Code flow. The browser never handles passwords directly;
# Cognito issues short-lived tokens that the app uses to verify identity and
# authorise calls to the agent Lambda.

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth  # Authlib handles the OAuth handshake complexity
import os


def add_oauth_routes(fastapi_app: FastAPI):
    """
    Registers three routes on the FastAPI app that together implement the
    OAuth 2.0 Authorization Code flow with Cognito:

        /login     → redirects the browser to Cognito's hosted login page
        /callback  → receives the authorization code from Cognito, exchanges it
                     for tokens, and stores them in the session
        /logout    → clears the local session and redirects to Cognito's logout
                     endpoint so the Cognito SSO session is also terminated

    Separating these routes into their own module keeps auth concerns out of
    app.py and makes it easy to swap Cognito for another provider later.
    """

    # -----------------------------------------------------------------------
    # Configuration — all values come from environment variables so that no
    # secrets are hard-coded and the same image works in every environment.
    # -----------------------------------------------------------------------
    COGNITO_SIGNIN_URL = os.getenv("COGNITO_SIGNIN_URL")           # Cognito hosted-UI base URL (used for logout redirect)
    COGNITO_LOGOUT_URL = os.getenv("COGNITO_LOGOUT_URL")           # Cognito logout endpoint URL
    COGNITO_WELL_KNOWN_ENDPOINT_URL = os.getenv("COGNITO_WELL_KNOWN_URL")  # OIDC discovery document; Authlib reads issuer, token endpoint, etc. from here
    COGNITO_CLIENT_ID = os.getenv("COGNITO_CLIENT_ID")             # Identifies this app to Cognito
    COGNITO_CLIENT_SECRET = os.getenv("COGNITO_CLIENT_SECRET")     # Proves this app's identity when exchanging codes for tokens (kept server-side only)

    # Where Cognito should redirect the browser after a successful login.
    # Must exactly match the "Allowed callback URL" configured in the Cognito app client.
    OAUTH_CALLBACK_URI = "http://localhost:8000/callback"

    # Where the user lands after logging out — the chat page, which will
    # redirect back to /login if no valid session exists.
    REDIRECT_AFTER_LOGOUT_URL = "http://localhost:8000/chat"

    # -----------------------------------------------------------------------
    # OAuth client setup
    # Authlib's OAuth helper manages state parameters, PKCE, token validation,
    # and the OIDC discovery document fetch automatically, which prevents
    # common implementation mistakes in the handshake.
    # -----------------------------------------------------------------------
    oauth = OAuth()
    oauth.register(
        name="cognito",
        client_id=COGNITO_CLIENT_ID,
        client_secret=COGNITO_CLIENT_SECRET,
        # Request these OIDC scopes so the token response includes a userinfo
        # object with the username and email we need.
        client_kwargs={"scope": "openid email profile"},
        # Authlib fetches this URL at startup to discover Cognito's token,
        # authorisation, and JWKS endpoints — avoids hard-coding them.
        server_metadata_url=COGNITO_WELL_KNOWN_ENDPOINT_URL,
        redirect_uri=OAUTH_CALLBACK_URI,
    )

    # -----------------------------------------------------------------------
    # Route: /login
    # Problem solved: the user is not authenticated and needs to prove identity.
    # Solution: generate a Cognito-hosted login URL (including a random `state`
    # parameter to prevent CSRF) and redirect the browser there.  The user
    # enters credentials on Cognito's page — never on our server.
    # -----------------------------------------------------------------------
    @fastapi_app.get("/login")
    async def login(req: Request):
        return await oauth.cognito.authorize_redirect(req, OAUTH_CALLBACK_URI)

    # -----------------------------------------------------------------------
    # Route: /callback
    # Problem solved: after the user logs in, Cognito redirects back here with
    # a one-time authorization code.  This route exchanges that code for tokens
    # (access + ID) using the client secret, then stores what it needs in the
    # server-side session so subsequent requests don't require re-authentication.
    #
    # Why store the access_token?  The chat() function in app.py forwards it as
    # a Bearer token when calling the agent Lambda, which validates it against
    # Cognito's JWKS to confirm the caller's identity.
    # -----------------------------------------------------------------------
    @fastapi_app.get("/callback")
    async def callback(req: Request):
        # Exchange the authorization code for tokens; Authlib also validates
        # the ID token signature and nonce to prevent replay attacks.
        tokens = await oauth.cognito.authorize_access_token(req)
        print(tokens)

        access_token = tokens["access_token"]
        # cognito:username is the Cognito-specific claim for the user's login name
        username = tokens["userinfo"]["cognito:username"]

        # Persist both values in the signed session cookie (see app.py for
        # how SessionMiddleware protects this cookie from tampering).
        req.session["access_token"] = access_token
        req.session["username"] = username

        print(f"username={username} access_token={access_token}")
        # Send the now-authenticated user to the chat UI
        return RedirectResponse(url="/chat")

    # -----------------------------------------------------------------------
    # Route: /logout
    # Problem solved: simply clearing the local session is not enough — the
    # user would still have an active SSO session with Cognito and could return
    # to /login and be logged straight back in without entering credentials.
    # Solution: clear the local session AND redirect to Cognito's logout
    # endpoint so both sessions are terminated together.
    # -----------------------------------------------------------------------
    @fastapi_app.get("/logout")
    async def logout(req: Request):
        # Invalidate the local session (removes access_token and username from
        # the signed cookie, so check_auth will redirect to /login on next visit)
        req.session.clear()

        # Build the Cognito logout URL with the post-logout redirect URI so
        # Cognito knows where to send the browser after it ends the SSO session.
        logout_url = f"{COGNITO_LOGOUT_URL}&logout_uri={REDIRECT_AFTER_LOGOUT_URL}"
        return RedirectResponse(url=logout_url)

