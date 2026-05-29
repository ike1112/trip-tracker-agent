# Dry Run — Fixture-Mode Chat Walkthrough

The first end-to-end exercise of the deployed system. **No travel-provider
keys, fixture MCP data, and only light Bedrock chat cost.** Drives the chat in
the browser and shows the verbatim agent response from the 2026-05-18 run
(Claude Sonnet 4.5, stub/fixture deploy) so you know what a correct
response looks like.

> The agent is an LLM. Your wording will differ from the captured text —
> that is expected. What must match is the **behavior** in the "Must do"
> line under each step, not the prose.

This is **not** the live 7-day price run — that is
[`live-launch.md`](./live-launch.md), a separate later milestone.

---

## Scope

**Tests (real, deployed):** Cognito login → API Gateway → agent authorizer →
travel-agent Lambda → Bedrock reasoning → fixture flight/hotel data →
DynamoDB watch CRUD → S3 session memory.

**Stubbed / fixture (not tested here):** poller decision (`bedrockMode=stub`),
scheduled SES alert delivery, real Duffel/LiteAPI prices (`mcpMode=fixture`).
The notifier no longer has an SES stub mode; if an alert is triggered, it
attempts a real SES email send. This chat walkthrough should not trigger one.

**Cost:** fractions of a cent (a few Claude Sonnet 4.5 chat turns). Far
under the $10 budget alarm. Nothing recurring; `cdk destroy` ends it.

---

## Prerequisites

- [ ] **Bedrock model access** — AWS console → Bedrock → (deploy region) →
      Model access. The chat agent has **no stub mode** — it always calls
      Bedrock. Enable the foundation model
      `anthropic.claude-sonnet-4-5-20250929-v1:0` and deploy with
      `-c agentBedrockModelId=us.anthropic.claude-sonnet-4-5-20250929-v1:0`
      (the `us.` inference profile; must match the `^us\.` regex in
      `lib/agent.js`). The default is currently this Sonnet 4.5 inference
      profile.
- [ ] **SES emails for deploy context** — provide valid sender and recipient
      email addresses. They only need to be SES-verified if you exercise an
      alert path that actually sends email.
- [ ] **`cdk bootstrap`** has been run for this AWS account + region.
- [ ] **Docker daemon running** — CDK bundles the Python Lambdas at deploy.
- [ ] `aws sts get-caller-identity` returns the account you intend to use.
- [ ] `npm install` at repo root + per-Lambda installs done
      (`agent-authorizer`, `mcp-authorizer`, `flights-mcp`, `hotels-mcp`).

---

## Deploy (fixture chat path)

```
aws sts get-caller-identity            # confirm correct account
cdk bootstrap                          # only if never done for this acct/region
cdk deploy -c bedrockMode=stub \
           -c notifierSenderEmail=YOUR_EMAIL@example.com \
           -c notifierRecipientEmail=YOUR_EMAIL@example.com
```

The context flags above are required for this dry run:
- `bedrockMode=stub` — stubs the poller decision call.
- `notifierSenderEmail` + `notifierRecipientEmail` — required because the
  notifier always attempts real SES when an alert is triggered. This chat
  walkthrough does not trigger an alert, but a scheduled-path fixture scenario
  can.
- `budgetAlarmEmail` is **not** needed separately — `budget-alarm.js` falls
  back to `notifierRecipientEmail`.
- `mcpMode` defaults to `fixture` (no Duffel/LiteAPI keys needed).

Review the IAM diff CDK prints, then approve. Note the stack outputs (API
URL, Cognito IDs).

If the deploy fails, the most common first-time culprits are: missing
`-c` flags (synth-fail), `cdk bootstrap` not yet run, Docker not running,
Bedrock model access not granted, region without Bedrock, or
`lambdas/agent-authorizer` missing its `node_modules` (`npm install` step
in that directory is load-bearing). See git history for the specific
fixes.

---

## Start the local web app

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
- Optional clean slate: click **Clear** above the chat box before step 1.

---

## 0. Access the UI

Open `http://localhost:8000/chat/`, log in as **Alice** at the Cognito
screen. Captured run used password `Passw0rd@` (set by `prep-web.sh`).

---

## 1. Create a watch

**Type:**
```
Watch Tokyo in October, 5 nights from SFO, max $1500 total
```

**Response from the run (verbatim):**
> Before I create the watch, let me confirm the details:
> Trip watch for Tokyo:
> Origin: SFO
> Destination: Tokyo
> Travel window: October 1–31, 2026 (earliest to latest departure)
> Nights: 5
> Passengers: 1
> Max total price: $1,500 (flight + hotel combined)
> Should I go ahead and create this watch?

**Must do:** echo the structured watch back in plain English, **ask to
confirm before saving**, and surface inferred values (the Oct 1–31 window,
1 passenger, the primary destination airport — Tokyo → NRT) rather than
silently assuming them. It must NOT have saved yet. The airport code is
what the poller passes to flight search later; the city name is what the
hotel search and alert prose use.

---

## 2. Confirm creation

**Type:**
```
Yes, create it.
```

**Response from the run (verbatim):**
> Watch created!
> You're now tracking SFO → Tokyo, October 1–31, 2026, 5 nights, 1
> passenger, up to $1,500 total.
> The system will check combined flight + hotel prices every few hours and
> email you when the total drops or hits an anomaly low.

**Must do:** confirm the watch is saved. Behind the scenes this writes a
row to DynamoDB. To verify independently (optional):
```
aws dynamodb scan --table-name <WatchesTable> \
  --query "Items[0].{dest:destination.S,price:maxTotalPrice.N,status:status.S}"
```
Expected: `dest=Tokyo, price=1500, status=active`. (`maxTotalPrice` must be
a number, not a float error — this is the Decimal fix.)

---

## 3. Status check

**Type:**
```
what's happening with my watches?
```

**Response from the run (verbatim):**
> Tokyo, Oct 1–31, 5 nights from SFO: Just created—no price data yet.
> The system will run its first poll in the next few hours and you'll start
> seeing snapshots. I'll email you when there's a price worth acting on.

**Must do:** one headline line per watch (destination, dates, status/trend).
NOT a raw JSON dump or multiple lines per watch. "No price data yet" is
correct here — the scheduled poller hasn't run, and in this stub deploy it
wouldn't price anyway.

---

## 4. Live price check

**Type:**
```
how much is Tokyo right now?
```

**Response from the run (verbatim):**
> Right now for mid-October (Oct 15–20):
> Best combination under your $1,500 budget:
> United 1-stop flight: $1,148
> Shibuya Business Hotel (3★): $485
> Total: $1,633 ❌ over budget
> Next option:
> United 1-stop flight: $1,148
> Need a hotel around $350 or less to stay under $1,500—none available in
> this sample.
> The ANA nonstop at $1,285 is premium but leaves only $215 for 5 nights of
> lodging, which won't cover anything decent.
> Bottom line: Mid-October is tight at $1,500 for flight + hotel. Your watch
> will scan the full October window to find date combos that fit. I can also
> search specific dates if you want to explore early or late October.

**Must do:** return concrete flight and hotel numbers (these come from the
**fixture** data — Duffel/LiteAPI are not called in this deploy) plus a
qualitative read against the $1,500 budget. This step proves the agent's
MCP tool calls work end to end. Exact numbers will match the fixtures;
phrasing will vary.

---

## 5. Refine the watch

**Type:**
```
tighten Tokyo to weekends only
```

**Response from the run (verbatim) — it asks to clarify first:**
> I need to clarify: do you want departures limited to Friday or Saturday
> only (so you'd fly out on a weekend and come back mid-week after 5
> nights), or do you want to capture any October weekend within the window?
> ... Would you like me to narrow it to Fridays and Saturdays only for
> departure?

**Then type:**
```
Yes, narrow it to Friday and Saturday departures only.
```

**Response from the run (verbatim):**
> Done. Your Tokyo watch is now limited to Friday and Saturday departures
> only in October 2026.
> The system will only scan weekend departure dates (5 nights, returning
> mid-week) and alert you when those combinations hit your $1,500 budget.

**Must do:** confirm before patching (no silent change), then apply the
patch. Independently verifiable in DynamoDB: the row now has
`preferences.departDaysOfWeek = [5, 6]`, `updatedAt` advanced, and
critically **`status` is still `active`** (it must never be dropped on a
patch — it is the poller's index key).

---

## Pass criteria

All five behave as the "Must do" lines say, the UI never shows a 500 / blank
screen / "Failed to communicate", and DynamoDB reflects steps 2 and 5. That
is a green end-to-end run.

If a step misbehaves: note the step number, exactly what you typed, the full
response, and (if you can) the CloudWatch log for `travel-agent-on-lambda`.
That is a real finding — file a commit + fix in the same loop as the
earlier real-run fixes already in git history.

## Tear down

```
cdk destroy
```

Nothing recurs; cost stops.

## What this does NOT cover

This is the **chat path** only. The scheduled poller → Bedrock decision →
SES alert email path is not exercised by these chat messages.

## Exercise the scheduled path in fixture mode (optional)

To verify the scheduled path (EventBridge poller → MCP search → FareHistory
write → Bedrock decision → notifier writeback) without spending real
provider calls, two ready-made fixture scenarios — Tokyo snapshot-only and
Paris alert-firing — are documented in
[`fixture-poller-notifier-scenarios.md`](./fixture-poller-notifier-scenarios.md):
chat inputs to create the watches, the matching fixture files, expected
log lines, and the manual poller-invoke command.

For the **live** scheduled path (real Duffel + LiteAPI + Bedrock + SES,
7-day evidence run), see [`live-launch.md`](./live-launch.md).
