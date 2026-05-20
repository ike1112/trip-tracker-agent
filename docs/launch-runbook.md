# Launch Runbook — Live Deploy, 7-Day Real Run, Demo

Closes the open launch-checklist items: live deploy, a real price-watch run
producing a genuine 7-day trend, one honest alert email, demo recording, and
surfacing inspectable proof in the README. Ordered by dependency — the
slow-to-approve items come first so the 7-day clock starts as early as
possible. Check items off as you go.

Objective: a reviewer who forks this repo comes away convinced it works.
Everything here is evidence, not features. No new scope.

This runbook was hardened by a CEO + engineering + outside-voice review.
The one non-reversible thing in this whole project is the 7-day wall-clock
run — most of the rigor below exists to keep a silent failure from costing
you a week you can't buy back.

---

## Phase 0 — Accounts and access (start first; approval lag)

- [ ] **AWS account** — `aws configure` done. Run `aws sts get-caller-identity`
      and confirm the account/ARN is the one you intend to deploy to. Note
      the **deploy region**: `__________` (must offer Bedrock + the models below).
- [ ] **Bedrock model access** — enable the **exact** models the code uses,
      in the deploy region. Do not trust prose; read the IDs from source so
      this can't rot:
      - Chat agent: `lib/agent.js` `DEFAULT_AGENT_BEDROCK_MODEL_ID`
        (currently `us.anthropic.claude-haiku-4-5-20251001-v1:0` — a `us.`
        inference profile over Claude Haiku 4.5). Enable model access for the
        **underlying foundation model** (`anthropic.claude-haiku-4-5-20251001-v1:0`)
        in every US region the profile routes to (the IAM grant in `agent.js`
        enumerates them).
      - Poller decision: `lambdas/poller/bedrock_decide.py` `DEFAULT_MODEL_ID`
        (currently `claude-haiku-4-5-20251001`).
- [ ] **AWS Marketplace gate (the non-obvious one).** Anthropic models on
      Bedrock now require an account-level AWS Marketplace subscription. A
      brand-new IAM principal in this account that has never invoked the
      model will hit:
      `AccessDeniedException ... aws-marketplace:ViewSubscriptions, aws-marketplace:Subscribe`
      even though Bedrock model access shows ACTIVE. Symptom is identical
      to a missing IAM grant. Fix: do the model-access request through the
      **Bedrock console** ("Manage model access"), not by editing IAM —
      the console flow handles the marketplace subscription as part of the
      request, clearing the gate stack-wide. A subscription created this
      way persists across stack destroy/recreate at the account level,
      but a fresh IAM principal in a different account starts the gate
      over.
- [ ] **Duffel** → **live access token** (not test). Search is free; Duffel
      charges on booking only and this system never books. Confirm the account
      is **activated for live search** (some Duffel accounts gate live access);
      a token that returns 403/empty on live search blocks the whole run.
- [ ] **LiteAPI** → production key (sandbox returns canned data). Confirm the
      key returns non-empty real results for a test city before you rely on it.
- [ ] **SES** — stay in the SES sandbox (no production-access wait for one
      recipient). **SES must be in the same region you deploy to** (or
      `notifier` config must point at the SES region explicitly — check
      `lib/`/notifier config); a region mismatch makes email silently fail.
      Verify **both** identities in that region: sender (`notifierSenderEmail`)
      and recipient (`notifierRecipientEmail`).

## Phase 1 — Local prerequisites (first-run deploy blockers)

- [ ] `cdk bootstrap` has been run for **this account + deploy region**
      (`aws cloudformation describe-stacks --stack-name CDKToolkit` succeeds).
- [ ] Node + Python versions match the project (CI uses Python 3.12; see
      `requirements-test.txt` / `.github/workflows/ci.yml`).
- [ ] Docker daemon running and your user can run `docker ps` without sudo
      (CDK bundles the Python Lambdas at deploy; tests skip bundling, a real
      deploy does not).
- [ ] `npm install` at repo root, then per-Lambda installs for
      `agent-authorizer`, `mcp-authorizer`, `flights-mcp`, `hotels-mcp`.
- [ ] `cp .env.example .env`, fill keys from Phase 0 (kept handy; CDK reads
      `-c` context, not a runtime `.env`).
- [ ] Sanity gate before spending: `npm test` + `npm run test:node` +
      `python -m pytest evals/tests -q` pass locally (fixture/stub —
      zero cost). If these fail, stop here.

## Phase 2 — Deploy live

- [ ] Review the IAM diff CDK prints before approving.
- [ ] Deploy:
      ```
      cdk deploy -c mcpMode=live -c duffelApiKey=… -c liteApiKey=… \
                 -c bedrockMode=live -c sesMode=live \
                 -c notifierSenderEmail=… -c notifierRecipientEmail=…
      ```
- [ ] Record the stack outputs (Lambda function names, API URLs) — you need
      the poller function name in Phase 3.
- [ ] **Budget alarm must be ACTIVE before any live poll.** Click the
      Budgets/SNS confirmation email, then verify it is actually confirmed:
      `aws budgets describe-budgets` shows the $10 budget and
      `aws sns list-subscriptions` shows the alarm subscription as
      **Confirmed** (not `PendingConfirmation`). An unconfirmed subscription
      is a silent backstop failure.

## Phase 3 — Verify the deploy works end to end (gate before the clock)

The clock does not start until **every** box here is checked. A clean deploy
that can't actually reach Bedrock/Duffel/LiteAPI is the failure that wastes
the week.

- [ ] `./prep-web.sh`, then web UI: `cd web` → venv →
      `pip install -r requirements.txt` → `python app.py` →
      `http://localhost:8000/chat/`, log in as `Alice`.
- [ ] **Pick the watched route by criteria, not vibe.** Requirements: a
      high-liquidity origin/destination pair; departure **4–10 weeks out**
      (far enough to be priced, near enough to have inventory); a date window
      that isn't a holiday/seasonal anomaly; both legs return **USD**. Record
      the chosen route + dates here: `__________`.
- [ ] **Chat path exercises Bedrock live.** Create the trend watch in chat
      (the route above). The agent echoing a structured watch back proves
      web → API GW → agent → Bedrock works end to end on the real model.
- [ ] **Live search works:** *"How much is <dest> right now?"* returns a real
      non-zero headline number — proves live Duffel + LiteAPI keys are good.
- [ ] **Poller path exercises Bedrock + providers live.** Manually invoke the
      poller Lambda (name from Phase 2 outputs):
      ```
      aws lambda invoke --function-name <POLLER_FN_NAME> \
        --payload '{}' --cli-binary-format raw-in-base64-out /tmp/poll.json
      ```
      The poller ignores `event` (EventBridge envelope), so `{}` is correct.
      Success = CloudWatch logs show `snapshot_written` with **non-null
      numeric** `flight_price` AND `hotel_price`, then `poll_complete` with
      `watches_errored: 0`. A `snapshot_skipped` line means no qualifying
      offers → fix the route/dates and retry; **do not** start the clock on a
      skip.
- [ ] Confirm one real `FareHistory` row exists for the trend watch with
      non-null numeric `flightPrice` + `hotelPrice` (query DynamoDB directly,
      not just "a row exists").

## Phase 4 — The two watches and the 7-day clock

Two watches, deliberately separated so neither contaminates the other.

**Watch TREND** (the headline evidence — runs the full 7 days):
- [ ] Set `maxTotalPrice` **well below** the current live total so it will
      **not** alert. Pure price-over-time evidence; no alert noise.
- [ ] Start date: `__________`  →  target evidence date (+7d): `__________`.
- [ ] Definition of "works" for this artifact (be explicit so a flat line is
      still defensible): poller fires every 4h → ≈ **42 scheduled polls** over
      7 days. Accept **≥ 38** `FareHistory` rows (allows a few missed
      ticks/retries), every row non-null numeric on both legs. A flat price
      curve is acceptable evidence **only** when the row count proves polling
      ran — annotate it "stable market over the window," not a broken poller.

**Daily success gate (abort + restart, do not skip):**
- [ ] **End of Day 1** and **End of Day 2**: query `FareHistory` for Watch
      TREND. Expected rows so far ≈ (hours elapsed / 4). If rows are missing,
      sparse, or null on either leg → **STOP the clock**. Root-cause: route
      liquidity, expired/invalid keys, EventBridge schedule disabled, or
      live-vs-fixture parse divergence (`snapshot_skipped` / `watch_errored`
      in logs). Fix it, **reset the start date above, and restart the 7-day
      clock.** Catching this on Day 1 costs 1 day; catching it in Phase 6
      costs the whole week.
- [ ] Days 3–7: one dashboard spot-check/day (last-poll advancing, error
      count flat).

**Watch ALERT** (one honest alert email — created ~1 day before recording):
- [ ] Create a second short-lived watch near recording day. Set its
      `maxTotalPrice` just **above its own** current live total. Trace
      (verified against `gates.py`): `lastAlertedPrice` is None → dedup gate
      open; total < maxTotalPrice → threshold passes; Bedrock decides → exactly
      one honest alert on the next poll. The model-written reason in that
      email is the thing ADR 0004 exists to justify.
- [ ] After the alert email lands and is captured (Phase 6), archive Watch
      ALERT. Its `FareHistory` is **never** used for the trend curve — no
      contamination.

## Phase 5 — During the wait (documentation edits, not app code)

Markdown/doc changes only — but still run a link + screenshot-path pass so
nothing rots on the first reviewer click. Do these while the clock runs.

- [ ] **README first-screen layout target** (avoid evidence soup — one tight
      "Proof it works" block, everything else linked not inlined). Order:
      1. one-line pitch
      2. 60-second fixture-mode try (use the quickstart command block in README)
      3. architecture diagram link
      4. **Proof it works** — ≤4 bullets: eval chat-pattern pass rate;
         decision-quality score; link to `evals/results/2026-05-13-baseline.md`;
         link to exported raw evidence (`docs/evidence/`, Phase 6)
      5. **What would change for real production** (lifted from
         production-readiness spec §4.6 item 7)
      6. deeper links (specs, ADRs, threat model)
- [ ] **Pre-empt the LLM-in-the-loop attack.** One line near the README
      Bedrock mention **and** in ADR 0004 pointing at the eval
      decision-quality numbers as the evidence the reason line earns its
      Bedrock call.
- [ ] Write `docs/demo-script.md` from production-readiness spec §4.7
      (0:00–1:30 outline) so the recording is one clean take.

## Phase 6 — After ~7 days: capture, EXPORT, then record

Screenshots alone are fragile. Export inspectable raw evidence **before**
`cdk destroy` — once the stack is gone the logs/metrics are gone.

- [ ] **Export sanitized raw evidence to `docs/evidence/` and commit it:**
      - Watch TREND `FareHistory` rows as JSON/CSV. Drop/truncate `userId`
        (Cognito sub); `watchId` is a uuid, safe to keep for correlation.
      - CloudWatch log excerpts: `snapshot_written`, `decision_made`,
        `poll_complete` lines across the run.
- [ ] Screenshot the price trend (status check / "lowest in N days"). The
      shot must show a **timestamp** and the **watchId** so it correlates to
      the exported rows.
- [ ] Screenshot/save the Watch ALERT email (model-written reason visible).
- [ ] Screenshot the CloudWatch dashboard — timestamp visible, account
      number cropped/redacted.
- [ ] Record the demo per `docs/demo-script.md`. If Loom, also commit a
      fallback GIF/stills — hiring reviewers hit this months later and Loom
      links rot.
- [ ] Place screenshots where the README layout (Phase 5) expects them.

## Phase 7 — Close out

- [ ] CI-green visibility (precise, not "it's green somewhere"): link the
      latest passing GitHub Actions run on **`main`**, or add a status badge
      to the README top. (Repo is public, `main` CI is currently green.)
- [ ] Tick the production-readiness spec §5 boxes that this run closes
      (7-day post-launch item + the evidence-surfacing items added here;
      note in the spec that the eval-link / production-delta items are new
      relative to its original §5 list).
- [ ] `cdk destroy` — **only after** `docs/evidence/` is committed and all
      screenshots/recording are captured. Verify the export is in git first.

---

## Cost note

Bedrock decisions ~2–3/day on Haiku (cents). Duffel/LiteAPI search free (no
booking). Watch ALERT adds one extra Bedrock call + one SES send — still
cents. Dominant risk is a runaway poll loop, not per-call cost — the $10
Budgets alarm must be **ACTIVE** (Phase 2) before the clock starts. Expected
spend for the run: under $1. `cdk destroy` (Phase 7) stops all of it.
