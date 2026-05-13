"""
JSON fixture loader for trip-tracker decision-quality evals.

Owns:
  - `Fixture` dataclass — the in-memory shape every downstream module reads.
  - `load_fixtures(path)` — scans a directory for `*.json`, validates each,
    returns a list sorted alphabetically by filename. Empty dir → `[]`.
  - `FixtureError` — raised loud with file path + offending field name so
    a malformed fixture never silently masquerades as a passing case.

Numeric fields inside `snapshot`, `watch`, and `history` are coerced via
`Decimal(str(value))` so prices loaded from JSON match the in-memory type
that `bedrock_decide.decide()` and the production poller produce. Going
through `str` (never `Decimal(float)`) is the same float-safe coercion
`lambdas/poller/snapshot.py` uses for DDB-bound numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


REQUIRED_KEYS = frozenset({
    "case_id", "notes", "snapshot", "watch", "history",
    "expected_alert", "expected_reason_themes",
})

NUMERIC_SNAPSHOT_KEYS = ("totalPrice", "flightPrice", "hotelPrice")
NUMERIC_WATCH_KEYS = ("maxTotalPrice",)
NUMERIC_HISTORY_KEYS = ("totalPrice", "flightPrice", "hotelPrice")


class FixtureError(ValueError):
    """Raised when a fixture file is missing a key, has an extra key, has
    a wrong-typed field, or cannot be parsed as JSON.

    The message always names the offending file path and the offending
    key so the error points at exactly the row that needs fixing.
    """


@dataclass(frozen=True)
class Fixture:
    case_id: str
    notes: str
    snapshot: dict
    watch: dict
    history: list[dict] = field(default_factory=list)
    expected_alert: bool = False
    expected_reason_themes: tuple[str, ...] = ()


def _to_decimal_in_place(d: dict, keys: tuple[str, ...]) -> None:
    """Coerce listed numeric keys in `d` from JSON int/float/str to `Decimal`.

    Goes through `str()` first so a JSON `1148.0` doesn't drag float
    imprecision into the decision pipeline. Missing keys are left alone —
    the caller has already enforced that the required wrapper keys are
    present.
    """
    for k in keys:
        if k in d and not isinstance(d[k], Decimal):
            d[k] = Decimal(str(d[k]))


def _normalise(parsed: dict, *, file_path: Path) -> Fixture:
    """Turn the parsed JSON dict into a `Fixture`, raising `FixtureError`
    on any deviation from the schema in `REQUIRED_KEYS`.

    Keys outside `REQUIRED_KEYS` are rejected loudly so typos and stale
    schema drift surface at load time, not at judge time.
    """
    actual_keys = set(parsed.keys())
    missing = REQUIRED_KEYS - actual_keys
    if missing:
        raise FixtureError(
            f"{file_path}: missing required key(s): "
            f"{sorted(missing)!r}"
        )
    extra = actual_keys - REQUIRED_KEYS
    if extra:
        raise FixtureError(
            f"{file_path}: unknown key(s) not allowed: {sorted(extra)!r}"
        )

    if not isinstance(parsed["case_id"], str) or not parsed["case_id"]:
        raise FixtureError(f"{file_path}: 'case_id' must be a non-empty string")
    if not isinstance(parsed["notes"], str):
        raise FixtureError(f"{file_path}: 'notes' must be a string")
    if not isinstance(parsed["snapshot"], dict):
        raise FixtureError(f"{file_path}: 'snapshot' must be a JSON object")
    if not isinstance(parsed["watch"], dict):
        raise FixtureError(f"{file_path}: 'watch' must be a JSON object")
    if not isinstance(parsed["history"], list):
        raise FixtureError(f"{file_path}: 'history' must be a JSON array")
    for i, row in enumerate(parsed["history"]):
        if not isinstance(row, dict):
            raise FixtureError(
                f"{file_path}: 'history[{i}]' must be a JSON object, "
                f"got {type(row).__name__}"
            )
    # `bool` is a subclass of `int`; reject ints, floats, strings that
    # would otherwise silently coerce.
    if not isinstance(parsed["expected_alert"], bool):
        raise FixtureError(
            f"{file_path}: 'expected_alert' must be a JSON boolean, "
            f"got {type(parsed['expected_alert']).__name__}"
        )
    if not isinstance(parsed["expected_reason_themes"], list):
        raise FixtureError(
            f"{file_path}: 'expected_reason_themes' must be a JSON array of "
            f"strings, got {type(parsed['expected_reason_themes']).__name__}"
        )
    for i, theme in enumerate(parsed["expected_reason_themes"]):
        if not isinstance(theme, str):
            raise FixtureError(
                f"{file_path}: 'expected_reason_themes[{i}]' must be a string, "
                f"got {type(theme).__name__}"
            )

    snapshot = dict(parsed["snapshot"])
    watch = dict(parsed["watch"])
    history = [dict(row) for row in parsed["history"]]

    _to_decimal_in_place(snapshot, NUMERIC_SNAPSHOT_KEYS)
    _to_decimal_in_place(watch, NUMERIC_WATCH_KEYS)
    for row in history:
        _to_decimal_in_place(row, NUMERIC_HISTORY_KEYS)

    return Fixture(
        case_id=parsed["case_id"],
        notes=parsed["notes"],
        snapshot=snapshot,
        watch=watch,
        history=history,
        expected_alert=parsed["expected_alert"],
        expected_reason_themes=tuple(parsed["expected_reason_themes"]),
    )


def _load_one(file_path: Path) -> Fixture:
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as e:
        raise FixtureError(f"{file_path}: unreadable: {e}") from e
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FixtureError(f"{file_path}: invalid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise FixtureError(
            f"{file_path}: top-level value must be a JSON object, "
            f"got {type(parsed).__name__}"
        )
    return _normalise(parsed, file_path=file_path)


def load_fixtures(path: str | Path) -> list[Fixture]:
    """Load every `*.json` fixture under `path`, sorted by filename.

    Non-JSON entries (`README.md`, etc.) are ignored. An empty directory
    is a valid input and yields `[]` so a CI runner that points at an
    empty dir doesn't error before any case has a chance to run.
    """
    p = Path(path)
    if not p.exists():
        raise FixtureError(f"fixtures path does not exist: {p}")
    if not p.is_dir():
        raise FixtureError(f"fixtures path is not a directory: {p}")
    json_files = sorted(p.glob("*.json"), key=lambda f: f.name)
    return [_load_one(f) for f in json_files]
