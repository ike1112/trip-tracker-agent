# Documentation Map

What each file in `docs/` is for. Use this to navigate.

---

## Project entry

| File | Purpose |
|---|---|
| [`../README.md`](../README.md) | Project overview, quickstart, embedded architecture diagram, list of components |

---

## How the system works

| File | Purpose |
|---|---|
| [`SYSTEM.md`](./SYSTEM.md) | System guide — personas, user stories, user flows, end-to-end sequence diagrams (mermaid) |
| [`DESIGN.md`](./DESIGN.md) | Per-component design rationale: constraints, alternatives rejected, tradeoffs accepted |
| [`threat-model.md`](./threat-model.md) | Trust boundaries between components + mitigations + threat scenarios |

---

## How to run it

| File | Purpose |
|---|---|
| [`dry-run.md`](./dry-run.md) | Cost-free fixture-mode walkthrough of the **chat path** (5 chat patterns, verbatim agent responses). Run this first. |
| [`fixture-poller-notifier-scenarios.md`](./fixture-poller-notifier-scenarios.md) | Two named fixture scenarios (Tokyo snapshot-only, Paris alert-firing) for exercising the **scheduled path** without spending live calls |
| [`live-launch.md`](./live-launch.md) | Live launch protocol — real Duffel + LiteAPI + Bedrock + SES, 7-day evidence run, demo recording, teardown |

---

## Visuals

All system visuals live in [`diagrams/`](./diagrams/).

| File | Purpose |
|---|---|
| [`diagrams/trip-tracker-architecture.drawio`](./diagrams/trip-tracker-architecture.drawio) + `.png` | Canonical architecture diagram — every AWS service, every flow, numbered steps with right-side narrative. Embedded in main README. |
| [`diagrams/trip-tracker-architecture-review-log.md`](./diagrams/trip-tracker-architecture-review-log.md) | The WHY behind every diagram change across nine review rounds (3 multi-agent passes + 6 human-driven style iterations) |
| [`diagrams/poller-notifier-flowchart.svg`](./diagrams/poller-notifier-flowchart.svg) | Zoomed-in scheduled-path decision flow (poller gates → Bedrock decision → notifier writeback). Embedded in main README. |
| `diagrams/identify-product-defects-using-industrial-computer-vision-ra.pdf` | AWS reference architecture — kept as a **style sample** for future diagram work (numbered-step + right-side-narrative pattern) |
| `diagrams/upload-process-notify-pipeline-v9.drawio` | Internal reference diagram — kept as a **style sample** (top-row-compute / bottom-row-storage layout that minimises edge crossings) |

---

## Decision records

| File | Purpose |
|---|---|
| [`adr/README.md`](./adr/README.md) | ADR index |
| [`adr/0001-user-scoped-tools-via-closure-factory.md`](./adr/0001-user-scoped-tools-via-closure-factory.md) | Why agent watch-CRUD tools close over a verified `user_id` instead of accepting it as an LLM parameter |
| [`adr/0002-fixture-replay-mode.md`](./adr/0002-fixture-replay-mode.md) | Why MCP servers have a fixture mode (cost-free deploys, deterministic tests) |
| [`adr/0003-sequential-poll-loop.md`](./adr/0003-sequential-poll-loop.md) | Why the poller walks watches sequentially instead of in parallel |
| [`adr/0004-bedrock-decision.md`](./adr/0004-bedrock-decision.md) | Why a Bedrock model decides alert-worthiness instead of pure threshold logic |
| [`adr/0005-after-ses-idempotency.md`](./adr/0005-after-ses-idempotency.md) | Why `lastAlertedAt` is written AFTER the SES send, not before |
| [`adr/0006-per-component-jwt-secrets.md`](./adr/0006-per-component-jwt-secrets.md) | Why the agent and the poller sign MCP calls with separate Secrets Manager secrets |
| [`adr/0007-watches-status-gsi.md`](./adr/0007-watches-status-gsi.md) | Why the poller reads active watches via a GSI instead of a Scan |

---

## Original design

| File | Purpose |
|---|---|
| [`superpowers/specs/2026-05-08-trip-tracker-agent-design.md`](./superpowers/specs/2026-05-08-trip-tracker-agent-design.md) | Pre-implementation system design spec — what was committed to before any code |

---

## Archive

[`docs/.archive/`](./.archive/) holds frozen historical artifacts that
were superseded by what shipped: 3 superseded specs, the 4 Ralph
autonomous-loop archives (plans + learnings), 5 PRP shipment reports.
Audit-only — nothing in `.archive/` is current.
