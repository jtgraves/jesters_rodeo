from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db import DISCOUNT_CODES, EVENTS, ORDERS
from app.main import app

client = TestClient(app)

OPEN_EVENT = {
    "event_id": "evt_2026", "year": 2026, "name": "Jester's Rodeo Ball",
    "date": "2026-03-14", "location": "New Orleans", "description": "Fun",
    "ticket_price_cents": 15000, "capacity": 300, "tickets_sold_count": 0,
    "registration_open": True, "status": "open",
}


def _put_event(**overrides):
    EVENTS().put_item(Item={**OPEN_EVENT, **overrides})


def _checkout(**overrides):
    data = {
        "event_id": "evt_2026", "quantity": "2", "buyer_name": "Jane Doe",
        "buyer_email": "jane@example.com", "attendee_names": "", "discount_code": "",
    }
    data.update(overrides)
    return client.post("/checkout", data=data, follow_redirects=False)


def _stripe_session():
    session = MagicMock()
    session.url = "https://checkout.stripe.com/fake-session"
    session.id = "cs_test_123"
    return session


def test_get_event_page_shows_open_event(dynamodb_tables):
    _put_event()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Jester's Rodeo Ball" in resp.text


def test_get_event_page_no_open_event(dynamodb_tables):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "no event" in resp.text.lower() or "not currently open" in resp.text.lower()


@patch("app.routes.public.stripe.checkout.Session.create")
def test_checkout_creates_pending_order_and_redirects(mock_create, dynamodb_tables):
    _put_event()
    mock_create.return_value = _stripe_session()

    resp = _checkout(quantity="2")

    assert resp.status_code == 303
    assert resp.headers["location"] == "https://checkout.stripe.com/fake-session"

    _, kwargs = mock_create.call_args
    line_item = kwargs["line_items"][0]
    assert line_item["price_data"]["unit_amount"] == 30000
    assert line_item["quantity"] == 1
    assert "expires_at" in kwargs, "unclaimed reservations must expire on Stripe's side too"

    orders = ORDERS().scan()["Items"]
    assert len(orders) == 1
    assert orders[0]["status"] == "pending"
    assert int(orders[0]["total_cents"]) == 30000
    assert orders[0]["stripe_checkout_session_id"] == "cs_test_123"

    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 2, "capacity is reserved at checkout"


@patch("app.routes.public.stripe.checkout.Session.create")
def test_checkout_charges_the_discounted_total_not_the_subtotal(mock_create, dynamodb_tables):
    """The regression that matters most: Stripe must be told the discounted price."""
    _put_event()
    DISCOUNT_CODES().put_item(Item={
        "code": "MEMBER20", "event_id": "evt_2026", "discount_type": "percent",
        "discount_value": 20, "max_uses": None, "uses_count": 0, "active": True,
    })
    mock_create.return_value = _stripe_session()

    resp = _checkout(quantity="2", discount_code="member20")

    assert resp.status_code == 303
    _, kwargs = mock_create.call_args
    line_item = kwargs["line_items"][0]
    # 2 x $150.00 = $300.00, less 20% = $240.00. Charging 30000 here means the
    # buyer paid full price while the order recorded the discount.
    assert line_item["price_data"]["unit_amount"] == 24000
    assert line_item["quantity"] == 1

    order = ORDERS().scan()["Items"][0]
    assert int(order["total_cents"]) == 24000
    assert order["discount_code"] == "MEMBER20", "codes are stored normalized"


@patch("app.routes.public.stripe.checkout.Session.create")
def test_checkout_rejects_unknown_discount_code(mock_create, dynamodb_tables):
    _put_event()
    resp = _checkout(discount_code="NOPE-NOT-A-CODE")

    assert resp.status_code == 200
    assert "don't recognize" in resp.text.lower() or "not recognized" in resp.text.lower()
    mock_create.assert_not_called()
    assert ORDERS().scan()["Items"] == []
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 0, "a rejected order reserves nothing"


def test_checkout_rejects_when_sold_out(dynamodb_tables):
    _put_event(capacity=1, tickets_sold_count=1)
    resp = _checkout(quantity="1")
    assert resp.status_code == 200
    assert "sold out" in resp.text.lower()


@patch("app.routes.public.stripe.checkout.Session.create")
def test_checkout_rejects_non_positive_quantity(mock_create, dynamodb_tables):
    """min="1" on the form input is client-side decoration; curl ignores it."""
    _put_event(tickets_sold_count=10)
    for bad in ("0", "-5"):
        resp = _checkout(quantity=bad)
        assert resp.status_code == 200
        assert "at least 1" in resp.text
    mock_create.assert_not_called()
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 10, "a negative quantity must not mint capacity"


@patch("app.routes.public.stripe.checkout.Session.create")
def test_checkout_rejects_quantity_above_per_order_cap(mock_create, dynamodb_tables):
    _put_event()
    resp = _checkout(quantity="500")
    assert resp.status_code == 200
    assert "at most 20" in resp.text
    mock_create.assert_not_called()


@patch("app.routes.public.stripe.checkout.Session.create")
def test_checkout_releases_reservation_when_stripe_fails(mock_create, dynamodb_tables):
    _put_event()
    mock_create.side_effect = RuntimeError("stripe is down")

    resp = _checkout(quantity="3")

    assert resp.status_code == 200
    assert "could not start checkout" in resp.text.lower()
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 0, "reservation must be rolled back"
    orders = ORDERS().scan()["Items"]
    assert orders[0]["status"] == "canceled"


def test_checkout_unknown_event_renders_error_not_500(dynamodb_tables):
    resp = _checkout(event_id="evt_does_not_exist")
    assert resp.status_code == 200
    assert "no longer available" in resp.text.lower()
