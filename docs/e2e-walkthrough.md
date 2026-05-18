# E2E Walkthrough — Run It Yourself in the Web UI

A step-by-step you drive in the browser. Each step gives the exact text to
type and the **verbatim agent response captured on the 2026-05-18 run**
(Claude Sonnet 4.5, stub/fixture deploy) so you know what a correct response
looks like.

> The agent is an LLM. Your wording will differ from the captured text —
> that is expected. What must match is the **behavior** in the "Must do"
> line under each step, not the prose.

---

## 0. Access the UI

The stack is deployed (us-east-1, stub/fixture — cost-free) and the local
web app serves the chat.

- If `http://localhost:8000/chat/` loads → go to step 1.
- If it doesn't, start the web app:
  ```
  cd web
  ./.venv/Scripts/python.exe app.py        # Windows venv
  # or: source .venv/bin/activate && python app.py
  ```
- Open `http://localhost:8000/chat/`, log in at the Cognito screen:
  **username `Alice`, password `Passw0rd@`**.
- Optional clean slate: click **Clear** above the chat box before step 1.

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
1 passenger) rather than silently assuming them. It must NOT have saved yet.

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
That is a real finding — same loop as the fixes already in
`docs/e2e-test-runbook.md`.

## What this does NOT cover

This is the **chat path** only. The scheduled poller → Bedrock decision →
SES alert email path is stubbed in this deploy and is exercised separately
by the live run in `docs/launch-runbook.md`. Engineering findings and the
bug fixes from the first run are recorded in `docs/e2e-test-runbook.md`.
