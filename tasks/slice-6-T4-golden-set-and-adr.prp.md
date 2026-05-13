# PRP: Slice 6 Task 4 — Golden set + baseline report + ADR 0004 + threat-model row

**Source-of-truth narrative:** [`tasks/slice-6-bedrock-decision.plan.md`](./slice-6-bedrock-decision.plan.md) §4.4 Task 4. This PRP is the executable artifact paired with that section.

**Prior commits this PRP builds on:**
- `16b6a96` (T1) — `bedrock_decide.py` + 39 tests.
- `5e5a49e` (T2) — `decision.py` wired + CDK IAM.
- `ce13c80` — pre-flight comment cleanup. **Inherited rule: no slice/T# refs.**
- `cc9e9ae` — todo sync.
- `88a57e3` (T3) — `evals/` package (loader + judge + report + runner) + 106 tests.

**Confidence:** **9/10** — content-heavy task with mechanically checkable gates.

---

## 1. Summary

Close out slice 6 by landing the artefacts that prove the decision-quality eval framework actually works: a 30-case hand-labelled golden set, a committed baseline report from the stub-mode runner over that set, ADR 0004 documenting the Bedrock-decision choice, and a threat-model row covering the new attack surface (prompt injection through provider strings + cost runaway via the IAM grant).

## 2. Files to create

| File | Purpose |
|---|---|
| `evals/fixtures/decision/0004-...json` ... `evals/fixtures/decision/0033-...json` | 30 hand-labelled fixtures: 15 with `expected_alert=true`, 15 with `expected_alert=false`. Schema matches `evals/loader.py`. Names: `NNNN-{alert\|no-alert}-{short-tag}.json`. |
| `evals/results/2026-05-13-baseline.md` | Committed sample run output from the stub-mode runner against the full corpus (all 33 fixtures = the initial 3 from T3 + 30 new). |
| `docs/adr/0004-bedrock-decision.md` | New ADR. Same Context / Decision / Consequences structure + depth as ADR 0001, 0002, 0003. |

## 3. Files to update

| File | Change |
|---|---|
| `docs/adr/README.md` | Flip the ADR 0004 row from `(planned)` to `Accepted` referencing this slice. Match the formatting of the existing 0001-0003 rows. |
| `docs/threat-model.md` | Append a new row for the Bedrock attack surface. Cover: prompt injection via `bestOfferBlob` (`hotelName`, `airline`), cost runaway via `bedrock:InvokeModel`, model-output validation as the defence layer. Use the same row format as existing rows `[3]`, `[3b]`, `[5]`. |

## 4. Fixture-authoring requirements

The corpus must be **realistic, not random.** Each fixture needs:

- **Snapshot shape** matching `lambdas/poller/snapshot.py:161-222` (`compose_snapshot` output). Include `watchId`, `timestamp`, `totalPrice` / `flightPrice` / `hotelPrice` (Decimal-as-string), full `bestOfferBlob` with all flight + hotel fields. Use distinct realistic-looking airlines (UA, NH, JL, AA, DL, BA, etc.), real-world destinations (Tokyo, Paris, NYC, London, etc.), and plausible Duffel-shaped offers.
- **Watch shape** matching `make_watch` in `lambdas/poller/tests/conftest.py:265-301`. Vary `maxTotalPrice`, `pax`, `preferences.maxStops`, `preferences.hotelMinStars`, sometimes set `lastAlertedAt` / `lastAlertedPrice` to exercise dedup blocking.
- **History shape**: 3-30 rows, each with a `totalPrice` Decimal-as-string. The history shape MUST motivate the `expected_alert` label — if labelled `expected_alert=true` due to anomaly, the snapshot total must actually be ≪ the 30-day median; if labelled `expected_alert=false` due to dedup, `lastAlertedAt` must be recent AND `lastAlertedPrice` close to snapshot total.
- **`expected_reason_themes`**: 2-4 short phrases that a reasonable judge would expect the model to touch. E.g., `["anomaly", "below median", "rare drop"]` for a clear anomaly case; `["over budget", "no anomaly"]` for a stable-above-budget case; `["dedup", "recent alert"]` for a dedup-blocked case.
- **`notes`**: one-paragraph human explanation of why this case is what it is.

**Coverage matrix — make sure the corpus exercises each row:**

| Bucket | Expected label | Reason class | Min cases |
|---|---|---|---|
| Clear anomaly (snapshot ≤ 60% of 30-day median) | alert | anomaly | 4 |
| Strong threshold pass (snapshot ≤ 80% of budget, no recent alert) | alert | under budget | 4 |
| Borderline anomaly + threshold (both signals weak but combined justify) | alert | combined signal | 2 |
| Anomaly + below budget (overlapping signals) | alert | budget + anomaly | 3 |
| Edge: snapshot equals exactly the previous min (no improvement) | alert | new-low equal | 2 |
| Dedup-blocked (recent alert at similar price) | no alert | dedup | 4 |
| Above budget (snapshot > maxTotalPrice) | no alert | over budget | 4 |
| No anomaly + no budget headroom | no alert | both gates miss | 3 |
| Stable fare over long horizon (history all within ±2%) | no alert | fare stable | 2 |
| Hotel/flight split where flight is good but hotel is bad pushing total over | no alert | hotel disqualifies | 2 |

Total: **30 fixtures**, file IDs `0004-0033`.

## 5. ADR 0004 requirements

Structure (mirror ADR 0001, 0002, 0003):

1. **Title:** `0004 — Bedrock decision: Haiku 4.5 as the alert-worthiness oracle, with eval-driven validation`
2. **Status:** `Accepted (slice 6, 2026-05-13)` — use this exact phrasing once committed.
3. **Context** — answer:
   - Why does the poller make an alert-worthiness call at all when the gates already filter?
   - Why a model and not a rule? What's the value the model adds over `if anomaly: alert`?
   - Why Haiku 4.5 specifically? Cost / latency / capability tradeoffs.
   - What's the cost surface (~$0.30/mo at personal scale based on Bedrock pricing for Haiku 4.5 at ~1500 tokens/poll × 4h cadence)?
4. **Decision** — answer:
   - The model produces `{alert: bool, reason: str}` where the `reason` string is the **user-visible value** of the alert email body, not just a flag.
   - `BEDROCK_MODE` env var (live/stub) toggles the call so tests/CI stay cost-free.
   - `BEDROCK_MODEL_ID` env var pins the exact model so an Anthropic-side rev doesn't silently change behaviour.
   - Defensive fallback: any failure (parse, network, throttle, IAM) → `{alert: False, reason: "model_*"}` so a Bedrock outage doesn't spam users.
   - Evals as repo artefacts (`evals/`) — 30+ hand-labelled cases + Sonnet 4.6 judge so prompt/model changes can be measured locally before they ship.
   - IAM grant resource-scoped to the foundation-model ARN, not `bedrock:*` and not `Resource: *`.
5. **Consequences** — answer:
   - What changes for production? (~$0.30/mo cost line, p99 latency now includes Bedrock RTT, defensive-fallback path is a known no-alert mode.)
   - What changes for development? (Need `ANTHROPIC_API_KEY` to run live evals; stub-mode runner needs nothing.)
   - Known failure modes: prompt-injection via provider strings (mitigated by user-role-only interpolation + model-output validation), model drift (mitigated by pinning ID + eval re-runs), cost runaway (mitigated by reserved concurrency = 1 + clamped poll cadence).
   - Open carryovers for slice 9: Bedrock-on-CI gating, cost-alarm in CDK.

Length: roughly the median of ADR 0001 + ADR 0002 + ADR 0003 (read them first, match their depth).

## 6. Threat-model row

Append (or extend if `[5]` already covers it) to `docs/threat-model.md`. The format of existing rows is the source of truth — look at `[3]`, `[3b]`, `[5]` and match. Include:

- **Boundary:** "Bedrock InvokeModel call (poller → bedrock-runtime)"
- **Attack vector(s):** prompt injection via `bestOfferBlob.hotelName` / `airline` controlled by upstream providers (Duffel, LiteAPI); cost runaway via abuse of the IAM grant; model-output exfiltration (low — Haiku has no tool-call surface in this flow); model drift causing silent alert-quality regression.
- **Defences in place:** provider strings restricted to user role only (tested in `bedrock_decide` group E sentinel test); model-output validated against `{alert, reason}` JSON contract; IAM resource-scoped to model ARN; reserved concurrency = 1 + clamped poll cadence (15–1440 min); defensive fallback collapses any failure to no-alert.
- **Residual risk:** judge model could be manipulated by a malicious `reason` string from the under-test model (acknowledged in `evals/judge_client.py` docstring); model drift surfaces only on next eval run, not in real time.

## 7. Baseline report generation

After the 30 fixtures are written, generate the baseline by running:

```bash
BEDROCK_MODE=stub \
  "C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" \
  "C:/Users/isabe/Downloads/trip-tracker-agent/evals/run_evals.py" \
    --fixtures-dir C:/Users/isabe/Downloads/trip-tracker-agent/evals/fixtures/decision \
    --out C:/Users/isabe/Downloads/trip-tracker-agent/evals/results/2026-05-13-baseline.md \
    --stub
```

The baseline shows the **stub judge's verdict** (label match) rather than a real Sonnet 4.6 judge call — slice 6 commits this as a sanity-check artefact, not as ground truth. T9 / slice 9 can regenerate with the live judge once `ANTHROPIC_API_KEY` is wired into a CI runner.

Add a one-paragraph preamble to the baseline report (above the auto-generated header) explaining: `> Generated with --stub. The runner exercised every fixture through bedrock_decide (BEDROCK_MODE=stub, so every actual.alert is True), then the stub judge graded label match. Live-judge runs require ANTHROPIC_API_KEY and will replace this file when CI lands the workflow_dispatch trigger.`

**Expected baseline result:** stub bedrock returns `alert=True` for all cases. Stub judge produces `pass` iff `expected_alert == true`, `fail` iff `expected_alert == false`. So the baseline should show **18 pass** (15 alert-labelled + 3 initial alert-labelled) and **15 fail** (15 no-alert-labelled, since the stub model's blanket True does not match). Exit code 1.

## 8. Validation gates (Ralph executes; all must pass)

### Gate 1 — Fixture count and label split

```bash
ls C:/Users/isabe/Downloads/trip-tracker-agent/evals/fixtures/decision/*.json | wc -l   # must be 33 (3 initial + 30 new)
```

Plus count expected_alert distribution: 18 true, 15 false (3 of the initial 3 were true, true, true; the 30 new are 15+15).

### Gate 2 — Schema validity

```bash
"C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" -c "
import sys
sys.path.insert(0, 'C:/Users/isabe/Downloads/trip-tracker-agent')
from evals.loader import load_fixtures
fxs = load_fixtures('C:/Users/isabe/Downloads/trip-tracker-agent/evals/fixtures/decision')
print('count', len(fxs))
print('alert_true', sum(1 for f in fxs if f.expected_alert))
print('alert_false', sum(1 for f in fxs if not f.expected_alert))
"
```

EXPECT: `count 33`, `alert_true 18`, `alert_false 15`.

### Gate 3 — No regression in evals tests or poller tests

```bash
"C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" -m pytest C:/Users/isabe/Downloads/trip-tracker-agent/evals/tests/ C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/poller/tests/ -q
```

EXPECT: 106 + 174 = 280 passing.

### Gate 4 — Baseline runner output

Run the baseline command from §7. EXPECT: exit code 1 (because there are no-alert fixtures the stub will mismatch), report file exists, contains "Pass | 18" and "Fail | 15" lines.

### Gate 5 — Comment cleanliness across all new docs

```bash
rg -n --no-heading 'slice[ -_]?\d|\bT[1-5]\b|\bTask [1-5]\b|[Ss]lice-\d' \
  C:/Users/isabe/Downloads/trip-tracker-agent/evals/fixtures/decision/0004-* \
  C:/Users/isabe/Downloads/trip-tracker-agent/evals/fixtures/decision/0005-* \
  C:/Users/isabe/Downloads/trip-tracker-agent/evals/results/2026-05-13-baseline.md \
  C:/Users/isabe/Downloads/trip-tracker-agent/docs/adr/0004-bedrock-decision.md \
  2>&1 || true
```

EXPECT: zero matches in the fixtures and baseline report. The ADR is **allowed** to reference "slice 6" once in its Status line (`Accepted (slice 6, 2026-05-13)`) because ADRs are durable historical records. The threat-model can reference slice numbers in its row tags (`[5]`, `[6]`) because those are markdown anchors. The ADR README row may say `slice 6`.

For the fixtures specifically: zero matches.

### Gate 6 — ADR 0004 well-formed

- Exists at `docs/adr/0004-bedrock-decision.md`.
- Has sections: `## Context`, `## Decision`, `## Consequences`.
- Length: 1500-3500 chars (in the same ballpark as ADR 0003).
- Status line says `Accepted (slice 6, 2026-05-13)`.

### Gate 7 — ADR README + threat-model updated

- `docs/adr/README.md` lists row for 0004 as `Accepted` (not `planned`).
- `docs/threat-model.md` has a new section/row describing Bedrock attack surface.

## 9. Constraints inherited

- Multi-model gate: **none** during T4 itself — Checkpoint B is run by the parent agent after T4 commits. Don't spawn reviewers from inside this PRP.
- All new content must avoid `slice X` / `T#` / `Task N` refs (with the ADR-status-line exception above).
- No nonsense filler ("basically", "just simply", "obviously", "essentially").
- No emojis.
- Use `.venv-tests/Scripts/python.exe` for all pytest runs.

## 10. Step-by-step

1. **Write the 30 fixtures.** Mind the coverage matrix in §4. Validate by running Gate 2.
2. **Write `docs/adr/0004-bedrock-decision.md`.** Read 0001-0003 first to match their style.
3. **Update `docs/adr/README.md`** — flip the 0004 row.
4. **Update `docs/threat-model.md`** — add the new row.
5. **Generate the baseline:** run the command from §7. Add the preamble paragraph.
6. **Run all 7 gates.** Fix anything that fails.
7. **STOP.** Don't commit — parent agent reviews and commits.

## 11. NOT building

- Live-judge baseline report (deferred until CI / `ANTHROPIC_API_KEY` is wired in slice 9).
- Cost-alarm CDK construct (slice 9).
- ADRs 0005-0007 (later slices).
- Bedrock-on-CI gating (slice 9).
