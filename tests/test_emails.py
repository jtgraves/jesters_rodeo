import email

import boto3
import pytest
from moto import mock_aws
from moto.core import DEFAULT_ACCOUNT_ID
from moto.ses.models import ses_backends

from app import emails as emails_module
from app.emails import send_confirmation_email
from app.models import Order, Ticket


@pytest.fixture
def ses_backend():
    emails_module.reset_clients()
    with mock_aws():
        client = boto3.client("ses", region_name="us-east-1")
        client.verify_email_identity(EmailAddress="noreply@example.com")
        yield ses_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    emails_module.reset_clients()


def _order(quantity: int = 2) -> Order:
    return Order(
        order_id="ord_1",
        event_id="evt_2026",
        buyer_name="Jane Doe",
        buyer_email="jane@example.com",
        attendees=[{"name": "Jane Doe"}, {"name": None}][:quantity],
        quantity=quantity,
        unit_price_cents=15000,
        total_cents=15000 * quantity,
        created_at="2026-01-01T00:00:00Z",
        status="paid",
    )


def test_send_confirmation_email_delivers_to_buyer(ses_backend):
    order = _order(quantity=1)
    tickets = [Ticket(ticket_id="tkt_1", order_id="ord_1", event_id="evt_2026", attendee_name="Jane Doe")]

    send_confirmation_email(order, tickets)

    assert len(ses_backend.sent_messages) == 1
    sent = ses_backend.sent_messages[0]
    assert sent.destinations == ["jane@example.com"]


def test_send_confirmation_email_has_valid_related_alternative_structure(ses_backend):
    order = _order(quantity=2)
    tickets = [
        Ticket(ticket_id="tkt_1", order_id="ord_1", event_id="evt_2026", attendee_name="Jane Doe"),
        Ticket(ticket_id="tkt_2", order_id="ord_1", event_id="evt_2026", attendee_name=None),
    ]

    send_confirmation_email(order, tickets)

    raw = ses_backend.sent_messages[0].raw_data
    msg = email.message_from_string(raw)

    # multipart/related wrapping a multipart/alternative is the structure that
    # renders correctly in Gmail, Apple Mail and Outlook. Attaching the plain
    # and HTML parts directly to the `related` container makes some clients
    # show the plain-text body *and* the HTML body stacked together.
    assert msg.get_content_type() == "multipart/related"
    subtypes = [p.get_content_type() for p in msg.get_payload()]
    assert subtypes[0] == "multipart/alternative"
    alternative = msg.get_payload(0)
    assert [p.get_content_type() for p in alternative.get_payload()] == ["text/plain", "text/html"]

    images = [p for p in msg.get_payload() if p.get_content_type() == "image/png"]
    assert len(images) == 2, "one inline QR code per ticket"
    assert {p["Content-ID"] for p in images} == {"<qr0>", "<qr1>"}

    html = alternative.get_payload(1).get_payload(decode=True).decode()
    assert 'src="cid:qr0"' in html and 'src="cid:qr1"' in html
    assert "tkt_1" in html and "tkt_2" in html
