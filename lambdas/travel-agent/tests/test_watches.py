"""
Unit tests for the watches data layer.

Focus areas:
  - CRUD round-trips work and write the spec'd schema (§3 of the design spec).
  - Ownership is enforced: every operation keyed on (userId, watchId) cannot
    reach another user's row even when given a valid watchId from that user.
    This is the production-readiness signal — if these tests pass, a prompt
    injection that fabricates a watchId can't exfiltrate or mutate data.
  - Status transitions (pause / resume / archive) work via the same path.
  - get_fare_history checks ownership before hitting the FareHistory table.
"""

import boto3
from datetime import datetime, timedelta, timezone

USER_A = "user-aaaaaaaa-1111-1111-1111-111111111111"
USER_B = "user-bbbbbbbb-2222-2222-2222-222222222222"


def _make_watch(watches, user_id):
    return watches.create_watch(
        user_id=user_id,
        origin="SFO",
        destination="Tokyo",
        destination_airport="NRT",
        date_window={
            "earliestDepart": "2026-10-15",
            "latestDepart": "2026-10-20",
            "nights": 5,
        },
        pax=1,
        max_total_price=1500,
        preferences={"maxStops": 1},
    )


# ---------------------------------------------------------------------------
# Schema + round-trip
# ---------------------------------------------------------------------------

def test_create_watch_writes_full_schema(watches_module):
    w = _make_watch(watches_module, USER_A)

    assert w["userId"] == USER_A
    assert isinstance(w["watchId"], str) and len(w["watchId"]) > 0
    assert w["type"] == "specific"
    assert w["origin"] == "SFO"
    assert w["destination"] == "Tokyo"
    assert w["destinationAirport"] == "NRT"
    assert w["dateWindow"]["nights"] == 5
    assert w["pax"] == 1
    assert w["maxTotalPrice"] == 1500
    assert w["preferences"] == {"maxStops": 1}
    assert w["status"] == "active"
    assert w["alertStrategy"] == "both"
    # `lastAlertedAt` / `lastAlertedPrice` are absent on a fresh watch
    # (never written as None / NULL — see comment in create_watch for
    # why; setting them as NULL silently breaks the notifier's dedup
    # writeback condition).
    assert "lastAlertedAt" not in w
    assert "lastAlertedPrice" not in w
    assert w["createdAt"] == w["updatedAt"]


def test_get_watch_round_trip(watches_module):
    created = _make_watch(watches_module, USER_A)
    fetched = watches_module.get_watch(USER_A, created["watchId"])
    assert fetched is not None
    assert fetched["watchId"] == created["watchId"]
    assert fetched["destination"] == "Tokyo"


def test_list_watches_default_active_only(watches_module):
    a1 = _make_watch(watches_module, USER_A)
    a2 = _make_watch(watches_module, USER_A)
    watches_module.set_watch_status(USER_A, a2["watchId"], "archived")

    active = watches_module.list_watches(USER_A)
    ids = {w["watchId"] for w in active}
    assert a1["watchId"] in ids
    assert a2["watchId"] not in ids


def test_list_watches_status_none_returns_all_statuses(watches_module):
    a1 = _make_watch(watches_module, USER_A)
    a2 = _make_watch(watches_module, USER_A)
    watches_module.set_watch_status(USER_A, a2["watchId"], "paused")

    everything = watches_module.list_watches(USER_A, status=None)
    assert len(everything) == 2


# ---------------------------------------------------------------------------
# Ownership boundary — the load-bearing security tests
# ---------------------------------------------------------------------------

def test_get_watch_returns_none_for_other_users_watch_id(watches_module):
    """User B can't read User A's watch even given the exact watchId."""
    a = _make_watch(watches_module, USER_A)
    assert watches_module.get_watch(USER_B, a["watchId"]) is None


def test_update_watch_returns_none_for_other_users_watch_id(watches_module):
    a = _make_watch(watches_module, USER_A)
    result = watches_module.update_watch(
        USER_B, a["watchId"], {"maxTotalPrice": 1}
    )
    assert result is None
    # And the original row must be untouched.
    untouched = watches_module.get_watch(USER_A, a["watchId"])
    assert untouched["maxTotalPrice"] == 1500


def test_set_watch_status_does_not_cross_users(watches_module):
    a = _make_watch(watches_module, USER_A)
    result = watches_module.set_watch_status(USER_B, a["watchId"], "archived")
    assert result is None
    untouched = watches_module.get_watch(USER_A, a["watchId"])
    assert untouched["status"] == "active"


def test_list_watches_does_not_leak_across_users(watches_module):
    _make_watch(watches_module, USER_A)
    _make_watch(watches_module, USER_A)
    _make_watch(watches_module, USER_B)

    a_watches = watches_module.list_watches(USER_A)
    b_watches = watches_module.list_watches(USER_B)
    assert len(a_watches) == 2
    assert len(b_watches) == 1
    assert all(w["userId"] == USER_A for w in a_watches)
    assert all(w["userId"] == USER_B for w in b_watches)


# ---------------------------------------------------------------------------
# update_watch field discipline
# ---------------------------------------------------------------------------

def test_update_watch_applies_patches(watches_module):
    a = _make_watch(watches_module, USER_A)
    updated = watches_module.update_watch(
        USER_A, a["watchId"], {"maxTotalPrice": 1700, "destination": "Kyoto"}
    )
    assert updated["maxTotalPrice"] == 1700
    assert updated["destination"] == "Kyoto"
    # Untouched fields preserved
    assert updated["origin"] == "SFO"
    # updatedAt advanced
    assert updated["updatedAt"] >= a["createdAt"]


def test_update_watch_ignores_immutable_fields(watches_module):
    a = _make_watch(watches_module, USER_A)
    result = watches_module.update_watch(
        USER_A,
        a["watchId"],
        {"userId": "evil", "watchId": "also-evil", "createdAt": "1970-01-01"},
    )
    # The update is a no-op patch (immutables filtered out) — should still
    # return the row, with identity intact.
    assert result is not None
    assert result["userId"] == USER_A
    assert result["watchId"] == a["watchId"]
    assert result["createdAt"] == a["createdAt"]


def test_update_watch_with_empty_patches_returns_current(watches_module):
    a = _make_watch(watches_module, USER_A)
    result = watches_module.update_watch(USER_A, a["watchId"], {})
    assert result == a


def test_update_watch_returns_none_for_unknown_watch(watches_module):
    result = watches_module.update_watch(
        USER_A, "watch-that-does-not-exist", {"status": "paused"}
    )
    assert result is None


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

def test_pause_resume_archive_round_trip(watches_module):
    a = _make_watch(watches_module, USER_A)
    paused = watches_module.set_watch_status(USER_A, a["watchId"], "paused")
    assert paused["status"] == "paused"
    resumed = watches_module.set_watch_status(USER_A, a["watchId"], "active")
    assert resumed["status"] == "active"
    archived = watches_module.set_watch_status(USER_A, a["watchId"], "archived")
    assert archived["status"] == "archived"


# ---------------------------------------------------------------------------
# get_fare_history — ownership gate before history read
# ---------------------------------------------------------------------------

def _seed_fare_history(watches_module, watch_id, n=3):
    """Drop n FareHistory rows for a given watch_id, oldest first."""
    table = watches_module._fare_history_table
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for i in range(n):
        ts = (base + timedelta(hours=i * 4)).isoformat()
        table.put_item(
            Item={
                "watchId": watch_id,
                "timestamp": ts,
                "flightPrice": 800 + i * 10,
                "hotelPrice": 600 + i * 5,
                "totalPrice": 1400 + i * 15,
                "ttl": 9999999999,
            }
        )


def test_get_fare_history_returns_newest_first_for_owner(watches_module):
    a = _make_watch(watches_module, USER_A)
    _seed_fare_history(watches_module, a["watchId"], n=3)

    rows = watches_module.get_fare_history(USER_A, a["watchId"], limit=10)
    assert len(rows) == 3
    timestamps = [r["timestamp"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


def test_get_fare_history_empty_for_other_user(watches_module):
    """Even if the watchId is real and has fare rows, a different user gets nothing."""
    a = _make_watch(watches_module, USER_A)
    _seed_fare_history(watches_module, a["watchId"], n=3)

    rows = watches_module.get_fare_history(USER_B, a["watchId"], limit=10)
    assert rows == []


def test_get_fare_history_respects_limit(watches_module):
    a = _make_watch(watches_module, USER_A)
    _seed_fare_history(watches_module, a["watchId"], n=5)

    rows = watches_module.get_fare_history(USER_A, a["watchId"], limit=2)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Tool factory — returns the right number of tools, none leak user_id
# ---------------------------------------------------------------------------

def test_make_watch_tools_returns_seven_tools(watches_module):
    tools = watches_module.make_watch_tools(USER_A)
    assert len(tools) == 7
    names = {t.tool_name for t in tools}
    assert names == {
        "add_watch",
        "list_watches",
        "update_watch",
        "pause_watch",
        "resume_watch",
        "remove_watch",
        "get_fare_history",
    }


def test_tool_schemas_do_not_expose_user_id(watches_module):
    """
    The whole point of the closure factory: user_id is captured in scope, not
    passed as a parameter. The tool schemas the LLM sees must not include it.
    """
    tools = watches_module.make_watch_tools(USER_A)
    for t in tools:
        spec = t.tool_spec
        params = spec.get("inputSchema", {}).get("json", {}).get("properties", {})
        assert "user_id" not in params, f"{spec['name']} leaked user_id in schema"
        assert "userId" not in params, f"{spec['name']} leaked userId in schema"
