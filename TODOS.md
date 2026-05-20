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

