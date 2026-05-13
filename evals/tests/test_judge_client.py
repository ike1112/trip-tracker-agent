"""Behavioural tests for `evals.judge_client.judge` — covers stub-mode
correctness, live-mode call shape, strict JSON parsing of the response,
and the error-to-`judge_unavailable` mapping. No real Anthropic API
calls fire; live-mode tests mock `anthropic.Anthropic`.

Test groups:
  A: stub-mode label match
  B: stub-mode label mismatch
  C: live-mode call shape (mocked SDK)
  D: live-mode strict response parsing
  E: live-mode error mapping
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from evals.judge_client import JudgeResult, judge
from evals.tests.conftest import make_fixture, mock_anthropic_response


# ===========================================================================
# Group A — stub-mode label match
# ===========================================================================

def test_A1_stub_expected_true_actual_true_returns_verdict_pass():
    fx = make_fixture(expected_alert=True)
    actual = {"alert": True, "reason": "good price", "bedrock_called": True}
    result = judge(fx, actual, stub=True)
    assert result.verdict == "pass"


def test_A2_stub_expected_false_actual_false_returns_verdict_pass():
    fx = make_fixture(expected_alert=False)
    actual = {"alert": False, "reason": "over budget", "bedrock_called": True}
    result = judge(fx, actual, stub=True)
    assert result.verdict == "pass"


def test_A3_stub_pass_rationale_is_nonempty_and_mentions_alert_match():
    fx = make_fixture(expected_alert=True)
    actual = {"alert": True, "reason": "x"}
    result = judge(fx, actual, stub=True)
    assert result.rationale
    assert "match" in result.rationale.lower() or "alert" in result.rationale.lower()


def test_A4_stub_mode_never_imports_anthropic_sdk_client():
    fx = make_fixture(expected_alert=True)
    actual = {"alert": True, "reason": "x"}
    # If stub mode were to accidentally construct the client, this patch
    # would raise on import inside the call. We assert it does NOT raise.
    with patch("anthropic.Anthropic", side_effect=AssertionError("must not be called in stub mode")):
        result = judge(fx, actual, stub=True)
    assert result.verdict == "pass"


def test_A5_stub_mode_is_deterministic_two_calls_same_inputs_return_equal_JudgeResult():
    fx = make_fixture(expected_alert=False)
    actual = {"alert": False, "reason": "fine"}
    r1 = judge(fx, actual, stub=True)
    r2 = judge(fx, actual, stub=True)
    assert r1 == r2


# ===========================================================================
# Group B — stub-mode label mismatch
# ===========================================================================

def test_B1_stub_expected_true_actual_false_returns_verdict_fail():
    fx = make_fixture(expected_alert=True)
    actual = {"alert": False, "reason": "stable"}
    result = judge(fx, actual, stub=True)
    assert result.verdict == "fail"


def test_B2_stub_expected_false_actual_true_returns_verdict_fail():
    fx = make_fixture(expected_alert=False)
    actual = {"alert": True, "reason": "anomaly"}
    result = judge(fx, actual, stub=True)
    assert result.verdict == "fail"


def test_B3_stub_fail_rationale_mentions_expected_and_actual_alert_values():
    fx = make_fixture(expected_alert=True)
    actual = {"alert": False, "reason": "x"}
    result = judge(fx, actual, stub=True)
    assert "True" in result.rationale or "true" in result.rationale.lower()
    assert "False" in result.rationale or "false" in result.rationale.lower()


def test_B4_stub_missing_alert_key_in_actual_returns_verdict_fail_not_keyerror():
    fx = make_fixture(expected_alert=True)
    actual = {"reason": "no alert key here"}
    result = judge(fx, actual, stub=True)
    assert result.verdict == "fail"
    assert "missing" in result.rationale.lower() or "non-bool" in result.rationale.lower()


# ===========================================================================
# Group C — live-mode call shape (mocked SDK)
# ===========================================================================

def _live_judge_with_response(text: str, *, fixture=None, actual=None, model="claude-sonnet-4-6"):
    fx = fixture or make_fixture()
    a = actual or {"alert": True, "reason": "good", "bedrock_called": True}
    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_anthropic_response(text)
        result = judge(fx, a, stub=False, model=model)
    return result, mock_cls, mock_client


def test_C1_live_calls_messages_create_with_judge_model_argument_exactly():
    _, _, mock_client = _live_judge_with_response(
        json.dumps({"verdict": "pass", "rationale": "ok"}),
        model="claude-sonnet-4-6",
    )
    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"


def test_C2_live_system_argument_is_decision_rubric_markdown_string():
    _, _, mock_client = _live_judge_with_response(
        json.dumps({"verdict": "pass", "rationale": "ok"}),
    )
    system = mock_client.messages.create.call_args.kwargs["system"]
    assert isinstance(system, str)
    assert "rubric" in system.lower() or "judge" in system.lower()
    # Sentinel markdown headings from `judge_prompts/decision.md`.
    assert "Decision-quality judge rubric" in system


def test_C3_live_messages_user_content_contains_case_id_expected_alert_and_actual():
    fx = make_fixture(case_id="case-007", expected_alert=False)
    actual = {"alert": True, "reason": "anomaly", "bedrock_called": True}
    _, _, mock_client = _live_judge_with_response(
        json.dumps({"verdict": "fail", "rationale": "model alerted on stable case"}),
        fixture=fx, actual=actual,
    )
    user_content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "case-007" in user_content
    assert "expected_alert" in user_content
    assert "false" in user_content.lower() or "False" in user_content
    assert "anomaly" in user_content


def test_C4_live_messages_user_content_uses_sort_keys_true_for_determinism():
    fx = make_fixture(case_id="case-x")
    # Build actuals with the same keys but different insertion order.
    a_one = {"alert": True, "reason": "r", "bedrock_called": True}
    a_two = {"bedrock_called": True, "alert": True, "reason": "r"}
    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_anthropic_response(
            json.dumps({"verdict": "pass", "rationale": "ok"})
        )
        judge(fx, a_one, stub=False)
        c1 = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
        mock_client.messages.create.reset_mock()
        mock_client.messages.create.return_value = mock_anthropic_response(
            json.dumps({"verdict": "pass", "rationale": "ok"})
        )
        judge(fx, a_two, stub=False)
        c2 = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert c1 == c2


def test_C5_live_messages_role_is_user_only_no_assistant_or_system_in_messages_list():
    _, _, mock_client = _live_judge_with_response(
        json.dumps({"verdict": "pass", "rationale": "ok"}),
    )
    messages = mock_client.messages.create.call_args.kwargs["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"


# ===========================================================================
# Group D — live-mode strict response parsing
# ===========================================================================

def test_D1_valid_pass_json_response_returns_JudgeResult_verdict_pass():
    result, _, _ = _live_judge_with_response(
        json.dumps({"verdict": "pass", "rationale": "good"}),
    )
    assert result.verdict == "pass"
    assert result.rationale == "good"


def test_D2_valid_fail_json_response_returns_JudgeResult_verdict_fail():
    result, _, _ = _live_judge_with_response(
        json.dumps({"verdict": "fail", "rationale": "alert mismatch"}),
    )
    assert result.verdict == "fail"
    assert "mismatch" in result.rationale


def test_D3_markdown_fenced_json_returns_judge_unavailable():
    fenced = "```json\n" + json.dumps({"verdict": "pass", "rationale": "ok"}) + "\n```"
    result, _, _ = _live_judge_with_response(fenced)
    assert result.verdict == "judge_unavailable"


def test_D4_extra_keys_returns_judge_unavailable():
    result, _, _ = _live_judge_with_response(
        json.dumps({"verdict": "pass", "rationale": "ok", "score": 0.9}),
    )
    assert result.verdict == "judge_unavailable"


def test_D5_missing_verdict_key_returns_judge_unavailable():
    result, _, _ = _live_judge_with_response(json.dumps({"rationale": "ok"}))
    assert result.verdict == "judge_unavailable"


def test_D6_verdict_value_outside_pass_fail_returns_judge_unavailable():
    result, _, _ = _live_judge_with_response(
        json.dumps({"verdict": "maybe", "rationale": "ok"}),
    )
    assert result.verdict == "judge_unavailable"


def test_D7_rationale_non_string_returns_judge_unavailable():
    result, _, _ = _live_judge_with_response(
        json.dumps({"verdict": "pass", "rationale": 42}),
    )
    assert result.verdict == "judge_unavailable"


def test_D8_non_json_text_response_returns_judge_unavailable():
    result, _, _ = _live_judge_with_response("Sure! The verdict is pass.")
    assert result.verdict == "judge_unavailable"


def test_D9_response_with_zero_content_blocks_returns_judge_unavailable():
    fx = make_fixture()
    actual = {"alert": True, "reason": "r"}
    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        empty_response = MagicMock()
        empty_response.content = []
        mock_client.messages.create.return_value = empty_response
        result = judge(fx, actual, stub=False)
    assert result.verdict == "judge_unavailable"


def test_D10_response_with_tool_use_then_text_block_picks_text_block():
    """A response whose first block is `tool_use` and second is the
    judge's `text` reply must still parse — we don't read `[0].text`
    blindly."""
    fx = make_fixture()
    actual = {"alert": True, "reason": "r"}
    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        # First block is tool_use (has no .text). Second is text.
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        del tool_block.text  # explicitly: no .text attr
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = json.dumps({"verdict": "pass", "rationale": "ok"})
        mixed_response = MagicMock()
        mixed_response.content = [tool_block, text_block]
        mock_client.messages.create.return_value = mixed_response
        result = judge(fx, actual, stub=False)
    assert result.verdict == "pass"


def test_D11_response_with_only_non_text_blocks_returns_judge_unavailable():
    fx = make_fixture()
    actual = {"alert": True, "reason": "r"}
    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        only_tool = MagicMock()
        only_tool.content = [tool_block]
        mock_client.messages.create.return_value = only_tool
        result = judge(fx, actual, stub=False)
    assert result.verdict == "judge_unavailable"
    assert "no text" in result.rationale.lower()


# ===========================================================================
# Group E — live-mode error mapping
# ===========================================================================

def _live_judge_with_exception(exc):
    fx = make_fixture()
    actual = {"alert": True, "reason": "r"}
    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.side_effect = exc
        return judge(fx, actual, stub=False)


def test_E1_generic_exception_returns_judge_unavailable():
    result = _live_judge_with_exception(RuntimeError("boom"))
    assert result.verdict == "judge_unavailable"


def test_E2_value_error_returns_judge_unavailable():
    result = _live_judge_with_exception(ValueError("bad"))
    assert result.verdict == "judge_unavailable"


def test_E3_connection_error_returns_judge_unavailable():
    result = _live_judge_with_exception(ConnectionError("no network"))
    assert result.verdict == "judge_unavailable"


def test_E4_judge_unavailable_rationale_contains_exception_class_name():
    result = _live_judge_with_exception(RuntimeError("specific message"))
    assert "RuntimeError" in result.rationale


def test_E5_judge_unavailable_rationale_truncated_to_under_200_chars():
    huge_msg = "x" * 500
    result = _live_judge_with_exception(RuntimeError(huge_msg))
    assert len(result.rationale) <= 200


def test_E6_anthropic_specific_apierror_returns_judge_unavailable():
    import anthropic
    # APIConnectionError is a concrete public subclass; build via its
    # request kwarg pattern so we don't rely on internal constructors.
    try:
        exc = anthropic.APIConnectionError(request=MagicMock())
    except TypeError:
        # SDK version drift fallback — any anthropic.APIError-derived
        # exception with the standard constructor shape.
        exc = RuntimeError("APIConnectionError stand-in")
    result = _live_judge_with_exception(exc)
    assert result.verdict == "judge_unavailable"


def test_E7_judge_call_with_failing_sdk_never_raises_out_of_judge_function():
    # If `judge()` raised, pytest itself would surface the exception
    # before our assertion. We assert nothing was raised by reading the
    # result.
    result = _live_judge_with_exception(RuntimeError("raise me"))
    assert isinstance(result, JudgeResult)


def test_E8_rubric_file_missing_returns_judge_unavailable_with_recognisable_rationale(monkeypatch, tmp_path):
    """If `evals/judge_prompts/decision.md` is deleted (or the path is
    wrong) the live-mode call must surface that as `judge_unavailable`
    with a rationale that names the failure mode — not a silent pass."""
    import evals.judge_client as jc
    missing_path = tmp_path / "deleted-rubric.md"  # does not exist
    monkeypatch.setattr(jc, "_RUBRIC_PATH", missing_path)
    fx = make_fixture()
    actual = {"alert": True, "reason": "r"}
    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_anthropic_response(
            json.dumps({"verdict": "pass", "rationale": "ok"})
        )
        result = jc.judge(fx, actual, stub=False)
    assert result.verdict == "judge_unavailable"
    assert "FileNotFoundError" in result.rationale or "rubric" in result.rationale.lower() or "no such file" in result.rationale.lower()
