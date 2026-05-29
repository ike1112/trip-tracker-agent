# Trip Tracker Agent

A personal trip-price tracker built as a user-aware AI agent on AWS
Lambda. You describe a candidate trip in chat — origin, destination,
date window, nights, budget — and the agent stores it as a *watch*. A
scheduled poller checks flight and hotel prices every few hours,
persists the **combined** flight + hotel cost over time, and emails you
when the total crosses your threshold or drops to an anomaly low
relative to recent history. Every alert carries a model-generated
explanation of why it is worth your attention.

No mainstream tool tracks the *combined* flight + hotel cost of a
specific candidate trip over time. This does. It is a single-user
personal project; the architecture is multi-tenant only because the
underlying scaffold is.

Start with the architecture below, then use the [Documentation](#documentation)
section to jump into design rationale, runbooks, ADRs, and threat modeling.

## What the agent does

- **Natural-language watch creation** — "watching Tokyo in October, 5
  nights, leaving SFO, flexible ±3 days, max $1500 total" becomes a
  structured watch, no form.
- **Natural-language refinement** — "tighten Tokyo to weekends only"
  patches the existing watch.
- **Alert-worthiness reasoning** — not just "below threshold" but "below
  threshold *or* meaningfully cheaper than the 30-day median," decided
  by Bedrock with a written justification per alert (ADR 0004).
- **Status summarization** — "what's happening with my watches?"
  returns a per-watch one-line trend, not raw rows.

Search and alert only — no booking in v1. Alerts link out to the
airline/OTA.

## Architecture

![Trip Tracker architecture](./docs/diagrams/trip-tracker-architecture.png)

Architecture source: [`docs/diagrams/trip-tracker-architecture.drawio`](./docs/diagrams/trip-tracker-architecture.drawio).

**Poller and notifier flow**

<img src="./docs/diagrams/poller-notifier-flowchart.svg" alt="Poller and notifier flowchart" width="100%">

**Components**

| Path | Role |
|------|------|
| `lambdas/travel-agent` | Strands chat agent; watch CRUD as local tools |
| `lambdas/agent-authorizer` | API Gateway authorizer — validates Cognito user JWTs |
| `lambdas/mcp-authorizer` | API Gateway authorizer — validates per-component JWTs (ADR 0006) |
| `lambdas/flights-mcp` | MCP server wrapping Duffel; fixture-replayable (ADR 0002) |
| `lambdas/hotels-mcp` | MCP server wrapping LiteAPI; fixture-replayable |
| `lambdas/poller` | Scheduled price poll → snapshot → gates → decision |
| `lambdas/notifier` | SES alert send + idempotent dedup writeback (ADR 0005) |
| `lib/data-stores.js` | `Watches` + `FareHistory` tables; `status-index` GSI (ADR 0007) |
| `lib/secrets.js` | Per-component JWT signing secrets in Secrets Manager (ADR 0006) |
| `lib/observability-dashboard.js` | CloudWatch dashboard across the Lambdas + APIs |
| `lib/budget-alarm.js` | Account-level $10/mo cost budget with email alerts |

### Authentication and authorization

- The chat agent is gated by [Amazon Cognito](https://aws.amazon.com/cognito/);
  `cdk deploy` provisions two demo users (`Alice`, `Bob`).
- The chat agent expects a JWT issued by Cognito whose subject is the
  user, validated against Cognito JWKS in the agent authorizer.
- The MCP servers expect a JWT minted per calling component (the chat
  agent and the poller each sign with their **own** Secrets Manager
  secret — ADR 0006), validated by the MCP authorizer.
- User identity is never inferred from an LLM response. It is always
  propagated as a JWT claim.

### Fixture vs live

Provider search can run in **fixture mode** and the poller decision can
run in **Bedrock stub mode** for deterministic rehearsal. The notifier
does not have an SES stub mode: when a notification is triggered, it
attempts a real SES email send. The MCP servers default to fixture mode;
Bedrock and SES default to live for production deploys. Pass
`-c bedrockMode=stub` for a poller-decision dry run (see Configure
below). The test suite mocks external sends/calls where needed.

## Running the project

Choose the runbook for the path you want to exercise:

| Goal | Start here |
|---|---|
| Chat-path fixture rehearsal | [`docs/dry-run.md`](./docs/dry-run.md) |
| Scheduled poller/notifier fixture scenarios | [`docs/fixture-poller-notifier-scenarios.md`](./docs/fixture-poller-notifier-scenarios.md) |
| Live launch and 7-day evidence run | [`docs/live-launch.md`](./docs/live-launch.md) |

Arm64 is the default Lambda architecture for cost efficiency; change the IaC if
you need x86.

### Prerequisites

- AWS CLI, Git, Docker, Node.js, Python 3.12
- AWS CDK
- Bedrock model access:
  - chat agent: `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
  - poller decision: `claude-haiku-4-5-20251001`
- SES sender/recipient emails verified if your account is in the SES sandbox

Configuration is supplied as CDK context; [`.env.example`](./.env.example) is
only a scratch template for values you pass into `cdk deploy`.

## Testing

```bat
npm test
npm --prefix lambdas/agent-authorizer test
npm --prefix lambdas/mcp-authorizer test
npm --prefix lambdas/flights-mcp test
npm --prefix lambdas/hotels-mcp test

python -m venv .venv-tests
.venv-tests\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-test.txt

.venv-tests\Scripts\python.exe -m pytest lambdas/poller/tests -q
.venv-tests\Scripts\python.exe -m pytest lambdas/notifier/tests -q
.venv-tests\Scripts\python.exe -m pytest lambdas/travel-agent/tests -q
.venv-tests\Scripts\python.exe -m pytest web/tests -q
cd evals
..\.venv-tests\Scripts\python.exe -m pytest tests -q
cd ..
```

Construct synth tests skip Docker bundling via the
`aws:cdk:bundling-stacks: []` context.

GitHub Actions ([`.github/workflows/ci.yml`](./.github/workflows/ci.yml))
runs the same suites on every push and pull request: the CDK construct
tests, the MCP-server / authorizer Node suites, and the Python Lambda +
evals suites against the pinned [`requirements-test.txt`](./requirements-test.txt).

## Documentation

### How the system works

| File | Purpose |
|---|---|
| [`docs/SYSTEM.md`](./docs/SYSTEM.md) | System guide: personas, user stories, user flows, and end-to-end sequence diagrams |
| [`docs/DESIGN.md`](./docs/DESIGN.md) | Per-component design rationale: constraints, alternatives rejected, and tradeoffs accepted |
| [`docs/threat-model.md`](./docs/threat-model.md) | Trust boundaries between components, mitigations, residual risks, and threat scenarios |

### How to run it

| File | Purpose |
|---|---|
| [`docs/dry-run.md`](./docs/dry-run.md) | Fixture-mode walkthrough of the chat path with the five main chat patterns and expected behavior |
| [`docs/fixture-poller-notifier-scenarios.md`](./docs/fixture-poller-notifier-scenarios.md) | Two named fixture scenarios, Tokyo snapshot-only and Paris alert-firing, for exercising the scheduled path |
| [`docs/live-launch.md`](./docs/live-launch.md) | Live launch protocol: real Duffel, LiteAPI, Bedrock, SES, evidence capture, and teardown |
| [`docs/demo-script.md`](./docs/demo-script.md) | Short recording outline and evidence checklist for presenting a fixture or live run |

### Visuals

All system visuals live in [`docs/diagrams/`](./docs/diagrams/).

| File | Purpose |
|---|---|
| [`docs/diagrams/trip-tracker-architecture.drawio`](./docs/diagrams/trip-tracker-architecture.drawio) + [`trip-tracker-architecture.png`](./docs/diagrams/trip-tracker-architecture.png) | Canonical architecture diagram: every AWS service, every flow, numbered steps with right-side narrative |
| [`docs/diagrams/poller-notifier-flowchart.svg`](./docs/diagrams/poller-notifier-flowchart.svg) | Zoomed-in scheduled-path decision flow: poller gates, Bedrock decision, notifier writeback |
| [`docs/diagrams/trip-tracker-architecture-review-log.md`](./docs/diagrams/trip-tracker-architecture-review-log.md) | Why the architecture diagram changed across review rounds |
| `docs/diagrams/identify-product-defects-using-industrial-computer-vision-ra.pdf` | AWS reference architecture used for diagram style |
| `docs/diagrams/upload-process-notify-pipeline-v9.drawio` | Internal reference diagram used for layout style |

### Decision records

| File | Purpose |
|---|---|
| [`docs/adr/README.md`](./docs/adr/README.md) | ADR index |
| [`docs/adr/0001-user-scoped-tools-via-closure-factory.md`](./docs/adr/0001-user-scoped-tools-via-closure-factory.md) | Why watch CRUD tools close over a verified `user_id` instead of accepting it as an LLM parameter |
| [`docs/adr/0002-fixture-replay-mode.md`](./docs/adr/0002-fixture-replay-mode.md) | Why MCP servers have fixture mode for no provider keys and deterministic tests |
| [`docs/adr/0003-sequential-poll-loop.md`](./docs/adr/0003-sequential-poll-loop.md) | Why the poller walks watches sequentially instead of in parallel |
| [`docs/adr/0004-bedrock-decision.md`](./docs/adr/0004-bedrock-decision.md) | Why a Bedrock model decides alert-worthiness instead of pure threshold logic |
| [`docs/adr/0005-after-ses-idempotency.md`](./docs/adr/0005-after-ses-idempotency.md) | Why `lastAlertedAt` is written after SES send, not before |
| [`docs/adr/0006-per-component-jwt-secrets.md`](./docs/adr/0006-per-component-jwt-secrets.md) | Why the agent and poller sign MCP calls with separate Secrets Manager secrets |
| [`docs/adr/0007-watches-status-gsi.md`](./docs/adr/0007-watches-status-gsi.md) | Why the poller reads active watches through a GSI instead of a Scan |

### Design history

| File | Purpose |
|---|---|
| [`docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md`](./docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md) | Original system design spec, updated where needed to reflect current implementation |

### Archive

[`docs/.archive/`](./docs/.archive/) contains historical artifacts superseded
by the shipped implementation.

## License

MIT — see [LICENSE](./LICENSE).
