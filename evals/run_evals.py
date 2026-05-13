"""
CLI entrypoint for trip-tracker decision-quality evals.

Owns the orchestration loop:
  1. Resolve `lambdas/poller/` on `sys.path` (anchored to this file's
     location, not the current working directory) so `import
     bedrock_decide` works whether the runner is launched from the repo
     root, from `evals/`, or from any other cwd.
  2. Load fixtures via `evals.loader`.
  3. For each fixture: call `bedrock_decide.decide(snapshot, watch,
     history)` (respects `BEDROCK_MODE`), then `evals.judge_client.judge`
     against the user-controlled `--stub` flag.
  4. Render a markdown report to `--out`. The output path's parent
     directory is created if needed so the runner doesn't fail at the
     last step on a missing `evals/results/` dir.
  5. Exit code is `0` iff every result's verdict is `pass`. Any `fail`
     or `judge_unavailable` collapses to `1`. Loader / IO errors also
     exit `1` (and log a clear stderr message). Argparse usage errors
     follow argparse's own convention (exit code 2).

The judge model defaults to `claude-sonnet-4-6` and is overridable via
`--judge-model` for forward compatibility. The under-test model ID is
not overridable here — it's pinned in `bedrock_decide.BEDROCK_MODEL_ID`
and selected at deploy time via the CDK context flag.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_POLLER_DIR = _REPO_ROOT / "lambdas" / "poller"


def _ensure_sys_path() -> None:
    """Add the repo root + `lambdas/poller/` to `sys.path` so the
    `evals.*` package imports and `import bedrock_decide` resolve
    regardless of which cwd the runner was launched from. Anchored to
    this file's location, not cwd. Called from `main()` rather than at
    import time so importing `evals.run_evals` is a side-effect-free
    operation for any consumer that wants the symbols without the path
    mutation."""
    for p in (str(_REPO_ROOT), str(_POLLER_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)


# Path mutation happens once at import time too, because the `from
# evals.* import ...` lines below would fail when the runner is launched
# as `python evals/run_evals.py` from outside the repo. `main()` calls
# `_ensure_sys_path()` again to cover the case where someone imports
# this module first and only later invokes `main()`.
_ensure_sys_path()


from evals.loader import FixtureError, load_fixtures
from evals.judge_client import DEFAULT_JUDGE_MODEL, JudgeResult, judge
from evals.report import CaseResult, RunMetadata, render_report


logger = logging.getLogger("evals.run_evals")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_evals",
        description="Run trip-tracker decision-quality evals against a fixture corpus.",
    )
    parser.add_argument("--fixtures-dir", required=True, type=Path,
                        help="Directory containing *.json fixtures.")
    parser.add_argument("--out", required=True, type=Path,
                        help="Markdown report destination.")
    parser.add_argument("--stub", action="store_true",
                        help="Use the deterministic local judge instead of "
                             "the Anthropic API. Independent of BEDROCK_MODE.")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL,
                        help=f"Judge model ID (default: {DEFAULT_JUDGE_MODEL}).")
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        help="Logger level (default: INFO).")
    return parser.parse_args(argv)


def _configure_logging(level: str) -> None:
    """Configure root logging on first call only.

    A host process (a future Makefile target, a CI runner, a parent
    Python program that imported `main`) may have already installed
    handlers. Re-running `basicConfig(force=True)` would nuke them; we
    instead set the level and return early if any handler is already
    attached to the root logger.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level))
    if root.handlers:
        return
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _run_one_case(fixture, *, stub: bool, judge_model: str):
    import bedrock_decide

    actual = bedrock_decide.decide(fixture.snapshot, fixture.watch, fixture.history)
    verdict = judge(fixture, actual, stub=stub, model=judge_model)
    return CaseResult(
        case_id=fixture.case_id,
        expected_alert=fixture.expected_alert,
        actual=actual,
        judge=verdict,
    )


def main(argv: list[str] | None = None) -> int:
    _ensure_sys_path()
    args = _parse_args(argv)
    _configure_logging(args.log_level)

    try:
        fixtures = load_fixtures(args.fixtures_dir)
    except FixtureError as e:
        logger.error("fixture_load_failed: %s", e)
        print(f"fixture_load_failed: {e}", file=sys.stderr)
        return 1

    results: list[CaseResult] = []
    for fx in fixtures:
        logger.info("eval_case_start case_id=%s expected_alert=%s",
                    fx.case_id, fx.expected_alert)
        try:
            results.append(_run_one_case(fx, stub=args.stub, judge_model=args.judge_model))
        except Exception as e:
            # Any per-fixture failure (a stray KeyError inside
            # bedrock_decide on a malformed snapshot, an import bug
            # surfacing late, etc.) is degraded to a judge_unavailable
            # row so the run still forward-progresses through the rest
            # of the corpus and the report names the offending case.
            logger.warning(
                "runner_case_failed case_id=%s error=%s",
                fx.case_id, type(e).__name__,
            )
            results.append(CaseResult(
                case_id=fx.case_id,
                expected_alert=fx.expected_alert,
                actual={"alert": None, "reason": "runner_error", "bedrock_called": False},
                judge=JudgeResult(
                    verdict="judge_unavailable",
                    rationale=f"runner_error: {type(e).__name__}: {str(e)[:160]}",
                ),
            ))
        logger.info("eval_case_done case_id=%s verdict=%s",
                    fx.case_id, results[-1].judge.verdict)

    metadata = RunMetadata(
        started_at=datetime.now(timezone.utc).isoformat(),
        # The actual under-test model ID lives in bedrock_decide; expose it
        # in the report so a reviewer sees what was tested.
        model=_under_test_model_id(),
        judge_model=args.judge_model,
        bedrock_mode=os.environ.get("BEDROCK_MODE", "live"),
        stub_judge=args.stub,
    )
    report_text = render_report(metadata, results)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report_text, encoding="utf-8")

    return 0 if all(r.judge.verdict == "pass" for r in results) else 1


def _under_test_model_id() -> str:
    """Expose `bedrock_decide.BEDROCK_MODEL_ID` without forcing an import
    at module load (sys.path has to be patched first; the import happens
    inside `_run_one_case`)."""
    try:
        import bedrock_decide
        return bedrock_decide.BEDROCK_MODEL_ID
    except Exception:
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())
