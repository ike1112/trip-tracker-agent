"""Behavioural tests for `evals.run_evals.main` — covers exit-code
contract, argparse and IO contract, stubbing semantics, sys.path
injection, and end-to-end report content. The runner is exercised by
calling `main(argv)` and observing exit code + file system + mock
interactions; no real Bedrock or Anthropic calls are made.

Test groups:
  A: exit-code contract
  B: argparse + IO contract
  C: stubbing contract
  D: sys.path injection
  E: report content end-to-end
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from evals.tests.conftest import fixture_to_json_string, make_fixture_dict


def _import_main():
    """Reimport run_evals fresh so any sys.path edits from a prior test
    don't leak. The autouse fixture in conftest already pops
    `evals.run_evals` between tests."""
    import importlib
    if "evals.run_evals" in sys.modules:
        del sys.modules["evals.run_evals"]
    return importlib.import_module("evals.run_evals").main


# ===========================================================================
# Group A — exit-code contract
# ===========================================================================

def test_A1_all_pass_corpus_main_returns_exit_code_0(fixtures_dir_with, tmp_path):
    main = _import_main()
    path = fixtures_dir_with([
        make_fixture_dict("c-1", expected_alert=True),   # stub bedrock returns alert=True -> pass
        make_fixture_dict("c-2", expected_alert=True),
    ])
    out = tmp_path / "report.md"
    rc = main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    assert rc == 0


def test_A2_corpus_with_one_fail_main_returns_exit_code_1(fixtures_dir_with, tmp_path):
    main = _import_main()
    path = fixtures_dir_with([
        make_fixture_dict("c-1", expected_alert=True),
        make_fixture_dict("c-2", expected_alert=False),  # stub returns alert=True; this fails
    ])
    out = tmp_path / "report.md"
    rc = main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    assert rc == 1


def test_A3_corpus_with_one_judge_unavailable_main_returns_exit_code_1(fixtures_dir_with, tmp_path):
    main = _import_main()
    path = fixtures_dir_with([make_fixture_dict("c-1", expected_alert=True)])
    out = tmp_path / "report.md"
    # Don't use --stub; mock the live judge to return judge_unavailable.
    from evals.judge_client import JudgeResult
    with patch("evals.run_evals.judge", return_value=JudgeResult("judge_unavailable", "down")):
        rc = main(["--fixtures-dir", str(path), "--out", str(out)])
    assert rc == 1


def test_A4_empty_fixtures_dir_main_returns_exit_code_0_and_writes_report(tmp_path):
    main = _import_main()
    empty = tmp_path / "empty"
    empty.mkdir()
    out = tmp_path / "report.md"
    rc = main(["--fixtures-dir", str(empty), "--out", str(out), "--stub"])
    assert rc == 0
    assert out.exists()
    assert "Fixtures evaluated | 0" in out.read_text(encoding="utf-8")


def test_A5_mixed_corpus_exit_code_is_1_regardless_of_pass_count(fixtures_dir_with, tmp_path):
    main = _import_main()
    path = fixtures_dir_with([
        make_fixture_dict("c-1", expected_alert=True),
        make_fixture_dict("c-2", expected_alert=True),
        make_fixture_dict("c-3", expected_alert=True),
        make_fixture_dict("c-4", expected_alert=False),  # single failure
    ])
    out = tmp_path / "report.md"
    rc = main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    assert rc == 1


def test_A6_per_case_exception_does_not_crash_run_records_judge_unavailable(
    fixtures_dir_with, tmp_path
):
    """A bug in `bedrock_decide.decide` (or any future per-case error)
    must NOT abort the corpus — the runner records `judge_unavailable`,
    keeps iterating, finishes the report, exits 1."""
    main = _import_main()
    path = fixtures_dir_with([
        make_fixture_dict("c-1"),
        make_fixture_dict("c-2"),
        make_fixture_dict("c-3"),
    ])
    out = tmp_path / "report.md"
    # Patch `_run_one_case` so the middle fixture raises.
    import evals.run_evals as run_evals_mod
    original = run_evals_mod._run_one_case
    call_count = {"n": 0}

    def _flaky_one_case(fx, *, stub, judge_model):
        call_count["n"] += 1
        if fx.case_id == "c-2":
            raise RuntimeError("simulated bedrock_decide explosion")
        return original(fx, stub=stub, judge_model=judge_model)

    with patch.object(run_evals_mod, "_run_one_case", side_effect=_flaky_one_case):
        rc = main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    assert rc == 1
    assert call_count["n"] == 3, "runner must continue iterating past the failing case"
    report = out.read_text(encoding="utf-8")
    # The failing case shows up as [UNAVAILABLE] with a runner_error rationale.
    assert "[UNAVAILABLE]" in report
    assert "runner_error" in report
    assert "RuntimeError" in report


# ===========================================================================
# Group B — argparse + IO contract
# ===========================================================================

def test_B1_missing_required_fixtures_dir_flag_exits_with_argparse_code_2(tmp_path, capsys):
    main = _import_main()
    out = tmp_path / "r.md"
    with pytest.raises(SystemExit) as exc_info:
        main(["--out", str(out), "--stub"])
    assert exc_info.value.code == 2


def test_B2_missing_required_out_flag_exits_with_argparse_code_2(tmp_path, capsys):
    main = _import_main()
    with pytest.raises(SystemExit) as exc_info:
        main(["--fixtures-dir", str(tmp_path), "--stub"])
    assert exc_info.value.code == 2


def test_B3_unknown_flag_exits_with_code_2(tmp_path, capsys):
    main = _import_main()
    with pytest.raises(SystemExit) as exc_info:
        main(["--fixtures-dir", str(tmp_path), "--out", str(tmp_path / "r.md"),
              "--stub", "--bogus"])
    assert exc_info.value.code == 2


def test_B4_nonexistent_fixtures_dir_main_returns_exit_code_1(tmp_path, capsys):
    main = _import_main()
    missing = tmp_path / "does-not-exist"
    out = tmp_path / "r.md"
    rc = main(["--fixtures-dir", str(missing), "--out", str(out), "--stub"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "fixture_load_failed" in err or "does not exist" in err.lower()


def test_B5_out_path_parent_directory_does_not_exist_is_created_before_write(
    fixtures_dir_with, tmp_path
):
    main = _import_main()
    path = fixtures_dir_with([make_fixture_dict("c-1")])
    nested_out = tmp_path / "nested" / "deeply" / "report.md"
    assert not nested_out.parent.exists()
    rc = main(["--fixtures-dir", str(path), "--out", str(nested_out), "--stub"])
    # rc may be 0 or 1 depending on stub verdict; regardless, file written
    assert nested_out.exists()
    assert nested_out.parent.is_dir()


def test_B6_out_file_is_written_and_nonempty_after_run(fixtures_dir_with, tmp_path):
    main = _import_main()
    path = fixtures_dir_with([make_fixture_dict("c-1")])
    out = tmp_path / "r.md"
    main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    assert out.exists()
    assert out.stat().st_size > 0


def test_B7_runner_emits_per_case_log_records_at_INFO_level(
    fixtures_dir_with, tmp_path, caplog
):
    """The per-case start/done events are the user-visible progress
    signal. `_configure_logging` leaves the root logger alone when a
    host (pytest, a CI runner) has already installed handlers, so
    caplog observes the records directly."""
    import logging
    main = _import_main()
    path = fixtures_dir_with([make_fixture_dict("c-1")])
    out = tmp_path / "r.md"
    with caplog.at_level(logging.INFO, logger="evals.run_evals"):
        main(["--fixtures-dir", str(path), "--out", str(out),
              "--stub", "--log-level", "INFO"])
    messages = [r.getMessage() for r in caplog.records
                if r.name == "evals.run_evals"]
    assert any("eval_case_start" in m for m in messages)
    assert any("eval_case_done" in m for m in messages)
    assert any("case_id=c-1" in m for m in messages)


# ===========================================================================
# Group C — stubbing contract
# ===========================================================================

def test_C1_stub_flag_true_no_anthropic_messages_create_call_attempted(
    fixtures_dir_with, tmp_path
):
    main = _import_main()
    path = fixtures_dir_with([make_fixture_dict("c-1")])
    out = tmp_path / "r.md"
    with patch("anthropic.Anthropic", side_effect=AssertionError("must not be called")):
        rc = main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    # rc is whatever stub verdict produces; the assertion is that no
    # anthropic call was attempted.
    assert rc in (0, 1)


def test_C2_stub_flag_false_invokes_live_judge_path(fixtures_dir_with, tmp_path):
    main = _import_main()
    path = fixtures_dir_with([make_fixture_dict("c-1", expected_alert=True)])
    out = tmp_path / "r.md"
    with patch("evals.run_evals.judge") as mock_judge:
        from evals.judge_client import JudgeResult
        mock_judge.return_value = JudgeResult("pass", "ok")
        main(["--fixtures-dir", str(path), "--out", str(out)])
    # Live mode (no --stub flag) means judge() was called with stub=False
    assert mock_judge.called
    call_kwargs = mock_judge.call_args.kwargs
    assert call_kwargs.get("stub") is False


def test_C3_BEDROCK_MODE_stub_causes_bedrock_decide_to_return_stub_shape(
    fixtures_dir_with, tmp_path
):
    main = _import_main()
    path = fixtures_dir_with([make_fixture_dict("c-1", expected_alert=True)])
    out = tmp_path / "r.md"
    # BEDROCK_MODE=stub is already set by conftest. Patch boto3 to
    # crash if reached — it must NOT be reached.
    with patch("boto3.client", side_effect=AssertionError("boto3 must not be called in stub mode")):
        rc = main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    assert rc in (0, 1)
    # Report contains the stub reason that bedrock_decide returns.
    report_text = out.read_text(encoding="utf-8")
    assert "stub" in report_text


def test_C4_judge_model_flag_value_propagates_to_judge_client_messages_create(
    fixtures_dir_with, tmp_path
):
    main = _import_main()
    path = fixtures_dir_with([make_fixture_dict("c-1", expected_alert=True)])
    out = tmp_path / "r.md"
    with patch("evals.run_evals.judge") as mock_judge:
        from evals.judge_client import JudgeResult
        mock_judge.return_value = JudgeResult("pass", "ok")
        main(["--fixtures-dir", str(path), "--out", str(out),
              "--judge-model", "claude-sonnet-fictional-2099"])
    call_kwargs = mock_judge.call_args.kwargs
    assert call_kwargs.get("model") == "claude-sonnet-fictional-2099"


# ===========================================================================
# Group D — sys.path injection
# ===========================================================================

def test_D1_main_invoked_from_cwd_outside_repo_actually_imports_bedrock_decide(
    fixtures_dir_with, tmp_path, monkeypatch
):
    """A run from a foreign cwd must actually load `bedrock_decide` and
    invoke its stub path — not silently fall through to runner_error."""
    main = _import_main()
    # Use a fixture where the stub's blanket `alert=True` matches, so we
    # can distinguish a real stub run (reason="stub") from a runner_error.
    path = fixtures_dir_with([make_fixture_dict("c-1", expected_alert=True)])
    out = tmp_path / "r.md"
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)
    rc = main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    assert rc == 0, "stub-mode run with matching expected_alert must exit 0"
    assert "bedrock_decide" in sys.modules, "main() must have imported bedrock_decide"
    text = out.read_text(encoding="utf-8")
    assert "`stub`" in text, "report must contain the stub literal as actual.reason"
    assert "runner_error" not in text, "runner_error in report means bedrock_decide failed to load"


def test_D2_main_inserts_lambdas_poller_path_into_sys_path(
    fixtures_dir_with, tmp_path
):
    main = _import_main()
    path = fixtures_dir_with([make_fixture_dict("c-1")])
    out = tmp_path / "r.md"
    main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    # Should be present after main runs.
    assert any("lambdas" in p and "poller" in p for p in sys.path)


def test_D4_bedrock_decide_called_in_alphabetical_fixture_order(
    fixtures_dir_with, tmp_path
):
    def _fixture(case_id: str) -> dict:
        d = make_fixture_dict(case_id)
        d["snapshot"] = {**d["snapshot"], "watchId": case_id}
        return d

    main = _import_main()
    path = fixtures_dir_with([
        _fixture("z-last"),
        _fixture("a-first"),
        _fixture("m-mid"),
    ])
    out = tmp_path / "r.md"
    call_order = []
    # Capture call order by patching bedrock_decide.decide via the
    # already-imported module. We let main import it first, then
    # re-patch through the loaded module.
    import importlib
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambdas" / "poller"))
    bd = importlib.import_module("bedrock_decide")
    original = bd.decide

    def _spy(snapshot, watch, history):
        call_order.append(snapshot.get("watchId", "?"))
        return original(snapshot, watch, history)

    with patch.object(bd, "decide", side_effect=_spy):
        main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])

    # Fixture order is alphabetical filename: a-first -> m-mid -> z-last.
    # Snapshot watchId was set to case_id above so call order is directly
    # observable here.
    assert call_order == ["a-first", "m-mid", "z-last"]


# ===========================================================================
# Group E — report content end-to-end
# ===========================================================================

def test_E1_written_report_for_three_fixtures_contains_each_case_id(
    fixtures_dir_with, tmp_path
):
    main = _import_main()
    path = fixtures_dir_with([
        make_fixture_dict("alpha"),
        make_fixture_dict("beta"),
        make_fixture_dict("gamma"),
    ])
    out = tmp_path / "r.md"
    main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    text = out.read_text(encoding="utf-8")
    assert "alpha" in text and "beta" in text and "gamma" in text


def test_E2_written_report_contains_exactly_one_FAIL_when_one_fixture_fails(
    fixtures_dir_with, tmp_path
):
    main = _import_main()
    path = fixtures_dir_with([
        make_fixture_dict("c-1", expected_alert=True),
        make_fixture_dict("c-2", expected_alert=False),  # stub returns alert=True; fails
        make_fixture_dict("c-3", expected_alert=True),
    ])
    out = tmp_path / "r.md"
    main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    text = out.read_text(encoding="utf-8")
    # One section's marker is [FAIL]; the rest are [PASS].
    assert text.count("[FAIL]") == 1
    assert text.count("[PASS]") == 2


def test_E3_report_summary_pass_count_matches_observed_pass_sections(
    fixtures_dir_with, tmp_path
):
    main = _import_main()
    path = fixtures_dir_with([
        make_fixture_dict("c-1", expected_alert=True),
        make_fixture_dict("c-2", expected_alert=True),
        make_fixture_dict("c-3", expected_alert=False),
    ])
    out = tmp_path / "r.md"
    main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    text = out.read_text(encoding="utf-8")
    assert "| Pass | 2 |" in text
    assert "| Fail | 1 |" in text


def test_E4_report_header_model_field_equals_bedrock_decide_BEDROCK_MODEL_ID(
    fixtures_dir_with, tmp_path
):
    """The under-test model ID shown in the report header must match
    what `bedrock_decide.BEDROCK_MODEL_ID` resolves to — guarantees the
    runner is reporting on what it actually called, not a stale string."""
    main = _import_main()
    path = fixtures_dir_with([make_fixture_dict("c-1")])
    out = tmp_path / "r.md"
    main(["--fixtures-dir", str(path), "--out", str(out), "--stub"])
    text = out.read_text(encoding="utf-8")
    import bedrock_decide
    expected_model = bedrock_decide.BEDROCK_MODEL_ID
    assert expected_model in text, (
        f"report header must include the under-test model ID "
        f"({expected_model!r}); got header:\n{text[:600]}"
    )
    assert "`unknown`" not in text, (
        "`_under_test_model_id` returned 'unknown' — bedrock_decide failed to import"
    )
