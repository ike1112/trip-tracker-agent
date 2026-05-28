# Trip Tracker Agent — Design Spec

| Field | Value |
|---|---|
| Date | 2026-05-08 |
| Status | Approved; brainstorming complete, ready for implementation planning |
| Owner | Isabel |
| Repo basis | `sample-serverless-mcp-servers/strands-agent-on-lambda` |

Repo basis context: existing Strands + Bedrock + Lambda + Cognito + MCP scaffold.

---

## 1. Problem & differentiator

### The personal problem
The user constantly searches for cheap flights and hotels but finds it hard to **track prices over time** for the trips they're considering — especially the **combined flight + hotel cost**, which is what actually matters as a buyer.

### The market reality
Existing tools (Google Flights, Hopper, Going, Kayak, Booking.com) cover slices of this:
- Google Flights tracks **flights** well, but not hotels.
- Hotels.com / Booking.com send marketing emails, not real per-listing alerts.
- **No mainstream tool tracks the combined flight + hotel cost** of a candidate trip over time.

### The differentiator (v1)
A **trip tracker agent**: the user describes a candidate trip in chat (origin, destination, date window, nights, budget). The agent stores it as a watch, polls Duffel (flights) + LiteAPI (hotels) every few hours, persists the combined trip price over time, and emails the user when the total crosses a threshold *or* hits an anomaly low relative to recent history. Each alert includes a model-generated explanation of *why* it's worth attention.

### Why this is more than a cron job
The agent is responsible for:
1. **Natural-language watch creation** — converting "watching Tokyo in October, 5 nights, leaving SFO, flexible ±3 days, max $1500 total" into a structured watch with no UI form.
2. **Natural-language refinement** — "tighten Tokyo to weekends only" → patch the watch.
3. **Alert worthiness reasoning** — going beyond "below threshold" to "below threshold *or* meaningfully cheaper than 30-day median," with a written justification per alert.
4. **Status summarization** — "what's happening with my watches?" returns a per-watch one-line summary with trend, not raw data.

### What this is NOT trying to be
Not a Booking.com replacement. Not a public service. Not a multi-user product (the user is the only intended user, though the architecture supports multi-tenancy because that's what the scaffold provides). Not a booking system in v1 (search + alert only; user clicks a deep link to book on the airline/OTA).

---

## 2. Architecture

### Starting point: AWS sample repo

This project starts from the AWS sample
`sample-serverless-mcp-servers/strands-agent-on-lambda`. The sample provides
the serverless agent foundation:

- Agent-on-Lambda pattern using Strands + Bedrock.
- Cognito login and JWT-based request authorization.
- S3-backed session memory for multi-turn conversations.
- MCP server pattern for tool integrations. The sample MCPs were dummy/demo
  placeholders; this project replaces them with provider-backed MCP servers
  that support both fixture replay and live API calls.
- A lightweight web UI shell.

Those pieces are reused as infrastructure patterns, not as the final product.

### Trip-tracker-specific work

The trip tracker turns that sample scaffold into a real price-watch system:

- Adds DynamoDB `Watches` and `FareHistory` tables.
- Adds local watch CRUD tools inside the Travel Agent Lambda.
- Replaces sample MCP/demo tooling with `flights-mcp` for Duffel and
  `hotels-mcp` for LiteAPI; each can run against committed fixtures or live
  provider APIs.
- Adds an EventBridge-driven Poller Lambda that checks active watches on a
  schedule.
- Adds alert decision logic and a Notifier Lambda that sends SES email.
- Adds production-hardening artifacts around the implementation: fixture mode,
  structured logs, X-Ray, CI, ADRs, threat model, budget alarm, and launch
  runbooks.

In short: the AWS sample supplies the agent/serverless skeleton; this project
supplies the trip-tracking product behavior and production-readiness layer.

### Architecture diagrams

Maintained architecture references live outside this original design spec:

- [`../../diagrams/trip-tracker-architecture.png`](../../diagrams/trip-tracker-architecture.png)
- [`../../diagrams/trip-tracker-architecture.drawio`](../../diagrams/trip-tracker-architecture.drawio)
- [`../../DESIGN.md`](../../DESIGN.md) (per-component rationale)
- [`../../SYSTEM.md`](../../SYSTEM.md) (flows + sequence diagrams)

This design spec captures the original product and system intent. The files
above are the maintained architecture references.

### Deliberate boundaries
- **MCP servers are isolated per provider.** Changing Duffel doesn't touch LiteAPI. Each MCP server is a separately-deployable Lambda with its own IAM role and secrets.
- **Watch CRUD is local tools, not MCP.** Internal data ops on tables in the same trust boundary as the agent — no need for the MCP transport overhead. Mirrors the existing `tools.py` pattern.
- **Poller and chat agent share the same MCP servers.** One integration point per provider, used in two contexts. Avoids drift between "what the agent sees" and "what the cron sees."

---

## 3. Data model

Two DynamoDB tables, on-demand billing.

### `Watches` table
- **Partition key:** `userId` (Cognito `sub`)
- **Sort key:** `watchId` (uuid)

| Field | Type | Notes |
|---|---|---|
| `userId` | string | Cognito `sub` |
| `watchId` | string | uuid |
| `type` | string | `"specific"` (v1); `"opportunity"` reserved for v1.5 |
| `origin` | string \| string[] | Airport code or list (e.g., `["SFO","OAK","SJC"]`) |
| `destination` | string | City name (e.g., `"Tokyo"`) — used for hotel search and alert prose |
| `destinationAirport` | string | IATA code (e.g., `"NRT"`) — used for flight search by the poller |
| `dateWindow` | object | `{earliestDepart, latestDepart, nights: int \| {min,max}}` |
| `pax` | int | Passenger count |
| `maxTotalPrice` | number | USD threshold for the simple alert path |
| `alertStrategy` | string | `"threshold" \| "anomaly" \| "both"` (default `"both"`); `"both"` = OR (either gate can trigger the agent decision) |
| `preferences` | object | `{maxStops, hotelMinStars, prefAirlines, redEyeOk, ...}` |
| `status` | string | `"active" \| "paused" \| "archived"` |
| `lastAlertedAt` | string \| null | ISO timestamp (anti-spam) |
| `lastAlertedPrice` | number \| null | USD (anti-spam) |
| `createdAt` | string | ISO timestamp |
| `updatedAt` | string | ISO timestamp |

### `FareHistory` table
- **Partition key:** `watchId`
- **Sort key:** `timestamp` (ISO; query descending for latest-first)

| Field | Type | Notes |
|---|---|---|
| `watchId` | string | FK to Watches |
| `timestamp` | string | ISO |
| `flightPrice` | number | USD |
| `hotelPrice` | number | USD |
| `totalPrice` | number | USD; what we threshold and rank against |
| `bestOfferBlob` | object | Denormalized snapshot: `{airline, flightNumber, stops, departDate, returnDate, hotelName, checkin, checkout, bookingDeepLink}` |
| `duffelRequestId` | string | For debugging |
| `liteApiRequestId` | string | For debugging |
| `ttl` | number | Unix epoch, 90 days from `timestamp` (DDB TTL) |

### Schema decisions worth flagging
- **`destination` (city) and `destinationAirport` (IATA) are stored as separate fields.** Hotels are city-scoped; flights are airport-scoped. The chat agent extracts both at watch-creation time (the LLM knows Tokyo → NRT, London → LHR). The poller has no LLM in its loop, so it can't infer one from the other at search time — storing both up front keeps the scheduled path simple and decoupled from any city-IATA resolver. The user still describes the trip by city in chat; the airport lookup happens transparently inside `add_watch`.
- **`bestOfferBlob` is denormalized.** Yes, it duplicates data also held inside Duffel/LiteAPI. But it lets the alert email say *"Lowest in 30 days: $1420 — AA non-stop on Oct 17, Park Hotel Tokyo"* without re-querying providers. Cheap storage; fast reads; simpler poller.
- **`lastAlertedAt` + `lastAlertedPrice` live on the watch.** Enables the anti-spam dedup gate in §5 — don't re-alert if the new price isn't meaningfully lower than the last alert.
- **`status: "paused"` is supported.** Cheaper than delete/recreate; lets the user mute a watch during an active trip.
- **TTL on fare history at 90 days.** Long enough for "is this a 30-day low?" reasoning, short enough that storage cost stays trivial.

---

## 4. Agent tools & chat patterns

### Tool surface

**Local tools (Python functions bundled with the Travel Agent Lambda):**

| Tool | Purpose |
|---|---|
| `add_watch(origin, destination, destinationAirport, earliestDepart, latestDepart, nights, pax, maxTotalPrice, preferences)` | Create a watch from chat. The agent supplies both the destination city and the primary IATA airport code. |
| `list_watches()` | Return user's active watches with latest price snapshot |
| `update_watch(watchId, patches)` | Patch mutable watch fields such as date window, budget, status, or preferences. |
| `pause_watch(watchId)` / `resume_watch(watchId)` | Mute during an active trip |
| `remove_watch(watchId)` | Soft-delete (`status="archived"`) |
| `get_fare_history(watchId, limit=30)` | Price snapshots for trend display |
| `get_user_location(ip)` | Already in repo — keep |
| `get_todays_date()` | Already in repo — keep |

**MCP tools (separate Lambdas):**

| Server | Tool | Purpose |
|---|---|---|
| `flights-mcp` | `search_flight_offers(origin, destination, departDate, returnDate, pax, maxStops?)` | Duffel-backed flight search; fixture or live mode |
| `flights-mcp` | `get_flight_offer_details(offerId)` | Full fare details + booking link |
| `hotels-mcp` | `search_hotel_offers(city, checkin, checkout, pax, minStars?)` | LiteAPI-backed hotel search; fixture or live mode |
| `hotels-mcp` | `get_hotel_details(hotelId)` | Amenities, photos, full rate breakdown |

### Five intended chat patterns

These are the product behaviors the agent prompt and evals are meant to keep
healthy. They describe the target user experience, not a guarantee that every
current edge case is fully solved.

1. **Setup** — *"watch Tokyo trips for me"*
   Agent asks the missing pieces (origin, dates, nights, budget) one at a time, echoes the full watch back in plain English, asks to confirm, then calls `add_watch`. **No silent defaults.**

2. **Status** — *"what's happening with my watches?"*
   `list_watches` + recent `get_fare_history` per watch; reply leads with a one-line summary per watch ("Tokyo: $1480, near 30-day low, ↓$120 from last week"), then offers details on request.

3. **Live search** — *"how much is Tokyo right now?"*
   `search_flight_offers` + `search_hotel_offers`; reply with the headline combined number plus a qualitative read ("$1640 — about average for these dates"). Offers to convert to a watch.

4. **Refinement** — *"tighten Tokyo to weekends only"*
   Clarifies the exact date/preference change, echoes it back, waits for confirmation, then calls `update_watch`.

5. **Acting on an alert** — *"show me the details from that email"*
   Looks up the most recent fare history row, surfaces `bestOfferBlob` + booking deep link.

### System prompt direction

The system prompt (in `agent_config.py`) must enforce:
- Always ask follow-ups for missing info before creating a watch — never silent defaults.
- Always echo the full watch back in plain English before saving.
- On status checks, lead with the one-line summary per watch, then offer details.
- On live search, lead with the headline number and *why* it is or isn't a good price (using fare history if a comparable watch exists).
- Use `get_todays_date` whenever the user mentions relative dates ("next month", "this fall").
- Never invent prices, airlines, hotel names, or availability not present in tool responses.

### Model choice
- **Chat agent:** Claude Sonnet 4.5 (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) is the CDK default injected through `AGENT_BEDROCK_MODEL_ID`. The chat-with-tool-calls pattern needs stronger reasoning than the original sample model. The literal fallback in `agent_config.py` remains the scaffold/test fallback; deployed stacks should use the CDK-provided value.
- **Alert decision (in poller):** Claude Haiku 4.5 (`claude-haiku-4-5-20251001`). The decision is small and bounded; Haiku is fast and cheap. Fixture deploys can set `bedrockMode=stub` for deterministic poller decisions.

---

## 5. Polling & alert decision

### Cadence
EventBridge schedule fires the Poller Lambda **every 4 hours** by default. The cadence is configurable through CDK context.

### Decision flow

The maintained flowchart is [`../../diagrams/poller-notifier-flowchart.svg`](../../diagrams/poller-notifier-flowchart.svg).

At runtime the poller:

1. Reads active watches.
2. Processes each watch sequentially.
3. Calls `flights-mcp` and `hotels-mcp`.
4. Builds the best combined snapshot.
5. Skips the watch if there are no qualifying flight/hotel offers.
6. Writes the snapshot to `FareHistory`.
7. Runs the alert gates.
8. Calls the Bedrock/stub decision layer only if the gates pass.
9. Invokes the Notifier Lambda only when the decision returns `alert: true`.
10. The notifier sends SES email, then writes `lastAlertedAt` and `lastAlertedPrice` back to the watch.

### What each gate does
- **Snapshot gate:** If no valid combined flight + hotel offer can be built, the poller logs `snapshot_skipped` and stops for that watch.
- **Dedup gate:** If the watch has never alerted, it can continue. Otherwise the new total must be strictly lower than `lastAlertedPrice * 0.95`.
- **Threshold gate:** The new total must be strictly below `maxTotalPrice` to pass this path.
- **Anomaly gate:** If threshold does not pass, the poller can still continue when the new total is at least 15% below the 30-day median or is a new 30-day low.
- **Decision layer:** Final yes/no. In live mode this is a Bedrock Haiku call; in fixture mode it can be deterministic stub logic. It sees the new total, 30-day price history, watch criteria, and stored preferences. It returns `{alert: bool, reason: string}`.

### Why keep a model decision layer
1. The `reason` line is the actual user value of the email. Generic "price dropped" notifications are why marketing emails get ignored. *Why* it's flagged is the trust-building part.
2. Lets soft preferences influence the decision ("I prefer non-stops" can affect whether a 1-stop fare is alert-worthy at a low price).
3. It's the agentic justification for using Bedrock at all in the background path — without this, the design is "scheduled DB query + SES."

### Cost bound
For 10 active watches polled every 4 hours, Bedrock is called only when the dedup gate passes and either the threshold or anomaly gate passes. Fixture deploys can use `bedrockMode=stub`, which avoids the model call entirely.

### Idempotency
The `lastAlertedAt` + `lastAlertedPrice` write happens **after** SES confirms send. If SES succeeds but the DynamoDB writeback fails, the next poll may send a duplicate because the watch still has the old alert state. Once writeback succeeds, the dedup gate works normally.

### Failure handling
- Per-watch errors (MCP timeout, provider 5xx, malformed offer data) are logged and skipped; the poller continues with the next watch. One bad watch never blocks the others.
- Poller-level errors (DDB unavailable, etc.) propagate as Lambda failure; EventBridge retries per its own policy.
- The notifier always attempts real SES when `decision.alert` is true. If SES fails, it logs `ses_send_failed`, raises, and does not update alert state.
- Poller metrics include watches polled, watches errored, alerts sent, and Bedrock decisions made. `alerts_sent` means the poller invoked the notifier; SES delivery success is logged by the notifier.

---

## 6. Evals

The current eval package focuses on the poller's alert-decision quality. Chat-pattern evals are still a planned extension.

| Layer | What it tests | How |
|---|---|---|
| **Unit** | MCP servers, local tools, poller gates, notifier, web OAuth, and eval runner plumbing | pytest/Jest, runs in CI |
| **Decision quality** | The poller's alert-worthiness decision returns the expected yes/no | `evals/run_evals.py` over hand-labeled `evals/fixtures/decision/*.json` |
| **Behavioral chat evals** | The 5 chat patterns from Section 4 | Planned; fixture folders are not implemented yet |

### Repo layout
```
evals/
  fixtures/
    decision/         # Hand-labeled alert-worthiness cases
  judge_prompts/
    decision.md       # Judge rubric for decision-quality evals
  run_evals.py        # Local decision-eval runner; not deployed
  results/            # Markdown reports from eval runs
  tests/              # Unit tests for loader, runner, report, judge client
```

### Operational notes
- The eval runner is a **local script**, not a deployed Lambda.
- CI runs the eval package unit tests, not the full model-backed eval run.
- Run the decision evals before changing `bedrock_decide.py`, changing gate thresholds in `gates.py`, regenerating decision fixtures, or bumping the model ID.
- The under-test decision model is whatever `bedrock_decide.BEDROCK_MODEL_ID` resolves to; fixture/smoke runs can use `BEDROCK_MODE=stub`.
- The judge defaults to Claude Sonnet 4.6, with `--stub` available for zero-network smoke tests.
- Deferred chat eval fixture folders: `chat_setup/`, `chat_status/`, `chat_search/`, `chat_refine/`, and `chat_alert/`.

---

## 7. Cost estimate

Personal usage estimate for a fixture or light live deployment. This is a
budgetary estimate, not a guarantee; verify in AWS Pricing Calculator before
running a heavier workload. It excludes external provider charges from Duffel
and LiteAPI.

Assumptions:
- One active user.
- Tens of chat requests per month, not thousands.
- Poller runs every 4 hours.
- Around 10 active watches.
- Bedrock decision calls happen only after the poller gates pass.

| Service | Cost/month |
|---|---|
| Bedrock Claude Haiku 4.5 (poller alert decisions) | Usually pennies; depends on gated decision volume |
| Bedrock Claude Sonnet 4.5 (chat agent) | Usually pennies to low dollars; depends on chat volume and output length |
| Lambda (all functions) | Usually $0 within the Lambda free tier for personal usage |
| DynamoDB on-demand (`Watches`, `FareHistory`) | Usually <$0.10 at personal usage |
| EventBridge schedule | Usually $0 or pennies |
| SES outbound email | $0.10 per 1,000 outbound emails plus data charges; effectively pennies for personal alerts |
| API Gateway | Usually <$0.10 at personal usage; free tier depends on account age/status |
| S3 (Strands sessions) | Usually <$0.05 |
| Cognito | Usually $0 for one user; pricing depends on user pool tier and account eligibility |
| CloudWatch logs/metrics/dashboard | Usually <$0.20 if logs stay small; can grow with verbose logs and Logs Insights use |
| **Expected total** | **~$1-5/month for light personal usage** |

Pricing references checked on 2026-05-27: [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/), [Claude Haiku 4.5 pricing](https://www.anthropic.com/claude/haiku), [Claude pricing](https://docs.claude.com/en/docs/about-claude/pricing), [Lambda pricing](https://aws.amazon.com/lambda/pricing/), [DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/), [SES pricing](https://aws.amazon.com/ses/pricing/), [API Gateway pricing](https://aws.amazon.com/api-gateway/pricing/), [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/), [Cognito pricing](https://aws.amazon.com/cognito/pricing/).

**Mandatory:** Configure the AWS Budget alarm with a **$10/month threshold** and email notification. This is a guardrail, not the expected monthly spend.

---

## 8. Still out of scope for v1

| Item | Why deferred | When |
|---|---|---|
| Booking via Duffel | Requires KYB onboarding, real-money handling, cancellation/refund flows, and customer-support policy | v2, after v1 has been used for real trip planning |
| Opportunity finder mode (multi-destination per watch) | Requires broader ranking UX and more provider calls per poll | v1.5 or later |
| Calendar-aware trip suggester | Requires Google Calendar OAuth and a separate trust boundary | v2; likely a separate MCP server |
| SMS / Discord / Telegram notifications | Email is enough for v1; each channel adds auth, delivery, and failure modes | Per-channel, on demand |
| Multi-trip combinatorial planner | Expands the search space beyond one watch -> one trip shape | v2 |
| Public demo mode | Fixture mode supports demos; public access, rate limits, abuse handling, and onboarding are separate product work | Maybe never |
| Mobile app / native push | Web + email works on phone for v1 | Maybe never |
| Multi-user marketing/onboarding | Architecture supports users, but the product is still scoped as a personal tool | If/when this stops being a personal project |

---

## 9. Launch checklist status

These were the original launch artifacts. Current status:

| Item | Status |
|---|---|
| README with pitch, architecture diagram, deployment instructions, and cost note | Done |
| Architecture diagram source + PNG | Done; maintained under `docs/diagrams/` |
| AWS Budget alarm at $10/month | Done in CDK via `lib/budget-alarm.js` |
| CloudWatch dashboard | Done in CDK via `lib/observability-dashboard.js`; it tracks poller EMF counters, Lambda health, API errors, and SES send/bounce/complaint metrics |
| Screenshots / demo evidence | Still useful; capture after a clean fixture or live run |
| 60-90 second Loom walkthrough | Still useful if this repo is being presented externally |
| One real watch active for several days | Optional evidence; useful for README screenshots, not required for the system design |

---

## 10. Implementation questions now resolved

These were open questions during brainstorming. The implementation has since answered them:

| Question | Current decision |
|---|---|
| CDK structure | One stack with separated constructs. See `lib/trip-tracker-stack.js` and `lib/README.md`. |
| Internal JWT signing secrets | Use Secrets Manager with separate agent and poller signing secrets. See ADR 0006. |
| Duffel / LiteAPI keys | Passed as Lambda environment variables from CDK context for v1 personal scale. Fixture mode avoids live provider keys for tests and demos. |
| Poller concurrency | Sequential per-watch loop. See ADR 0003. |
| Bedrock cost cap | No separate per-day token budget in v1. Cost is bounded by fixture mode, dedup-first gating, reserved concurrency, clamped poll cadence, model-scoped IAM, and the $10 AWS Budget alarm. |
| Failed MCP calls | Fail fast per watch and rely on the next scheduled poll. One bad watch does not block the rest of the loop. |

Remaining future hardening belongs in production-readiness follow-up work, not in this original design spec.
