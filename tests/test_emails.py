import email
from unittest.mock import MagicMock, patch

import pytest

from app.emails import send_confirmation_email
from app.models import Order, Ticket


@pytest.fixture
def smtp_mock():
    """A fake smtplib.SMTP whose `with smtplib.SMTP(...) as smtp:` binds to
    the SAME mock instance sendmail()/login() calls land on -- MagicMock's
    __enter__ returns a fresh mock by default, so this is set explicitly."""
    with patch("app.emails.smtplib.SMTP") as mock_smtp_cls:
        instance = mock_smtp_cls.return_value
        instance.__enter__ = MagicMock(return_value=instance)
        instance.__exit__ = MagicMock(return_value=False)
        yield instance


def _order(quantity: int = 2) -> Order:
    return Order(
        order_id="ord_1",
        event_id="evt_2026",
        buyer_name="Jane Doe",
        buyer_email="jane@example.com",
        quantity=quantity,
        unit_price_cents=15000,
        total_cents=15000 * quantity,
        created_at="2026-01-01T00:00:00Z",
        status="paid",
    )


def test_send_confirmation_email_authenticates_and_delivers_to_buyer(smtp_mock):
    order = _order(quantity=1)
    tickets = [Ticket(ticket_id="tkt_1", order_id="ord_1", event_id="evt_2026", attendee_name="Jane Doe")]

    send_confirmation_email(order, tickets)

    smtp_mock.starttls.assert_called_once()
    smtp_mock.login.assert_called_once()
    smtp_mock.sendmail.assert_called_once()
    _, to_addrs, _ = smtp_mock.sendmail.call_args[0]
    assert to_addrs == ["jane@example.com"]


def test_send_confirmation_email_has_valid_related_alternative_structure(smtp_mock):
    order = _order(quantity=2)
    tickets = [
        Ticket(ticket_id="tkt_1", order_id="ord_1", event_id="evt_2026", attendee_name="Jane Doe"),
        Ticket(ticket_id="tkt_2", order_id="ord_1", event_id="evt_2026", attendee_name=None),
    ]

    send_confirmation_email(order, tickets)

    _, _, raw = smtp_mock.sendmail.call_args[0]
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
