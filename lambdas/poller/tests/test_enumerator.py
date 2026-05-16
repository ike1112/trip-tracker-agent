"""Tests for `enumerator.iter_active_watches`.

Covers:
- Status filter: returns active rows, hides paused / archived.
- Empty table: returns nothing without raising.
- Multi-user: returns rows across different userIds (the GSI is keyed on
  `status`, not `userId`, so the Query spans all users).
- Pagination: when DynamoDB returns `LastEvaluatedKey`, the iterator
  follows it transparently. Proven two ways — a real-moto smoke (many
  padded rows) and a deterministic stubbed-`query` test that does not
  depend on moto's GSI-pagination fidelity.
- Query-not-Scan: the read goes through `query(IndexName="status-index")`
  and the full row is returned (GSI projects ALL).
- Misconfiguration: missing `WATCHES_TABLE_NAME` raises a clear error
  instead of crashing on `NoneType.query()`.

Yield order is unspecified (GSI Query does not guarantee order); the
order-insensitive tests sort before asserting on purpose.
"""

import importlib
import os
import sys

import pytest

from boto3.dynamodb.conditions import Key

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


def test_query_not_scan_and_full_row_projected(enumerator_module):
    """The read is a GSI Query (never a Scan) and returns the full row.

    Wraps the bound table's `query` to capture its kwargs and makes
    `scan` fail if ever called. Asserts the index name + key condition,
    and that a yielded row still carries `preferences`/`dateWindow`
    (ProjectionType ALL — the poller needs the whole row).
    """
    enumerator, watches = enumerator_module
    watches.put_item(Item=make_watch(
        "u1", "w1", status="active",
        preferences={"cabin": "economy"},
    ))

    captured = {}
    real_query = enumerator._watches_table.query

    def spy_query(**kwargs):
        captured.update(kwargs)
        return real_query(**kwargs)

    enumerator._watches_table.query = spy_query
    enumerator._watches_table.scan = lambda **k: pytest.fail(
        "enumerator must Query the GSI, never Scan"
    )

    rows = list(enumerator.iter_active_watches())

    assert captured["IndexName"] == "status-index"
    expr = captured["KeyConditionExpression"].get_expression()
    assert expr["operator"] == "="
    assert expr["values"][0].name == "status"
    assert expr["values"][1] == "active"
    assert len(rows) == 1
    assert rows[0]["preferences"] == {"cabin": "economy"}
    assert rows[0]["dateWindow"]["nights"] == 5


def test_pagination_follows_last_evaluated_key_deterministically(enumerator_module):
    """Prove the `LastEvaluatedKey` loop independent of moto fidelity.

    Replaces the bound table with a stub whose `query` returns a first
    page WITH a `LastEvaluatedKey`, then a second page without. Asserts
    both pages' rows are yielded in order and that call 2 received the
    key from call 1 as `ExclusiveStartKey`.
    """
    enumerator, _ = enumerator_module

    calls = []

    class _StubTable:
        def query(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {"Items": [{"watchId": "wA"}], "LastEvaluatedKey": {"k": 1}}
            return {"Items": [{"watchId": "wB"}]}

    enumerator._watches_table = _StubTable()

    rows = list(enumerator.iter_active_watches())

    assert [r["watchId"] for r in rows] == ["wA", "wB"]
    assert len(calls) == 2
    assert "ExclusiveStartKey" not in calls[0]
    assert calls[1]["ExclusiveStartKey"] == {"k": 1}
    assert calls[1]["IndexName"] == "status-index"


def test_paginates_when_query_returns_last_evaluated_key(enumerator_module):
    """Real-moto smoke: enough padded rows to exceed the 1MB page cap.

    Each row is padded with a ~3KB `preferences.notes` field so 400 rows
    are well over the 1MB DDB page cap. This is a smoke check; the
    deterministic pagination contract is pinned by
    `test_pagination_follows_last_evaluated_key_deterministically`.
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
    iterator raises EnvironmentError, not AttributeError on NoneType.query().
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
