from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.db import DISCOUNT_CODES, EVENTS, ORDERS, TICKETS, WAITLIST
from app.main import app


@pytest.fixture
def admin_client(dynamodb_tables):
    """A client carrying a valid session cookie, with token verification stubbed.

    Token verification itself is covered end-to-end in tests/test_auth.py
    against real RS256 signatures; stubbing it here keeps these tests about
    the routes.
    """
    with patch.object(auth, "verify_cognito_token", return_value={"sub": "admin-1"}):
        client = TestClient(app)
        client.cookies.set(settings.session_cookie_name, auth.issue_session("fake-id-token"))
        yield client


def _put_event(event_id="evt_2026", **overrides):
    item = {
        "event_id": event_id, "year": 2026, "name": "Test", "date": "2026-03-14",
        "location": "NOLA", "description": "d", "ticket_price_cents": 15000,
        "capacity": 300, "tickets_sold_count": 5, "registration_open": True,
        "status": "open", "registration_opens_at": None, "registration_closes_at": None,
    }
    item.update(overrides)
    EVENTS().put_item(Item=item)


def _put_ticket(ticket_id, event_id="evt_2026", **overrides):
    item = {
        "ticket_id": ticket_id, "order_id": "ord_1", "event_id": event_id,
        "attendee_name": "Jane", "checked_in": False, "checked_in_at": None,
        "voided": False, "voided_at": None,
    }
    item.update(overrides)
    TICKETS().put_item(Item=item)


def _put_order(order_id="ord_1", **overrides):
    item = {
        "order_id": order_id, "event_id": "evt_2026", "buyer_name": "Jane",
        "buyer_email": "jane@example.com", "attendees": [{"name": "Jane"}, {"name": None}],
        "quantity": 2, "unit_price_cents": 15000, "total_cents": 30000,
        "status": "paid", "created_at": "2026-01-01T00:00:00Z", "discount_code": None,
        "stripe_checkout_session_id": "cs_1", "stripe_payment_intent_id": "pi_1",
    }
    item.update(overrides)
    ORDERS().put_item(Item=item)


def test_admin_redirects_unauthenticated_browsers_to_login(dynamodb_tables):
    client = TestClient(app)
    resp = client.get("/admin/events", headers={"accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_admin_401s_unauthenticated_api_clients(dynamodb_tables):
    client = TestClient(app)
    resp = client.post(
        "/admin/checkin/tkt_x?event_id=evt_2026", headers={"accept": "application/json"}
    )
    assert resp.status_code == 401


def test_admin_rejects_a_forged_session_cookie(dynamodb_tables):
    """The cookie is signed; editing it must not grant access."""
    client = TestClient(app)
    client.cookies.set(settings.session_cookie_name, "not-a-valid-signed-value")
    resp = client.get("/admin/events", headers={"accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 303


def test_admin_can_create_event(admin_client):
    resp = admin_client.post("/admin/events", data={
        "year": "2027", "name": "Next Year Ball", "date": "2027-03-06",
        "location": "NOLA", "description": "d", "ticket_price_cents": "15000",
        "capacity": "300",
    }, follow_redirects=False)
    assert resp.status_code == 303
    items = EVENTS().scan()["Items"]
    assert any(int(e["year"]) == 2027 for e in items)


def test_opening_an_event_closes_any_other_open_event(admin_client):
    """The public page shows `the` open event; two would make it arbitrary."""
    _put_event("evt_2026", status="open", registration_open=True)
    _put_event("evt_2027", status="draft", registration_open=False)

    admin_client.post("/admin/events/evt_2027/open", follow_redirects=False)

    assert EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["status"] == "closed"
    assert EVENTS().get_item(Key={"event_id": "evt_2027"})["Item"]["status"] == "open"


def test_checkin_marks_ticket_checked_in(admin_client):
    _put_ticket("tkt_abc")
    resp = admin_client.post("/admin/checkin/tkt_abc?event_id=evt_2026")
    assert resp.status_code == 200
    assert resp.json()["status"] == "checked_in"
    assert TICKETS().get_item(Key={"ticket_id": "tkt_abc"})["Item"]["checked_in"] is True


def test_checkin_rejects_unknown_ticket(admin_client):
    resp = admin_client.post("/admin/checkin/tkt_nonexistent?event_id=evt_2026")
    assert resp.status_code == 200
    assert resp.json()["status"] == "invalid"


def test_checkin_flags_duplicate(admin_client):
    _put_ticket("tkt_dup", checked_in=True, checked_in_at="2026-03-14T20:00:00Z")
    resp = admin_client.post("/admin/checkin/tkt_dup?event_id=evt_2026")
    assert resp.json()["status"] == "already_checked_in"


def test_checkin_rejects_ticket_from_a_different_event(admin_client):
    """Past years stay queryable, so last year's QR code is a live object."""
    _put_ticket("tkt_lastyear", event_id="evt_2025")
    resp = admin_client.post("/admin/checkin/tkt_lastyear?event_id=evt_2026")
    assert resp.json()["status"] == "wrong_event"
    assert TICKETS().get_item(Key={"ticket_id": "tkt_lastyear"})["Item"]["checked_in"] is False


def test_checkin_rejects_voided_ticket(admin_client):
    _put_ticket("tkt_refunded", voided=True, voided_at="2026-02-01T00:00:00Z")
    resp = admin_client.post("/admin/checkin/tkt_refunded?event_id=evt_2026")
    assert resp.json()["status"] == "voided"
    assert TICKETS().get_item(Key={"ticket_id": "tkt_refunded"})["Item"]["checked_in"] is False


@patch("app.routes.admin.stripe.Refund.create")
def test_refund_marks_refunded_voids_tickets_and_frees_capacity(mock_refund, admin_client):
    _put_event(tickets_sold_count=5)
    _put_order("ord_refund")
    _put_ticket("tkt_r1", order_id="ord_refund")
    _put_ticket("tkt_r2", order_id="ord_refund")

    resp = admin_client.post("/admin/orders/ord_refund/refund", follow_redirects=False)

    assert resp.status_code == 303
    mock_refund.assert_called_once_with(payment_intent="pi_1")
    assert ORDERS().get_item(Key={"order_id": "ord_refund"})["Item"]["status"] == "refunded"
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 3

    for tid in ("tkt_r1", "tkt_r2"):
        ticket = TICKETS().get_item(Key={"ticket_id": tid})["Item"]
        assert ticket["voided"] is True, "a refunded ticket must not scan at the door"


@patch("app.routes.admin.stripe.Refund.create")
def test_refund_is_idempotent(mock_refund, admin_client):
    """Double-clicking Refund must not refund twice or free capacity twice."""
    _put_event(tickets_sold_count=5)
    _put_order("ord_refund")

    admin_client.post("/admin/orders/ord_refund/refund", follow_redirects=False)
    admin_client.post("/admin/orders/ord_refund/refund", follow_redirects=False)

    assert mock_refund.call_count == 1
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 3


@patch("app.routes.admin.stripe.Refund.create", side_effect=RuntimeError("stripe down"))
def test_failed_refund_leaves_the_order_paid(mock_refund, admin_client):
    _put_event(tickets_sold_count=5)
    _put_order("ord_refund")

    resp = admin_client.post("/admin/orders/ord_refund/refund", follow_redirects=False)

    assert resp.status_code == 502
    assert ORDERS().get_item(Key={"order_id": "ord_refund"})["Item"]["status"] == "paid"
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 5


def test_refund_unknown_order_404s(admin_client):
    resp = admin_client.post("/admin/orders/ord_missing/refund")
    assert resp.status_code == 404


def test_orders_export_returns_csv(admin_client):
    _put_order("ord_csv", quantity=1, attendees=[{"name": "Jane"}], total_cents=15000)
    resp = admin_client.get("/admin/orders/export?event_id=evt_2026")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "jane@example.com" in resp.text


def test_orders_export_neutralizes_spreadsheet_formulas(admin_client):
    """Buyer names are attacker-controlled free text that lands in a spreadsheet."""
    _put_order("ord_evil", buyer_name="=cmd|'/c calc'!A1")
    resp = admin_client.get("/admin/orders/export?event_id=evt_2026")
    # The formula prefix is escaped with a leading single quote
    assert "'=cmd" in resp.text
    # Verify no direct formula injection by checking the raw field isn't unquoted
    lines = resp.text.split('\n')
    # Find the data row (skip header)
    data_row = [l for l in lines if "ord_evil" in l][0]
    # The buyer_name field should be escaped with a single quote prefix
    assert ",'=" in data_row  # Escaped formula marker


def test_discount_code_is_stored_normalized(admin_client):
    admin_client.post("/admin/discount-codes", data={
        "code": " member20 ", "event_id": "evt_2026",
        "discount_type": "percent", "discount_value": "20", "max_uses": "",
    }, follow_redirects=False)
    assert DISCOUNT_CODES().get_item(Key={"code": "MEMBER20"}).get("Item") is not None


def test_waitlist_notify_marks_entry(admin_client):
    WAITLIST().put_item(Item={
        "waitlist_id": "wl_1", "event_id": "evt_2026", "name": "Sam",
        "email": "sam@example.com", "requested_quantity": 2,
        "created_at": "2026-01-01T00:00:00Z", "notified": False,
    })
    admin_client.post("/admin/waitlist/wl_1/notify", data={"event_id": "evt_2026"},
                      follow_redirects=False)
    assert WAITLIST().get_item(Key={"waitlist_id": "wl_1"})["Item"]["notified"] is True
