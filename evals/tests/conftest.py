"""
Shared test fixtures and helpers for the evals package.

Two environment invariants are set at module load so they're in place
before any test module imports `bedrock_decide` or the anthropic SDK:

  - `BEDROCK_MODE=stub` — keeps the under-test bedrock_decide call from
    burning real Bedrock invocations even when a test forgets to patch
    `boto3`.
  - `ANTHROPIC_API_KEY=test-key-stub` — lets the anthropic SDK import
    without its own env-var guard firing. Live network calls are
    blocked separately by patching `anthropic.Anthropic` at the call
    site in each judge-client test that needs it.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import MagicMock

import pytest


os.environ.setdefault("BEDROCK_MODE", "stub")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-stub")


# Resolve the poller dir relative to this file so the path is correct
# regardless of where pytest was invoked from.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_POLLER_DIR = _REPO_ROOT / "lambdas" / "poller"
if str(_POLLER_DIR) not in sys.path:
    sys.path.insert(0, str(_POLLER_DIR))


@pytest.fixture(autouse=True)
def _reset_eval_module_state():
    """Pop eval and under-test modules from sys.modules between tests so
    each test starts from a clean import (mirrors
    `lambdas/poller/tests/test_bedrock_decide.py:52-62`).

    Saves and restores BEDROCK_MODE / BEDROCK_MODEL_ID / ANTHROPIC_API_KEY
    so a test that flips one of them via `os.environ[...] = ...` cannot
    leak into the next test.
    """
    saved_env = {
        k: os.environ.get(k)
        for k in ("BEDROCK_MODE", "BEDROCK_MODEL_ID", "ANTHROPIC_API_KEY")
    }
    yield
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    for name in (
        "evals.loader", "evals.judge_client", "evals.report", "evals.run_evals",
        "bedrock_decide",
    ):
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _make_default_snapshot() -> dict:
    return {
        "watchId": "w-default",
        "timestamp": "2026-10-15T12:00:00+00:00",
        "totalPrice": Decimal("1200.00"),
        "flightPrice": Decimal("900.00"),
        "hotelPrice": Decimal("300.00"),
        "bestOfferBlob": {
            "airline": "UA",
            "flightNumber": "100",
            "stops": 0,
            "departDate": "2026-10-15T10:00:00",
            "returnDate": "2026-10-20T17:00:00",
            "hotelName": "Park Central",
            "checkin": "2026-10-15",
            "checkout": "2026-10-20",
            "bookingDeepLink": "https://example.test/h-default",
        },
    }


def _make_default_watch() -> dict:
    return {
        "userId": "u-default",
        "watchId": "w-default",
        "type": "specific",
        "origin": "SFO",
        "destination": "Tokyo",
        "destinationAirport": "NRT",
        "dateWindow": {
            "earliestDepart": "2026-10-15",
            "latestDepart": "2026-10-15",
            "nights": 5,
        },
        "pax": 1,
        "maxTotalPrice": Decimal("1500.00"),
        "alertStrategy": "both",
        "preferences": {"maxStops": 1, "hotelMinStars": 4},
        "status": "active",
        "lastAlertedAt": None,
        "lastAlertedPrice": None,
        "createdAt": "2026-05-01T00:00:00+00:00",
        "updatedAt": "2026-05-01T00:00:00+00:00",
    }


def make_fixture_dict(
    case_id: str = "case-001",
    *,
    expected_alert: bool = True,
    expected_reason_themes: Iterable[str] = ("budget",),
    snapshot: dict | None = None,
    watch: dict | None = None,
    history: list[dict] | None = None,
    notes: str = "default test fixture",
) -> dict:
    """Build a dict in the on-disk JSON-shape the loader consumes."""
    snap = snapshot if snapshot is not None else _make_default_snapshot()
    w = watch if watch is not None else _make_default_watch()
    h = history if history is not None else [
        {"totalPrice": Decimal("1180.00")},
        {"totalPrice": Decimal("1200.00")},
        {"totalPrice": Decimal("1220.00")},
    ]
    return {
        "case_id": case_id,
        "notes": notes,
        "snapshot": snap,
        "watch": w,
        "history": h,
        "expected_alert": expected_alert,
        "expected_reason_themes": list(expected_reason_themes),
    }


def make_fixture(
    case_id: str = "case-001",
    **kwargs,
):
    """Build a `Fixture` dataclass instance directly (skipping disk
    round-trip). Forwards every kwarg to `make_fixture_dict`, then
    normalises through `loader._normalise`."""
    from evals.loader import _normalise  # local import: tests reimport
    data = make_fixture_dict(case_id, **kwargs)
    return _normalise(_serialisable(data), file_path=Path(f"<test:{case_id}.json>"))


def _serialisable(value: Any) -> Any:
    """Recursively convert Decimal → str so json.dumps and the loader
    both accept the value. Mirrors the on-disk JSON shape."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _serialisable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialisable(v) for v in value]
    if isinstance(value, tuple):
        return [_serialisable(v) for v in value]
    return value


def fixture_to_json_string(fixture_dict: dict) -> str:
    """Serialise a fixture dict to the JSON-on-disk shape with
    Decimal → string coercion and stable key order."""
    return json.dumps(_serialisable(fixture_dict), sort_keys=True, indent=2)


@pytest.fixture
def fixtures_dir_with(tmp_path):
    """Returns a callable that writes a sequence of fixture dicts to a
    fresh tmpdir as `<case_id>.json` files and returns the dir path.

    Usage:
        def test_x(fixtures_dir_with):
            path = fixtures_dir_with([
                make_fixture_dict("case-001"),
                make_fixture_dict("case-002", expected_alert=False),
            ])
            ...
    """
    counter = {"n": 0}

    def _writer(fixture_dicts: list[dict], *, subdir: str | None = None) -> Path:
        counter["n"] += 1
        target = tmp_path / (subdir or f"corpus-{counter['n']:02d}")
        target.mkdir(parents=True, exist_ok=True)
        for d in fixture_dicts:
            (target / f"{d['case_id']}.json").write_text(
                fixture_to_json_string(d), encoding="utf-8"
            )
        return target

    return _writer


# ---------------------------------------------------------------------------
# Anthropic SDK mock helpers
# ---------------------------------------------------------------------------

def mock_anthropic_response(text: str):
    """Build an object shaped like `anthropic.Anthropic().messages.create`'s
    return value: a Message with `.content = [TextBlock(type='text',
    text=...)]`."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def make_run_metadata(
    *,
    started_at: str = "2026-05-13T12:00:00+00:00",
    model: str = "claude-haiku-4-5-20251001",
    judge_model: str = "claude-sonnet-4-6",
    bedrock_mode: str = "stub",
    stub_judge: bool = True,
):
    from evals.report import RunMetadata
    return RunMetadata(
        started_at=started_at,
        model=model,
        judge_model=judge_model,
        bedrock_mode=bedrock_mode,
        stub_judge=stub_judge,
    )


def make_case_result(
    case_id: str = "case-001",
    *,
    expected_alert: bool = True,
    actual: dict | None = None,
    verdict: str = "pass",
    rationale: str = "ok",
):
    from evals.judge_client import JudgeResult
    from evals.report import CaseResult
    return CaseResult(
        case_id=case_id,
        expected_alert=expected_alert,
        actual=actual if actual is not None else {"alert": expected_alert, "reason": "fine", "bedrock_called": True},
        judge=JudgeResult(verdict=verdict, rationale=rationale),
    )
