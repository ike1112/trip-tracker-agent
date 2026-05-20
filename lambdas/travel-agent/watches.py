"""
Trip-tracker Watches data layer + user-scoped tool factory.

Two responsibilities, both small and deliberately separated from the LLM-facing
tool surface:

1. Data-access functions over the Watches and FareHistory DynamoDB tables.
   Every function takes `user_id` (Cognito `sub`) as its first argument and
   uses it as the Watches partition key. Ownership is enforced at the data
   layer — a fabricated `watchId` belonging to another user simply returns
   no row, with no special-case code needed.

2. `make_watch_tools(user_id) -> list` factory that returns the seven watch
   CRUD tools as `@tool`-decorated closures bound to the verified `user_id`.
   The LLM's tool schema never exposes `user_id`, so the model cannot be
   tricked (via prompt injection or otherwise) into operating on a different
   user's watches.

See `docs/adr/0001-user-scoped-tools-via-closure-factory.md` for the rationale
behind keeping `user_id` out of the tool schema entirely.
"""

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Any

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError
from strands import tool
from aws_lambda_powertools import Logger

logger = Logger(service="travel-agent")

WATCHES_TABLE_NAME = os.environ.get("WATCHES_TABLE_NAME")
FARE_HISTORY_TABLE_NAME = os.environ.get("FARE_HISTORY_TABLE_NAME")

# Resource API gives us automatic Python<->DDB type marshalling (dict -> Map,
# list -> List, etc.) which keeps the call sites below readable.
_dynamodb = boto3.resource("dynamodb")
_watches_table = _dynamodb.Table(WATCHES_TABLE_NAME) if WATCHES_TABLE_NAME else None
_fare_history_table = (
    _dynamodb.Table(FARE_HISTORY_TABLE_NAME) if FARE_HISTORY_TABLE_NAME else None
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Data-access functions. All Watches operations key on (userId, watchId), so a
# request keyed with the wrong user_id silently returns nothing — there is no
# code path that accepts a watchId without also requiring the matching userId.
# ---------------------------------------------------------------------------


def _decimalize(value: Any) -> Any:
    """Coerce floats to Decimal(str(x)) for DynamoDB, recursing containers.

    boto3's DynamoDB resource rejects native Python floats
    ("Float types are not supported. Use Decimal types instead.").
    The agent's tool args arrive as floats (e.g. maxTotalPrice from the
    LLM), so every numeric value bound for a write must be coerced. Uses
    Decimal(str(x)) — never Decimal(float) — so 1500.0 stays 1500, not
    1499.9999…, matching the poller/notifier convention. Bools are left
    alone (bool is an int subclass; not a DDB float hazard).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimalize(v) for v in value]
    return value

def create_watch(
    user_id: str,
    origin: Any,
    destination: str,
    destination_airport: str,
    date_window: dict,
    pax: int,
    max_total_price: float,
    preferences: Optional[dict] = None,
    alert_strategy: str = "both",
) -> dict:
    """Insert a new active watch and return the row that was written.

    `destination` is the user-facing city ("Tokyo") used for hotel search
    and alert prose; `destination_airport` is the IATA code ("NRT") the
    flight search uses. Two fields because hotels are city-scoped while
    flights are airport-scoped — the poller has no LLM to resolve one
    from the other at search time, so both must be stored on the watch.
    """
    watch_id = uuid.uuid4().hex
    now = _now_iso()
    # `lastAlertedAt` and `lastAlertedPrice` are deliberately ABSENT — not
    # written as `None`. boto3 marshals Python `None` to a DDB `NULL`-
    # valued attribute (attribute present, value typed NULL), which
    # silently breaks the Notifier's dedup writeback: its condition
    # `attribute_not_exists(lastAlertedAt) OR lastAlertedAt < :now`
    # evaluates to `false` on a NULL-valued attribute (it exists, and
    # NULL doesn't compare strictly less than a string timestamp), so
    # every first-alert writeback would fail with
    # ConditionalCheckFailedException and the dedup gate would never
    # arm. Writing the keys as truly absent until the Notifier's first
    # successful alert keeps the writer's intended semantics intact.
    item = {
        "userId": user_id,
        "watchId": watch_id,
        "type": "specific",
        "origin": origin,
        "destination": destination,
        "destinationAirport": destination_airport,
        "dateWindow": date_window,
        "pax": pax,
        "maxTotalPrice": max_total_price,
        "alertStrategy": alert_strategy,
        "preferences": preferences or {},
        "status": "active",
        "createdAt": now,
        "updatedAt": now,
    }
    _watches_table.put_item(Item=_decimalize(item))
    logger.info(
        "watch_created",
        extra={"watch_id": watch_id, "user_id_prefix": user_id[:8]},
    )
    return item


def get_watch(user_id: str, watch_id: str) -> Optional[dict]:
    """Return the watch only if it belongs to user_id, else None."""
    resp = _watches_table.get_item(Key={"userId": user_id, "watchId": watch_id})
    return resp.get("Item")


def list_watches(user_id: str, status: Optional[str] = "active") -> list[dict]:
    """List all watches for a user, optionally filtered by status."""
    kwargs = {"KeyConditionExpression": Key("userId").eq(user_id)}
    if status:
        # FilterExpression runs after the per-user Query — fine at
        # personal scale. The `status-index` GSI (ADR 0007) does NOT
        # serve this path: it is poll-only (cross-user, `status` PK).
        # A per-user active-list optimisation would need a separate
        # `userId`+`status` composite index — out of scope (ADR 0007).
        kwargs["FilterExpression"] = Attr("status").eq(status)
    resp = _watches_table.query(**kwargs)
    return resp.get("Items", [])


def update_watch(
    user_id: str, watch_id: str, patches: dict
) -> Optional[dict]:
    """
    Patch fields on a watch. Returns the updated row, or None if no watch
    exists at (user_id, watch_id) — which is also the failure mode if the
    LLM hallucinated a watchId or tried one belonging to another user.
    """
    if not patches:
        return get_watch(user_id, watch_id)

    # Block changes to identity / immutable fields even if the LLM passes them.
    immutable = {"userId", "watchId", "createdAt"}
    sets = {k: v for k, v in patches.items() if k not in immutable}
    if not sets:
        return get_watch(user_id, watch_id)
    sets["updatedAt"] = _now_iso()

    expr_names = {f"#k{i}": k for i, k in enumerate(sets)}
    expr_values = {f":v{i}": _decimalize(v) for i, v in enumerate(sets.values())}
    set_clause = ", ".join(f"#k{i} = :v{i}" for i in range(len(sets)))

    try:
        resp = _watches_table.update_item(
            Key={"userId": user_id, "watchId": watch_id},
            UpdateExpression=f"SET {set_clause}",
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
            # ConditionExpression turns "row doesn't exist for this user" into
            # ConditionalCheckFailedException, which we map back to None.
            ConditionExpression="attribute_exists(userId)",
            ReturnValues="ALL_NEW",
        )
        return resp.get("Attributes")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info(
                "watch_update_no_match",
                extra={"watch_id": watch_id, "user_id_prefix": user_id[:8]},
            )
            return None
        raise


def set_watch_status(
    user_id: str, watch_id: str, status: str
) -> Optional[dict]:
    """Pause / resume / archive helper."""
    return update_watch(user_id, watch_id, {"status": status})


def get_fare_history(
    user_id: str, watch_id: str, limit: int = 30
) -> list[dict]:
    """
    Return recent FareHistory rows for a watch, newest first.

    FareHistory's PK is `watchId` only (not user-scoped), so ownership has to
    be checked explicitly: confirm the watch belongs to user_id via the
    Watches table before returning history rows.
    """
    if get_watch(user_id, watch_id) is None:
        return []
    resp = _fare_history_table.query(
        KeyConditionExpression=Key("watchId").eq(watch_id),
        ScanIndexForward=False,  # newest first
        Limit=limit,
    )
    return resp.get("Items", [])


# ---------------------------------------------------------------------------
# Tool factory. Closures capture `user_id` so it never appears in the
# LLM-visible tool schema. agent.py calls this per request, after JWT verify.
# ---------------------------------------------------------------------------

def make_watch_tools(user_id: str) -> list:
    """Build the seven trip-tracker watch tools bound to a verified user_id."""

    @tool(
        name="add_watch",
        description=(
            "Create a new trip watch. The system will then poll combined "
            "flight + hotel prices for this trip every few hours and email "
            "the user when the total drops or hits an anomaly low. "
            "Always echo every field back to the user in plain English and "
            "wait for confirmation before calling this tool. Never invent "
            "values for missing fields — ask the user. You must always "
            "supply both the destination city name (for hotel search and "
            "alert prose) AND the primary IATA airport code (for flight "
            "search). Use the airport you would book if a passenger asked "
            "for that city — e.g. Tokyo → NRT, London → LHR, Paris → CDG. "
            "Multi-airport cities pick the most common: New York → JFK."
        ),
    )
    def add_watch(
        origin: Any,
        destination: str,
        destinationAirport: str,
        earliestDepart: str,
        latestDepart: str,
        nights: int,
        pax: int,
        maxTotalPrice: float,
        preferences: Optional[dict] = None,
    ) -> dict:
        """
        Args:
            origin: Airport code (e.g. "SFO") or list of codes (e.g. ["SFO","OAK"]).
            destination: City name (e.g. "Tokyo"), used for hotel search + prose.
            destinationAirport: IATA airport code (e.g. "NRT"), used for flight search.
            earliestDepart: ISO date YYYY-MM-DD of earliest departure.
            latestDepart:   ISO date YYYY-MM-DD of latest departure.
            nights: Number of nights at the destination.
            pax: Passenger count.
            maxTotalPrice: USD threshold for the combined flight+hotel price.
            preferences: Optional, e.g. {"maxStops": 1, "hotelMinStars": 4}.
        Returns:
            The created watch row including its watchId.
        """
        date_window = {
            "earliestDepart": earliestDepart,
            "latestDepart": latestDepart,
            "nights": nights,
        }
        return create_watch(
            user_id=user_id,
            origin=origin,
            destination=destination,
            destination_airport=destinationAirport,
            date_window=date_window,
            pax=pax,
            max_total_price=maxTotalPrice,
            preferences=preferences,
        )

    @tool(
        name="list_watches",
        description=(
            "Return all of the user's active trip watches. Use this for "
            "questions like 'what am I watching?' or as the first step "
            "for 'what's happening with my watches?'."
        ),
    )
    def list_watches_tool() -> list[dict]:
        """Returns: a list of active watch rows belonging to the current user."""
        return list_watches(user_id=user_id, status="active")

    @tool(
        name="update_watch",
        description=(
            "Patch one or more fields on an existing watch. Use this for "
            "refinement requests like 'tighten Tokyo to weekends only' or "
            "'raise my budget to $1700'. Returns the updated watch, or null "
            "if no watch with that id exists for the user."
        ),
    )
    def update_watch_tool(watchId: str, patches: dict) -> Optional[dict]:
        """
        Args:
            watchId: The watch to update.
            patches: A dict of {field: new_value}. Only fields present are
                updated. userId, watchId, createdAt cannot be changed.
        """
        return update_watch(user_id=user_id, watch_id=watchId, patches=patches)

    @tool(
        name="pause_watch",
        description=(
            "Pause a watch so it stops being polled (sets status='paused'). "
            "Useful while the user is on the trip or otherwise wants to mute "
            "alerts without losing the watch."
        ),
    )
    def pause_watch_tool(watchId: str) -> Optional[dict]:
        return set_watch_status(user_id=user_id, watch_id=watchId, status="paused")

    @tool(
        name="resume_watch",
        description="Resume a previously paused watch (sets status='active').",
    )
    def resume_watch_tool(watchId: str) -> Optional[dict]:
        return set_watch_status(user_id=user_id, watch_id=watchId, status="active")

    @tool(
        name="remove_watch",
        description=(
            "Soft-delete a watch (sets status='archived'). The row is "
            "preserved for audit but the watch will no longer be polled."
        ),
    )
    def remove_watch_tool(watchId: str) -> Optional[dict]:
        return set_watch_status(user_id=user_id, watch_id=watchId, status="archived")

    @tool(
        name="get_fare_history",
        description=(
            "Return recent fare history snapshots for a watch, newest first. "
            "Use this for trend questions ('is Tokyo cheaper than last week?') "
            "or to surface the booking link from a recent alert email."
        ),
    )
    def get_fare_history_tool(watchId: str, limit: int = 30) -> list[dict]:
        """
        Args:
            watchId: The watch to look up.
            limit: How many recent rows to return (default 30, max bounded by DDB Query).
        """
        return get_fare_history(user_id=user_id, watch_id=watchId, limit=limit)

    return [
        add_watch,
        list_watches_tool,
        update_watch_tool,
        pause_watch_tool,
        resume_watch_tool,
        remove_watch_tool,
        get_fare_history_tool,
    ]
