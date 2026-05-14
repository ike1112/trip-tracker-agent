"""
Cross-language sync gate: assert that the JS-side dashboard construct
and the Python-side metrics module agree on namespace + metric names.

If someone updates `lambdas/poller/metrics.py` without bumping
`lib/observability-dashboard.js` (or vice versa), the dashboard would
graph the wrong namespace or miss a counter — visible only via "metric
shows zero" in the CloudWatch console with no error anywhere.

Reads the JS construct as text and regex-extracts the constants. No
node interpreter required.
"""

import re
from pathlib import Path

import metrics

REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_JS = REPO_ROOT / "lib" / "observability-dashboard.js"


def _extract_namespace(text: str) -> str:
    m = re.search(
        r"const\s+POLLER_METRIC_NAMESPACE\s*=\s*'([^']+)';",
        text,
    )
    assert m, "POLLER_METRIC_NAMESPACE not found in lib/observability-dashboard.js"
    return m.group(1)


def _extract_metric_names(text: str) -> list[str]:
    block_match = re.search(
        r"const\s+POLLER_METRIC_NAMES\s*=\s*\[(.*?)\];",
        text,
        re.DOTALL,
    )
    assert block_match, "POLLER_METRIC_NAMES not found in lib/observability-dashboard.js"
    return re.findall(r"'([^']+)'", block_match.group(1))


def test_dashboard_js_namespace_matches_python_namespace():
    text = DASHBOARD_JS.read_text(encoding="utf-8")
    assert _extract_namespace(text) == metrics.NAMESPACE


def test_dashboard_js_metric_names_match_python_metric_constants():
    text = DASHBOARD_JS.read_text(encoding="utf-8")
    js_names = _extract_metric_names(text)
    python_names = [
        metrics.WATCHES_POLLED,
        metrics.WATCHES_ERRORED,
        metrics.BEDROCK_DECISIONS_MADE,
        metrics.ALERTS_SENT,
    ]
    assert js_names == python_names, (
        f"JS-side metric names {js_names} differ from Python-side {python_names}; "
        "update lib/observability-dashboard.js or lambdas/poller/metrics.py to match"
    )


def test_dashboard_js_file_exists():
    assert DASHBOARD_JS.is_file(), (
        f"expected dashboard construct at {DASHBOARD_JS} but it is missing"
    )
