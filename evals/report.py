"""
Markdown report writer for trip-tracker decision-quality evals.

Owns:
  - `RunMetadata` and `CaseResult` dataclasses — the inputs `render_report`
    consumes.
  - `render_report(run_metadata, results)` — a pure function: same inputs
    yield byte-identical output across runs. Embedded dicts serialise via
    `json.dumps(sort_keys=True)`; the `started_at` timestamp is read off
    the metadata dataclass, never sampled from the wall clock inside the
    function. Both properties keep the unit-test suite's byte-stability
    assertions honest.

Per the repo style rule (CLAUDE.md): no emojis. Pass / fail / unavailable
verdicts use the text tokens `[PASS]`, `[FAIL]`, `[UNAVAILABLE]` so
greppability survives rendering.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from evals.judge_client import JudgeResult


VERDICT_MARKERS = {
    "pass": "[PASS]",
    "fail": "[FAIL]",
    "judge_unavailable": "[UNAVAILABLE]",
}


@dataclass(frozen=True)
class RunMetadata:
    started_at: str
    model: str
    judge_model: str
    bedrock_mode: str
    stub_judge: bool


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    expected_alert: bool
    actual: dict
    judge: JudgeResult


def _decimal_safe(value: Any) -> Any:
    """Recursively stringify Decimals so `json.dumps` accepts the value
    and the report stays byte-stable across runs."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _decimal_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimal_safe(v) for v in value]
    return value


def _stable_json(value: Any) -> str:
    """Sorted-key JSON for any value that ends up in the report so two
    runs over the same inputs produce identical text."""
    return json.dumps(_decimal_safe(value), sort_keys=True)


def _bool_to_lower(value: bool) -> str:
    """Render booleans as JSON-style `true`/`false`, not Python `True`/`False`,
    so the rendered text matches the JSON the user fed in."""
    return "true" if value else "false"


def _count_verdicts(results: list[CaseResult]) -> tuple[int, int, int]:
    """Return (pass, fail, judge_unavailable) counts."""
    p = sum(1 for r in results if r.judge.verdict == "pass")
    f = sum(1 for r in results if r.judge.verdict == "fail")
    u = sum(1 for r in results if r.judge.verdict == "judge_unavailable")
    return p, f, u


def _render_header(meta: RunMetadata, results: list[CaseResult]) -> str:
    p, f, u = _count_verdicts(results)
    return (
        f"# Decision-quality evals — {meta.started_at}\n"
        "\n"
        "| Field | Value |\n"
        "|---|---|\n"
        f"| Model under test | `{meta.model}` |\n"
        f"| Judge | `{meta.judge_model}` |\n"
        f"| BEDROCK_MODE | `{meta.bedrock_mode}` |\n"
        f"| --stub | {_bool_to_lower(meta.stub_judge)} |\n"
        f"| Fixtures evaluated | {len(results)} |\n"
        f"| Pass | {p} |\n"
        f"| Fail | {f} |\n"
        f"| Judge unavailable | {u} |\n"
    )


def _render_case(result: CaseResult) -> str:
    marker = VERDICT_MARKERS[result.judge.verdict]
    actual_alert_raw = result.actual.get("alert")
    actual_reason_raw = result.actual.get("reason", "")
    # Pipes and newlines in `reason` would otherwise break the section
    # layout; render reason as inline-code so the table stays intact and
    # an injected pipe doesn't smuggle column breaks past the renderer.
    actual_reason_safe = (
        str(actual_reason_raw).replace("\n", " ").replace("|", "\\|")
    )
    if isinstance(actual_alert_raw, bool):
        actual_alert_str = _bool_to_lower(actual_alert_raw)
    else:
        actual_alert_str = f"<missing or non-bool: {type(actual_alert_raw).__name__}>"
    actual_full_json = _stable_json(result.actual)
    return (
        f"### {result.case_id} — {marker} {result.judge.verdict}\n"
        "\n"
        f"- **expected_alert**: {_bool_to_lower(result.expected_alert)}\n"
        f"- **actual.alert**: {actual_alert_str}\n"
        f"- **actual.reason**: `{actual_reason_safe}`\n"
        f"- **judge rationale**: {result.judge.rationale}\n"
        f"- **actual (full)**: `{actual_full_json}`\n"
    )


def render_report(run_metadata: RunMetadata, results: list[CaseResult]) -> str:
    """Render a deterministic markdown report.

    Two calls with `==` inputs produce `==` strings. Inputs the function
    needs are the metadata dataclass and the list of `CaseResult`s — it
    does not read the wall clock, does not access the filesystem, does
    not call out to anything.
    """
    sections = [_render_header(run_metadata, results)]
    if not results:
        sections.append("\n## Per-case results\n\n(no fixtures evaluated)\n")
    else:
        sections.append("\n## Per-case results\n")
        for r in results:
            sections.append("\n" + _render_case(r))
    return "".join(sections)
