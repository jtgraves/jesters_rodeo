from datetime import datetime, timedelta, timezone

from app.db import EVENTS, ORDERS
from scripts.cleanup_pending_orders import expire_stale_reservations


def _ts(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _put_event(tickets_sold_count: int = 3) -> None:
    EVENTS().put_item(Item={
        "event_id": "evt_2026", "year": 2026, "name": "Test", "date": "2026-03-14",
        "location": "NOLA", "description": "d", "ticket_price_cents": 15000,
        "capacity": 300, "tickets_sold_count": tickets_sold_count,
        "registration_open": True, "status": "open",
    })


def _put_order(order_id: str, minutes_ago: int, quantity: int = 1, status: str = "pending") -> None:
    ORDERS().put_item(Item={
        "order_id": order_id, "event_id": "evt_2026", "buyer_name": order_id,
        "buyer_email": f"{order_id}@example.com",
        "quantity": quantity, "unit_price_cents": 15000, "total_cents": 15000 * quantity,
        "status": status, "created_at": _ts(minutes_ago), "discount_code": None,
        "stripe_checkout_session_id": None, "stripe_payment_intent_id": None,
    })


def test_expires_stale_reservations_and_frees_capacity(dynamodb_tables):
    _put_event(tickets_sold_count=3)
    _put_order("ord_old", minutes_ago=120, quantity=1)
    _put_order("ord_recent", minutes_ago=5, quantity=2)

    assert expire_stale_reservations(max_age_minutes=45) == 1

    assert ORDERS().get_item(Key={"order_id": "ord_old"})["Item"]["status"] == "expired"
    assert ORDERS().get_item(Key={"order_id": "ord_recent"})["Item"]["status"] == "pending"
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 2


def test_stale_orders_are_kept_not_deleted(dynamodb_tables):
    """A late webhook must still find the order — see Task 7's late-payment path."""
    _put_event()
    _put_order("ord_old", minutes_ago=120)

    expire_stale_reservations(max_age_minutes=45)

    assert ORDERS().get_item(Key={"order_id": "ord_old"}).get("Item") is not None


def test_paid_and_refunded_orders_are_untouched(dynamodb_tables):
    _put_event(tickets_sold_count=5)
    _put_order("ord_paid", minutes_ago=5000, status="paid", quantity=2)
    _put_order("ord_refunded", minutes_ago=5000, status="refunded", quantity=1)

    assert expire_stale_reservations(max_age_minutes=45) == 0
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 5


def test_running_twice_releases_capacity_only_once(dynamodb_tables):
    """EventBridge can invoke a schedule more than once; so can a manual retry."""
    _put_event(tickets_sold_count=3)
    _put_order("ord_old", minutes_ago=120, quantity=2)

    first = expire_stale_reservations(max_age_minutes=45)
    second = expire_stale_reservations(max_age_minutes=45)

    assert (first, second) == (1, 0)
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 1
