# Trip-tracker evals

Local-only decision-quality evaluation for the trip-tracker poller's
Bedrock alert-decision call. Not deployed; not wired to CI yet.

## What this measures

For each fixture, we ask the under-test model
(`claude-haiku-4-5-20251001` by default — same one production uses) to
decide whether a snapshot is alert-worthy, then ask a stronger judge
model (Claude Sonnet 4.6 by default) to grade the decision against a
hand-labelled expectation. The runner emits a markdown report and
exits non-zero if any fixture fails the judge.

## How to run

```bash
# From the repo root.
ANTHROPIC_API_KEY=sk-...  python evals/run_evals.py \
    --fixtures-dir evals/fixtures/decision \
    --out evals/results/$(date +%F).md
```

Useful flags:

| Flag | Purpose |
|---|---|
| `--stub` | Skip the Anthropic judge call. Verdict is deterministic — `pass` iff `actual.alert == expected_alert`. Costs nothing; good for smoke-testing the runner itself. |
| `--judge-model <id>` | Override the judge model. Defaults to `claude-sonnet-4-6`. |
| `--log-level DEBUG` | Verbose logging including per-case start/done events. |

The under-test model is whatever `bedrock_decide.BEDROCK_MODEL_ID`
resolves to at import time — set `BEDROCK_MODEL_ID` in the environment
to override at runtime. `BEDROCK_MODE=stub` short-circuits the
under-test call (returns the stub shape from `bedrock_decide.decide`)
which is what the unit tests use.

## Cost

Sonnet 4.6 input is ~1500 tokens per fixture × 30 fixtures × current
list price (see [Anthropic pricing](https://www.anthropic.com/pricing))
puts a single full run at roughly **$0.05**. Re-run before any
`bedrock_decide.py` edit, any prompt change, or any model-ID bump.

## When to re-run

- Before changing the system or user prompt in `bedrock_decide.py`.
- Before bumping `BEDROCK_MODEL_ID` to a newer Haiku.
- Before changing gate thresholds in `gates.py` (an indirect prompt change).
- After regenerating fixtures.

## Fixture schema

Each `evals/fixtures/decision/*.json` follows the `Fixture` shape in
`evals/loader.py`:

```json
{
  "case_id": "0001-no-alert-stable-fare",
  "notes": "Fare has been flat for 30 days; nothing anomalous; over budget.",
  "snapshot": {
    "watchId": "w-001",
    "timestamp": "2026-10-15T12:00:00+00:00",
    "totalPrice": "1650.00",
    "flightPrice": "1200.00",
    "hotelPrice": "450.00",
    "bestOfferBlob": { "...": "..." }
  },
  "watch": {
    "watchId": "w-001",
    "userId": "u-1",
    "maxTotalPrice": "1500.00",
    "preferences": { "...": "..." }
  },
  "history": [
    { "totalPrice": "1648.00" },
    { "totalPrice": "1652.00" }
  ],
  "expected_alert": false,
  "expected_reason_themes": ["over budget", "no anomaly"]
}
```

Numeric fields (`totalPrice`, `flightPrice`, `hotelPrice`,
`maxTotalPrice`) are loaded as `Decimal(str(value))` so float
imprecision never enters the decision pipeline. The loader rejects
unknown keys and missing keys loudly — a malformed fixture is a load
error, not a silent pass.

## Stub mode

`--stub` bypasses the Anthropic API. Verdict logic: `pass` iff
`actual.alert == expected_alert`, else `fail`. Useful for confirming
the runner is wired up correctly without spending API credits, or for
isolating a `bedrock_decide` regression from a judge regression.

Stub mode is **independent** of `BEDROCK_MODE`. You can run with
`BEDROCK_MODE=stub` AND `--stub` to exercise the runner pipeline with
zero network traffic — that's what the unit-test suite does.

## Deferred — not in scope for the local runner today

- `make evals` Makefile target.
- GitHub Actions `workflow_dispatch` runner — running evals in CI
  requires a per-PR cost discipline that isn't in place yet.
- Chat-pattern fixtures (`chat_setup/`, `chat_status/`, etc.) per
  `docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md` §6.
