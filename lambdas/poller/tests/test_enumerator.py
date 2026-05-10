"""Tests for `enumerator.iter_active_watches`.

Covers:
- Status filter: returns active rows, hides paused / archived.
- Empty table: returns nothing without raising.
- Multi-user partitioning: returns rows across different userIds in one Scan.
- Pagination: when DynamoDB returns `LastEvaluatedKey`, the iterator follows
  it transparently. We simulate this by stuffing enough items into one
  partition that DynamoDB has to break the response into multiple pages.
- Misconfiguration: missing `WATCHES_TABLE_NAME` raises a clear error
  instead of crashing on `NoneType.scan()`.

The `multi_page` test uses 400 items × ~1KB each; moto enforces the same
1MB page boundary as real DDB, so the iterator MUST resume from
`LastEvaluatedKey`. If pagination is broken, this test will return only
the first page's worth of rows and the assertion will fail.
"""

import importlib
import os
import sys

import pytest

from tests.conftest import make_watch


def test_returns_only_active_watches(enumerator_module):
    enumerator, watches = enumerator_module
    watches.put_item(Item=make_watch("u1", "w-active",   status="active"))
    watches.put_item(Item=make_watch("u1", "w-paused",   status="paused"))
    watches.put_item(Item=make_watch("u1", "w-archived", status="archived"))

    ids = sorted(w["watchId"] for w in enumerator.iter_active_watches())

    assert ids == ["w-active"]


def test_empty_table_returns_no_rows(enumerator_module):
    enumerator, _ = enumerator_module

    assert list(enumerator.iter_active_watches()) == []


def test_returns_active_rows_across_users(enumerator_module):
    enumerator, watches = enumerator_module
    watches.put_item(Item=make_watch("u1", "wA", status="active",   destination="Tokyo"))
    watches.put_item(Item=make_watch("u2", "wB", status="active",   destination="Paris"))
    watches.put_item(Item=make_watch("u3", "wC", status="paused",   destination="London"))
    watches.put_item(Item=make_watch("u3", "wD", status="active",   destination="Rome"))

    rows = list(enumerator.iter_active_watches())
    seen = sorted((r["userId"], r["watchId"]) for r in rows)

    assert seen == [("u1", "wA"), ("u2", "wB"), ("u3", "wD")]


def test_paginates_when_scan_returns_last_evaluated_key(enumerator_module):
    """Insert enough rows to force at least two Scan pages from moto.

    Each row is padded with a ~3KB `preferences.notes` field so 400 rows are
    well over the 1MB DDB page cap, guaranteeing pagination kicks in.
    """
    enumerator, watches = enumerator_module
    pad = "x" * 3000  # 3 KB
    expected_ids = []
    with watches.batch_writer() as bw:
        for i in range(400):
            wid = f"w-{i:04d}"
            bw.put_item(Item=make_watch(
                "u1", wid,
                status="active",
                preferences={"notes": pad},
            ))
            expected_ids.append(wid)

    ids = sorted(w["watchId"] for w in enumerator.iter_active_watches())

    assert ids == sorted(expected_ids), \
        f"pagination dropped rows: got {len(ids)} of {len(expected_ids)}"


def test_missing_table_env_var_raises_clear_error():
    """Reimport `enumerator` with WATCHES_TABLE_NAME unset and assert the
    iterator raises EnvironmentError, not AttributeError on NoneType.scan().
    """
    saved = os.environ.pop("WATCHES_TABLE_NAME", None)
    try:
        sys.modules.pop("enumerator", None)
        enumerator = importlib.import_module("enumerator")
        with pytest.raises(EnvironmentError, match="WATCHES_TABLE_NAME"):
            list(enumerator.iter_active_watches())
    finally:
        if saved is not None:
            os.environ["WATCHES_TABLE_NAME"] = saved
        sys.modules.pop("enumerator", None)
