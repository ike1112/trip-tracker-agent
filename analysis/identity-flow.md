# Identity Flow — End to End

Concrete walk-through of how a request from Alice's browser ends up calling an MCP tool, with every authentication and authorization step in the path. File and line references were captured against the code at the time of writing and may drift as files evolve.

## Diagram

Sequence diagram of the full flow (Excalidraw, partial header upload — full diagram source is at the bottom of this file):

https://excalidraw.com/#json=msXmJWi9SmkqfjvlbEu8r,4ecn3k1aeLW_y6Zly3DtdA

There are **three distinct identity hops**, each with different crypto. Once you see this, the rest of the code makes sense.

---

## 0. Identities created at deploy time (one-shot)

`lib/cognito.js`:
- Creates a **Cognito User Pool** with `selfSignUpEnabled: false` (only admins create users) — line 13.
- Creates a **User Pool Client** with the OIDC `authorizationCodeGrant` flow, scopes `openid email profile`, callback `http://localhost:8000/callback`, 8-hour token validity — lines 18–33.
- Creates two `CfnUserPoolUser` resources for **Alice** and **Bob**, with `messageAction: 'SUPPRESS'` so Cognito doesn't email them temp passwords — lines 42–52.
- Provisions a Cognito-hosted login domain `strands-on-lambda-<random>` — line 36.

Passwords for Alice/Bob are set **out-of-band** after deploy by `prep-web.sh` calling `aws cognito-idp admin-set-user-password ... --permanent`. So the IaC creates accounts, but creds are stamped in via admin API. This is the standard "don't put passwords in CloudFormation" pattern.

## 1. Browser → Web app → Cognito (OIDC Authorization Code grant)

The web app at `web/app.py` mounts Gradio at `/chat` behind a `check_auth` dependency (line 83). If there's no session cookie, it 302s to `/login` (line 24).

The OIDC dance (`web/oauth.py`):
- `/login` → `oauth.cognito.authorize_redirect(...)` builds the Cognito hosted-UI URL with `client_id`, `redirect_uri`, `state` (lines 25–27). User types Alice / `Passw0rd@` on Cognito's domain — **the password never touches the web app**.
- Cognito redirects browser to `/callback?code=<auth_code>&state=...`.
- `/callback` → `authorize_access_token(...)` does the back-channel POST to Cognito's token endpoint with the `code` + `client_secret` and gets back `id_token` / `access_token` / `userinfo` (lines 30–38).
- The web app stores `access_token` and `username` in the session cookie managed by Starlette `SessionMiddleware` (`app.py`). In this setup the cookie is **signed** (integrity protected), not encrypted (confidentiality protected). With `secret_key="secret"` hardcoded, this is suitable for demo only and should be replaced in real deployments.

Notable quirks of *this* implementation:
- The web app puts `access_token` (not `id_token`) into the session and forwards that as the bearer to the Agent API (`app.py:33,39`). Both work for Cognito — the access_token is also a JWT — but most OIDC tutorials forward the `id_token` since that's the one with user claims for downstream consumption.
- The `cognito:username` claim is read from `userinfo`, not from the JWT directly (`oauth.py:34`).

## 2. Web app → Agent API Gateway → Agent Authorizer (RS256 / JWKS)

`web/app.py:37-42` POSTs to `AGENT_ENDPOINT_URL` with `Authorization: Bearer <access_token>` and a JSON body `{"text": message}`.

API Gateway has a **TokenAuthorizer** in front of the agent route (`lib/agent.js:78–86`) that runs `lambdas/agent-authorizer/index.js`:
- Splits `"Bearer <jwt>"`, then verifies the JWT (line 26).
- Verification is **RS256**: it fetches Cognito's public keys from `COGNITO_JWKS_URL` (the `.well-known/jwks.json` endpoint), picks the key whose `kid` matches the JWT header, and verifies the signature (lines 6–18, 26).
- On success it returns an IAM policy `Effect=Allow` with `principalId = "<sub>|<username>"` (lines 28–29). API Gateway caches that decision and lets the request through.

**Pattern name:** *external-IdP token verification via JWKS rotation* — the authorizer trusts whoever holds Cognito's signing key, and Cognito is free to rotate keys without redeploying anything.

## 3. Agent Lambda parses the JWT *again*

`lambdas/travel-agent/app.py:14–33` re-parses the same JWT to extract `sub` and `username` and builds a `User(id=..., name=...)` object. The authorizer already validated the signature; the agent re-parses because the API Gateway authorizer's `principalId` only carries a string, not the full claims, and the agent needs the raw `sub`/`username` for downstream calls.

It builds a `composite_prompt` mixing `User name`, `User IP` (from API Gateway's `requestContext.identity.sourceIp`), and the user's text — that's how the LLM "knows" who it's talking to without inferring it from the conversation.

## 4. Agent → MCP API Gateway (HS256 / shared-secret token exchange)

This is the second identity hop. The agent does **not** forward the user's Cognito token to MCP. Instead it **mints a brand-new JWT** signed with a shared secret (`lambdas/travel-agent/mcp_client_manager.py:23–33`):

```python
token = jwt.encode({
    "sub":"travel-agent",          # the agent IS the principal
    "user_id": user.id,            # human user is a *claim*, not the principal
    "user_name": user.name,
}, jwt_signature_secret, algorithm="HS256")
```

Two design points worth understanding:
1. **`sub` is the agent itself**, not the user. The MCP authorizer enforces `claims.sub === 'travel-agent'` (`lambdas/mcp-authorizer/index.js:16-19`); any other principal is denied. So the MCP server only accepts calls from the travel-agent service.
2. The user is propagated as **side claims** (`user_id`, `user_name`). This is the "**at no point in time user identity is inferred from LLM's response**" guarantee the README brags about — the user identity rides on a token the LLM cannot see or fabricate.

Why HS256 instead of RS256 like the Cognito hop? Because both ends are owned by *us* — there's no third-party IdP. A shared symmetric secret is fine and cheaper. In current code this is implemented with per-component Secrets Manager secrets passed by ARN to the Lambdas.

## 5. MCP API Gateway → MCP Authorizer → MCP Server

Each MCP API Gateway uses a TokenAuthorizer (`lib/flights-mcp-server.js` and `lib/hotels-mcp-server.js`), running `lambdas/mcp-authorizer/index.js`. It verifies the JWT against the per-component signing secret set and enforces allowed `sub` values.

Then `lambdas/flights-mcp/index.js` and `lambdas/hotels-mcp/index.js` run a *second* validation layer at the application boundary — they re-verify the JWT in-handler before dispatching to MCP tools. Two layers of validation is overkill but harmless; it means either MCP server can run behind a different upstream boundary without changing auth semantics.

Both MCP Lambdas use direct handlers (no Express/LWA): API Gateway events are parsed and dispatched through in-memory MCP transport.

## 6. User context flows into each tool

The MCP handlers pass verified claims through tool context (`ctx.authInfo`) so tools can enforce user-scoped behavior without trusting LLM text.

```javascript
async ({ city, date, nights }, ctx) => {
    const userName = ctx.authInfo.user_name;   // the user identity
    return { content: [...`Booked hotel in ${city} for ${userName}...`] }
}
```

That is the endpoint of the chain — every tool can know *who* it is working for, without trusting anything the LLM said.

---

## IAM permissions configured

Beyond the identity tokens above, the AWS-side IAM is small:

| Resource | Permissions |
|---|---|
| Agent Lambda role | `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` on `*` (`lib/agent.js:52-55`); read/write to session S3 bucket via `grantReadWrite` (line 57); basic Lambda exec role (CW Logs) |
| Agent authorizer role | Basic Lambda exec role only — no AWS calls needed (JWKS is public HTTPS) |
| MCP server Lambda role | Basic Lambda exec role only — no AWS calls |
| MCP authorizer role | Basic Lambda exec role only |
| API Gateway → Lambda | Auto-generated `lambda:InvokeFunction` permission for both Lambdas + their authorizers |
| Cognito User Pool Client | OAuth flows: `authorizationCodeGrant` only; scopes `openid email profile` (`lib/cognito.js:21-30`) |

---

## Patterns to take away

1. **OIDC Authorization Code grant** for human login (web app never sees the password).
2. **Bearer token forwarding** from web app to API.
3. **JWKS-based RS256 verification** for tokens issued by an external IdP (Cognito).
4. **API Gateway TokenAuthorizer** as a stateless, cacheable per-request gate.
5. **Token exchange** at trust boundaries: agent mints a *new* JWT with itself as `sub` and the user as a claim, instead of forwarding the user's IdP token.
6. **HS256 + shared secret** is fine when both endpoints are in the same trust domain and you control the rotation.
7. **Identity as side-channel claim**, not LLM input — the agent can't lie about who the user is because the JWT carries the truth out-of-band.

## Quirks worth flagging

- Secret material is in Secrets Manager and loaded lazily in verifier/minter paths; no hard-coded signing literal remains in stack code.
- Cognito client secret is exported as a CFN output in plaintext (`lib/cognito.js:88-93`) — file even self-comments this is for brevity.
- Web session middleware uses `secret_key="secret"` — fine for a demo, would be a vuln in real life.
- Both Lambda authorizers are created without an explicit cache TTL override, so API Gateway default authorizer caching behavior applies (commonly 5 minutes). Practical effect: a logged-out token may continue to pass for a short window after revocation.
- Two layers of JWT validation on the MCP side (API authorizer + in-handler verifier) — defensive but redundant.

---

## Sequence summary (text version of the diagram)

```
Phase 1 — Login (OIDC Authorization Code Grant)
  1. Browser → Web App        : GET /chat
  2. Web App → Browser        : 302 → /login
  3. Browser → Cognito        : hosted UI (user enters credentials)
  4. Cognito → Browser        : 302 /callback?code=...
  5. Browser → Web App        : GET /callback?code=...
  6. Web App → Cognito        : POST /token (back-channel, with client_secret)
  7. Cognito → Web App        : id_token + access_token
  8. Web App → Browser        : Set-Cookie (session) + 302 /chat

Phase 2 — Chat (RS256 verify + Token Exchange)
  9.  Browser → Web App       : user types prompt
  10. Web App → Agent API     : POST   Bearer <Cognito access_token>
                                  (Agent authorizer verifies RS256 via JWKS)
  11. Agent  → MCP API        : POST /mcp   Bearer <NEW agent JWT>
                                  (TOKEN EXCHANGE: sub=travel-agent,
                                   user_id/user_name as CLAIMS)
                                  (MCP authorizer verifies HS256 + runs tool)
  12. MCP   → Agent           : tool result
  13. Agent → Web App         : agent reply (rendered in chat)
```

---

## Diagram source

The diagram source is in `analysis/identity-flow.excalidraw.json` in **Claude-Excalidraw simplified format** (uses `label` on shapes, which Claude expands into bound text elements at render time). It is *not* a native `.excalidraw` file, so excalidraw.com will not load it directly.

To re-render visually any time:
1. Open this analysis folder in a Claude session.
2. Ask Claude: *"render the elements from `analysis/identity-flow.excalidraw.json` with Excalidraw"*. Claude will use the MCP tool to draw it in the chat.

If you want a native `.excalidraw` file (loadable in excalidraw.com), ask Claude to convert the source — every labeled shape gets a sibling text element with `containerId` binding, and a few standard fields (`version`, `seed`, `versionNonce`, etc.) need to be added per element.
