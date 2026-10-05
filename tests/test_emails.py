import email
from html import escape
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.emails import send_confirmation_email
from app.models import Event, Order


@pytest.fixture
def smtp_mock():
    """A fake smtplib.SMTP_SSL whose `with smtplib.SMTP_SSL(...) as smtp:`
    binds to the SAME mock instance sendmail()/login() calls land on --
    MagicMock's __enter__ returns a fresh mock by default, so this is set
    explicitly."""
    with patch("app.emails.smtplib.SMTP_SSL") as mock_smtp_cls:
        instance = mock_smtp_cls.return_value
        instance.__enter__ = MagicMock(return_value=instance)
        instance.__exit__ = MagicMock(return_value=False)
        yield instance


def _order(quantity: int = 2, **overrides) -> Order:
    fields = dict(
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
    fields.update(overrides)
    return Order(**fields)


def _event(**overrides) -> Event:
    fields = dict(
        event_id="evt_2026",
        year=2026,
        name="Jester's Reaux-de-Eaux Parade",
        date="Saturday, March 14, 2026",
        location="New Orleans",
        address="123 Canal St, New Orleans, LA",
        description="d",
        ticket_price_cents=15000,
        capacity=300,
        logo_url="https://example.com/logo.png",
        timeline=[{"time": "6:00pm", "activity": "Lineup"}, {"time": "7:30pm", "activity": "Roll out"}],
    )
    fields.update(overrides)
    return Event(**fields)


def test_send_confirmation_email_authenticates_and_delivers_to_buyer(smtp_mock):
    send_confirmation_email(_order(quantity=1))

    smtp_mock.login.assert_called_once()
    smtp_mock.sendmail.assert_called_once()
    _, to_addrs, _ = smtp_mock.sendmail.call_args[0]
    assert to_addrs == ["jane@example.com"]


def test_send_confirmation_email_strips_stray_whitespace_from_the_password(smtp_mock):
    # Google displays an App Password as "abcd efgh ijkl mnop"; copying it
    # can carry a non-breaking space (\xa0), not a plain one. Confirmed in
    # production as the actual cause of a previously-mysterious
    # SMTPServerDisconnected failure -- this must never regress silently.
    dirty = "abcd\xa0efgh ijkl\tmnop"
    with patch.object(settings, "smtp_password", dirty):
        send_confirmation_email(_order(quantity=1))

    _, used_password = smtp_mock.login.call_args[0]
    assert used_password == "abcdefghijklmnop"


def test_send_confirmation_email_has_one_qr_code_for_the_whole_party(smtp_mock):
    order = _order(quantity=3)

    send_confirmation_email(order)

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
    assert len(images) == 1, "one QR code for the whole party, regardless of quantity"
    assert images[0]["Content-ID"] == "<qr>"

    text = alternative.get_payload(0).get_payload(decode=True).decode()
    html = alternative.get_payload(1).get_payload(decode=True).decode()
    assert "party of 3" in text.lower()
    assert "party of 3" in html.lower()
    assert order.order_id in text
    assert order.order_id in html
    assert 'src="cid:qr"' in html


def test_send_confirmation_email_includes_event_details(smtp_mock):
    event = _event()
    send_confirmation_email(_order(quantity=1), event)

    _, _, raw = smtp_mock.sendmail.call_args[0]
    msg = email.message_from_string(raw)
    alternative = msg.get_payload(0)
    text = alternative.get_payload(0).get_payload(decode=True).decode()
    html = alternative.get_payload(1).get_payload(decode=True).decode()

    assert event.name in text
    assert escape(event.name) in html
    for blob in (text, html):
        assert event.date in blob
        assert event.address in blob
        assert "6:00pm" in blob and "Lineup" in blob
        assert settings.base_url in blob
    assert f'src="{event.logo_url}"' in html


def test_send_confirmation_email_shows_date_as_month_day_year(smtp_mock):
    # event.date is stored as plain YYYY-MM-DD (see the admin form's
    # placeholder) -- shown in month-day-year order instead, same as the
    # public event page.
    event = _event(date="2027-02-05")
    send_confirmation_email(_order(quantity=1), event)

    _, _, raw = smtp_mock.sendmail.call_args[0]
    msg = email.message_from_string(raw)
    alternative = msg.get_payload(0)
    text = alternative.get_payload(0).get_payload(decode=True).decode()
    html = alternative.get_payload(1).get_payload(decode=True).decode()

    for blob in (text, html):
        assert "Feb 05, 2027" in blob
        assert "2027-02-05" not in blob


def test_send_confirmation_email_renders_markdown_in_timeline_activity(smtp_mock):
    event = _event(timeline=[
        {"time": "7:30pm", "activity": "**Cocktails** at [the veranda](https://example.com/map)"},
    ])
    send_confirmation_email(_order(quantity=1), event)

    _, _, raw = smtp_mock.sendmail.call_args[0]
    msg = email.message_from_string(raw)
    alternative = msg.get_payload(0)
    text = alternative.get_payload(0).get_payload(decode=True).decode()
    html = alternative.get_payload(1).get_payload(decode=True).decode()

    # HTML part: a real, clickable link and bold text.
    assert '<a href="https://example.com/map" target="_blank" rel="noopener">the veranda</a>' in html
    assert "<strong>Cocktails</strong>" in html
    assert "**Cocktails**" not in html

    # Plain-text part: no markup to render into, so the destination is kept
    # visible as "label (url)" rather than dropped or left as raw syntax.
    assert "Cocktails at the veranda (https://example.com/map)" in text
    assert "**Cocktails**" not in text
    assert "[the veranda]" not in text


def test_send_confirmation_email_includes_timeline_details_sub_bullet(smtp_mock):
    event = _event(timeline=[
        {"time": "7:30pm", "activity": "Dinner", "details": "**Vegetarian** option available"},
    ])
    send_confirmation_email(_order(quantity=1), event)

    _, _, raw = smtp_mock.sendmail.call_args[0]
    msg = email.message_from_string(raw)
    alternative = msg.get_payload(0)
    text = alternative.get_payload(0).get_payload(decode=True).decode()
    html = alternative.get_payload(1).get_payload(decode=True).decode()

    assert "<li>7:30pm - Dinner<ul><li><strong>Vegetarian</strong> option available</li></ul></li>" in html
    assert "- 7:30pm - Dinner" in text
    assert "  - Vegetarian option available" in text


def test_send_confirmation_email_omits_event_section_when_event_is_none(smtp_mock):
    # e.g. the event was deleted after the order was placed -- must still
    # send the QR code, just without the now-unavailable event details.
    send_confirmation_email(_order(quantity=1), None)
    smtp_mock.sendmail.assert_called_once()


def test_send_confirmation_email_shows_prices_in_dollars_not_cents(smtp_mock):
    order = _order(quantity=2, unit_price_cents=15000, donation_cents=1000,
                    processing_fee_cents=250, total_cents=31250)
    send_confirmation_email(order)

    _, _, raw = smtp_mock.sendmail.call_args[0]
    msg = email.message_from_string(raw)
    alternative = msg.get_payload(0)
    text = alternative.get_payload(0).get_payload(decode=True).decode()
    html = alternative.get_payload(1).get_payload(decode=True).decode()

    for blob in (text, html):
        assert "$150.00 each" in blob
        assert "$300.00" in blob  # subtotal: 2 x $150
        assert "$2.50" in blob  # processing fee
        assert "$10.00" in blob  # donation
        assert "$312.50" in blob  # total
        # Never the raw cents values anywhere a human would read them.
        assert "15000" not in blob
        assert "31250" not in blob
