from app.models import Event, Order, Ticket, DiscountCode, WaitlistEntry


def test_event_defaults():
    event = Event(
        event_id="evt_2026",
        year=2026,
        name="Jester's Reaux-de-Eaux Parade",
        date="2026-03-14",
        location="New Orleans, LA",
        description="Annual krewe parade",
        ticket_price_cents=15000,
        capacity=300,
    )
    assert event.tickets_sold_count == 0
    assert event.registration_open is False
    assert event.status == "draft"


def test_order_defaults():
    order = Order(
        order_id="ord_1",
        event_id="evt_2026",
        buyer_name="Jane Doe",
        buyer_email="jane@example.com",
        quantity=2,
        unit_price_cents=15000,
        discount_code=None,
        total_cents=30000,
        stripe_checkout_session_id=None,
        stripe_payment_intent_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    assert order.status == "pending"
    assert order.donation_cents == 0


from app.db import EVENTS, WAITLIST, paginate
from app.models import normalize_code


def test_events_table_roundtrip(dynamodb_tables):
    table = EVENTS()
    table.put_item(Item={"event_id": "evt_2026", "year": 2026, "name": "Test"})
    resp = table.get_item(Key={"event_id": "evt_2026"})
    assert resp["Item"]["name"] == "Test"


def test_normalize_code_is_case_and_space_insensitive():
    assert normalize_code(" member20 ") == "MEMBER20"
    assert normalize_code("Member20") == "MEMBER20"


def test_paginate_follows_last_evaluated_key(dynamodb_tables):
    table = WAITLIST()
    for i in range(7):
        table.put_item(Item={
            "waitlist_id": f"wl_{i}", "event_id": "evt_2026",
            "name": f"Person {i}", "email": f"p{i}@example.com",
            "requested_quantity": 1, "created_at": "2026-01-01T00:00:00Z",
            "notified": False,
        })

    # Limit=2 forces DynamoDB to return LastEvaluatedKey on every page but
    # the last, which is exactly the truncation a bare resp["Items"] hides.
    items = paginate(table.scan, Limit=2)
    assert len(items) == 7
    assert {i["waitlist_id"] for i in items} == {f"wl_{n}" for n in range(7)}

    truncated = table.scan(Limit=2)["Items"]
    assert len(truncated) == 2, "sanity check: a single scan really does truncate"
