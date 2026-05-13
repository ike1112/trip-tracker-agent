# Architecture Decision Records

Single-page records for the load-bearing architectural decisions in this repo.
Format follows Michael Nygard's "Context / Decision / Consequences."

| ID | Title | Slice |
|----|---|---|
| [0001](./0001-user-scoped-tools-via-closure-factory.md) | User-scoped tools via closure factory | 2 |
| [0002](./0002-fixture-replay-mode.md) | Fixture replay mode for external-API MCP servers | 3 |
| [0003](./0003-sequential-poll-loop.md) | Sequential per-watch poll loop | 5 |
| [0004](./0004-bedrock-decision.md) | Bedrock Haiku 4.5 as the alert-worthiness oracle | 6 |
| 0005 | After-SES idempotency for `lastAlertedAt` writeback | 7 (planned) |
| 0006 | Lambda env vars (not Secrets Manager) for external API keys | 9 (planned) |
| 0007 | Watches table without status GSI | 9 (planned, backfill) |

Trip-tracker design context: [`../superpowers/specs/2026-05-08-trip-tracker-agent-design.md`](../superpowers/specs/2026-05-08-trip-tracker-agent-design.md).
Production-readiness companion spec: [`../superpowers/specs/2026-05-10-trip-tracker-production-readiness.md`](../superpowers/specs/2026-05-10-trip-tracker-production-readiness.md).
