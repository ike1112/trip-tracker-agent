# E2E Test Runbook — Cost-Free Dry Run

First end-to-end test of the running system. Proves the **chat pipeline**
works against a real deploy with near-zero cost and no third-party keys.

This is **not** the live 7-day price run — that's
[`launch-runbook.md`](./launch-runbook.md), a separate later milestone. Keep
them distinct.

## Scope

**Tests (real, deployed):** Cognito login → API Gateway → agent authorizer →
travel-agent Lambda → Bedrock reasoning → fixture flight/hotel data →
DynamoDB watch CRUD → S3 session memory.

**Stubbed / fixture (not tested here):** poller decision (`bedrockMode=stub`),
SES email (`sesMode=stub`), real Duffel/LiteAPI prices (`mcpMode=fixture`).

**Cost:** fractions of a cent (a few Claude 3.5 Haiku chat turns). Far under
the $10 budget alarm. Nothing recurring; `cdk destroy` ends it.

---

## Prerequisites (only 3 are real — the rest is stubbed)

- [ ] **Bedrock model access** — AWS console → Bedrock → (deploy region) →
      Model access. The chat agent has **no stub mode** — it always calls
      Bedrock, so this is unavoidable even for a dry run. Enable the model
      you deploy the agent with:
      - **Chosen: Claude Sonnet 4.5** — enable foundation model
        `anthropic.claude-sonnet-4-5-20250929-v1:0`; deploy the agent with
        `-c agentBedrockModelId=us.anthropic.claude-sonnet-4-5-20250929-v1:0`
        (the `us.` inference profile; must match the `^us\.` regex in
        `lib/agent.js:58`).
      - Default if the flag is omitted is Claude 3.5 Haiku
        (`lib/agent.js:13`) — then enable that one instead.
- [ ] **`cdk bootstrap`** has been run for this AWS account + region. If
      you've never deployed CDK here, you haven't done this yet.
- [ ] **Docker daemon running** — CDK bundles the Python Lambdas at deploy.
- [ ] `aws sts get-caller-identity` returns the account you intend to use.
- [ ] `npm install` at repo root + per-Lambda installs done
      (`agent-authorizer`, `mcp-authorizer`, `flights-mcp`, `hotels-mcp`).

---

## Step 1 — Deploy (cost-free path)

```
aws sts get-caller-identity            # confirm correct account
cdk bootstrap                          # only if never done for this acct/region
cdk deploy -c bedrockMode=stub -c sesMode=stub \
           -c notifierSenderEmail=YOUR_EMAIL@example.com \
           -c notifierRecipientEmail=YOUR_EMAIL@example.com
```

- These **four context flags are all required even for the cost-free dry
  run**, and each is enforced at **synth** (fails before bootstrap/Docker):
  - `bedrockMode=stub` — stubs the poller decision call.
  - `sesMode=stub` — no email is ever *sent*.
  - `notifierSenderEmail` + `notifierRecipientEmail` — `notifier-server.js:54`
    validates **both** before it checks `sesMode`, so stub mode does **not**
    exempt them. In stub mode SES never sends, so these need not be
    SES-verified addresses — any well-formed email works. Use your real
    address anyway.
  - `budgetAlarmEmail` is **not** needed separately — `budget-alarm.js:62`
    falls back to `notifierRecipientEmail`.
- `mcpMode` defaults to `fixture` (no Duffel/LiteAPI keys needed).
- Review the IAM diff CDK prints, then approve.
- [ ] Deploy succeeded. Note the stack outputs (API URL, Cognito IDs).

**If it fails, expected first-deploy culprits (in order they surface):**
1. `notifierSenderEmail`/`notifierRecipientEmail`/`budgetAlarmEmail ... is
   required` at **synth** → an email `-c` flag is missing. The command above
   has the complete set; this was verified green on 2026-05-17.
2. `cdk bootstrap` not run → run it, redeploy.
3. Docker not running → start Docker, redeploy.
4. Bedrock `AccessDeniedException` at first chat → model access not enabled
   for `anthropic.claude-3-5-haiku-20241022-v1:0` in the deploy region.
5. Region has no Bedrock / model → redeploy to a Bedrock region (e.g.
   `us-east-1` / `us-west-2`).

---

## Step 2 — Start the local web app

The web UI is a local Flask app pointing at the deployed backend.

```
./prep-web.sh                          # sets Alice/Bob passwords, writes web/.env
cd web
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py                          # http://localhost:8000/chat/
```

- [ ] `http://localhost:8000/chat/` loads (redirects to the Cognito login).
- [ ] You have the **Alice** password (in `web/.env` or `prep-web.sh`
      output — generated locally, never leaves your machine).

---

## Step 3 — Drive the chat (the actual e2e test)

Log in as `Alice`, then run these five design-spec §4 patterns **in order**.
The "Expect" column is your manual pass/fail check — it's also exactly what
the assisting browser agent verifies.

| # | You type | Expect (pass criteria) |
|---|----------|------------------------|
| 1 | `Watch Tokyo in October, 5 nights from SFO, max $1500 total` | Agent asks for any missing field **one at a time**, echoes the **full structured watch back in plain English**, saves **only after you confirm**. No silent defaults. |
| 2 | `confirm` (or your confirmation) | Confirms saved + a headline price. A new row appears in the **Watches** DynamoDB table (`status=active`, your Cognito sub as `userId`). |
| 3 | `what's happening with my watches?` | **One headline line per watch** (destination, current total, trend), not raw rows. Details only on request. |
| 4 | `how much is Tokyo right now?` | A headline number + qualitative read, sourced from **fixture** flight+hotel data (proves the MCP path works without real keys). Offers to make a watch. |
| 5 | `tighten Tokyo to weekends only` | Patches the existing watch (same confirm-before-save rule). `updatedAt` changes; `status` is **never dropped**. |

Behaviors that are **bugs** if you see them:
- Watch saved without an explicit confirm, or fields silently invented.
- Status check dumps raw JSON / multiple lines per watch.
- Live search returns an error or empty (fixture data should always resolve).
- A refine that drops `status` or creates a duplicate watch.
- Any 500 / blank screen / agent says it called a tool but DynamoDB is unchanged.

---

## Step 4 — Verify the writes (independent of the chat)

- [ ] DynamoDB `Watches` table: one row per created watch, correct `userId`,
      `status=active`, fields match what you confirmed.
- [ ] After pattern 5: same `watchId`, fields updated, `status` still present.
- [ ] CloudWatch logs for the travel-agent Lambda show structured JSON lines
      (tool calls, no stack traces).

---

## Step 5 — Tear down

- [ ] `cdk destroy` when done. Nothing recurs; cost stops.

---

## Findings from the first real run (2026-05-17)

Real e2e immediately surfaced 5 defects that no unit/fixture test caught.
All hurt the "a reviewer forks it and it works" objective.

1. **Cost-free deploy command was incomplete.** `bedrockMode=stub
   sesMode=stub` alone fails synth — `notifier-server.js:54` requires
   `notifierSenderEmail` + `notifierRecipientEmail` before it checks
   `sesMode`. Fixed in this doc. **`README.md:137` still has the broken
   2-flag command** — a forking reviewer hits it on their first command.
2. **`prep-web.sh` is broken.** It queries stack outputs by
   `ExportName=='Cognito...'`, but the CDK stack sets no export names, so
   every `web/.env` value comes back empty and the Cognito
   `admin-set-user-password` call fails with "expected one argument."
   Worked around by writing `web/.env` from `describe-stacks` OutputKeys +
   `describe-user-pool-client` for the secret.
3. **`web/requirements.txt` is missing `requests`.** Gradio 5.33.2 imports
   it; the web app crashes on `import gradio` with `ModuleNotFoundError:
   No module named 'requests'`. Pin `requests` in `web/requirements.txt`.
4. **Stale upstream branding.** The deployed chat UI greets "Welcome to
   AcmeCorp Travel Agent — book your next business trip." The product is a
   trip-price tracker. The web UI copy was never updated from the scaffold;
   it contradicts every doc a reviewer reads.
5. **agent-authorizer ships with no dependencies → every authed request
   500s.** `lib/agent.js:224` uses `Code.fromAsset('./lambdas/agent-authorizer')`
   with **no bundling step**, and `lambdas/agent-authorizer/node_modules`
   is absent (the other 3 authorizer/MCP dirs have theirs, so only this
   path breaks). The authorizer crashes at init with `Cannot find package
   'jsonwebtoken'`; API Gateway returns 500; the agent Lambda is never
   reached. `cdk deploy` succeeds anyway — no build-time guard. Fix:
   `cd lambdas/agent-authorizer && npm install` then redeploy, OR add a
   `bundling`/`npm ci` step to the construct so a fresh clone can't ship
   a broken auth layer. The README's per-Lambda `npm install` step is
   load-bearing and silently fatal if skipped.

**Status after fixes:** #1 fixed in this doc (README still broken). #2/#3/#4
documented, not yet fixed (one-line each, don't block the chat path). #5
**fixed** (`npm install` in `lambdas/agent-authorizer`, redeployed — authed
requests now reach the agent Lambda).

6. **MCP client/server transport mismatch → 502 on every tool call
   (CURRENT BLOCKER).** The flights/hotels MCP servers implement a custom
   one-shot `LambdaTransport` (`lambdas/flights-mcp/index.js:139-173`:
   fresh server+transport per invocation, `dispatch(rpc)` then `close()`,
   stateless). The agent's MCP **client** uses the streamable-HTTP
   transport (`/opt/python/mcp/client/streamable_http.py`). The `initialize`
   call succeeds (one request/response — `flights-mcp-server` logs a clean
   200). The streamable-HTTP client's **next** step hits the Lambda, the
   handler returns/exits without settling its promise →
   `Runtime.NodeJsExit: a Promise that was never settled` (25ms, no app
   log) → API Gateway 502 → agent surfaces "Failed to initialize MCP
   Client." Affects flights AND hotels identically. Every unit/fixture
   test passes because they call the handler in-process and `dispatch()`
   returns fine — only real e2e through API Gateway with the actual
   streamable-HTTP client exposes it. This is an architecture fix
   (align client and server transports), not a config change.

**#5 + #6 fixed and verified.** #5: `npm install` in `lambdas/agent-authorizer`,
redeployed — authed requests reach the agent. #6: notification ack added to
both MCP servers (`flights-mcp/index.js`, `hotels-mcp/index.js`), 43/43 tests
green, redeployed — agent logs now show `mcp_connect` ×2, no 502, no
`NodeJsExit`. The failure moved cleanly past MCP.

7. **Misleading catch-all error (repo bug).** `lambdas/travel-agent` reports
   `"Failed to initialize MCP Client, see logs"` for **any** exception in a
   turn — including a Bedrock `AccessDeniedException` that has nothing to do
   with MCP (`handler:86`). This cost real debugging time chasing MCP when
   the fault was Bedrock. Fix: surface the actual exception class/message,
   or at least don't hard-code an MCP-specific string for a generic catch.

8. **Current blocker — Bedrock model access not enabled (NOT a repo bug,
   account action).** Agent `ConverseStream` →
   `AccessDeniedException: Model access is denied ... AWS Marketplace
   actions (aws-marketplace:ViewSubscriptions...)`. This is the documented
   Phase-0 prerequisite: the Anthropic models are not enabled in the
   Bedrock console for this account/region. Only the account owner can
   grant it (console "Model access" page). Code path is otherwise clear
   end to end up to the model call.

**#8 resolved by you** (Bedrock model access granted) + agent moved to
**Claude Sonnet 4.5** (`-c agentBedrockModelId=us.anthropic.claude-sonnet-4-5-20250929-v1:0`).

9. **✅ Pattern 1 PASS (first real end-to-end conversation).** On Sonnet
   4.5 the agent echoed the structured watch back in plain English, asked
   to confirm before saving, and explicitly surfaced its inferred
   assumptions (Oct 1–31 window, pax=1) instead of silently defaulting —
   exactly design-spec §4 behavior. Full chain proven: Cognito → API GW →
   authorizer → agent → Sonnet 4.5 → MCP connect.

10. **Stack recreate on context change wipes Cognito/endpoints (operational
    hazard).** A `cdk deploy` cycle came back `CREATE_COMPLETE` (full
    recreate, not `UPDATE_COMPLETE`): new Cognito pool ID, client id/secret,
    and API endpoints. This silently invalidated the running web app,
    `web/.env`, and all logged-in sessions. After any recreate you must
    re-run password set + rewrite `web/.env` + restart the web app + fresh
    login. Likely trigger: a prior failed/rolled-back deploy forcing
    delete+recreate. Worth a guard/runbook note before the live run.

11. **create_watch / update_watch wrote float `maxTotalPrice` to DynamoDB
    → watch creation fails (repo bug, fixed).** `watches.py` put the LLM's
    float price straight into `put_item`/`update_item`; boto3 rejects
    floats. The agent surfaced it only as a vague "issue with how the
    price is being processed." Fixed with a recursive `_decimalize()`
    (`Decimal(str(x))`, the project's own convention used by
    poller/notifier) at both write points. **Test-quality gap:** the
    18 travel-agent tests passed *with the bug present* — moto tolerates
    floats and no test asserts the persisted type is Decimal. Add a test
    that asserts no float reaches the table.

> Diagnostic note (environment, not a repo bug): on Git Bash for Windows,
> `aws logs` calls mangle `/aws/lambda/...` via MSYS path conversion.
> Prefix with `MSYS_NO_PATHCONV=1` or use `filter-log-events`.

## Pass/fail

**Pass:** all 5 patterns behave per the Expect column, DynamoDB reflects the
chat, no 500s/silent failures. The interactive system is proven e2e.

**Any divergence:** note the pattern #, what you typed, what happened vs
Expect, and the Lambda log line. That's a real bug found by real e2e — triage
it before the live runbook.

> To run the chat test yourself in the browser with the exact expected
> responses at each step, follow [`e2e-walkthrough.md`](./e2e-walkthrough.md).

## RESULT — first full e2e run (2026-05-18, Claude Sonnet 4.5, stub/fixture)

**ALL 5 PATTERNS PASS**, verified at both the chat layer and DynamoDB:

| # | Pattern | Result |
|---|---------|--------|
| 1 | Create watch | PASS — structured echo, confirm-before-save, surfaced inferred Oct 1–31 / pax=1 instead of silent defaults |
| 2 | Confirm | PASS — "Watch created"; DDB row written (`maxTotalPrice=1500` numeric, status=active, correct userId) |
| 3 | Status | PASS — one headline line/watch ("Tokyo, Oct 1–31… just created—no price data yet"), not raw rows |
| 4 | Live search | PASS — MCP fixture tools returned offers (United $1,148 + Shibuya 3★ $485 = $1,633), headline + qualitative read. Proves the MCP transport fix end to end |
| 5 | Refine | PASS — asked to confirm, then patched; DDB shows `preferences.departDaysOfWeek=[5,6]`, `status` preserved, `updatedAt` advanced |

The system works end to end. It took 5 code fixes (authorizer deps; MCP
notification ack ×2; Decimal coercion in create/update) + 1 account action
(Bedrock access) + the model set to Sonnet 4.5 to get here from a system
that had never once been run.

**Still open for the portfolio "forking reviewer" goal (none block the run,
all hurt first impressions):** #1 README command, #2 `prep-web.sh`, #3
`web/requirements.txt`, #4 stale "AcmeCorp" branding, #7 misleading
catch-all error, #10 recreate-rotates-Cognito guard, #11 test-quality gap
(add a Decimal-assertion test). The 5 code fixes from this run are now
committed to git.

---

## Scheduled-path verification (2026-05-20, fixture + SES stub)

The 2026-05-18 RESULT above proves the **chat path**. It does not exercise
the **scheduled path** — the EventBridge poller → snapshot → Bedrock
decision → SES notifier → dedup writeback. That path has no LLM in the loop
to paper over data-shape mismatches, so it needs its own verification.
Exercising it on 2026-05-20 surfaced two more silent bugs the chat run
could not have caught.

### Finding #12 — poller flight search passed a city name, not an IATA code

`lambdas/poller/app.py` passed `watch["destination"]` (a city, e.g.
"Tokyo") into flights-mcp, which keys both its fixture replay and its live
Duffel queries on IATA airport codes ("NRT"). Every poll's flight lookup
missed; `FareHistory` stayed empty. The chat path masked it because the LLM
resolves the airport inside its tool call — the poller has no LLM and
ships whatever the row stored. Fix (`55c0626`): split the stored field into
`destination` (city — hotel search + alert prose) and `destinationAirport`
(IATA — flight search); the chat agent extracts both at watch-creation
time.

### Finding #13 — NULL `lastAlertedAt` permanently disarmed the dedup gate

`create_watch` initialized the row with `lastAlertedAt: None,
lastAlertedPrice: None`. boto3's DynamoDB resource marshals Python `None`
into a DDB `NULL`-typed attribute — present, not absent. The notifier's
writeback condition `attribute_not_exists(lastAlertedAt) OR lastAlertedAt <
:now` therefore evaluated false on both clauses, so the first writeback for
every watch failed with `ConditionalCheckFailedException`. The handler logs
that as `writeback_conflict` and returns 200 (SES already sent), so it
never surfaced as an error code — only as a silently broken anti-spam
guarantee: alerts would re-fire on every tick. The notifier's own tests
missed it because `make_watch` in `notifier/tests/conftest.py` already
omitted the keys when callers passed `None`, working around the production
bug for test purposes. Fix (`479c54c`): omit the keys from the initial put
(the notifier writes them on the first real alert) and harden the writer
condition with an explicit `OR lastAlertedAt = :null` branch for legacy
rows.

### Step-by-step — verifying the scheduled path

Prerequisites: the stack deployed cost-free (Step 1 above), and at least
one **active** watch whose `origin` / `destinationAirport` / `destination`
/ `dateWindow.earliestDepart` match a bundled fixture pair. Fixtures live
at `lambdas/{flights,hotels}-mcp/fixtures/`, keyed
`{origin}-{IATA}-{departDate}.json` for flights and `{city}-{checkin}.json`
for hotels.

1. **Snapshot-write check.** Use a watch matching `SFO-NRT-2026-10-15.json`
   + `Tokyo-2026-10-15.json` (origin SFO, destinationAirport NRT,
   destination Tokyo, earliestDepart 2026-10-15). Invoke the poller:
   ```
   aws lambda invoke --function-name trip-tracker-poller \
     --payload '{}' --cli-binary-format raw-in-base64-out poll.json
   ```
   Poller logs should show `snapshot_written` with flight/hotel/total
   prices; `FareHistory` should gain one row per matching watch.

2. **Alert + writeback check.** Use a watch whose fixture total is **under
   budget** so the threshold gate fires — e.g. the `LHR-CDG-2026-12-20`
   pair (flight 142.30 + hotel 410.00 = 552.30) with `maxTotalPrice` 800.
   Invoke the poller again. Expect `decision_made alert=true`, the notifier
   invoked async, and notifier logs showing `notification_sent` (**not**
   `writeback_conflict`). The watch's `Watches` row should now carry a real
   `lastAlertedAt` ISO timestamp and `lastAlertedPrice`.

3. **Dedup-gate check.** Wait ~10s for the `status-index` GSI to propagate
   (ADR 0007 — eventually consistent), then invoke the poller once more.
   The just-alerted watch should now log `decision_made alert=false
   reason=dedup_blocked` — the gate is armed.

### RESULT — scheduled-path run (2026-05-20, fixture + SES stub)

| Check | Result |
|---|---|
| Snapshot write | PASS — Tokyo watch (NRT): `snapshot_written flight=1148 hotel=485 total=1633`; `decision_made alert=false` (over the $1500 budget, no history) |
| Alert + writeback | PASS — Paris watch (CDG, budget 800): `decision_made alert=true`, notifier invoked; writeback accepted on a legacy NULL row → `lastAlertedAt=2026-05-20T19:17:52.664673+00:00`, `lastAlertedPrice=552.3` |
| Dedup gate | PASS — after GSI propagation, both Paris and Tokyo log `decision_made alert=false reason=dedup_blocked` |
| Unit suite | PASS — 459 green (208 poller + 18 watches + 127 notifier + 106 evals) |

**Scope caveats — what this run did *not* prove:**

- **Fixture + SES stub only.** Live Duffel / LiteAPI / SES is untested;
  that is the live run in `launch-runbook.md`.
- **The Bedrock decision step was stubbed** (`bedrockMode=stub`). The alert
  in check #2 fired with `reason=stub` — the snapshot → decision →
  notifier → writeback → dedup *plumbing* is proven, but the real Bedrock
  decision call in that path is not.
- **The threshold and anomaly gates were not exercised against accrued
  history** (`history=0` on every run). The dedup gate is proven; the
  30-day anomaly window is not.
