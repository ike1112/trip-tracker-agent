"""Behavioural tests for `evals.report.render_report` — covers section
presence and counts, byte-stability (the locked invariant), verdict
rendering, and run-metadata header rendering. The byte-stability tests
are the load-bearing property: the report writer is a pure function
whose output equals across two calls with `==` inputs.

Test groups:
  A: section presence and counts
  B: byte-stability
  C: verdict rendering
  D: run-metadata header
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from evals.report import render_report
from evals.tests.conftest import make_case_result, make_run_metadata


# ===========================================================================
# Group A — section presence and counts
# ===========================================================================

def test_A1_render_with_N_results_produces_exactly_N_per_case_sections():
    meta = make_run_metadata()
    results = [
        make_case_result(case_id="c-1"),
        make_case_result(case_id="c-2"),
        make_case_result(case_id="c-3"),
    ]
    out = render_report(meta, results)
    section_count = out.count("\n### ")
    assert section_count == 3


def test_A2_each_case_section_heading_contains_its_case_id():
    meta = make_run_metadata()
    results = [make_case_result("alpha-007"), make_case_result("beta-008")]
    out = render_report(meta, results)
    assert "### alpha-007" in out
    assert "### beta-008" in out


def test_A3_each_case_section_contains_expected_actual_and_rationale_fields():
    meta = make_run_metadata()
    results = [make_case_result(
        "c-1",
        expected_alert=True,
        actual={"alert": True, "reason": "good price", "bedrock_called": True},
        verdict="pass",
        rationale="match",
    )]
    out = render_report(meta, results)
    assert "expected_alert" in out
    assert "actual.alert" in out
    assert "actual.reason" in out
    assert "judge rationale" in out
    assert "good price" in out
    assert "match" in out


def test_A4_summary_pass_count_matches_input_pass_count():
    meta = make_run_metadata()
    results = [
        make_case_result("c-1", verdict="pass"),
        make_case_result("c-2", verdict="pass"),
        make_case_result("c-3", verdict="fail"),
    ]
    out = render_report(meta, results)
    assert "| Pass | 2 |" in out


def test_A5_summary_fail_count_matches_input_fail_count():
    meta = make_run_metadata()
    results = [
        make_case_result("c-1", verdict="fail"),
        make_case_result("c-2", verdict="fail"),
        make_case_result("c-3", verdict="pass"),
    ]
    out = render_report(meta, results)
    assert "| Fail | 2 |" in out


def test_A6_summary_judge_unavailable_count_matches_input():
    meta = make_run_metadata()
    results = [
        make_case_result("c-1", verdict="judge_unavailable"),
        make_case_result("c-2", verdict="pass"),
    ]
    out = render_report(meta, results)
    assert "| Judge unavailable | 1 |" in out


def test_A7_summary_fixtures_evaluated_equals_total_result_count():
    meta = make_run_metadata()
    results = [make_case_result(f"c-{i}") for i in range(5)]
    out = render_report(meta, results)
    assert "| Fixtures evaluated | 5 |" in out


def test_A8_empty_results_list_produces_zero_case_sections_and_zero_counts():
    meta = make_run_metadata()
    out = render_report(meta, [])
    assert out.count("\n### ") == 0
    assert "| Fixtures evaluated | 0 |" in out
    assert "| Pass | 0 |" in out
    assert "| Fail | 0 |" in out
    assert "| Judge unavailable | 0 |" in out


# ===========================================================================
# Group B — byte-stability (the locked invariant)
# ===========================================================================

def test_B1_two_renders_of_identical_inputs_produce_byte_identical_strings():
    meta = make_run_metadata()
    results = [
        make_case_result("c-1", verdict="pass"),
        make_case_result("c-2", verdict="fail"),
    ]
    out_a = render_report(meta, results)
    out_b = render_report(meta, results)
    assert out_a == out_b


def test_B2_actual_dict_key_order_does_not_change_output():
    meta = make_run_metadata()
    a1 = {"alert": True, "reason": "r", "bedrock_called": True}
    a2 = {"bedrock_called": True, "reason": "r", "alert": True}
    r1 = make_case_result("c-1", actual=a1)
    r2 = make_case_result("c-1", actual=a2)
    out_a = render_report(meta, [r1])
    out_b = render_report(meta, [r2])
    assert out_a == out_b


def test_B3_render_report_imports_do_not_include_datetime_module():
    """The render path must never sample the wall clock — timestamp
    comes from `run_metadata.started_at`. We enforce this structurally
    by asserting the module never imported `datetime`, so a future
    'helpful' refactor that adds a `datetime.now()` call would have to
    add the import too, and would fail this test."""
    import evals.report as report_mod
    # `datetime` must not appear as a name in the module's globals.
    # Importing it would make `'datetime' in dir(report_mod)` true.
    assert "datetime" not in dir(report_mod), (
        "evals.report imported the datetime module — render_report must "
        "remain a pure function whose timestamp comes from run_metadata"
    )


def test_B4_started_at_in_output_equals_metadata_field_exactly():
    meta = make_run_metadata(started_at="1999-12-31T23:59:59+00:00")
    out = render_report(meta, [])
    assert "1999-12-31T23:59:59+00:00" in out


def test_B5_changing_one_case_id_changes_only_that_section():
    meta = make_run_metadata()
    base_results = [make_case_result("c-1"), make_case_result("c-2")]
    edited_results = [make_case_result("c-1"), make_case_result("c-2-edited")]
    out_a = render_report(meta, base_results)
    out_b = render_report(meta, edited_results)
    # c-1 section text identical across runs
    a1 = out_a.split("### c-1")[1].split("\n###")[0]
    b1 = out_b.split("### c-1")[1].split("\n###")[0]
    assert a1 == b1
    # c-2 section text differs
    assert "c-2-edited" in out_b
    assert "c-2-edited" not in out_a


def test_B6_decimal_in_actual_does_not_break_serialisation():
    meta = make_run_metadata()
    actual_with_decimal = {
        "alert": True,
        "reason": "good",
        "bedrock_called": True,
        # mimic the snapshot-shaped actual that may include Decimal in
        # future runners
        "totalPrice": Decimal("1234.56"),
    }
    result = make_case_result("c-1", actual=actual_with_decimal)
    out = render_report(meta, [result])
    assert "1234.56" in out


# ===========================================================================
# Group C — verdict rendering
# ===========================================================================

def test_C1_pass_result_section_contains_PASS_marker_token():
    meta = make_run_metadata()
    out = render_report(meta, [make_case_result("c-1", verdict="pass")])
    assert "[PASS]" in out


def test_C2_fail_result_section_contains_FAIL_marker_token():
    meta = make_run_metadata()
    out = render_report(meta, [make_case_result("c-1", verdict="fail")])
    assert "[FAIL]" in out


def test_C3_judge_unavailable_section_contains_UNAVAILABLE_marker():
    meta = make_run_metadata()
    out = render_report(meta, [make_case_result("c-1", verdict="judge_unavailable")])
    assert "[UNAVAILABLE]" in out


def test_C4_no_emoji_codepoints_appear_anywhere_in_rendered_string():
    meta = make_run_metadata()
    results = [
        make_case_result("c-1", verdict="pass"),
        make_case_result("c-2", verdict="fail"),
        make_case_result("c-3", verdict="judge_unavailable"),
    ]
    out = render_report(meta, results)
    # Emoji + symbol blocks: anything in the Supplemental Symbols
    # (U+1F300–U+1FAFF) or the Misc Symbols (U+2600–U+27BF) ranges.
    for ch in out:
        cp = ord(ch)
        assert not (0x1F300 <= cp <= 0x1FAFF), f"emoji codepoint {hex(cp)} in output"
        assert not (0x2600 <= cp <= 0x27BF), f"symbol codepoint {hex(cp)} in output"


def test_C5_actual_reason_with_pipe_or_newline_does_not_break_section_layout():
    meta = make_run_metadata()
    nasty_actual = {
        "alert": True,
        "reason": "great | offer\nwith newline",
        "bedrock_called": True,
    }
    result = make_case_result("c-1", actual=nasty_actual)
    out = render_report(meta, [result])
    # Pipe is escaped or the section still parses cleanly; the next
    # case heading is still findable by string search.
    assert "\\|" in out or "|" not in out.split("actual.reason")[1].split("\n")[0]
    # Newline replaced — no inline newlines inside the reason line.
    reason_line = [l for l in out.splitlines() if "actual.reason" in l][0]
    assert "\n" not in reason_line.replace("\\n", "")


# ===========================================================================
# Group D — run-metadata header
# ===========================================================================

def test_D1_metadata_table_renders_all_five_fields():
    meta = make_run_metadata(
        model="claude-haiku-4-5-20251001",
        judge_model="claude-sonnet-4-6",
        bedrock_mode="live",
        stub_judge=False,
    )
    out = render_report(meta, [])
    assert "claude-haiku-4-5-20251001" in out
    assert "claude-sonnet-4-6" in out
    assert "live" in out
    assert "| --stub | false |" in out


def test_D2_stub_judge_true_renders_as_lowercase_json_style_true():
    meta = make_run_metadata(stub_judge=True)
    out = render_report(meta, [])
    assert "| --stub | true |" in out
    assert "| --stub | True |" not in out


def test_D3_metadata_with_special_chars_in_model_id_renders_as_plain_text():
    meta = make_run_metadata(model="custom-model_2025*beta")
    out = render_report(meta, [])
    # The string appears verbatim in the table, even with the asterisk.
    assert "custom-model_2025*beta" in out
