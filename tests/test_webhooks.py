from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db import DISCOUNT_CODES, EVENTS, ORDERS, TICKETS
from app.main import app

client = TestClient(app)


def _stripe_event(order_id: str, event_type: str = "checkout.session.completed") -> dict:
    return {
        "type": event_type,
        "data": {"object": {
            "id": "cs_test_123",
            "payment_intent": "pi_test_456",
            "metadata": {"order_id": order_id},
        }},
    }


def _put_order(order_id: str, **overrides) -> None:
    item = {
        "order_id": order_id, "event_id": "evt_2026", "buyer_name": "Jane",
        "buyer_email": "jane@example.com",
        "quantity": 2, "unit_price_cents": 15000, "total_cents": 30000,
        "status": "pending", "created_at": "2026-01-01T00:00:00Z",
        "discount_code": None, "stripe_checkout_session_id": "cs_test_123",
        "stripe_payment_intent_id": None,
    }
    item.update(overrides)
    ORDERS().put_item(Item=item)


def _put_event(tickets_sold_count: int = 2) -> None:
    EVENTS().put_item(Item={
        "event_id": "evt_2026", "year": 2026, "name": "Test", "date": "2026-03-14",
        "location": "NOLA", "description": "d", "ticket_price_cents": 15000,
        "capacity": 300, "tickets_sold_count": tickets_sold_count,
        "registration_open": True, "status": "open",
    })


def _post_webhook():
    return client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "fake"})


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_webhook_marks_order_paid_and_creates_tickets(mock_construct, dynamodb_tables):
    _put_event()
    _put_order("ord_1")
    mock_construct.return_value = _stripe_event("ord_1")

    with patch("app.fulfillment.send_confirmation_email") as mock_email:
        resp = _post_webhook()

    assert resp.status_code == 200
    order = ORDERS().get_item(Key={"order_id": "ord_1"})["Item"]
    assert order["status"] == "paid"
    assert order["stripe_payment_intent_id"] == "pi_test_456"

    tickets = TICKETS().scan()["Items"]
    assert len(tickets) == 2  # one per ticket in quantity
    assert all(t["attendee_name"] is None for t in tickets)
    assert all(t["voided"] is False for t in tickets)
    mock_email.assert_called_once()


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_webhook_ignores_replay_of_an_already_paid_order(mock_construct, dynamodb_tables):
    _put_event()
    _put_order("ord_2", status="paid", stripe_payment_intent_id="pi_existing")
    mock_construct.return_value = _stripe_event("ord_2")

    with patch("app.fulfillment.send_confirmation_email") as mock_email:
        resp = _post_webhook()

    assert resp.status_code == 200
    assert TICKETS().scan()["Items"] == []
    mock_email.assert_not_called()


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_webhook_delivered_twice_fulfils_exactly_once(mock_construct, dynamodb_tables):
    """Stripe retries. Two deliveries of the same event must produce one fulfilment.

    This is the test a read-then-write guard passes only by luck: it works
    when the deliveries are strictly sequential, and fails in production when
    they overlap. The conditional write makes it hold either way.
    """
    _put_event()
    _put_order("ord_dup")
    mock_construct.return_value = _stripe_event("ord_dup")

    with patch("app.fulfillment.send_confirmation_email") as mock_email:
        first = _post_webhook()
        second = _post_webhook()

    assert first.status_code == 200 and second.status_code == 200
    assert len(TICKETS().scan()["Items"]) == 2, "quantity of 2, not doubled by the retry"
    assert mock_email.call_count == 1


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_webhook_increments_discount_code_usage(mock_construct, dynamodb_tables):
    _put_event()
    DISCOUNT_CODES().put_item(Item={
        "code": "MEMBER20", "event_id": "evt_2026", "discount_type": "percent",
        "discount_value": 20, "max_uses": None, "uses_count": 3, "active": True,
    })
    _put_order("ord_3", quantity=1, total_cents=12000, discount_code="MEMBER20")
    mock_construct.return_value = _stripe_event("ord_3")

    with patch("app.fulfillment.send_confirmation_email"):
        _post_webhook()

    code = DISCOUNT_CODES().get_item(Key={"code": "MEMBER20"})["Item"]
    assert int(code["uses_count"]) == 4


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_expired_session_releases_reserved_capacity(mock_construct, dynamodb_tables):
    _put_event(tickets_sold_count=10)
    _put_order("ord_exp", quantity=2)
    mock_construct.return_value = _stripe_event("ord_exp", "checkout.session.expired")

    resp = _post_webhook()

    assert resp.status_code == 200
    assert ORDERS().get_item(Key={"order_id": "ord_exp"})["Item"]["status"] == "expired"
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 8


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_expired_session_does_not_release_capacity_twice(mock_construct, dynamodb_tables):
    _put_event(tickets_sold_count=10)
    _put_order("ord_exp2", quantity=2)
    mock_construct.return_value = _stripe_event("ord_exp2", "checkout.session.expired")

    _post_webhook()
    _post_webhook()

    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 8, "released once, not twice"


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_late_payment_on_an_expired_order_still_fulfils_and_reclaims_capacity(
    mock_construct, dynamodb_tables
):
    """A payment that lands after we gave up must never strand the customer.

    We would rather oversell by two seats — visibly, in the admin dashboard —
    than take someone's money and send them no ticket.
    """
    _put_event(tickets_sold_count=8)
    _put_order("ord_late", status="expired")
    mock_construct.return_value = _stripe_event("ord_late")

    with patch("app.fulfillment.send_confirmation_email") as mock_email:
        resp = _post_webhook()

    assert resp.status_code == 200
    assert ORDERS().get_item(Key={"order_id": "ord_late"})["Item"]["status"] == "paid"
    assert len(TICKETS().scan()["Items"]) == 2
    mock_email.assert_called_once()
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 10, "seats re-claimed for a paid order"


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_fulfilment_failure_is_flagged_not_retried(mock_construct, dynamodb_tables):
    """A failure AFTER the order flips to `paid` cannot be repaired by a retry.

    The conditional write is the idempotency guard, so Stripe's retry sees
    `paid` and no-ops. A 500 here would buy nothing and lose the evidence;
    instead we acknowledge, log, and leave `fulfillment_error` on the order.
    """
    _put_event()
    _put_order("ord_broken")
    mock_construct.return_value = _stripe_event("ord_broken")

    with patch(
        "app.fulfillment.send_confirmation_email",
        side_effect=RuntimeError("SES is down"),
    ):
        resp = _post_webhook()

    assert resp.status_code == 200, "Stripe must not be told to retry an unrepairable event"
    order = ORDERS().get_item(Key={"order_id": "ord_broken"})["Item"]
    assert order["status"] == "paid", "the customer was charged; the order stays paid"
    assert order["fulfillment_error"] is True, "the admin needs a way to find this order"


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_successful_fulfilment_sets_no_error_flag(mock_construct, dynamodb_tables):
    _put_event()
    _put_order("ord_ok")
    mock_construct.return_value = _stripe_event("ord_ok")

    with patch("app.fulfillment.send_confirmation_email"):
        _post_webhook()

    order = ORDERS().get_item(Key={"order_id": "ord_ok"})["Item"]
    assert order.get("fulfillment_error") is None


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_webhook_rejects_bad_signature(mock_construct, dynamodb_tables):
    mock_construct.side_effect = ValueError("bad signature")
    resp = _post_webhook()
    assert resp.status_code == 400


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_webhook_ignores_unrelated_event_types(mock_construct, dynamodb_tables):
    _put_event()
    _put_order("ord_other")
    mock_construct.return_value = _stripe_event("ord_other", "payment_intent.created")

    resp = _post_webhook()

    assert resp.status_code == 200
    assert ORDERS().get_item(Key={"order_id": "ord_other"})["Item"]["status"] == "pending"
