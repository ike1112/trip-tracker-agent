# Architecture Decision Records

Single-page records for the load-bearing architectural decisions in this repo.
Format follows Michael Nygard's "Context / Decision / Consequences."

| ID | Title | Status |
|----|---|---|
| [0001](./0001-user-scoped-tools-via-closure-factory.md) | User-scoped tools via closure factory | Accepted |
| [0002](./0002-fixture-replay-mode.md) | Fixture replay mode for external-API MCP servers | Accepted |
| [0003](./0003-sequential-poll-loop.md) | Sequential per-watch poll loop | Accepted |
| [0004](./0004-bedrock-decision.md) | Bedrock Haiku 4.5 as the alert-worthiness oracle | Accepted |
| [0005](./0005-after-ses-idempotency.md) | After-SES idempotent writeback for `lastAlertedAt` | Accepted |
| [0006](./0006-per-component-jwt-secrets.md) | Per-component JWT signing secrets via Secrets Manager | Accepted |
| [0007](./0007-watches-status-gsi.md) | Watches table status GSI for `Query` (not `Scan`) | Accepted |

Trip-tracker design context: [`../superpowers/specs/2026-05-08-trip-tracker-agent-design.md`](../superpowers/specs/2026-05-08-trip-tracker-agent-design.md).
