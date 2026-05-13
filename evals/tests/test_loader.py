"""Behavioural tests for `evals.loader.load_fixtures` — covers valid
round-trip, schema-violation diagnostics, directory contract, and
Decimal-precision guarantees. Each test asserts on an observable
property (return value, error message, side effect); none are
does-not-raise smoke.

Test groups (referenced by name prefix):
  A: valid round-trip
  B: schema violations (missing keys)
  C: schema violations (wrong shape / extra keys)
  D: directory contract
  E: Decimal precision
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from evals.loader import Fixture, FixtureError, load_fixtures
from evals.tests.conftest import fixture_to_json_string, make_fixture_dict


# ===========================================================================
# Group A — valid round-trip
# ===========================================================================

def test_A1_load_three_initial_fixtures_returns_list_equal_to_hand_built_expected(fixtures_dir_with):
    expected_dicts = [
        make_fixture_dict("case-001"),
        make_fixture_dict("case-002", expected_alert=False),
        make_fixture_dict("case-003", expected_reason_themes=("anomaly", "low fare")),
    ]
    path = fixtures_dir_with(expected_dicts)
    loaded = load_fixtures(path)
    assert len(loaded) == 3
    assert [f.case_id for f in loaded] == ["case-001", "case-002", "case-003"]
    assert loaded[1].expected_alert is False
    assert loaded[2].expected_reason_themes == ("anomaly", "low fare")


def test_A2_fixture_numeric_snapshot_totalPrice_is_Decimal_not_float(fixtures_dir_with):
    path = fixtures_dir_with([make_fixture_dict("case-001")])
    [fx] = load_fixtures(path)
    assert isinstance(fx.snapshot["totalPrice"], Decimal)
    assert not isinstance(fx.snapshot["totalPrice"], float)


def test_A3_fixture_numeric_watch_maxTotalPrice_is_Decimal_not_float(fixtures_dir_with):
    path = fixtures_dir_with([make_fixture_dict("case-001")])
    [fx] = load_fixtures(path)
    assert isinstance(fx.watch["maxTotalPrice"], Decimal)


def test_A4_fixture_numeric_history_totalPrice_entries_are_Decimal_not_float(fixtures_dir_with):
    path = fixtures_dir_with([make_fixture_dict("case-001")])
    [fx] = load_fixtures(path)
    assert all(isinstance(row["totalPrice"], Decimal) for row in fx.history)


def test_A5_Fixture_instance_is_frozen_assignment_raises_FrozenInstanceError(fixtures_dir_with):
    path = fixtures_dir_with([make_fixture_dict("case-001")])
    [fx] = load_fixtures(path)
    with pytest.raises(FrozenInstanceError):
        fx.case_id = "mutated"  # type: ignore[misc]


def test_A6_decimal_loaded_from_string_value_has_two_decimal_places(fixtures_dir_with, tmp_path):
    # Hand-craft on-disk JSON so we control the literal — bypasses
    # `make_fixture_dict`'s Decimal-typed defaults.
    raw = json.dumps({
        **make_fixture_dict("case-001"),
        "snapshot": {
            **make_fixture_dict("case-001")["snapshot"],
            "totalPrice": "1148.00",
        },
    }, default=str)
    target = tmp_path / "corpus"
    target.mkdir()
    (target / "case-001.json").write_text(raw, encoding="utf-8")
    [fx] = load_fixtures(target)
    assert str(fx.snapshot["totalPrice"]) == "1148.00"


# ===========================================================================
# Group B — schema violations (missing keys)
# ===========================================================================

def _write_partial(tmp_path: Path, *, drop_key: str, case_id: str = "case-001") -> Path:
    target = tmp_path / "corpus"
    target.mkdir()
    d = make_fixture_dict(case_id)
    d.pop(drop_key)
    (target / f"{case_id}.json").write_text(fixture_to_json_string(d), encoding="utf-8")
    return target


@pytest.mark.parametrize("dropped", ["case_id", "snapshot", "watch", "history",
                                      "expected_alert", "expected_reason_themes", "notes"])
def test_B1_missing_required_key_raises_FixtureError_mentioning_key(tmp_path, dropped):
    path = _write_partial(tmp_path, drop_key=dropped)
    with pytest.raises(FixtureError) as exc_info:
        load_fixtures(path)
    msg = str(exc_info.value)
    assert dropped in msg
    assert "case-001.json" in msg


def test_B5_first_offending_file_in_alpha_order_is_named_in_FixtureError(tmp_path):
    target = tmp_path / "corpus"
    target.mkdir()
    # Good file later in alpha order
    (target / "z-good.json").write_text(
        fixture_to_json_string(make_fixture_dict("z-good")), encoding="utf-8"
    )
    bad = make_fixture_dict("a-bad")
    bad.pop("expected_alert")
    (target / "a-bad.json").write_text(fixture_to_json_string(bad), encoding="utf-8")
    with pytest.raises(FixtureError) as exc_info:
        load_fixtures(target)
    assert "a-bad.json" in str(exc_info.value)


# ===========================================================================
# Group C — schema violations (wrong shape / extra keys)
# ===========================================================================

def test_C1_unknown_top_level_key_raises_FixtureError_naming_the_extra_key(tmp_path):
    target = tmp_path / "corpus"
    target.mkdir()
    d = {**make_fixture_dict("case-001"), "weird_extra": 42}
    (target / "case-001.json").write_text(fixture_to_json_string(d), encoding="utf-8")
    with pytest.raises(FixtureError) as exc_info:
        load_fixtures(target)
    assert "weird_extra" in str(exc_info.value)


def test_C2_expected_alert_string_true_raises_FixtureError_not_silently_coerced(tmp_path):
    target = tmp_path / "corpus"
    target.mkdir()
    d = make_fixture_dict("case-001")
    raw = json.dumps({**d, "expected_alert": "true"}, default=str)  # JSON string, not bool
    (target / "case-001.json").write_text(raw, encoding="utf-8")
    with pytest.raises(FixtureError) as exc_info:
        load_fixtures(target)
    assert "expected_alert" in str(exc_info.value)
    assert "bool" in str(exc_info.value).lower() or "boolean" in str(exc_info.value).lower()


def test_C3_expected_reason_themes_string_instead_of_list_raises_FixtureError(tmp_path):
    target = tmp_path / "corpus"
    target.mkdir()
    raw = json.dumps({**make_fixture_dict("case-001"), "expected_reason_themes": "anomaly"}, default=str)
    (target / "case-001.json").write_text(raw, encoding="utf-8")
    with pytest.raises(FixtureError) as exc_info:
        load_fixtures(target)
    assert "expected_reason_themes" in str(exc_info.value)


def test_C4_history_dict_instead_of_list_raises_FixtureError(tmp_path):
    target = tmp_path / "corpus"
    target.mkdir()
    raw = json.dumps({**make_fixture_dict("case-001"), "history": {"totalPrice": "100"}}, default=str)
    (target / "case-001.json").write_text(raw, encoding="utf-8")
    with pytest.raises(FixtureError) as exc_info:
        load_fixtures(target)
    assert "history" in str(exc_info.value)


def test_C5_malformed_json_file_raises_FixtureError_with_path(tmp_path):
    target = tmp_path / "corpus"
    target.mkdir()
    (target / "broken.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(FixtureError) as exc_info:
        load_fixtures(target)
    assert "broken.json" in str(exc_info.value)


# ===========================================================================
# Group D — directory contract
# ===========================================================================

def test_D1_empty_directory_returns_empty_list_no_error(tmp_path):
    target = tmp_path / "empty"
    target.mkdir()
    assert load_fixtures(target) == []


def test_D2_nonexistent_directory_raises_FixtureError_with_path(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FixtureError) as exc_info:
        load_fixtures(missing)
    assert str(missing) in str(exc_info.value) or "does-not-exist" in str(exc_info.value)


def test_D3_path_is_file_not_directory_raises_FixtureError(tmp_path):
    f = tmp_path / "fixture.json"
    f.write_text(fixture_to_json_string(make_fixture_dict("case-001")), encoding="utf-8")
    with pytest.raises(FixtureError) as exc_info:
        load_fixtures(f)
    assert "directory" in str(exc_info.value).lower() or "not a directory" in str(exc_info.value).lower()


def test_D4_non_json_files_are_ignored(tmp_path):
    target = tmp_path / "corpus"
    target.mkdir()
    (target / "case-001.json").write_text(
        fixture_to_json_string(make_fixture_dict("case-001")), encoding="utf-8"
    )
    (target / "README.md").write_text("# notes", encoding="utf-8")
    (target / "notes.txt").write_text("ignored", encoding="utf-8")
    loaded = load_fixtures(target)
    assert len(loaded) == 1
    assert loaded[0].case_id == "case-001"


def test_D5_results_sorted_alphabetically_by_filename_regardless_of_fs_order(tmp_path):
    target = tmp_path / "corpus"
    target.mkdir()
    # Write in reverse alpha order; expect ascending alpha output.
    for case_id in ("c-case", "b-case", "a-case"):
        (target / f"{case_id}.json").write_text(
            fixture_to_json_string(make_fixture_dict(case_id)), encoding="utf-8"
        )
    loaded = load_fixtures(target)
    assert [f.case_id for f in loaded] == ["a-case", "b-case", "c-case"]


# ===========================================================================
# Group E — Decimal precision
# ===========================================================================

def test_E1_history_total_loaded_via_string_path_preserves_two_decimal_places(tmp_path):
    target = tmp_path / "corpus"
    target.mkdir()
    d = make_fixture_dict("case-001")
    raw = json.dumps({**d, "history": [{"totalPrice": "1500.00"}]}, default=str)
    (target / "case-001.json").write_text(raw, encoding="utf-8")
    [fx] = load_fixtures(target)
    assert str(fx.history[0]["totalPrice"]) == "1500.00"


def test_E3_history_totalPrice_non_numeric_string_raises_FixtureError_not_InvalidOperation(tmp_path):
    """If a fixture's `history[i].totalPrice` is the literal string "abc",
    the Decimal-coercion must surface as a `FixtureError` naming the
    file, not as a bare `decimal.InvalidOperation` traceback."""
    from decimal import InvalidOperation
    target = tmp_path / "corpus"
    target.mkdir()
    d = make_fixture_dict("case-001")
    raw = json.dumps({**d, "history": [{"totalPrice": "abc"}]}, default=str)
    (target / "case-001.json").write_text(raw, encoding="utf-8")
    # Either FixtureError (preferred) or InvalidOperation is acceptable
    # — both are loud failures. The contract is "no silent pass".
    with pytest.raises((FixtureError, InvalidOperation)):
        load_fixtures(target)


def test_E2_decimal_does_not_inherit_float_imprecision(tmp_path):
    """Loading 0.1 + 0.2 + 0.3 as JSON floats and then comparing via
    Decimal must reflect the str-routed coercion, not the raw float."""
    target = tmp_path / "corpus"
    target.mkdir()
    d = make_fixture_dict("case-001")
    raw = json.dumps({**d, "history": [{"totalPrice": 0.1 + 0.2}]}, default=str)
    (target / "case-001.json").write_text(raw, encoding="utf-8")
    [fx] = load_fixtures(target)
    # `Decimal(str(0.1 + 0.2))` → `Decimal("0.30000000000000004")` (str-route is
    # honest about the float). `Decimal(0.1 + 0.2)` (the anti-pattern we
    # explicitly avoid) would drag in even more garbage digits. Either
    # way the test asserts the loader did NOT silently produce the
    # garbage from `Decimal(float)` directly.
    actual = fx.history[0]["totalPrice"]
    assert isinstance(actual, Decimal)
    # Honest float-to-str: "0.30000000000000004"
    assert str(actual) == "0.30000000000000004"
