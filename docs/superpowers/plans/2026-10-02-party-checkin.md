# Party Check-in Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-ticket QR codes and per-ticket check-in with one QR code per order, representing the whole party, and a scan-then-confirm check-in flow that shows door staff the buyer's name and party size before admitting.

**Architecture:** Check-in state moves from a removed `Ticket`/`TicketsTable` onto `Order` itself (`checked_in`, `checked_in_at`). The QR code encodes `order_id` (already a random `uuid4().hex`, same entropy as the old `ticket_id`). Scanning or manual entry now hits a read-only lookup endpoint first; the result renders in a confirm/cancel `<dialog>` over the camera view, and only a Confirm tap commits the check-in via a second request. This is a **removal**, not an addition alongside the old system — tasks are sequenced so the Ticket infrastructure is migrated off incrementally (Task 1 adds the new fields, Tasks 2-3 move every consumer onto them) and only deleted once nothing references it (Task 4), so the test suite stays green after every task.

**Tech Stack:** FastAPI (Jinja2 templates), DynamoDB (boto3 + moto for tests), plain JS (`<dialog>` element, no library), AWS CDK (Python).

**Spec:** `docs/superpowers/specs/2026-10-02-party-checkin-design.md`

## Global Constraints

- All current orders are UAT test data — no backward compatibility with old per-ticket QR codes is required; this is a clean cutover (spec, "Decisions already made").
- Check-in is whole-party, atomic: a scan either admits the entire party or nothing (spec, "Decisions already made").
- The QR code encodes `order_id` directly — no separate opaque token is introduced (spec, "Decisions already made").
- An already-checked-in scan's confirm dialog must show the buyer's name and party size alongside the original check-in time, not just a bare warning (spec, "Decisions already made"; reaffirmed by the user: "for an already checked in scan, go ahead and display the number in the party in the dialog ... this will allow the person doing checkins an easy way to re-lookup party size if necessary").
- The name/email search table's "Check In" button stays a direct one-step action (no confirm dialog) — the row already shows buyer name and quantity before the click (spec, "Decisions already made").
- `TicketsTable` has `RemovalPolicy.RETAIN` like every other table in this stack; removing it from the CDK stack orphans it in AWS rather than deleting data. No special cleanup is needed given it's test data (spec, "Decisions already made").
- Every write path that creates an `Order` must explicitly set `checked_in: False` (not omit the key and rely on a Pydantic default) — the check-in commit route's `ConditionExpression="checked_in = :f"` requires the DynamoDB attribute to actually exist, or the very first legitimate check-in attempt on that order fails the condition and is misreported as already-checked-in. This mirrors how the old `Ticket` row always explicitly initialized `checked_in`/`checked_in_at` at creation for the same reason.

---

## Task 1: `Order` gains check-in fields, initialized at both creation sites

**Files:**
- Modify: `app/models.py` (the `Order` class)
- Modify: `app/routes/public.py` (checkout's order creation)
- Modify: `app/routes/admin.py` (`give_tickets`'s order creation)
- Modify: `tests/test_public_routes.py` (add one assertion)
- Modify: `tests/test_admin_routes.py` (`_put_order` helper gains the two fields; add one assertion to the give-tickets test)
- Modify: `tests/test_webhooks.py` (`_put_order` helper gains the two fields, for realism/consistency — webhook tests don't exercise check-in directly)

**Interfaces:**
- Produces: `Order.checked_in: bool = False`, `Order.checked_in_at: str | None = None` — every later task reads these two fields (both as Pydantic model attributes and as raw DynamoDB dict keys `order["checked_in"]` / `order["checked_in_at"]`, which is safe specifically because this task guarantees every order dict has them from creation).

This task is purely additive: nothing is removed, the `Ticket` model and `TicketsTable` are untouched, so the whole suite stays green throughout.

- [ ] **Step 1: Add the fields to `Order`**

In `app/models.py`, find the `Order` class (it ends with `comp: bool = False`). Add two fields after it:

```python
class Order(BaseModel):
    order_id: str
    event_id: str
    buyer_name: str
    buyer_email: str
    quantity: int
    unit_price_cents: int
    donation_cents: int = 0
    processing_fee_cents: int = 0
    total_cents: int
    stripe_checkout_session_id: str | None = None
    stripe_payment_intent_id: str | None = None
    status: Literal["pending", "paid", "refunded", "canceled", "expired"] = "pending"
    created_at: str
    fulfillment_error: bool = False
    comp: bool = False
    # Check-in moves here from the (removed) Ticket model -- one scan admits
    # the whole party at once. Every write path that creates an Order MUST
    # set checked_in explicitly (not rely on this default) -- see Global
    # Constraints.
    checked_in: bool = False
    checked_in_at: str | None = None
```

- [ ] **Step 2: Initialize the fields at checkout (public.py)**

In `app/routes/public.py`, find the `ORDERS().put_item(Item={...})` call inside the checkout handler (it currently ends with `"status": "pending", "created_at": datetime.now(timezone.utc).isoformat(),`). Add the two fields:

```python
    order_id = f"ord_{uuid.uuid4().hex}"
    ORDERS().put_item(Item={
        "order_id": order_id,
        "event_id": event_id,
        "buyer_name": buyer_name,
        "buyer_email": buyer_email,
        "quantity": quantity,
        "unit_price_cents": unit_price,
        "donation_cents": donation_cents,
        "processing_fee_cents": processing_fee_cents,
        "total_cents": total,
        "stripe_checkout_session_id": None,
        "stripe_payment_intent_id": None,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "checked_in": False,
        "checked_in_at": None,
    })
```

- [ ] **Step 3: Initialize the fields on a comp order (admin.py's `give_tickets`)**

In `app/routes/admin.py`, find the `order_item = {...}` dict inside `give_tickets` (it currently ends with `"created_at": datetime.now(timezone.utc).isoformat(),`). Add the two fields:

```python
    order_id = f"ord_{uuid.uuid4().hex}"
    order_item = {
        "order_id": order_id, "event_id": event_id,
        "buyer_name": buyer_name, "buyer_email": buyer_email,
        "quantity": quantity, "unit_price_cents": 0,
        "donation_cents": 0, "total_cents": 0,
        "stripe_checkout_session_id": None, "stripe_payment_intent_id": None,
        "status": "paid", "comp": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "checked_in": False, "checked_in_at": None,
    }
    ORDERS().put_item(Item=order_item)
```

- [ ] **Step 4: Add a failing-then-passing assertion for checkout**

In `tests/test_public_routes.py`, find `test_checkout_creates_pending_order_and_redirects`. Add one line after the existing `assert orders[0]["stripe_checkout_session_id"] == "cs_test_123"`:

```python
    assert orders[0]["stripe_checkout_session_id"] == "cs_test_123"
    assert orders[0]["checked_in"] is False
```

Run: `.venv/bin/python -m pytest tests/test_public_routes.py::test_checkout_creates_pending_order_and_redirects -v`
Expected: FAIL (`KeyError: 'checked_in'`) before Step 2, PASS after.

- [ ] **Step 5: Add the matching assertion for give-tickets**

In `tests/test_admin_routes.py`, find `test_give_tickets_creates_paid_comp_order_with_tickets`. Add one line right after `assert order["stripe_payment_intent_id"] is None`:

```python
    assert order["stripe_payment_intent_id"] is None
    assert order["checked_in"] is False
```

(Leave the rest of that test, including its `TICKETS().scan()` ticket-row assertions, untouched for now — Task 2 removes those when `fulfill_order` stops creating ticket rows. Adding an unrelated assertion here does not break anything in the meantime.)

- [ ] **Step 6: Give both `_put_order` test fixtures the same two fields**

In `tests/test_admin_routes.py`, find `_put_order`:

```python
def _put_order(order_id="ord_1", **overrides):
    item = {
        "order_id": order_id, "event_id": "evt_2026", "buyer_name": "Jane",
        "buyer_email": "jane@example.com",
        "quantity": 2, "unit_price_cents": 15000, "total_cents": 30000,
        "status": "paid", "created_at": "2026-01-01T00:00:00Z",
        "stripe_checkout_session_id": "cs_1", "stripe_payment_intent_id": "pi_1",
        "checked_in": False, "checked_in_at": None,
    }
    item.update(overrides)
    ORDERS().put_item(Item=item)
```

In `tests/test_webhooks.py`, find `_put_order` (a separate, smaller copy in that file) and make the same addition:

```python
def _put_order(order_id: str, **overrides) -> None:
    item = {
        "order_id": order_id, "event_id": "evt_2026", "buyer_name": "Jane",
        "buyer_email": "jane@example.com",
        "quantity": 2, "unit_price_cents": 15000, "total_cents": 30000,
        "status": "pending", "created_at": "2026-01-01T00:00:00Z",
        "stripe_checkout_session_id": "cs_test_123",
        "stripe_payment_intent_id": None,
        "checked_in": False, "checked_in_at": None,
    }
    item.update(overrides)
    ORDERS().put_item(Item=item)
```

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, same count as before plus the 2 new assertions (no new test functions in this task, just new assertion lines).

- [ ] **Step 8: Commit**

```bash
git add app/models.py app/routes/public.py app/routes/admin.py \
        tests/test_public_routes.py tests/test_admin_routes.py tests/test_webhooks.py
git commit -m "Add Order.checked_in/checked_in_at, initialized at both creation sites"
```

---

## Task 2: One QR code per order; fulfillment no longer creates ticket rows

**Files:**
- Modify: `app/tickets.py`
- Modify: `app/emails.py`
- Modify: `app/fulfillment.py`
- Modify: `app/routes/admin.py` (`_resend_confirmation_email` only — not refund/checkin/orders-column, which Task 3 owns)
- Modify: `tests/test_tickets.py`
- Modify: `tests/test_emails.py`
- Modify: `tests/test_webhooks.py`
- Modify: `tests/test_admin_routes.py` (give-tickets test's ticket-row assertion)

**Interfaces:**
- Consumes: `Order.checked_in`/`checked_in_at` (Task 1) — not read yet, just confirms the model already has them so nothing here needs to change again later.
- Produces: `generate_qr_code_png(value: str) -> bytes` (renamed parameter, same behavior). `send_confirmation_email(order: Order, event: Event | None = None) -> None` (drops the `tickets` parameter entirely — this is the signature every later task and any future caller must use). `fulfill_order(order_id: str, order_item: dict) -> None` (unchanged signature, new body: no ticket rows, just the email).

At the end of this task, `app/models.py`'s `Ticket` class and `app/db.py`'s `TICKETS()` still exist (admin.py's refund/checkin/orders-column code still uses them) — only `fulfillment.py` and `send_confirmation_email`'s callers stop using them.

- [ ] **Step 1: Generalize the QR helper and drop ticket-ID generation**

Replace the full contents of `app/tickets.py`:

```python
import io
import qrcode


def generate_qr_code_png(value: str) -> bytes:
    img = qrcode.make(value)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
```

- [ ] **Step 2: Update `tests/test_tickets.py`**

Replace its full contents:

```python
from app.tickets import generate_qr_code_png


def test_generate_qr_code_png_returns_bytes():
    png_bytes = generate_qr_code_png("ord_abc123")
    assert isinstance(png_bytes, bytes)
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
```

Run: `.venv/bin/python -m pytest tests/test_tickets.py -v`
Expected: PASS (this file now has no reference to `generate_ticket_id`, matching its removal in Step 1).

- [ ] **Step 3: Rewrite `send_confirmation_email`**

In `app/emails.py`, change the import line (drop `Ticket`):

```python
from app.models import Event, Order
```

Replace the full `send_confirmation_email` function (everything from `def send_confirmation_email` through the blank line before `def send_announcement_email`):

```python
def send_confirmation_email(order: Order, event: Event | None = None) -> None:
    # multipart/related
    #   +-- multipart/alternative
    #   |     +-- text/plain
    #   |     +-- text/html   (references the QR image below by cid:; the
    #   |                      event logo, if any, is a normal remote <img>
    #   |                      since it's already hosted in S3)
    #   +-- image/png (qr)
    #
    # The alternative part MUST be nested inside the related part. Attaching
    # text/plain and text/html as direct siblings of the image makes clients
    # treat them as two separate body parts to display, not as alternatives.
    msg = MIMEMultipart("related")
    msg["Subject"] = "Your Jester's Reaux-de-Eaux tickets"
    msg["From"] = formataddr((FROM_DISPLAY_NAME, settings.ses_sender_email))
    msg["To"] = order.buyer_email

    event_text, event_html = _event_details_sections(event, settings.base_url)
    order_text, order_html = _order_summary_sections(order)
    party_word = "person" if order.quantity == 1 else "people"

    text_lines = [
        f"Thanks, {order.buyer_name}! You're confirmed -- party of {order.quantity}.", "",
        *event_text, "",
        *order_text, "",
        f"Show this QR code at the door ({order.quantity} {party_word}, one scan):",
        f"Order ID: {order.order_id}",
    ]
    html_parts = [
        f"<p>Thanks, {escape(order.buyer_name)}! "
        f"You're confirmed &mdash; party of {order.quantity}.</p>",
        *event_html,
        *order_html,
        '<div style="text-align:center;margin-top:16px">'
        f"<p><strong>{escape(order.buyer_name)}</strong><br>"
        f"Party of {order.quantity}<br>"
        f"<code>{escape(order.order_id)}</code></p>"
        '<img src="cid:qr" alt="Check-in QR code" width="200" height="200">'
        "</div>",
    ]

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText("\n".join(text_lines), "plain", "utf-8"))
    alternative.attach(
        MIMEText("<html><body>" + "".join(html_parts) + "</body></html>", "html", "utf-8")
    )
    msg.attach(alternative)

    image = MIMEImage(generate_qr_code_png(order.order_id), _subtype="png")
    image.add_header("Content-ID", "<qr>")
    image.add_header("Content-Disposition", "inline", filename=f"{order.order_id}.png")
    msg.attach(image)

    _send_mime_message(msg, order.buyer_email)

```

(`_event_details_sections`, `_order_summary_sections`, `_dollars`, `_send_mime_message`, `send_announcement_email` are all unchanged -- do not touch them.)

- [ ] **Step 4: Rewrite `tests/test_emails.py`'s QR/structure tests**

Replace the full contents of `tests/test_emails.py`:

```python
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
```

Run: `.venv/bin/python -m pytest tests/test_emails.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Simplify `fulfill_order`**

Replace the full contents of `app/fulfillment.py`:

```python
from __future__ import annotations

import logging

from app.db import EVENTS, ORDERS
from app.emails import send_confirmation_email
from app.models import Event, Order

logger = logging.getLogger(__name__)


def fulfill_order(order_id: str, order_item: dict) -> None:
    """Email the buyer their QR code.

    Shared by the Stripe webhook (a completed checkout) and the admin
    "give tickets" page (a comped order). The caller is responsible for having
    already written the order and reserved capacity.
    """
    # Tolerates a missing event (e.g. deleted after the order was placed):
    # send_confirmation_email treats event=None as "omit the event-details
    # section" rather than failing the whole send.
    event_item = EVENTS().get_item(Key={"event_id": order_item["event_id"]}).get("Item")
    event = Event(**event_item) if event_item else None
    send_confirmation_email(Order(**order_item), event)


def flag_fulfillment_error(order_id: str) -> None:
    """Leave a durable marker on the order so the failure is discoverable."""
    try:
        ORDERS().update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET fulfillment_error = :t",
            ExpressionAttributeValues={":t": True},
        )
    except Exception:
        # The log line at the call site is the last line of defence if even
        # this fails.
        logger.error("Could not flag fulfillment_error on order %s", order_id, exc_info=True)
```

(`order_id` is now an unused parameter on `fulfill_order` -- left in place because both call sites pass it positionally today and changing the signature is out of scope for this task; it's harmless.)

- [ ] **Step 6: Update `_resend_confirmation_email` in `app/routes/admin.py`**

Find:

```python
def _resend_confirmation_email(order_id: str) -> dict:
    order_item = _get_order_or_404(order_id)
    tickets = [Ticket(**t) for t in _tickets_for_order(order_id)]
    event_item = EVENTS().get_item(Key={"event_id": order_item["event_id"]}).get("Item")
    event = Event(**event_item) if event_item else None
    send_confirmation_email(Order(**order_item), tickets, event)
    return order_item
```

Replace with:

```python
def _resend_confirmation_email(order_id: str) -> dict:
    order_item = _get_order_or_404(order_id)
    event_item = EVENTS().get_item(Key={"event_id": order_item["event_id"]}).get("Item")
    event = Event(**event_item) if event_item else None
    send_confirmation_email(Order(**order_item), event)
    return order_item
```

Do **not** touch `_tickets_for_order`, `refund_order`, the checkin routes, `_search_checkin`, or the orders page's check-in column in this task -- they still use `TICKETS`/`Ticket` and are Task 3's job. Do **not** remove the `Ticket`/`TICKETS` imports from `app/routes/admin.py` yet -- they're still used elsewhere in this same file.

- [ ] **Step 7: Fix up `tests/test_webhooks.py`'s ticket-row assertions**

`fulfill_order` no longer creates any `Ticket` rows, so every assertion that scanned the (still-existing) `TICKETS` table for rows created by this flow needs to go. Make these edits:

In `test_webhook_marks_order_paid_and_creates_tickets`, remove:

```python
    tickets = TICKETS().scan()["Items"]
    assert len(tickets) == 2  # one per ticket in quantity
    assert all(t["attendee_name"] is None for t in tickets)
    assert all(t["voided"] is False for t in tickets)
```

Rename the test to `test_webhook_marks_order_paid` (drop "_and_creates_tickets" -- no longer accurate). Its remaining body (order status/payment_intent assertions, `mock_email.assert_called_once()`) is unchanged.

In `test_webhook_ignores_replay_of_an_already_paid_order`, remove:

```python
    assert TICKETS().scan()["Items"] == []
```

(Keep `mock_email.assert_not_called()` -- that's the real invariant this test checks.)

In `test_webhook_delivered_twice_fulfils_exactly_once`, remove:

```python
    assert len(TICKETS().scan()["Items"]) == 2, "quantity of 2, not doubled by the retry"
```

(Keep `mock_email.call_count == 1`.)

In `test_late_payment_on_an_expired_order_still_fulfils_and_reclaims_capacity`, remove:

```python
    assert len(TICKETS().scan()["Items"]) == 2
```

Finally, drop the now-unused `TICKETS` import:

```python
from app.db import EVENTS, ORDERS
```

- [ ] **Step 8: Fix up `tests/test_admin_routes.py`'s give-tickets ticket-row assertion**

In `test_give_tickets_creates_paid_comp_order_with_tickets`, remove:

```python
    tickets = [t for t in TICKETS().scan()["Items"] if t["order_id"] == order["order_id"]]
    assert len(tickets) == 3
```

Rename the test to `test_give_tickets_creates_paid_comp_order` (drop "_with_tickets"). Everything else in it -- including the `assert order["checked_in"] is False` line Task 1 added -- is unchanged. `TICKETS` stays imported in this file; other tests (refund, check-in) still use it until Task 3.

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. Watch specifically for `tests/test_admin_routes.py`'s refund and check-in tests (`test_checkin_*`, `test_refund_*`) still passing unchanged -- they don't touch `send_confirmation_email`/`fulfill_order` at all, so they should be unaffected by this task.

- [ ] **Step 10: Commit**

```bash
git add app/tickets.py app/emails.py app/fulfillment.py app/routes/admin.py \
        tests/test_tickets.py tests/test_emails.py tests/test_webhooks.py tests/test_admin_routes.py
git commit -m "One QR code per order; fulfillment no longer creates ticket rows"
```

---

## Task 3: Scan-then-confirm check-in UI, order-level check-in routes, refund and orders-page cleanup

**Files:**
- Modify: `app/routes/admin.py`
- Modify: `app/templates/admin/checkin.html`
- Modify: `app/templates/admin/orders.html`
- Modify: `tests/test_admin_routes.py`

**Interfaces:**
- Consumes: `Order.checked_in`/`checked_in_at` (Task 1), `send_confirmation_email(order, event)` (Task 2, via the already-updated `_resend_confirmation_email`).
- Produces: `GET /admin/checkin/lookup/{order_id}?event_id=...` (JSON: `{"status": "ready"|"already_checked_in"|"wrong_event"|"voided"|"invalid", "buyer_name"?, "quantity"?, "checked_in_at"?}`). `POST /admin/checkin/{order_id}?event_id=...&q=...` (same response shape as the old `/admin/checkin/{ticket_id}`, now order-keyed; same content-negotiation via `_checkin_response`).

At the end of this task, nothing in `app/routes/admin.py` references `Ticket`, `TICKETS`, or `_tickets_for_order` any more -- Task 4 can delete them with no further hunting required.

- [ ] **Step 1: Replace the check-in search, lookup, and commit routes**

In `app/routes/admin.py`, find `_search_checkin` through the end of `checkin_ticket` (from `def _search_checkin(event_id: str, q: str) -> list[dict]:` down through the closing of `checkin_ticket`, just before `@member_router.post("/checkin/{order_id}/resend-email")`). Replace that entire block with:

```python
def _search_checkin(event_id: str, q: str) -> list[dict]:
    """Every paid order for this event whose buyer name or email matches --
    for door staff working from a name instead of a scannable QR code."""
    needle = q.strip().lower()
    if not needle:
        return []
    orders = paginate(
        ORDERS().query,
        IndexName="event_id-index",
        KeyConditionExpression="event_id = :e",
        ExpressionAttributeValues={":e": event_id},
    )
    return [
        o for o in orders
        if o["status"] == "paid"
        and (needle in o["buyer_name"].lower() or needle in o["buyer_email"].lower())
    ]


@member_router.get("/checkin")
def checkin_page(request: Request, event_id: str = "", q: str = "") -> Response:
    redirect = _resolve_event_id(request, event_id, "/admin/checkin")
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(
        request, "admin/checkin.html",
        {"event_id": event_id, "q": q, "matches": _search_checkin(event_id, q)},
    )


def _checkin_response(request: Request, result: dict, event_id: str, q: str) -> Response:
    # Same content-negotiation convention as app.auth._not_authenticated:
    # a browser requesting an HTML page (the name/email search results' Check
    # In button, a plain form submit) gets redirected back to a fresh
    # rendering; anything else -- the JS scanner's fetch, which sets
    # Accept: application/json, and any client that sends no Accept header at
    # all -- gets the JSON result directly.
    if "text/html" in request.headers.get("accept", ""):
        suffix = f"&q={quote(q)}" if q else ""
        return RedirectResponse(f"/admin/checkin?event_id={event_id}{suffix}", status_code=303)
    return JSONResponse(result)


def _checkin_status_payload(order: dict) -> dict:
    """The shape every ready/already-checked-in check-in response shares."""
    return {"buyer_name": order["buyer_name"], "quantity": int(order["quantity"])}


def _resolve_checkin_order(order_id: str, event_id: str) -> tuple[dict | None, str | None]:
    """Shared validation for both the lookup and commit endpoints. Returns
    (order, None) if the order is scannable (whether or not already checked
    in), or (None, status) for a short-circuit status with nothing to
    confirm: not found, from a different event, or not a valid paid order
    (pending/refunded/canceled/expired -- "voided" covers all of these, same
    status name the old per-ticket check used for a refunded ticket)."""
    order = ORDERS().get_item(Key={"order_id": order_id}).get("Item")
    if not order:
        return None, "invalid"
    if order["event_id"] != event_id:
        return None, "wrong_event"
    if order["status"] != "paid":
        return None, "voided"
    return order, None


@member_router.get("/checkin/lookup/{order_id}")
def checkin_lookup(order_id: str, event_id: str) -> Response:
    order, status = _resolve_checkin_order(order_id, event_id)
    if status:
        return JSONResponse({"status": status})
    if order["checked_in"]:
        return JSONResponse({
            "status": "already_checked_in",
            "checked_in_at": order["checked_in_at"],
            **_checkin_status_payload(order),
        })
    return JSONResponse({"status": "ready", **_checkin_status_payload(order)})


@member_router.post("/checkin/{order_id}")
def checkin_order(request: Request, order_id: str, event_id: str, q: str = "") -> Response:
    order, status = _resolve_checkin_order(order_id, event_id)
    if status:
        return _checkin_response(request, {"status": status}, event_id, q)

    if order["checked_in"]:
        return _checkin_response(request, {
            "status": "already_checked_in",
            "checked_in_at": order["checked_in_at"],
            **_checkin_status_payload(order),
        }, event_id, q)

    now = datetime.now(timezone.utc).isoformat()
    try:
        ORDERS().update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET checked_in = :t, checked_in_at = :now",
            # Two doors, two phones, one party: only one confirm may win.
            ConditionExpression="checked_in = :f",
            ExpressionAttributeValues={":t": True, ":now": now, ":f": False},
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return _checkin_response(request, {
                "status": "already_checked_in",
                "checked_in_at": now,  # close enough: the other request just won
                **_checkin_status_payload(order),
            }, event_id, q)
        raise

    return _checkin_response(
        request, {"status": "checked_in", **_checkin_status_payload(order)}, event_id, q
    )
```

- [ ] **Step 2: Remove the ticket-void loop from `refund_order`**

In `app/routes/admin.py`, find `refund_order`. Remove this block (it currently sits between the `EVENTS().update_item(...)` capacity release and the final `return RedirectResponse(...)`):

```python
    now = datetime.now(timezone.utc).isoformat()
    for ticket in _tickets_for_order(order_id):
        TICKETS().update_item(
            Key={"ticket_id": ticket["ticket_id"]},
            UpdateExpression="SET voided = :t, voided_at = :now",
            ExpressionAttributeValues={":t": True, ":now": now},
        )

```

(Leave the `EVENTS().update_item(...)` capacity-release call and the `return RedirectResponse(...)` immediately after it -- just delete the ticket-voiding loop between them. A refunded order's `status` flipping to `"refunded"` is already sufficient for `_resolve_checkin_order` to refuse it at the door -- there's no longer a separate per-ticket void flag to set.)

- [ ] **Step 3: Delete `_tickets_for_order`**

In `app/routes/admin.py`, find and delete:

```python
def _tickets_for_order(order_id: str) -> list[dict]:
    return paginate(
        TICKETS().query,
        IndexName="order_id-index",
        KeyConditionExpression="order_id = :o",
        ExpressionAttributeValues={":o": order_id},
    )


```

- [ ] **Step 4: Simplify the orders page's Check-in column**

In `app/routes/admin.py`, find `_checkin_counts_for_event` and delete it entirely:

```python
def _checkin_counts_for_event(event_id: str) -> dict[str, tuple[int, int]]:
    """order_id -> (checked_in_count, ticket_count), for the orders page's
    Check-in column. One scan of every ticket for the event, same pattern as
    _search_checkin, rather than a per-order query (which would be an N+1
    DynamoDB round trip per page view). A voided (refunded) ticket is
    excluded from both counts -- nobody's expected to show up for it."""
    tickets = paginate(TICKETS().scan, FilterExpression=Attr("event_id").eq(event_id))
    counts: dict[str, list[int]] = {}
    for t in tickets:
        if t.get("voided"):
            continue
        entry = counts.setdefault(t["order_id"], [0, 0])
        entry[1] += 1
        if t.get("checked_in"):
            entry[0] += 1
    return {order_id: (checked, total) for order_id, (checked, total) in counts.items()}


```

In `list_orders`, find:

```python
    orders = _orders_for_event(event_id, q, status, sort, dir)
    checkin_counts = _checkin_counts_for_event(event_id)
    for o in orders:
        o["checked_in_count"], o["ticket_count"] = checkin_counts.get(o["order_id"], (0, 0))
    return templates.TemplateResponse(
```

Replace with:

```python
    orders = _orders_for_event(event_id, q, status, sort, dir)
    return templates.TemplateResponse(
```

- [ ] **Step 5: Remove the now-unused `Ticket`/`TICKETS` imports**

In `app/routes/admin.py`, change:

```python
from app.db import (
    ANNOUNCEMENTS,
    CLOWN_PROFILES,
    EVENTS,
    FAQ_ENTRIES,
    KREWE_LINKS,
    ORDERS,
    PAST_BENEFICIARIES,
    TICKETS,
    WAITLIST,
    paginate,
)
```

to:

```python
from app.db import (
    ANNOUNCEMENTS,
    CLOWN_PROFILES,
    EVENTS,
    FAQ_ENTRIES,
    KREWE_LINKS,
    ORDERS,
    PAST_BENEFICIARIES,
    WAITLIST,
    paginate,
)
```

and change:

```python
from app.models import (
    Announcement,
    ClownProfile,
    Event,
    FaqEntry,
    KreweLink,
    Order,
    PastBeneficiary,
    Ticket,
)
```

to:

```python
from app.models import (
    Announcement,
    ClownProfile,
    Event,
    FaqEntry,
    KreweLink,
    Order,
    PastBeneficiary,
)
```

- [ ] **Step 6: Update `admin/checkin.html`**

Replace the full contents of `app/templates/admin/checkin.html`:

```html
{% extends "admin/_base.html" %}
{% block content %}
<h1>Check-in</h1>
<p>Event: {{ event_id }}</p>
<div id="reader" style="width:min(400px, 100%)"></div>
<p><button id="start-camera">📷 Start camera</button></p>
<p id="result" role="status" style="font-size:1.5rem; padding:0.5rem"></p>
<p>
  <input id="manual-order-id" placeholder="Order ID (manual entry)">
  <button id="manual-submit">Look up</button>
</p>

<dialog id="confirm-dialog">
  <p id="confirm-name" style="font-size:1.3rem;font-weight:600"></p>
  <p id="confirm-party"></p>
  <p id="confirm-note" style="color:#b06000"></p>
  <button id="confirm-yes">Confirm check-in</button>
  <button id="confirm-cancel">Cancel</button>
</dialog>

<h2>Look up by name or email</h2>
<form method="get" action="/admin/checkin">
  <input type="hidden" name="event_id" value="{{ event_id }}">
  <input type="text" name="q" value="{{ q }}" placeholder="Name or email">
  <button type="submit">Search</button>
</form>
{% if q %}
<div class="table-scroll">
<table>
<tr><th>Buyer</th><th>Email</th><th>Party</th><th>Status</th><th></th></tr>
{% for m in matches %}
<tr>
  <td>{{ m.buyer_name }}</td>
  <td>{{ m.buyer_email }}</td>
  <td>{{ m.quantity }}</td>
  <td>{% if m.checked_in %}Checked in{% else %}Not checked in{% endif %}</td>
  <td>
    {% if not m.checked_in %}
    <form method="post" action="/admin/checkin/{{ m.order_id }}?event_id={{ event_id }}&q={{ q | urlencode }}" style="display:inline">
      <button type="submit">Check In</button>
    </form>
    {% endif %}
    <form method="post" action="/admin/checkin/{{ m.order_id }}/resend-email?q={{ q | urlencode }}" style="display:inline">
      <button type="submit">Resend email</button>
    </form>
  </td>
</tr>
{% else %}
<tr><td colspan="5">No matches.</td></tr>
{% endfor %}
</table>
</div>
{% endif %}

<script src="/static/html5-qrcode.min.js"></script>
<script>
  const EVENT_ID = {{ event_id | tojson }};
  const MESSAGES = {
    checked_in:        (d) => ["✅ Welcome, " + d.buyer_name + " (party of " + d.quantity + ")", "#137333"],
    already_checked_in:(d) => ["⚠️ Already checked in: " + d.buyer_name, "#b06000"],
    wrong_event:       ()  => ["⛔ Order is for a different event", "#b3261e"],
    voided:            ()  => ["⛔ Order is not a valid paid order", "#b3261e"],
    invalid:           ()  => ["⛔ Unrecognized order", "#b3261e"],
  };
  let busy = false;
  let pendingOrderId = null;

  function showResult(data) {
    const [text, color] = (MESSAGES[data.status] || MESSAGES.invalid)(data);
    const el = document.getElementById("result");
    el.textContent = text;
    el.style.color = color;
  }

  function showConfirmDialog(orderId, data) {
    pendingOrderId = orderId;
    document.getElementById("confirm-name").textContent = data.buyer_name;
    document.getElementById("confirm-party").textContent = "Party of " + data.quantity;
    document.getElementById("confirm-note").textContent =
      data.status === "already_checked_in"
        ? "⚠️ Already checked in at " + new Date(data.checked_in_at).toLocaleTimeString()
        : "";
    document.getElementById("confirm-yes").style.display =
      data.status === "already_checked_in" ? "none" : "";
    document.getElementById("confirm-dialog").showModal();
  }

  async function lookup(orderId) {
    if (busy || !orderId) return;
    busy = true;
    try {
      const url = "/admin/checkin/lookup/" + encodeURIComponent(orderId)
                + "?event_id=" + encodeURIComponent(EVENT_ID);
      const resp = await fetch(url, {headers: {"Accept": "application/json"}});
      if (resp.status === 401) {
        document.getElementById("result").textContent = "Session expired — reload and sign in.";
        return;
      }
      const data = await resp.json();
      if (data.status === "ready" || data.status === "already_checked_in") {
        showConfirmDialog(orderId, data);
      } else {
        showResult(data);
      }
    } finally {
      // Debounce so one QR code in front of the camera is not scanned 10x/sec.
      setTimeout(() => { busy = false; }, 1500);
    }
  }

  document.getElementById("confirm-cancel").onclick = () => {
    document.getElementById("confirm-dialog").close();
    pendingOrderId = null;
  };

  document.getElementById("confirm-yes").onclick = async () => {
    const orderId = pendingOrderId;
    document.getElementById("confirm-dialog").close();
    pendingOrderId = null;
    if (!orderId) return;
    const url = "/admin/checkin/" + encodeURIComponent(orderId)
              + "?event_id=" + encodeURIComponent(EVENT_ID);
    const resp = await fetch(url, {method: "POST", headers: {"Accept": "application/json"}});
    showResult(await resp.json());
  };

  document.getElementById("manual-submit").onclick = () =>
    lookup(document.getElementById("manual-order-id").value.trim());

  // iOS WebKit (Safari, and Chrome-on-iOS, which is WebKit underneath) will
  // silently refuse getUserMedia -- no permission prompt, no error dialog --
  // when it's requested without a direct tap in progress. Auto-starting on
  // page load looked identical to "no camera" there. Require a tap instead:
  // it satisfies WebKit's gesture requirement everywhere this needs to run,
  // and works the same on browsers that never needed it.
  const scanner = new Html5Qrcode("reader");
  const startButton = document.getElementById("start-camera");
  startButton.onclick = async () => {
    startButton.disabled = true;
    try {
      await scanner.start({facingMode: "environment"}, {fps: 10, qrbox: 250}, lookup);
      startButton.style.display = "none";
    } catch (err) {
      console.error("Camera start failed:", err);
      document.getElementById("result").textContent =
        "Camera unavailable (" + (err && (err.name || err.message) || err)
        + ") — use manual entry below.";
      startButton.disabled = false;
    }
  };
</script>
{% endblock %}
```

- [ ] **Step 7: Update the orders page's Check-in column**

In `app/templates/admin/orders.html`, find:

```html
  <td>
    {% if o.ticket_count == 0 %}—
    {% elif o.checked_in_count == o.ticket_count %}✅ {{ o.checked_in_count }}/{{ o.ticket_count }}
    {% elif o.checked_in_count > 0 %}🟡 {{ o.checked_in_count }}/{{ o.ticket_count }}
    {% else %}{{ o.checked_in_count }}/{{ o.ticket_count }}
    {% endif %}
  </td>
```

Replace with:

```html
  <td>{% if o.checked_in %}✅ {{ o.checked_in_at | datetime }}{% else %}—{% endif %}</td>
```

- [ ] **Step 8: Rewrite the check-in and orders-checkin-column tests in `tests/test_admin_routes.py`**

Remove the `_put_ticket` helper entirely:

```python
def _put_ticket(ticket_id, event_id="evt_2026", **overrides):
    item = {
        "ticket_id": ticket_id, "order_id": "ord_1", "event_id": event_id,
        "attendee_name": "Jane", "checked_in": False, "checked_in_at": None,
        "voided": False, "voided_at": None,
    }
    item.update(overrides)
    TICKETS().put_item(Item=item)
```

Remove `TICKETS` from the `from app.db import (...)` import block at the top of the file (leave `ANNOUNCEMENTS`, `EVENTS`, `FAQ_ENTRIES`, `ORDERS`, `PAST_BENEFICIARIES`, `WAITLIST` as they are).

Replace `test_admin_401s_unauthenticated_api_clients`'s path (cosmetic -- it never resolves to a real record, the 401 fires before any lookup, but "tkt_x" is a stale name now):

```python
def test_admin_401s_unauthenticated_api_clients(dynamodb_tables):
    client = TestClient(app)
    resp = client.post(
        "/admin/checkin/ord_x?event_id=evt_2026", headers={"accept": "application/json"}
    )
    assert resp.status_code == 401
```

Replace the entire block of check-in tests, from `test_checkin_marks_ticket_checked_in` through `test_checkin_search_shows_no_matches_message`, with:

```python
def test_checkin_lookup_returns_ready_without_mutating_state(admin_client):
    _put_order("ord_abc", buyer_name="Jane Doe", quantity=2)
    resp = admin_client.get("/admin/checkin/lookup/ord_abc?event_id=evt_2026")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "buyer_name": "Jane Doe", "quantity": 2}
    # A second lookup is still "ready" -- a lookup never commits anything.
    resp2 = admin_client.get("/admin/checkin/lookup/ord_abc?event_id=evt_2026")
    assert resp2.json()["status"] == "ready"


def test_checkin_lookup_rejects_unknown_order(admin_client):
    resp = admin_client.get("/admin/checkin/lookup/ord_nonexistent?event_id=evt_2026")
    assert resp.json() == {"status": "invalid"}


def test_checkin_lookup_on_already_checked_in_order_shows_party_size(admin_client):
    _put_order("ord_dup", buyer_name="Jane Doe", quantity=4,
               checked_in=True, checked_in_at="2026-03-14T20:00:00Z")
    resp = admin_client.get("/admin/checkin/lookup/ord_dup?event_id=evt_2026")
    assert resp.json() == {
        "status": "already_checked_in", "buyer_name": "Jane Doe", "quantity": 4,
        "checked_in_at": "2026-03-14T20:00:00Z",
    }


def test_checkin_lookup_rejects_order_from_a_different_event(admin_client):
    """Past years stay queryable, so last year's QR code is a live object."""
    _put_order("ord_lastyear", event_id="evt_2025")
    resp = admin_client.get("/admin/checkin/lookup/ord_lastyear?event_id=evt_2026")
    assert resp.json()["status"] == "wrong_event"


def test_checkin_lookup_rejects_non_paid_order(admin_client):
    _put_order("ord_refunded", status="refunded")
    resp = admin_client.get("/admin/checkin/lookup/ord_refunded?event_id=evt_2026")
    assert resp.json()["status"] == "voided"


def test_checkin_order_commits_and_is_idempotent(admin_client):
    _put_order("ord_abc", buyer_name="Jane Doe", quantity=2)

    first = admin_client.post("/admin/checkin/ord_abc?event_id=evt_2026")
    assert first.status_code == 200
    assert first.json() == {"status": "checked_in", "buyer_name": "Jane Doe", "quantity": 2}
    stored = ORDERS().get_item(Key={"order_id": "ord_abc"})["Item"]
    assert stored["checked_in"] is True
    checked_in_at = stored["checked_in_at"]

    second = admin_client.post("/admin/checkin/ord_abc?event_id=evt_2026")
    assert second.json()["status"] == "already_checked_in"
    assert second.json()["checked_in_at"] == checked_in_at
    assert ORDERS().get_item(Key={"order_id": "ord_abc"})["Item"]["checked_in_at"] == checked_in_at


def test_checkin_order_rejects_unknown_order(admin_client):
    resp = admin_client.post("/admin/checkin/ord_nonexistent?event_id=evt_2026")
    assert resp.status_code == 200
    assert resp.json()["status"] == "invalid"


def test_checkin_order_rejects_order_from_a_different_event(admin_client):
    _put_order("ord_lastyear", event_id="evt_2025")
    resp = admin_client.post("/admin/checkin/ord_lastyear?event_id=evt_2026")
    assert resp.json()["status"] == "wrong_event"
    assert ORDERS().get_item(Key={"order_id": "ord_lastyear"})["Item"]["checked_in"] is False


def test_checkin_order_rejects_refunded_order(admin_client):
    _put_order("ord_refunded", status="refunded")
    resp = admin_client.post("/admin/checkin/ord_refunded?event_id=evt_2026")
    assert resp.json()["status"] == "voided"
    assert ORDERS().get_item(Key={"order_id": "ord_refunded"})["Item"]["checked_in"] is False


def test_checkin_from_a_browser_form_redirects_instead_of_returning_json(admin_client):
    """The name/email search results' Check In button is a plain HTML form
    submit, not the JS scanner's fetch -- it should behave like every other
    admin form (redirect back to the page), not hand back a raw JSON body.
    """
    _put_order("ord_abc")
    resp = admin_client.post(
        "/admin/checkin/ord_abc?event_id=evt_2026&q=jane",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/checkin?event_id=evt_2026&q=jane"


def test_checkin_search_finds_by_buyer_name_and_email(admin_client):
    _put_order("ord_1", buyer_name="Jane Doe", buyer_email="jane@example.com")

    by_name = admin_client.get("/admin/checkin?event_id=evt_2026&q=jane doe")
    assert "Jane Doe" in by_name.text

    by_email = admin_client.get("/admin/checkin?event_id=evt_2026&q=jane@example.com")
    assert "Jane Doe" in by_email.text


def test_checkin_search_excludes_other_events(admin_client):
    _put_order("ord_1", event_id="evt_2025", buyer_name="Jane Doe")

    resp = admin_client.get("/admin/checkin?event_id=evt_2026&q=jane")
    assert "Jane Doe" not in resp.text


def test_checkin_search_excludes_unpaid_orders(admin_client):
    _put_order("ord_1", status="pending", buyer_name="Jane Doe")

    resp = admin_client.get("/admin/checkin?event_id=evt_2026&q=jane")
    assert "Jane Doe" not in resp.text


def test_checkin_search_shows_no_matches_message(admin_client):
    resp = admin_client.get("/admin/checkin?event_id=evt_2026&q=nobody-like-this")
    assert "No matches" in resp.text
```

Leave `test_resend_email_from_checkin_redirects_back_to_search` exactly as it is (it mocks `send_confirmation_email` entirely, so it doesn't care about the signature change, and its `_put_ticket("tkt_1", order_id="ord_1")` line -- now referring to a deleted helper -- must be removed since `_put_ticket` no longer exists):

```python
@patch("app.routes.admin.send_confirmation_email")
def test_resend_email_from_checkin_redirects_back_to_search(mock_send, admin_client):
    _put_order("ord_1")

    resp = admin_client.post(
        "/admin/checkin/ord_1/resend-email?q=jane", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/checkin?event_id=evt_2026&q=jane"
    mock_send.assert_called_once()
```

Replace the refund tests' ticket-voiding assertions. `test_refund_marks_refunded_voids_tickets_and_frees_capacity` becomes (rename to drop "_voids_tickets", drop the `_put_ticket` calls and the ticket-voided-assertion loop):

```python
@patch("app.routes.admin.stripe.Refund.create")
def test_refund_marks_refunded_and_frees_capacity(mock_refund, admin_client):
    _put_event(tickets_sold_count=5)
    _put_order("ord_refund")

    resp = admin_client.post("/admin/orders/ord_refund/refund", follow_redirects=False)

    assert resp.status_code == 303
    mock_refund.assert_called_once_with(payment_intent="pi_1")
    assert ORDERS().get_item(Key={"order_id": "ord_refund"})["Item"]["status"] == "refunded"
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 3
```

`test_refunding_a_comp_skips_stripe_but_voids_tickets` becomes (rename to drop "_voids_tickets", drop the `_put_ticket` call and the ticket-voided assertion):

```python
@patch("app.routes.admin.stripe.Refund.create")
def test_refunding_a_comp_skips_stripe(mock_refund, admin_client):
    _put_event(tickets_sold_count=5)
    _put_order("ord_comp", comp=True, total_cents=0, unit_price_cents=0,
               stripe_payment_intent_id=None)

    resp = admin_client.post("/admin/orders/ord_comp/refund", follow_redirects=False)

    assert resp.status_code == 303
    mock_refund.assert_not_called()
    assert ORDERS().get_item(Key={"order_id": "ord_comp"})["Item"]["status"] == "refunded"
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 3
```

`test_refund_is_idempotent` and `test_failed_refund_leaves_the_order_paid` are unchanged (they never touched tickets).

Finally, replace the two orders-page check-in-progress tests. `test_orders_page_shows_checkin_progress` becomes:

```python
def test_orders_page_shows_checkin_status(admin_client):
    _put_order("ord_not_in", buyer_name="Not Checked In")
    _put_order("ord_in", buyer_name="Checked In",
               checked_in=True, checked_in_at="2026-03-14T20:00:00Z")

    resp = admin_client.get("/admin/orders?event_id=evt_2026")
    assert "Mar 14, 2026 08:00 PM" in resp.text  # ord_in's checked_in_at, via the datetime filter
    before_in = resp.text.index("Checked In")
    before_mark = resp.text.index("✅")
    assert before_mark > before_in, "the checkmark belongs to the checked-in order's row"
```

Delete `test_orders_checkin_progress_excludes_voided_tickets` entirely -- there's no longer a separate per-ticket voided flag; a refunded order's `status` alone is enough, and that's already covered by the check-in route tests above.

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add app/routes/admin.py app/templates/admin/checkin.html app/templates/admin/orders.html \
        tests/test_admin_routes.py
git commit -m "Scan-then-confirm check-in UI; order-level check-in, refund, and orders-page cleanup"
```

---

## Task 4: Delete the `Ticket` model, `TicketsTable`, and remaining dead references

**Files:**
- Modify: `app/models.py`
- Modify: `app/db.py`
- Modify: `app/config.py`
- Modify: `infra/jesters_rodeo_stack.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_db.py`

**Interfaces:**
- Consumes: nothing new -- by the start of this task, Tasks 1-3 have already moved every production-code and test-code consumer off `Ticket`/`TICKETS`/`tickets_table`. This task only deletes what's now unused.

- [ ] **Step 1: Delete the `Ticket` model**

In `app/models.py`, find and delete the entire `Ticket` class:

```python
class Ticket(BaseModel):
    ticket_id: str
    order_id: str
    event_id: str
    attendee_name: str | None = None
    checked_in: bool = False
    checked_in_at: str | None = None
    voided: bool = False
    voided_at: str | None = None
```

(By this point in the plan nothing constructs a `Ticket` any more, so this is a pure deletion with nothing left to migrate.)

- [ ] **Step 2: Delete `TICKETS()` from `app/db.py`**

Remove:

```python
def TICKETS() -> Any:
    return get_table(settings.tickets_table)


```

- [ ] **Step 3: Delete `tickets_table` from `app/config.py`**

Remove the `tickets_table: str` line from the `Settings` class.

- [ ] **Step 4: Remove `TicketsTable` from the CDK stack**

In `infra/jesters_rodeo_stack.py`'s `_create_tables`, remove:

```python
        tickets_table = dynamodb.Table(
            self, "TicketsTable",
            partition_key=dynamodb.Attribute(name="ticket_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        tickets_table.add_global_secondary_index(
            index_name="order_id-index",
            partition_key=dynamodb.Attribute(name="order_id", type=dynamodb.AttributeType.STRING),
        )
```

and remove `"tickets": tickets_table,` from the dict `_create_tables` returns.

In `__init__` (or wherever `common_env` is built), remove:

```python
            "TICKETS_TABLE": tables["tickets"].table_name,
```

`RemovalPolicy.RETAIN` means the next `cdk deploy` orphans the existing Tickets table in AWS rather than deleting it -- no further action needed given this is all UAT test data (Global Constraints).

- [ ] **Step 5: Remove the Tickets table and env var from `tests/conftest.py`**

Remove:

```python
os.environ.setdefault("TICKETS_TABLE", "Tickets")
```

and remove the whole `client.create_table(TableName="Tickets", ...)` block (the one with the `order_id-index` GSI) from the `dynamodb_tables` fixture.

- [ ] **Step 6: Drop the unused `Ticket` import from `tests/test_db.py`**

Find the import line:

```python
from app.models import Event, Order, Ticket, WaitlistEntry
```

Change to:

```python
from app.models import Event, Order, WaitlistEntry
```

(`Ticket` was already unused in this file before this change -- confirm with a search for `Ticket(` in the file turning up nothing before deleting the import, so this is a safe no-op beyond the import line itself.)

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, with the same test count as after Task 3 (this task deletes production/infra code and dead test setup, not test cases).

- [ ] **Step 8: Verify nothing else references the removed names**

Run: `grep -rn "Ticket\b\|TICKETS\|tickets_table\|TicketsTable\|TICKETS_TABLE" app/ infra/jesters_rodeo_stack.py tests/ --include=*.py`

Expected: no output (aside from unrelated hits like `MAX_TICKETS_PER_ORDER`, `event.get("ticket_price_cents")`, `event_images_bucket`, or similar substrings that are a different concept entirely -- read each hit before concluding it's unrelated, don't just eyeball the word "ticket").

- [ ] **Step 9: Commit**

```bash
git add app/models.py app/db.py app/config.py infra/jesters_rodeo_stack.py \
        tests/conftest.py tests/test_db.py
git commit -m "Remove the Ticket model and TicketsTable (unused since Tasks 2-3)"
```

---

## Verification

1. `.venv/bin/python -m pytest -q` -- full suite green after every task, not just at the end.
2. From `infra/`: `cdk synth -c site_url=... -c cognito_domain_prefix=... -c ses_sender_email=...` -- confirm the synthesized template has no `TicketsTable` resource and no `TICKETS_TABLE` environment variable on `AppFunction`, and that no unrelated resource changed.
3. Deploy, then manually: buy a test ticket (quantity > 1), confirm the email has exactly one QR code and says "Party of N"; scan it on the Check-in page and confirm the dialog shows the buyer's name and party size with Confirm/Cancel; confirm; confirm the Orders page shows ✅ with a check-in time; scan the same QR again and confirm the dialog shows "Already checked in" with the same party size and time, Cancel-only (no Confirm button).
