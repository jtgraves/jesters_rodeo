from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from moto.core import DEFAULT_ACCOUNT_ID
from moto.ses.models import ses_backends

from app.db import ANNOUNCEMENTS, ORDERS, WAITLIST
from scripts.send_announcement import handler, send_announcement


@pytest.fixture
def ses_backend(dynamodb_tables):
    """DynamoDB + SES mocked together: send_announcement touches both in one call.

    `dynamodb_tables` already has an active `mock_aws()` context open (and
    tears it down after this fixture is torn down), and mock_aws() mocks
    every AWS service at once, so a plain boto3 SES client made here is
    mocked too -- same idea as tests/test_emails.py's ses_backend fixture,
    just also depending on dynamodb_tables for the DB side.
    """
    from app import emails as emails_module

    emails_module.reset_clients()
    client = boto3.client("ses", region_name="us-east-1")
    client.verify_email_identity(EmailAddress="noreply@example.com")
    yield ses_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    emails_module.reset_clients()


def _put_order(order_id, email, status="paid", **overrides):
    item = {
        "order_id": order_id, "event_id": "evt_2026", "buyer_name": "Jane",
        "buyer_email": email,
        "quantity": 1, "unit_price_cents": 15000, "total_cents": 15000,
        "status": status, "created_at": "2026-01-01T00:00:00Z", "discount_code": None,
        "stripe_checkout_session_id": "cs_1", "stripe_payment_intent_id": "pi_1",
    }
    item.update(overrides)
    ORDERS().put_item(Item=item)


def _put_waitlist(waitlist_id, email, **overrides):
    item = {
        "waitlist_id": waitlist_id, "event_id": "evt_2026", "name": "Jo",
        "email": email, "requested_quantity": 1,
        "created_at": "2026-01-01T00:00:00Z", "notified": False,
    }
    item.update(overrides)
    WAITLIST().put_item(Item=item)


def _put_announcement(announcement_id="ann_1", audience=("attendees",), **overrides):
    item = {
        "announcement_id": announcement_id, "event_id": "evt_2026",
        "subject": "Update", "body": "Details inside.", "audience": list(audience),
        "status": "queued", "recipient_count": None, "sent_count": 0,
        "error": None, "created_at": "2026-01-01T00:00:00Z",
    }
    item.update(overrides)
    ANNOUNCEMENTS().put_item(Item=item)
    return announcement_id


def test_dedupes_attendee_and_waitlist_by_email(ses_backend):
    _put_order("ord_1", "Person@Example.com")
    _put_waitlist("wl_1", "person@example.com")
    ann_id = _put_announcement(audience=["attendees", "waitlist"])

    send_announcement(ann_id)

    assert len(ses_backend.sent_messages) == 1


def test_only_paid_orders_count_as_attendees(ses_backend):
    _put_order("ord_1", "paid@example.com", status="paid")
    _put_order("ord_2", "pending@example.com", status="pending")
    _put_order("ord_3", "refunded@example.com", status="refunded")
    ann_id = _put_announcement(audience=["attendees"])

    send_announcement(ann_id)

    assert [m.destinations for m in ses_backend.sent_messages] == [["paid@example.com"]]


def test_audience_filtering_waitlist_only(ses_backend):
    _put_order("ord_1", "attendee@example.com")
    _put_waitlist("wl_1", "waitlisted@example.com")
    ann_id = _put_announcement(audience=["waitlist"])

    send_announcement(ann_id)

    assert [m.destinations for m in ses_backend.sent_messages] == [["waitlisted@example.com"]]


def test_audience_filtering_attendees_only(ses_backend):
    _put_order("ord_1", "attendee@example.com")
    _put_waitlist("wl_1", "waitlisted@example.com")
    ann_id = _put_announcement(audience=["attendees"])

    send_announcement(ann_id)

    assert [m.destinations for m in ses_backend.sent_messages] == [["attendee@example.com"]]


def test_updates_status_and_counts_on_success(ses_backend):
    _put_order("ord_1", "a@example.com")
    _put_order("ord_2", "b@example.com")
    ann_id = _put_announcement(audience=["attendees"])

    send_announcement(ann_id)

    item = ANNOUNCEMENTS().get_item(Key={"announcement_id": ann_id})["Item"]
    assert item["status"] == "sent"
    assert int(item["sent_count"]) == 2
    assert int(item["recipient_count"]) == 2
    assert item["error"] is None


def test_partial_failure_still_marks_sent_with_error_summary(ses_backend):
    _put_order("ord_1", "good@example.com")
    _put_order("ord_2", "bad@example.com")
    ann_id = _put_announcement(audience=["attendees"])

    def _maybe_fail(to_email, subject, body):
        if to_email == "bad@example.com":
            raise RuntimeError("boom")

    with patch("scripts.send_announcement.send_announcement_email", side_effect=_maybe_fail):
        send_announcement(ann_id)

    item = ANNOUNCEMENTS().get_item(Key={"announcement_id": ann_id})["Item"]
    assert item["status"] == "sent"
    assert item["error"] == "1 of 2 failed to send"
    assert int(item["sent_count"]) == 1


def test_missing_announcement_is_a_noop(dynamodb_tables):
    send_announcement("does-not-exist")  # must not raise


def test_handler_marks_failed_on_unexpected_crash(dynamodb_tables):
    ann_id = _put_announcement()

    with patch("scripts.send_announcement.send_announcement", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            handler({"announcement_id": ann_id}, None)

    item = ANNOUNCEMENTS().get_item(Key={"announcement_id": ann_id})["Item"]
    assert item["status"] == "failed"


def test_handler_returns_announcement_id(ses_backend):
    ann_id = _put_announcement()
    result = handler({"announcement_id": ann_id}, None)
    assert result == {"announcement_id": ann_id}
