import html
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db import DISCOUNT_CODES, EVENTS, ORDERS, WAITLIST
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
    # Autoescaping renders the apostrophe as an entity; unescape before
    # asserting so the test checks what the reader sees, not which entity
    # form Jinja happened to pick.
    assert "Jester's Rodeo Ball" in html.unescape(resp.text)


def test_get_event_page_no_open_event(dynamodb_tables):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "no event" in resp.text.lower() or "not currently open" in resp.text.lower()


def test_event_page_falls_back_to_gradient_without_banner(dynamodb_tables):
    """Most events won't have a banner/logo set -- the page must not look broken."""
    _put_event()
    resp = client.get("/")
    assert "event-hero--fallback" in resp.text
    assert "event-logo" not in resp.text


def test_event_page_renders_banner_and_logo_when_set(dynamodb_tables):
    _put_event(
        banner_image_url="https://example.com/banner.jpg",
        logo_url="https://example.com/logo.png",
    )
    resp = client.get("/")
    assert 'src="https://example.com/banner.jpg"' in resp.text
    assert 'src="https://example.com/logo.png"' in resp.text


def test_event_page_rotates_between_multiple_banner_images(dynamodb_tables):
    _put_event(
        banner_image_url="https://example.com/b1.jpg",
        banner_image_url_2="https://example.com/b2.jpg",
        banner_image_url_3="https://example.com/b3.jpg",
    )
    resp = client.get("/")
    assert "event-hero-rotator event-hero-rotator--3" in resp.text
    for n in (1, 2, 3):
        assert f'src="https://example.com/b{n}.jpg"' in resp.text
    assert "event-hero--fallback" not in resp.text


def test_event_page_uses_plain_hero_for_a_single_banner_image(dynamodb_tables):
    _put_event(banner_image_url="https://example.com/only.jpg")
    resp = client.get("/")
    assert 'src="https://example.com/only.jpg"' in resp.text
    assert "event-hero-rotator" not in resp.text


def test_event_page_rotator_skips_empty_banner_slots(dynamodb_tables):
    _put_event(
        banner_image_url="https://example.com/b1.jpg",
        banner_image_url_3="https://example.com/b3.jpg",
    )
    resp = client.get("/")
    assert "event-hero-rotator--2" in resp.text
    assert 'src="https://example.com/b1.jpg"' in resp.text
    assert 'src="https://example.com/b3.jpg"' in resp.text


def test_event_page_shows_banner_when_set(dynamodb_tables):
    _put_event(banner_message="This event has been cancelled.", banner_style="urgent")
    resp = client.get("/")
    assert "This event has been cancelled." in resp.text
    assert "event-banner--urgent" in resp.text


def test_event_page_links_to_register_and_has_no_inline_form(dynamodb_tables):
    _put_event()
    resp = client.get("/")
    assert 'href="/register?event_id=evt_2026"' in resp.text
    assert 'action="/checkout"' not in resp.text  # the form moved to its own page


def test_event_page_embeds_a_map_for_the_address(dynamodb_tables):
    _put_event(address="123 Bourbon St, New Orleans, LA")
    resp = client.get("/")
    assert "google.com/maps?q=123" in resp.text
    assert "output=embed" in resp.text


def test_event_page_map_falls_back_to_location_without_address(dynamodb_tables):
    _put_event()  # location is "New Orleans", no address
    resp = client.get("/")
    assert "google.com/maps?q=New" in resp.text


def test_event_page_shows_contact_section_when_set(dynamodb_tables):
    _put_event(
        contact_name="Jane Krewe", contact_email="jane@krewe.org",
        contact_phone="1 (555) 123-4567",
    )
    resp = client.get("/")
    assert "Jane Krewe" in resp.text
    assert 'href="mailto:jane@krewe.org"' in resp.text
    assert "(555)123-4567" in resp.text  # normalized to (###)###-####


def test_event_page_shows_unrecognized_phone_as_entered(dynamodb_tables):
    _put_event(contact_phone="call the hall")
    resp = client.get("/")
    assert "call the hall" in resp.text


def test_event_page_has_no_contact_section_without_contact_fields(dynamodb_tables):
    _put_event()
    resp = client.get("/")
    assert "event-contact" not in resp.text


def test_register_page_shows_form_header_and_cancel(dynamodb_tables):
    _put_event()
    resp = client.get("/register?event_id=evt_2026")
    assert resp.status_code == 200
    assert 'action="/checkout"' in resp.text
    assert "Jester's Rodeo Ball" in html.unescape(resp.text)  # same header
    assert 'href="/"' in resp.text  # Cancel link back to the main page


def test_register_page_unknown_event_shows_no_event(dynamodb_tables):
    resp = client.get("/register?event_id=nope")
    assert resp.status_code == 200
    assert "no event" in resp.text.lower() or "not currently open" in resp.text.lower()


def test_register_page_sold_out_shows_waitlist_not_form(dynamodb_tables):
    _put_event(capacity=1, tickets_sold_count=1)
    resp = client.get("/register?event_id=evt_2026")
    assert "sold out" in resp.text.lower()
    assert 'action="/checkout"' not in resp.text


def test_register_page_not_open_shows_message_not_form(dynamodb_tables):
    _put_event(registration_open=False)
    resp = client.get("/register?event_id=evt_2026")
    assert "not currently open" in resp.text.lower()
    assert 'action="/checkout"' not in resp.text


def test_closed_event_with_banner_still_shows_on_homepage(dynamodb_tables):
    """Closing registration must not hide a cancellation notice behind the
    generic "no event" page -- that would defeat the point of the banner.
    """
    _put_event(
        status="closed", registration_open=False,
        banner_message="This event has been cancelled.", banner_style="urgent",
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert "This event has been cancelled." in resp.text
    assert "Registration is not currently open." in resp.text


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
    body = html.unescape(resp.text).lower()
    assert "don't recognize" in body or "not recognized" in body
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


def _waitlist(**overrides):
    data = {
        "event_id": "evt_2026", "name": "Sam Smith",
        "email": "sam@example.com", "requested_quantity": "2",
    }
    data.update(overrides)
    return client.post("/waitlist", data=data, follow_redirects=False)


def test_waitlist_signup_creates_entry(dynamodb_tables):
    _put_event(capacity=1, tickets_sold_count=1)
    resp = _waitlist()
    assert resp.status_code == 303
    items = WAITLIST().scan()["Items"]
    assert len(items) == 1
    assert items[0]["name"] == "Sam Smith"
    assert items[0]["notified"] is False


def test_waitlist_signup_rejects_unknown_event(dynamodb_tables):
    """The event_id is a hidden form field — curl can put anything there."""
    resp = _waitlist(event_id="evt_does_not_exist")
    assert resp.status_code == 404
    assert "no longer available" in html.unescape(resp.text).lower()
    assert WAITLIST().scan()["Items"] == [], "no orphaned entry for a nonexistent event"


def test_waitlist_signup_rejects_out_of_range_quantity(dynamodb_tables):
    _put_event(capacity=1, tickets_sold_count=1)
    for bad in ("0", "-3", "999999999"):
        resp = _waitlist(requested_quantity=bad)
        assert resp.status_code == 400, bad
        assert "between 1 and 20" in html.unescape(resp.text)
    assert WAITLIST().scan()["Items"] == []
