# PRP: Slice 6 Task 3 — Eval scaffolding (`evals/` package + runner + tests)

**Source-of-truth narrative:** [`tasks/slice-6-bedrock-decision.plan.md`](./slice-6-bedrock-decision.plan.md) §4.3 Task 3 (lines 200–225). This PRP fleshes that task into a Ralph-executable artifact — code patterns, file outputs, validation gates. It does **not** redesign the task.

**Prior commits this PRP builds on:**
- `16b6a96` — T1, `bedrock_decide.py` module + 39 tests.
- `5e5a49e` — T2, `decision.py` wired to `bedrock_decide`, CDK IAM, conftest stub mode.
- `ce13c80` — pre-flight cleanup, stripped slice/T# refs from source comments. **All new code in this PRP inherits this rule.**

**Confidence score:** **8/10** for one-pass Ralph execution. Main risk is the judge-client design — see §10 Open Questions.

---

## 1. Summary

Land the `evals/` Python package that runs trip-tracker's decision-quality evaluation locally: load JSON fixtures, call `bedrock_decide.decide()` for each, ask Claude Sonnet 4.6 to judge the model's `{alert, reason}` against expected behaviour, write a markdown report, exit non-zero if any fixture fails. Ship 2–3 initial fixtures so the runner is testable end-to-end; the full 30-case golden set arrives in T4.

## 2. Problem statement

Slice 6 T1+T2 swapped the alert-decision stub for a real Bedrock Haiku 4.5 call (`bedrock_decide.decide`). There's currently no way to detect regressions in the model's behaviour — no way to answer "did this prompt change make the alerts better or worse." T3 builds the loop.

## 3. Solution statement

A standalone Python package `evals/` that:
1. Reuses `bedrock_decide.decide` as the unit under test (same code path production uses; honours `BEDROCK_MODE`).
2. Uses the Anthropic SDK directly for the Sonnet 4.6 judge call (off-Lambda script, no need for Bedrock here — the project already mixes provider SDKs).
3. Renders a deterministic markdown report (per-case verdict + judge rationale + overall pass/fail counts).
4. Returns a non-zero exit code when any fixture fails, so a future CI workflow_dispatch (slice 9) can gate on it.
5. Is fully tested without making any real network calls — both `bedrock_decide.decide` (via `BEDROCK_MODE=stub`) and the judge client (via a `--stub` flag) are stubbable.

## 4. Metadata

| Field | Value |
|---|---|
| Type | NEW_CAPABILITY |
| Complexity | MEDIUM |
| Systems Affected | new `evals/` package; no changes to `lambdas/`, `lib/`, or `docs/` |
| New deps | `anthropic` (Python SDK) — pinned in `evals/requirements.txt` |
| Estimated tasks | 8 sub-tasks (test-design gate → loader → judge client → report → runner → fixtures → e2e gate → final verification) |
| Test target | All new tests pass; full poller suite (174) still green |

## 5. UX design

### Before state (today, after T2)

```
╔═══════════════════════════════════════════════════════════════════════════════╗
║                              BEFORE STATE                                      ║
╠═══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║   ┌──────────────┐     ┌──────────────────┐     ┌──────────────┐             ║
║   │  developer   │ ──► │ tweaks prompt in │ ──► │  ¯\_(ツ)_/¯  │             ║
║   │              │     │ bedrock_decide.py│     │  push & pray │             ║
║   └──────────────┘     └──────────────────┘     └──────────────┘             ║
║                                                                               ║
║   USER_FLOW: edit → unit tests → commit → discover regressions in prod.       ║
║   PAIN_POINT: no measurement loop; unit tests pin the parser, not the model.  ║
║   DATA_FLOW: model output vanishes into CloudWatch — never graded.            ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝
```

### After state (after this PRP lands)

```
╔═══════════════════════════════════════════════════════════════════════════════╗
║                               AFTER STATE                                      ║
╠═══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║   ┌────────────┐  ┌───────────────────────┐  ┌─────────────┐  ┌────────────┐ ║
║   │ developer  │─►│ python evals/run.py   │─►│ Haiku 4.5   │─►│ Sonnet 4.6 │ ║
║   │ tweaks     │  │  --fixtures-dir       │  │ (under test)│  │  (judge)   │ ║
║   │ prompt     │  │   evals/fixtures/...  │  └─────────────┘  └────────────┘ ║
║   └────────────┘  └───────────────────────┘            │             │       ║
║                              │                          ▼             ▼       ║
║                              ▼                  ┌─────────────────────────┐  ║
║                   ┌──────────────────────┐      │ evals/results/<date>.md │  ║
║                   │ exit 0 (all passed)  │ ◄────│ + per-case verdict       │  ║
║                   │ exit 1 (any failed)  │      │ + judge rationale        │  ║
║                   └──────────────────────┘      └─────────────────────────┘  ║
║                                                                               ║
║   USER_FLOW: edit → `make evals` → read report → commit only if green.        ║
║   VALUE_ADD: prompt regressions caught locally, before they ship.             ║
║   DATA_FLOW: fixture → bedrock_decide → judge → markdown → exit code.         ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝
```

### Interaction changes

| Location | Before | After | Developer impact |
|---|---|---|---|
| Tweaking `bedrock_decide.py` | Push and pray | `python evals/run_evals.py --fixtures-dir evals/fixtures/decision --out evals/results/dryrun.md` runs the corpus | Local regression signal before commit |
| Adding a fixture | N/A | Drop a JSON file in `evals/fixtures/decision/` matching the schema in `evals/README.md` | Corpus growth is one-file diffs |

---

## 6. Mandatory reading (Ralph must read these before drafting)

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 | `lambdas/poller/bedrock_decide.py` | 1–237 (whole file) | The module the runner imports + invokes. `decide()` signature and return shape are the contract. |
| P0 | `lambdas/poller/snapshot.py` | 161–222 (`compose_snapshot`) | Defines the snapshot dict shape fixtures must construct. |
| P0 | `lambdas/poller/tests/conftest.py` | 150–301 (`make_flight_offer`, `make_hotel_offer`, `make_watch`, `_dec`) | Source of truth for fixture-construction helpers. The runner's `decision/*.json` fixtures should be the *output* shape of `compose_snapshot`, not raw provider responses. |
| P0 | `lambdas/poller/tests/test_bedrock_decide.py` | 1–120 | Testing convention — `_snap()`, `_hist()` helpers, autouse fixture for env reset, `MemoryLogHandler` import from conftest. Mirror this pattern in `evals/tests/`. |
| P1 | `lambdas/poller/decision.py` | 1–50 | Confirms `bedrock_decide.decide(snapshot, watch, history)` is the public surface. |
| P1 | `docs/superpowers/specs/2026-05-08-trip-tracker-agent-design.md` §6 | grep `^## 6\.` | Evals repo layout (fixtures/judge_prompts/run_evals.py/results/). Locked. |
| P1 | `docs/superpowers/specs/2026-05-10-trip-tracker-production-readiness.md` §4.3 | grep `^### 4\.3` | `evals/results/2026-05-XX-baseline.md` is committed; `make evals` is the local runner; CI is `workflow_dispatch`-only (slice 9, not this PRP). |
| P2 | `lambdas/poller/requirements.txt` + `dev-requirements.txt` | all | Pin style: `package==version`. The `evals/requirements.txt` we create must match. |

**External documentation:**

| Source | Section | Why |
|---|---|---|
| [Anthropic Python SDK](https://docs.anthropic.com/en/api/client-sdks#python) | `Quickstart` + `Models` | Pattern for `client.messages.create(model=..., system=..., messages=...)`. The judge calls Sonnet 4.6 (`claude-sonnet-4-6`) with a short rubric system prompt + the case-under-judgment in the user message. |
| [Anthropic Python SDK error types](https://docs.anthropic.com/en/api/errors) | Exception classes | Map `anthropic.APIError` subclasses to a runner-level fallback verdict (treat as "judge unavailable" — report it, exit non-zero). |

---

## 7. Patterns to mirror

### NAMING + DOCSTRING_HEADER (from `bedrock_decide.py:1-24`)

```python
"""
<one-line module purpose>.

Owns <noun>:
  - <responsibility 1>
  - <responsibility 2>
  - ...
Modes — <if applicable>:
  - <mode-a>: <behaviour>
  - <mode-b>: <behaviour>

<Notable invariant or design constraint>.
"""
```

**Use this exact structure** for `evals/run_evals.py`, `evals/loader.py`, `evals/judge_client.py`, `evals/report.py`.

### TEST_HELPER_HEADER (from `tests/test_bedrock_decide.py:1-20`)

```python
"""Behavioural tests for <module>.<function>() — covers <list of concerns,
comma-separated>. <Notable invariant>. No real <external system> calls fire —
<stubbing strategy>.

Test groups (referenced by name prefix in this file):
  A: <group name>
  B: <group name>
  ...
"""
```

### TEST_REIMPORT_FIXTURE (from `tests/test_bedrock_decide.py:39-62`)

```python
def _import_module(env_var: str | None = "stub"):
    """Reimport `<module>` with the given env. Reset on each test via the
    autouse fixture below."""
    if env_var is None:
        os.environ.pop("ENV_NAME", None)
    else:
        os.environ["ENV_NAME"] = env_var
    sys.modules.pop("<module>", None)
    return importlib.import_module("<module>")


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Save/restore env so reimports don't leak across tests."""
    saved = {k: os.environ.get(k) for k in ("ENV_NAME",)}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    sys.modules.pop("<module>", None)
```

### DEFENSIVE_FALLBACK (from `bedrock_decide.py:79-86, 215-236`)

```python
def _fallback(reason_code: str) -> dict:
    return {"alert": False, "reason": reason_code, "bedrock_called": True}

try:
    response = _call_external()
except Exception as e:
    logger.warning("<event_name>", extra={"error": type(e).__name__, "error_msg": str(e)[:200]})
    return _fallback(_REASON_FAILED)
```

**Apply to:** the judge client (any Anthropic SDK exception → log + return "judge unavailable" verdict, runner downgrades to non-zero exit).

### DETERMINISTIC_RENDERING (from `bedrock_decide.py:124-127`)

```python
prefs = watch.get("preferences") or {}
prefs_str = json.dumps(prefs, sort_keys=True) if prefs else "{}"
```

**Apply to:** the markdown report writer — every dict serialised must be `json.dumps(..., sort_keys=True)` so report text is byte-stable across runs with identical fixtures, which makes the report writer testable by exact-string comparison.

### LOGGING (`bedrock_decide.py:37`)

```python
from aws_lambda_powertools import Logger
logger = Logger(service="trip-tracker-poller")
```

**Apply to:** runner modules use **plain `logging`** instead — powertools is a Lambda dependency, not appropriate for an off-Lambda CLI. Pattern:

```python
import logging
logger = logging.getLogger("evals.<module-leaf>")  # 'evals.loader', 'evals.judge_client', etc.
```

`run_evals.py` configures the root handler once at CLI entry.

---

## 8. Files to create

| File | Action | Justification |
|---|---|---|
| `evals/__init__.py` | CREATE (empty) | Make `evals` an importable package so tests can `from evals.run_evals import main`. |
| `evals/requirements.txt` | CREATE | Pin `anthropic` SDK version. Separate from `lambdas/poller/requirements.txt` because evals are dev-only. |
| `evals/loader.py` | CREATE | Load + validate JSON fixtures from a directory. Schema validation by hand (no jsonschema dep — kept minimal). |
| `evals/judge_client.py` | CREATE | Wraps the Anthropic SDK Sonnet 4.6 call. Stub mode returns a deterministic local verdict (compares `actual_alert == fixture.expected_alert`). |
| `evals/judge_prompts/decision.md` | CREATE | Rubric the judge applies. Plain markdown loaded as a string. |
| `evals/report.py` | CREATE | Markdown report writer. Pure function: `(run_metadata, list[CaseResult]) -> str`. |
| `evals/run_evals.py` | CREATE | CLI entrypoint (argparse). Orchestrates loader → bedrock_decide → judge_client → report → exit code. |
| `evals/README.md` | CREATE | How to run, cost note (~$0.05 per 30-case run on Sonnet 4.6), when to re-run, fixture schema. |
| `evals/fixtures/decision/0001-no-alert-stable-fare.json` | CREATE | Initial fixture: fare unchanged for 30 days, no anomaly, well over budget. `expected_alert=false`. |
| `evals/fixtures/decision/0002-alert-clear-anomaly.json` | CREATE | Initial fixture: snapshot total is 40% below 30-day median. `expected_alert=true`. |
| `evals/fixtures/decision/0003-alert-under-budget-fresh.json` | CREATE | Initial fixture: under budget by 25%, no prior alerts, no dedup conflict. `expected_alert=true`. |
| `evals/tests/__init__.py` | CREATE (empty) | Make `evals.tests` a package. |
| `evals/tests/conftest.py` | CREATE | Sets `BEDROCK_MODE=stub` + `ANTHROPIC_API_KEY=test-key-stub` at module load. Mirrors `lambdas/poller/tests/conftest.py:25` pattern. |
| `evals/tests/test_loader.py` | CREATE | Loader tests — valid fixture round-trip, missing key, unknown key, empty dir, deterministic ordering. |
| `evals/tests/test_judge_client.py` | CREATE | Judge tests — stub returns correct verdict on label match + mismatch, live mode formats prompt correctly (mocked `anthropic.Anthropic`), API error returns "judge_unavailable" verdict. |
| `evals/tests/test_report.py` | CREATE | Report tests — every section present, per-case verdicts shown, byte-stable across runs (regen → diff is empty). |
| `evals/tests/test_run_evals.py` | CREATE | End-to-end tests of `main()` — all-pass corpus exits 0, any-fail corpus exits 1, judge-unavailable exits 1, fixture-load failure exits 1, generated report file is non-empty markdown. |

**Files to UPDATE:** *(none — this PRP is greenfield)*. `lambdas/poller/dev-requirements.txt` deliberately untouched; eval deps live in `evals/requirements.txt`.

---

## 9. NOT building (scope limits)

| Out of scope | Where it lands |
|---|---|
| Full 30-case golden set (only 3 initial fixtures here) | T4 |
| `docs/adr/0004-bedrock-decision.md` | T4 |
| `docs/threat-model.md` Bedrock row | T4 |
| `evals/results/2026-05-10-baseline.md` (a committed sample run) | T4 |
| CI workflow_dispatch trigger | Slice 9 |
| Chat-pattern eval fixtures (`evals/fixtures/chat_*`) | Out of slice 6 entirely — design-spec §6 mentions them but production-readiness §4.3 punts them to v1.5 |
| `make evals` Makefile target | Slice 9 (CI-readiness); for now invoke runner directly |
| Streaming Anthropic responses | Judge response is short JSON-shaped; non-streaming is fine |

---

## 10. Open questions (decisions Ralph must lock during the test-design gate)

1. **Judge client: Anthropic SDK vs boto3-Bedrock?**
   **Recommendation: Anthropic SDK.** Rationale: (a) off-Lambda script, no IAM context to leverage; (b) `ANTHROPIC_API_KEY` env var is a one-line setup; (c) the project already mixes provider SDKs at boundaries (Bedrock for the in-prod decision, Anthropic SDK for the dev-side judge is a defensible split — they're testing different things). If the user disagrees, switching to `boto3.client("bedrock-runtime")` + Sonnet 4.6 inference profile is a 30-line change isolated to `judge_client.py`.

2. **Runner exit-code semantics.**
   **Recommendation: exit non-zero (1) if ANY fixture fails the judge OR if the runner errors during load/parse/judge.** This matches existing pytest convention and gives the future CI workflow a clean gate.

3. **Fixture file format.**
   **Recommendation: JSON.** Native Python parser, no extra dep. Decimals serialise as strings; the loader casts to `Decimal(str(value))` per `snapshot.py:69-73`.

4. **`evals/` as proper Python package or script directory?**
   **Recommendation: proper package** (`__init__.py` present). Tests can `from evals.run_evals import main` for unit testing. Mirrors `lambdas/poller/` structure where the package importability is what makes `test_bedrock_decide.py` work.

5. **Fixture schema validation: jsonschema or hand-rolled?**
   **Recommendation: hand-rolled** (~30 lines in `loader.py`). `jsonschema` IS already in `venv-tests` but adding it to `evals/requirements.txt` increases the cost of "what's a fixture" — keep it as Python dataclass-shaped validation that fails loud and points at the specific missing key.

---

## 11. Constraints inherited from prior work

| Constraint | Source | Where Ralph applies it |
|---|---|---|
| Multi-model gate: test-engineer BEFORE writing runner tests | `slice-6-bedrock-decision.plan.md` §5 + memory `feedback_multi_model_workflow` | Task 1 below |
| All tests assert real behaviour (no placeholder / does-not-raise) | memory `feedback_meaningful_tests` | Every test file |
| No `slice X` / `T#` / `Task N` refs in comments | commit `ce13c80` | All new files |
| No nonsense filler words ("basically", "just simply", "obviously", "essentially") | this PRP, §13 validation gate | All new files |
| `.venv-tests` interpreter for pytest | parent agent | All validation commands below |
| Sequential reviewer subagents, not parallel | memory `feedback_subagents_sequential` | Checkpoint A (run by parent agent after this PRP completes) |
| `BEDROCK_MODE=stub` convention | `conftest.py:25` | `evals/tests/conftest.py` |

---

## 12. Step-by-step tasks (Ralph executes top-to-bottom)

### Task 1 — Test-design gate (BEFORE writing any code)

**ACTION**: Spawn `agent-skills:test-engineer` (Sonnet) with the following brief:

> Design the test matrix for the `evals/` package described in `tasks/slice-6-T3-evals-scaffold.prp.md` §8 file list. For each of the four production modules (`loader.py`, `judge_client.py`, `report.py`, `run_evals.py`), enumerate test groups (A, B, C…) with at least one assertion per group on real behaviour — no `does-not-raise` smoke. Note any fixture shapes needed. Return the matrix as a tree of `test_<name>` function names grouped by file and group. Do NOT write the test code — only the matrix.

**VALIDATE**: Test matrix received and includes:
- `loader.py`: valid round-trip, missing key, unknown key, empty dir, deterministic ordering (alphabetical by filename).
- `judge_client.py`: stub-mode label-match → "pass", stub-mode label-mismatch → "fail", live-mode prompt formatting with mocked `anthropic.Anthropic`, API error → "judge_unavailable".
- `report.py`: section presence, per-case verdict text, byte-stability under re-render.
- `run_evals.py`: all-pass → exit 0, any-fail → exit 1, judge-unavailable → exit 1, missing fixtures dir → exit 1, report file written.

If the matrix omits any of the above, push back and require it.

### Task 2 — `evals/__init__.py` + `evals/requirements.txt`

**ACTION**: CREATE both files.

```python
# evals/__init__.py
"""Trip-tracker decision-quality evaluation harness — local-only.

See `evals/README.md` for usage. Not deployed; lives in the repo so
prompt and model changes can be measured before they ship.
"""
```

```
# evals/requirements.txt
# Install:  pip install -r evals/requirements.txt -r lambdas/poller/dev-requirements.txt
anthropic==0.39.0
```

**VALIDATE**: `"C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/pip.exe" install -r evals/requirements.txt` succeeds.

### Task 3 — `evals/loader.py`

**ACTION**: CREATE. Module surface:
- `load_fixtures(path: pathlib.Path) -> list[Fixture]` — sorted alphabetically by filename, raises `FixtureError` with the file path + missing key on schema violation.
- `Fixture` is a `@dataclass(frozen=True)` with fields: `case_id: str`, `notes: str`, `snapshot: dict`, `watch: dict`, `history: list[dict]`, `expected_alert: bool`, `expected_reason_themes: list[str]`.
- Numeric fields (`totalPrice`, `flightPrice`, `hotelPrice`, `maxTotalPrice`) are loaded as `Decimal(str(...))` so the runner can pass them straight to `bedrock_decide.decide` without type drift.

**MIRROR**: `lambdas/poller/snapshot.py:69-73` for the `Decimal(str(...))` pattern.

**GOTCHA**: JSON numbers come through as `int`/`float`. Stringify before `Decimal` to avoid float imprecision.

**VALIDATE**: `pytest evals/tests/test_loader.py -q` — written in Task 6 after the test matrix is locked.

### Task 4 — `evals/judge_prompts/decision.md`

**ACTION**: CREATE. Rubric the judge applies. Plain markdown loaded as a string by `judge_client.py`. Structure:

```markdown
# Decision-quality judge rubric

You are evaluating whether trip-tracker's alert-decision model behaved correctly
for one fixture. The model produced `{"alert": bool, "reason": str}`. The fixture
carries `expected_alert: bool` and `expected_reason_themes: list[str]`.

Grade the model's output on two axes:

1. **Alert correctness** — does `actual_alert == expected_alert`?
2. **Reason quality** — does the model's `reason` string touch at least one of
   the `expected_reason_themes`, in spirit? (Themes are short phrases the
   author hand-labelled; the model's exact wording will differ.)

Output strict JSON:

    {"verdict": "pass"|"fail", "rationale": "<one short sentence>"}

`pass` requires BOTH axes correct. Any other state is `fail`.
```

**VALIDATE**: file exists, ≥ 200 bytes, is valid markdown.

### Task 5 — `evals/judge_client.py`

**ACTION**: CREATE. Module surface:
- `judge(fixture: Fixture, actual: dict, *, stub: bool, model: str = "claude-sonnet-4-6") -> JudgeResult`.
- `JudgeResult` dataclass: `verdict: Literal["pass", "fail", "judge_unavailable"]`, `rationale: str`.
- `stub=True` path: deterministic — `verdict = "pass"` iff `actual["alert"] == fixture.expected_alert`, else `"fail"`. `rationale` mentions which axis failed.
- `stub=False` path: read `decision.md` rubric → call `anthropic.Anthropic().messages.create(model=model, system=<rubric>, messages=[{"role": "user", "content": <fixture+actual as JSON>}])` → parse strict JSON response. Same strict-JSON parser as `bedrock_decide._parse_response` — copy that approach (refuses markdown fences, extra/missing keys, wrong types).
- Any `anthropic.APIError` (or any other exception in the SDK call): log WARNING + return `JudgeResult(verdict="judge_unavailable", rationale="<error type>: <truncated message>")`. Never raises out of `judge()`.

**MIRROR**: `bedrock_decide.py:157-190` (`_parse_response`) for the strict-JSON parser. `bedrock_decide.py:79-86` for the fallback shape.

**GOTCHA**: `anthropic.Anthropic()` reads `ANTHROPIC_API_KEY` from env automatically; tests don't need to inject the key explicitly as long as conftest sets a stub value.

**VALIDATE**: `pytest evals/tests/test_judge_client.py -q`.

### Task 6 — `evals/tests/conftest.py` + the four test files

**ACTION**: CREATE all five files following the test matrix from Task 1.

`evals/tests/conftest.py`:

```python
"""Test fixtures for the evals package.

Sets BEDROCK_MODE=stub so the under-test bedrock_decide call never burns a
real Bedrock invocation. Sets ANTHROPIC_API_KEY to a stub value so the
anthropic SDK can be imported without an env-var assertion firing; live
network calls are blocked by mocking the SDK client at the call site.
"""
import os

os.environ.setdefault("BEDROCK_MODE", "stub")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-stub")
```

The four test files follow the matrix from Task 1. **Every test asserts on observable state** (returned values, file contents, exit codes) — no `does-not-raise` smoke. Mirror the autouse env-reset fixture pattern from `tests/test_bedrock_decide.py:52-62` wherever a test mutates env vars.

**GOTCHA**: `lambdas/poller/` is not on the default Python path for tests in `evals/tests/`. The runner needs to add it. Either:
- (a) `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambdas" / "poller"))` at the top of `evals/run_evals.py`, or
- (b) configure `pytest.ini` (or `pyproject.toml`) to add it via `pythonpath`.

**Recommend (a)** — keeps the runner self-contained and the test config minimal. Document this in `evals/README.md`.

**VALIDATE**: `pytest evals/tests/ -q` — all tests in the matrix pass.

### Task 7 — `evals/report.py`

**ACTION**: CREATE. Module surface:
- `render_report(run_metadata: RunMetadata, results: list[CaseResult]) -> str`.
- `RunMetadata` dataclass: `started_at: str` (ISO 8601 UTC), `model: str`, `judge_model: str`, `bedrock_mode: str`, `stub_judge: bool`.
- `CaseResult` dataclass: `case_id: str`, `expected_alert: bool`, `actual: dict` (the `bedrock_decide.decide` return), `judge: JudgeResult`.

Output structure:

```markdown
# Decision-quality evals — <ISO-8601 started_at>

| Field | Value |
|---|---|
| Model under test | <model> |
| Judge | <judge_model> |
| BEDROCK_MODE | <bedrock_mode> |
| --stub | <stub_judge> |
| Fixtures evaluated | <N> |
| Pass | <P> |
| Fail | <F> |
| Judge unavailable | <U> |

## Per-case results

### <case_id> — <verdict-emoji-or-prefix> <verdict>

- **expected_alert**: <bool>
- **actual.alert**: <bool>
- **actual.reason**: `<reason>`
- **judge rationale**: <rationale>

<repeat>
```

**MIRROR**: `bedrock_decide.py:124-127` deterministic rendering pattern — any embedded dict serialises with `json.dumps(..., sort_keys=True)` so the report is byte-stable.

**GOTCHA**: Don't use emojis — repo rule (CLAUDE.md). Use a leading text token: `[PASS]` / `[FAIL]` / `[UNAVAILABLE]`.

**VALIDATE**: `pytest evals/tests/test_report.py -q` — including a byte-stability check (render the same input twice and assert identical strings).

### Task 8 — `evals/run_evals.py` (CLI) + initial fixtures + README

**ACTION**: CREATE `evals/run_evals.py` orchestrator. Argparse surface:
```
python evals/run_evals.py
  --fixtures-dir <path>   (required)
  --out <path>            (required — markdown report destination)
  --stub                  (optional — stub the judge client; under-test bedrock_decide
                           uses BEDROCK_MODE from env regardless)
  --judge-model <id>      (optional, default 'claude-sonnet-4-6')
  --log-level <level>     (optional, default 'INFO')
```

`main()`:
1. Add `lambdas/poller/` to `sys.path` so `import bedrock_decide` works.
2. Parse argv.
3. `fixtures = load_fixtures(args.fixtures_dir)`.
4. For each fixture: call `bedrock_decide.decide(snapshot, watch, history)` → call `judge_client.judge(fixture, actual, stub=args.stub, model=args.judge_model)` → append a `CaseResult`.
5. Render report → write to `args.out`.
6. Exit code: `0` iff every `CaseResult.judge.verdict == "pass"`; else `1`.

Also CREATE the three initial fixtures (`0001-no-alert-stable-fare.json`, `0002-alert-clear-anomaly.json`, `0003-alert-under-budget-fresh.json`) following the schema enforced by `loader.py`. Use the `make_watch` / `make_flight_offer` / `make_hotel_offer` shapes from `tests/conftest.py:150-301` as the data model — but emit them as JSON so they live as data, not code.

CREATE `evals/README.md`. Sections:
- Purpose (one paragraph)
- How to run (the exact CLI invocation above)
- Cost note (Sonnet 4.6 input tokens × 30 fixtures × ~1500 tokens each ≈ $0.05 per run at current pricing — note rate may drift, link to Anthropic pricing)
- When to re-run (before any change to `bedrock_decide.py`, before any prompt edit, before any model-ID bump)
- Fixture schema (the `Fixture` dataclass shape, with one example fixture inlined)
- Stub mode (what `--stub` does; mention that `BEDROCK_MODE=stub` is independent)

**MIRROR**: README structure follows `docs/adr/0002-fixture-replay-mode.md` for tone.

**VALIDATE**: End-to-end gate (see §13).

---

## 13. Validation gates (Ralph runs these; ALL must pass before declaring T3 complete)

### Gate 1 — Unit tests of the evals package

```bash
"C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" \
  -m pytest C:/Users/isabe/Downloads/trip-tracker-agent/evals/tests/ -q
```

**EXPECT**: All evals tests pass. No tests skipped.

### Gate 2 — No regression in the poller suite

```bash
"C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" \
  -m pytest C:/Users/isabe/Downloads/trip-tracker-agent/lambdas/poller/tests/ -q
```

**EXPECT**: 174 passed (the slice-5 + T1 + T2 baseline). Zero new failures.

### Gate 3 — Comment-cleanliness (NEW project rule, non-negotiable)

```bash
# Stale task-context refs — must be zero hits across all new code.
rg -n --no-heading 'slice[ -_]?\d|\bT[1-5]\b|\bTask [1-5]\b|[Ss]lice-\d' \
  C:/Users/isabe/Downloads/trip-tracker-agent/evals/ || true
```

Then count matches; if non-zero, fail the gate. Same for nonsense filler:

```bash
rg -n --no-heading -w 'basically|simply|obviously|essentially' \
  C:/Users/isabe/Downloads/trip-tracker-agent/evals/ || true
```

**EXPECT**: zero matches in both. The `|| true` keeps `rg`'s exit-1-on-no-matches from killing the script.

### Gate 4 — Runner end-to-end with stubs

```bash
BEDROCK_MODE=stub "C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" \
  C:/Users/isabe/Downloads/trip-tracker-agent/evals/run_evals.py \
    --fixtures-dir C:/Users/isabe/Downloads/trip-tracker-agent/evals/fixtures/decision \
    --out /tmp/test-report.md \
    --stub
echo "exit=$?"
test -s /tmp/test-report.md
grep -q "Decision-quality evals" /tmp/test-report.md
```

**EXPECT**: exit-code semantics — when ALL three initial fixtures match `expected_alert` against the stub's `alert=True` output, only `0002-alert-clear-anomaly.json` and `0003-alert-under-budget-fresh.json` (`expected_alert=true`) will pass; `0001-no-alert-stable-fare.json` (`expected_alert=false`) will fail. **So exit code is 1.** This is correct — the stub's blanket `alert=True` doesn't match a `false` fixture. Ralph asserts: exit code is exactly 1, report contains one `[FAIL]` and two `[PASS]` markers. (This is the smoke test that the runner WIRES UP correctly; T4's golden set is what proves the model is good.)

### Gate 5 — Runner end-to-end with all-pass corpus (sanity check)

Create a temp dir with only the two `expected_alert=true` fixtures, re-run, assert exit 0.

```bash
mkdir -p /tmp/evals-allpass && \
  cp C:/Users/isabe/Downloads/trip-tracker-agent/evals/fixtures/decision/0002-*.json /tmp/evals-allpass/ && \
  cp C:/Users/isabe/Downloads/trip-tracker-agent/evals/fixtures/decision/0003-*.json /tmp/evals-allpass/ && \
BEDROCK_MODE=stub "C:/Users/isabe/Downloads/trip-tracker-agent/.venv-tests/Scripts/python.exe" \
  C:/Users/isabe/Downloads/trip-tracker-agent/evals/run_evals.py \
    --fixtures-dir /tmp/evals-allpass \
    --out /tmp/test-report-allpass.md \
    --stub
echo "exit=$?"
```

**EXPECT**: exit 0.

### Gate 6 — Self-check: file inventory

```bash
ls -la C:/Users/isabe/Downloads/trip-tracker-agent/evals/
ls -la C:/Users/isabe/Downloads/trip-tracker-agent/evals/fixtures/decision/
ls -la C:/Users/isabe/Downloads/trip-tracker-agent/evals/tests/
ls C:/Users/isabe/Downloads/trip-tracker-agent/evals/judge_prompts/
```

**EXPECT**: all 17 files from §8 present.

---

## 14. Acceptance criteria (matches parent plan §4.3 Task 3)

- [ ] `evals/run_evals.py` is a working CLI — argparse, the four flags above.
- [ ] `evals/judge_prompts/decision.md` is a real rubric, not a stub.
- [ ] `evals/fixtures/decision/` has 2–3 initial fixtures (T4 expands to 30+).
- [ ] `evals/README.md` covers how to run, cost note, when to re-run.
- [ ] `evals/tests/test_eval_runner.py`-equivalent tests cover loader, judge-prompt formatter, report writer, exit codes — split across `test_loader.py`, `test_judge_client.py`, `test_report.py`, `test_run_evals.py`.
- [ ] All 6 validation gates pass.
- [ ] Zero `slice/T#` refs and zero nonsense filler in new code (Gate 3).
- [ ] No regression in the poller suite (Gate 2).

---

## 15. Completion checklist

- [ ] All 8 sub-tasks executed top-to-bottom.
- [ ] Test-design gate (Task 1) ran BEFORE any test code was written.
- [ ] Every new test asserts on observable state — no `does-not-raise` smoke.
- [ ] Gates 1–6 all green.
- [ ] Working tree clean except for `evals/` additions.
- [ ] Ready to commit as `slice 6 T3: evals scaffolding (runner + judge + initial fixtures)`.
- [ ] Parent agent then runs Checkpoint A: code-reviewer (Sonnet) → test-engineer (Sonnet), sequentially.

---

## 16. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `anthropic==0.39.0` API surface drift before T4 lands | LOW | LOW | Pin exact version; document upgrade path in README. |
| Judge prompt rubric ambiguous → flaky verdicts in T4 | MED | MED | Keep rubric short and concrete (alert correctness + at least one theme match). T4 calibration round will tighten if needed. |
| Stub-mode "all alert=True" doesn't exercise the false-positive path in this slice's gate | LOW | LOW | Gate 4 explicitly tests the mixed corpus exit-code path (one expected-false fixture exists for this reason). The real model behaviour gets exercised in T4 with the full corpus, not here. |
| `sys.path` injection in `run_evals.py` is fragile across `cwd` | MED | LOW | Use `pathlib.Path(__file__).resolve().parents[1]` (anchored to the file, not the cwd). Test_run_evals covers a non-`evals/` cwd. |
| Adding `anthropic` to `evals/requirements.txt` and the venv blocks Ralph until installed | LOW | LOW | Task 2 explicitly runs the pip install. If `.venv-tests` is read-only for some reason, Ralph reports the failure and waits for human. |

---

## 17. Notes

- **Why `evals/` as its own package and not under `lambdas/`**: evals are a dev-only concern, never deployed. Mixing them with the Lambda package would muddle the CDK asset bundle exclusion list in `lib/poller-server.js:97-100`.
- **Why hand-rolled fixture schema validation**: 30 lines of dataclass + key-check is cheaper to maintain than a `jsonschema` dependency + a JSON schema file. Reconsider in T4 if the schema becomes non-trivial.
- **Why a separate stub for the judge but env-var for bedrock_decide**: the under-test module already owns its mode toggle. The judge client is new code we control entirely, so a flag is cleaner than another env var.
- **What this PRP intentionally does not specify**: the exact text of the three initial fixtures' `notes` and `expected_reason_themes` strings. Ralph picks reasonable values from the design-spec §3 schema; the test-design gate confirms they're internally consistent.
