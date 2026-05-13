"""
Sonnet 4.6 judge for trip-tracker decision-quality evals.

Owns:
  - `JudgeResult` — the verdict + rationale the runner aggregates into
    its report and exit code.
  - `judge(fixture, actual, *, stub, model)` — the public entry point.
    Returns a `JudgeResult`; never raises out. Maps every Anthropic SDK
    error (including subclasses of `APIError`) to a `judge_unavailable`
    verdict so a transient API hiccup doesn't crash the run.

Modes:
  - `stub=True` (default for fast local checks and the unit-test suite):
    no network call. Verdict is deterministic — `pass` iff
    `actual["alert"] == fixture.expected_alert`, else `fail`. The
    rationale string names the observed mismatch so a stub-run report
    is still informative.
  - `stub=False` (live judge): calls `anthropic.Anthropic().messages.create`
    with the rubric in `judge_prompts/decision.md` as the system message
    and a JSON payload (case_id + expected_alert + expected themes +
    model output) as the user message. The response is parsed under the
    same strict-JSON rules as `bedrock_decide._parse_response` —
    markdown fences, extra keys, wrong types, missing keys all collapse
    to `judge_unavailable`.

Prompt determinism: every embedded dict in the user message is
serialised with `json.dumps(sort_keys=True)` so two judge calls with
the same `(fixture, actual)` produce byte-identical prompts. This is
what lets the report writer's byte-stability test hold.

Prompt-injection posture: the under-test model's `reason` string and
the snapshot's provider-controlled fields (hotel name, airline) reach
the judge through `_build_user_message`. This is symmetric to the
posture documented in `lambdas/poller/bedrock_decide.py:19-23` — the
judge sees these as USER-role data, never as instructions, and a
malicious string can at worst cause a misjudgement (which surfaces in
the report), not exfiltrate cross-fixture state.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from evals.loader import Fixture


logger = logging.getLogger("evals.judge_client")

DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"
MAX_RATIONALE_CHARS = 200
_RUBRIC_PATH = Path(__file__).resolve().parent / "judge_prompts" / "decision.md"

Verdict = Literal["pass", "fail", "judge_unavailable"]


@dataclass(frozen=True)
class JudgeResult:
    verdict: Verdict
    rationale: str


def _load_rubric() -> str:
    """Read the rubric markdown at import-call time (not import time) so a
    rubric edit is picked up by the very next `judge()` call without a
    Python restart."""
    return _RUBRIC_PATH.read_text(encoding="utf-8")


def _truncate(text: str, limit: int = MAX_RATIONALE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _decimal_safe(value: Any) -> Any:
    """Stringify Decimal so `json.dumps` doesn't choke. Recurses into
    dicts and lists so a snapshot nested under `actual` round-trips."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _decimal_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimal_safe(v) for v in value]
    return value


def _build_user_message(fixture: Fixture, actual: dict) -> str:
    payload = {
        "case_id": fixture.case_id,
        "expected_alert": fixture.expected_alert,
        "expected_reason_themes": list(fixture.expected_reason_themes),
        "actual": _decimal_safe(actual),
    }
    return json.dumps(payload, sort_keys=True)


def _stub_verdict(fixture: Fixture, actual: dict) -> JudgeResult:
    actual_alert = actual.get("alert")
    if not isinstance(actual_alert, bool):
        return JudgeResult(
            verdict="fail",
            rationale=(
                f"actual.alert is missing or not a bool "
                f"(got {type(actual_alert).__name__}); expected "
                f"{fixture.expected_alert}"
            ),
        )
    if actual_alert == fixture.expected_alert:
        return JudgeResult(
            verdict="pass",
            rationale=(
                f"alert match: expected={fixture.expected_alert} "
                f"actual={actual_alert}"
            ),
        )
    return JudgeResult(
        verdict="fail",
        rationale=(
            f"alert mismatch: expected={fixture.expected_alert} "
            f"actual={actual_alert}"
        ),
    )


def _parse_judge_response(raw: str) -> JudgeResult | None:
    """Strict JSON parser for the judge response.

    Mirrors `bedrock_decide._parse_response`: first char must be `{`, last
    must be `}`, top-level keys are exactly `{verdict, rationale}`,
    `verdict` is in {pass, fail}, `rationale` is a non-empty string.
    Returns None on any deviation so the caller can map to
    `judge_unavailable`.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if set(parsed.keys()) != {"verdict", "rationale"}:
        return None
    verdict = parsed["verdict"]
    rationale = parsed["rationale"]
    if verdict not in ("pass", "fail"):
        return None
    if not isinstance(rationale, str) or not rationale:
        return None
    return JudgeResult(verdict=verdict, rationale=_truncate(rationale))


def _live_verdict(fixture: Fixture, actual: dict, *, model: str) -> JudgeResult:
    # Local import so the package can be imported without `anthropic`
    # installed when only stub-mode tests run. The PRP commits anthropic
    # to evals/requirements.txt, but keeping the import lazy means a
    # consumer who never calls live mode never pays for it.
    import anthropic

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=300,
            system=_load_rubric(),
            messages=[{"role": "user", "content": _build_user_message(fixture, actual)}],
        )
        # Anthropic response shape: `.content` is a list of typed blocks
        # (TextBlock, ToolUseBlock, possibly others in future SDK
        # versions). The decision-quality judge only ever asks for text,
        # so pick the first text block. A response that contains zero
        # text blocks (e.g., a stray tool_use under unusual conditions)
        # collapses to judge_unavailable so the runner reports it
        # cleanly instead of bubbling an AttributeError.
        blocks = response.content or []
        text = next(
            (b.text for b in blocks if getattr(b, "type", None) == "text"),
            None,
        )
        if text is None:
            logger.warning(
                "judge_no_text_block",
                extra={
                    "case_id": fixture.case_id,
                    "block_count": len(blocks),
                    "block_types": [getattr(b, "type", "?") for b in blocks],
                },
            )
            return JudgeResult(
                verdict="judge_unavailable",
                rationale="judge response contained no text content blocks",
            )
    except Exception as e:
        logger.warning(
            "judge_call_failed",
            extra={
                "case_id": fixture.case_id,
                "error": type(e).__name__,
                "error_msg": str(e)[:MAX_RATIONALE_CHARS],
            },
        )
        return JudgeResult(
            verdict="judge_unavailable",
            rationale=_truncate(f"{type(e).__name__}: {e}"),
        )

    parsed = _parse_judge_response(text)
    if parsed is None:
        logger.warning(
            "judge_response_invalid",
            extra={"case_id": fixture.case_id, "text_preview": text[:200]},
        )
        return JudgeResult(
            verdict="judge_unavailable",
            rationale="judge response did not parse as strict JSON {verdict, rationale}",
        )
    return parsed


def judge(
    fixture: Fixture,
    actual: dict,
    *,
    stub: bool,
    model: str = DEFAULT_JUDGE_MODEL,
) -> JudgeResult:
    """Grade `actual` against `fixture`. Never raises.

    `stub=True` short-circuits to a deterministic local verdict so unit
    tests and quick local sanity runs never burn an Anthropic API call.
    """
    if stub:
        return _stub_verdict(fixture, actual)
    return _live_verdict(fixture, actual, model=model)
