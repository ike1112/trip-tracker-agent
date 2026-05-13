# ADR 0004 — Bedrock Haiku 4.5 as the alert-worthiness oracle

**Date:** 2026-05-13
**Status:** Accepted
**Slice:** 6

## Context

The trip-tracker poller (slice 5) reaches a `decision.decide(snapshot,
watch, history)` call after every successful poll. Slice 5 wired it to
a stub that always returned `{"alert": True, "reason": "stub"}` once
the gate cascade (dedup → threshold OR anomaly) passed. Slice 6
replaces the stub with a real model call.

Three shapes were considered:

1. **Rules-only.** Keep the gates, skip the model. Threshold or
   anomaly already filters most noise; a hand-coded `if` picks the
   canned reason string.
2. **Larger model (Sonnet 4.6).** Same prompt, more reasoning headroom.
3. **Small model (Haiku 4.5) with strict JSON output.** One-shot:
   snapshot + watch + history → `{"alert": bool, "reason": string}`.

The decision is small and bounded — binary classifier plus a sentence
explaining why. The call only fires when the gates have already
filtered out the obvious non-alerts (dedup blocks ~80% of poll cycles
at steady state). The user-visible value is the **reason string**, not
the alert flag: the user already knows the snapshot is interesting
because the email exists; the model's job is to articulate *why*.

Cost surface at personal scale (1-2 active watches, 4-hour cron, ~5%
of polls survive dedup and pass at least one gate): Haiku 4.5 at
current Bedrock list price ≈ **$0.30/mo**. Sonnet 4.6 at the same call
volume ≈ $5/mo.

## Decision

- **Model:** Claude Haiku 4.5, pinned to
  `claude-haiku-4-5-20251001` via `BEDROCK_MODEL_ID` (CDK context
  overridable) so an Anthropic-side point release does not silently
  change behaviour.
- **The reason string is the product.** The model returns
  `{"alert": bool, "reason": str}` and `reason` is templated verbatim
  into the alert email. That is the user value the model adds over a
  hand-coded "anomaly: fare dropped 30%" — context and tradeoff
  framing static templates cannot match.
- **`BEDROCK_MODE` env var** (`live` / `stub`) toggles between the
  real call and a deterministic stub returning
  `{"alert": True, "reason": "stub", "bedrock_called": True}`. Tests
  pin `BEDROCK_MODE=stub` in `lambdas/poller/tests/conftest.py` so the
  full poller suite never burns a real Bedrock call.
- **Defensive fallback.** Any failure (network, IAM, throttle, parse,
  malformed output) collapses to
  `{alert: False, reason: "model_*", bedrock_called: True}`. The
  metric still fires; no spurious alert reaches the user during a
  Bedrock outage.
- **Strict JSON-only parser** (`bedrock_decide._parse_response`).
  First char must be `{`, last must be `}`, top-level keys exactly
  `{alert, reason}`, `alert` is a Python `bool` (not `int`), `reason`
  is a non-empty string ≤200 chars. Any deviation routes to the
  defensive fallback.
- **Prompt-injection posture.** Provider-controlled strings (hotel
  names, airline codes) land in the user message only, never the
  system message. Sentinel-based test (`test_bedrock_decide.py`
  group E) catches any refactor that violates this.
- **IAM grant resource-scoped** to the foundation-model ARN in
  `lib/poller-server.js`. Not `bedrock:*`, not `Resource: '*'`.
- **Evals as repo artefacts.** The `evals/` package (T3) ships a
  loader, Sonnet 4.6 judge client, runner, and a 30+ case
  hand-labelled corpus (T4). Runner is local-only; CI workflow_dispatch
  is slice-9 work.

## Consequences

**Good:**

- **Written justification, not just a flag.** "Fare dropped 28% below
  the 30-day median" reads better than a fixed template, and a wrong
  call is itself a regression we can measure with the eval framework.
- **Cost is bounded and observable.** Haiku 4.5 + dedup-gate-first +
  reserved-concurrency-1 + clamped poll cadence (ADR 0003) cap the
  rate at which `bedrock:InvokeModel` can fire. AWS Budget alarm at
  $10/mo is slice-9 work but the per-call ceiling is already in
  place.
- **Tests stay cost-free.** Stub mode is the conftest default. The
  174-test poller suite plus the 106-test eval suite runs without
  touching Bedrock.
- **Defensive fallback is a known no-alert mode.** A Bedrock outage
  yields zero alerts, not bad ones. The
  `bedrock_decisions_made` metric stays honest about what the
  cascade attempted.

**Cost:**

- **p99 latency now includes Bedrock RTT.** Haiku 4.5 adds
  ~300-800ms per watch. Total wall time stays bounded by
  `lambdaTimeoutSeconds` (default 60s, clamped 30-300s).
- **One more external dependency.** Defensive fallback caps the
  blast radius at "no alert for one watch this cycle."
- **Eval discipline is a maintenance burden.** Any prompt edit in
  `bedrock_decide.py` or model-ID bump should be preceded by a local
  eval run. The README documents the loop; CI enforcement is
  slice-9.
- **Model judgement is not unit-testable.** Tests pin the parser,
  prompt-builder determinism, injection posture, and fallback paths,
  but the model's actual *call* on a snapshot is only verified at
  eval time against a finite corpus. Drift between corpus and
  production traffic is real.

**Not chosen — and why:**

- **Rules-only decision.** Rejected because the reason string is the
  product. A hand-coded template does not pick up the nuances the
  user cares about (anomaly-vs-history, under-budget framing,
  seasonal pattern hints).
- **Sonnet 4.6 for the decision call.** Rejected at personal scale —
  ~10× cost delta for marginal quality on this bounded yes/no.
  Sonnet 4.6 stays as the **judge** in evals, where stronger
  reasoning pays off for grading the under-test Haiku output.
- **Streaming Bedrock responses.** Response is ≤200 chars; streaming
  adds parser complexity for no user-visible latency win.
- **Per-user model preference.** Out of scope for v1.

## References

- Design spec §5 (alert-worthiness decision flow):
  `docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md`.
- Production-readiness companion §4.3 (evals as repo artefacts):
  `docs/superpowers/specs/2026-05-10-trip-tracker-production-readiness.md`.
- `lambdas/poller/bedrock_decide.py` — the only call site.
- `lambdas/poller/decision.py` — the router that delegates when the
  gate cascade passes.
- `lib/poller-server.js` — IAM grant + env-var wiring.
- `evals/` — loader, judge, runner, fixtures, baseline.
- Threat model boundary `[6]` — the new attack surface.
