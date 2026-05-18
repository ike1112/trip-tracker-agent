# TODOS

Deferred work captured during reviews. Each item has enough context to pick
up cold in 3 months.

---

## 1. Reviewer-runnable fixture-mode demo (P2)

**What:** A one-command demo (`make demo` or a documented script) that runs
the 5 design-spec §4 chat patterns end-to-end against fixture mode, plus a
README "Try it in 60 seconds, no keys" block.

**Why:** The locked objective is "a reviewer forks the repo and is convinced
it works." A reviewer running it themselves with zero setup is stronger proof
than any screenshot or Loom of the author's deploy. Highest-leverage evidence
for the actual objective.

**Pros:** Leverages existing fixture mode (no new infra). Converts passive
evidence into active proof. Small effort.

**Cons:** Adds a maintained surface (the demo script can rot if chat patterns
change). Not load-bearing for the live-run proof, so deferring it doesn't
block the launch runbook.

**Context:** Deferred during the engineering review of `docs/launch-runbook.md`
(2026-05-16) so the proof-gap scope stayed tight. Fixture mode already exists
(ADR 0002); the 5 patterns are enumerated in
`docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md` §4. Start
from `evals/run_evals.py` (already drives fixture flows) — the demo can be a
thin, human-readable wrapper. Reference it from the README first-screen layout
slot reserved in the launch runbook Phase 5.

**Depends on / blocked by:** Nothing. Independent of the live run.

---

## 2. Fix stale model name in the design spec (P2)

**What:** `docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md` §4
states the chat agent is "Claude Sonnet 4.6." The deployed default is
`us.anthropic.claude-3-5-haiku-20241022-v1:0` (`lib/agent.js:13`,
`DEFAULT_AGENT_BEDROCK_MODEL_ID`). Reconcile the spec to the code (or document
why they differ and what the intended production model is).

**Why:** The objective is a hiring artifact. A reviewer who reads the design
spec and then the code finds a direct contradiction on a load-bearing claim
(which model powers the agent). That reads as "the docs lie," which is worse
than having no spec. Cheap to fix, disproportionate credibility cost if left.

**Pros:** Removes a concrete contradiction a careful reviewer will catch.
Forces an explicit decision: is 3.5 Haiku the intended agent model, or is
Sonnet 4.6 the target and the default is a cost placeholder? Either answer,
stated, is fine — the silent mismatch is the problem.

**Cons:** None of substance. ~5 minutes. Only risk is fixing the prose
without deciding the actual intent — capture the *decision*, not just a word
swap.

**Context:** Found during the engineering review of `docs/launch-runbook.md`
(2026-05-16) while pulling exact Bedrock model IDs for Phase 0. The poller
decision model (`lambdas/poller/bedrock_decide.py:40`,
`claude-haiku-4-5-20251001`) is a separate ID in a different format and is
*not* stale — only the agent/chat model claim in §4 is wrong. The launch
runbook Phase 0 already instructs the operator to trust the source files over
the spec, so this is not blocking the live run — it's a standalone doc-accuracy
fix for the portfolio read.

**Depends on / blocked by:** Nothing.

