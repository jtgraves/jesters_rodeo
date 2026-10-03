# Party check-in — one QR per order, confirm-before-admit

## Context

Today, checkout creates one `Ticket` row per unit purchased (`fulfill_order`
in `app/fulfillment.py`), each with its own generated `ticket_id` and its own
QR code embedded in the confirmation email. Door staff scan each attendee's
individual QR code; scanning immediately flips that ticket's `checked_in`
flag and shows a toast — no confirmation step, no visibility into who's being
admitted until after the fact.

For a multi-ticket order (a "party"), this means issuing N separate QR codes
for one purchase, and N separate scans at the door with no indication to
staff of who they're admitting or how many people are in the party.

This redesigns check-in around the **order as the unit of entry**: one QR
code per order regardless of party size, and a scan-then-confirm flow that
shows staff the buyer's name and party size before committing.

## Decisions already made (do not re-litigate)

- **One QR code per order**, encoding `order_id` — not one per ticket.
  `order_id` is already a random `uuid4().hex` (`ord_<32 hex>`), the same
  entropy class as today's `ticket_id`, so it's reused directly rather than
  minting a second opaque token. (Trade-off noted and accepted: `order_id`
  also appears in the public `/order/{order_id}/confirmation` URL, a
  slightly more "visible" surface than a ticket_id that only ever appeared
  in the email — acceptable for a single-event community check-in tool.)
- **Check-in is whole-party, atomic.** A scan either admits the entire party
  at once or not at all. No partial/per-attendee admission tracking.
- **The `Ticket` model and `TicketsTable` are removed entirely**, not kept
  alongside the new flow. Nothing reads per-unit data today:
  `attendee_name` has never been set by any code path, refunds already void
  every ticket in an order together as one step, and capacity
  (`tickets_sold_count` on `Event`) is a counter maintained directly from
  `order.quantity` at checkout/refund time — never derived from ticket rows.
  Check-in state (`checked_in` / `checked_in_at`) moves onto `Order` itself.
- **Clean cutover, no migration path.** All current orders are UAT test
  data; there is no requirement to keep recognizing old-style per-ticket QR
  codes. `TicketsTable` is simply removed from the CDK stack; like every
  other table here it has `RemovalPolicy.RETAIN`, so `cdk deploy` orphans it
  in AWS rather than deleting data (same handling as the old Cognito pool
  during the earlier case-sensitivity migration) — no special cleanup
  needed given it's test data.
- **Scan-then-confirm, not scan-then-commit.** Scanning (camera or manual
  entry) looks up the order and shows a dialog over the camera view with the
  buyer's name and party size; staff explicitly confirms or cancels before
  the check-in actually commits. An **already-checked-in** scan shows the
  same dialog shape (name + party size + the original check-in time) rather
  than just a bare warning — this doubles as an easy way for staff to
  re-look-up a party's size after the fact, without it needing a separate
  feature.
- **The name/email search table keeps its direct one-step "Check In"
  button**, no confirmation dialog. Unlike a scan, the row already shows the
  buyer's name and quantity in plain view before staff clicks anything, so
  there's nothing the dialog would add.

## Data model (`app/models.py`)

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
    # New: check-in moves from the (now-removed) Ticket model onto the
    # order itself -- one scan admits the whole party at once.
    checked_in: bool = False
    checked_in_at: str | None = None
```

`class Ticket` is deleted from `app/models.py` entirely.

## `app/db.py`

Delete `TICKETS()`. No replacement needed -- every call site below reads
`ORDERS()` directly.

## `app/tickets.py`

```python
import io
import qrcode


def generate_qr_code_png(value: str) -> bytes:
    img = qrcode.make(value)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
```

`generate_ticket_id()` is deleted (nothing to generate an ID for anymore --
`order_id` already exists by the time a QR is needed). The function
parameter is renamed `ticket_id` → `value` since it now encodes an
`order_id`, not a ticket_id -- this file is generic QR generation, not
ticket-specific.

## `app/fulfillment.py`

Fulfillment no longer creates any rows -- it's just "send the confirmation
email now that the order is paid":

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
        logger.error("Could not flag fulfillment_error on order %s", order_id, exc_info=True)
```

(`flag_fulfillment_error` is unchanged -- shown for completeness.)

## `app/emails.py`

`send_confirmation_email` drops the `tickets` parameter; the per-ticket QR
loop is replaced with a single QR block. Keep everything already built this
session (event details, price breakdown in dollars, `settings.base_url`
link) exactly as-is -- only the "which QR codes, and what text accompanies
them" part changes.

```python
def send_confirmation_email(order: Order, event: Event | None = None) -> None:
    # multipart/related
    #   +-- multipart/alternative
    #   |     +-- text/plain
    #   |     +-- text/html   (references the QR image below by cid:)
    #   +-- image/png (qr)
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

`_event_details_sections` and `_order_summary_sections` (added earlier this
session) are unchanged. The `Ticket` import is removed from this file's
imports.

## `app/routes/admin.py` — check-in routes

Replace the ticket-keyed routes with order-keyed ones, and split the old
single-step POST into a read-only lookup plus a commit:

```python
def _checkin_status_payload(order: dict) -> dict:
    """The shape every check-in response (lookup or commit) shares."""
    return {
        "buyer_name": order["buyer_name"],
        "quantity": int(order["quantity"]),
    }


def _resolve_checkin_order(order_id: str, event_id: str) -> tuple[dict | None, str | None]:
    """Shared validation for both the lookup and commit endpoints. Returns
    (order, None) if scannable/already-checked-in, or (None, status) for a
    short-circuit status that has nothing to confirm (not found, wrong
    event, or not a valid paid order)."""
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

`_checkin_response` (content-negotiation: redirect for a browser form post,
JSON for the scanner's fetch) is unchanged.

`_search_checkin` is rewritten to search orders directly instead of
tickets, since there's no more per-attendee name to search:

```python
def _search_checkin(event_id: str, q: str) -> list[dict]:
    """Every order for this event whose buyer name or email matches -- for
    door staff working from a name instead of a scannable QR code."""
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
```

## `app/routes/admin.py` — refund, resend, give-tickets, orders page

- **`refund_order`**: delete the `for ticket in _tickets_for_order(order_id): ...`
  void loop entirely -- there are no ticket rows to void. The order's
  `status` flipping to `"refunded"` is already sufficient; `checkin_lookup`
  /`checkin_order`'s `order["status"] != "paid"` check already treats a
  refunded order as unscannable (today's `"voided"` status name is kept for
  the check-in response, now meaning "not a valid paid order" rather than
  "ticket was voided").
- **`_resend_confirmation_email`**: drop `_tickets_for_order`/`Ticket`
  entirely; becomes `send_confirmation_email(Order(**order_item), event)`
  with the same event lookup already added earlier this session.
- **`_tickets_for_order`** is deleted (no longer called anywhere once the
  above two are updated).
- **`give_tickets`**: unchanged in shape -- it already just builds an Order
  and calls `fulfill_order`; it simply stops indirectly creating ticket rows
  now that `fulfill_order` doesn't either.
- **Orders page Check-in column** (`list_orders`, `admin/orders.html`):
  delete `_checkin_counts_for_event` and the per-order loop that attached
  `checked_in_count`/`ticket_count` -- replace with reading `o.checked_in`/
  `o.checked_in_at` directly (already on the order, no extra query needed
  at all). Column becomes:

  ```html
  <td>{% if o.checked_in %}✅ {{ o.checked_in_at | datetime }}{% else %}—{% endif %}</td>
  ```

## `app/templates/admin/checkin.html` — scan-then-confirm UI

- The name/email search table's "Attendee" column goes away (nothing to
  show per-attendee anymore); "Buyer" stays, plus a "Party" column showing
  `m.quantity`. "Check In" button posts the same as today, one step, no
  dialog (per the decision above).
- Manual entry field's placeholder changes from "Ticket ID" to "Order ID".
- New dialog markup (plain `<dialog>` element, no library needed) rendered
  once in the page, populated by JS on each lookup result:

  ```html
  <dialog id="confirm-dialog">
    <p id="confirm-name" style="font-size:1.3rem;font-weight:600"></p>
    <p id="confirm-party"></p>
    <p id="confirm-note" style="color:#b06000"></p>
    <button id="confirm-yes">Confirm check-in</button>
    <button id="confirm-cancel">Cancel</button>
  </dialog>
  ```

- JS flow, replacing today's single `checkin(ticketId)`:

  ```js
  let pendingOrderId = null;

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
      setTimeout(() => { busy = false; }, 1500);
    }
  }

  function showConfirmDialog(orderId, data) {
    pendingOrderId = orderId;
    document.getElementById("confirm-name").textContent = data.buyer_name;
    document.getElementById("confirm-party").textContent =
      "Party of " + data.quantity;
    document.getElementById("confirm-note").textContent =
      data.status === "already_checked_in"
        ? "⚠️ Already checked in at " + new Date(data.checked_in_at).toLocaleTimeString()
        : "";
    document.getElementById("confirm-yes").style.display =
      data.status === "already_checked_in" ? "none" : "";
    document.getElementById("confirm-dialog").showModal();
  }

  document.getElementById("confirm-cancel").onclick = () => {
    document.getElementById("confirm-dialog").close();
    pendingOrderId = null;
  };

  document.getElementById("confirm-yes").onclick = async () => {
    const orderId = pendingOrderId;
    document.getElementById("confirm-dialog").close();
    pendingOrderId = null;
    const url = "/admin/checkin/" + encodeURIComponent(orderId)
              + "?event_id=" + encodeURIComponent(EVENT_ID);
    const resp = await fetch(url, {method: "POST", headers: {"Accept": "application/json"}});
    showResult(await resp.json());
  };

  function showResult(data) {
    const [text, color] = (MESSAGES[data.status] || MESSAGES.invalid)(data);
    const el = document.getElementById("result");
    el.textContent = text;
    el.style.color = color;
  }
  ```

  `MESSAGES` (the `checked_in`/`wrong_event`/`voided`/`invalid` toast-text
  map) is unchanged, minus `already_checked_in` which now only fires via
  `showResult` for the commit-endpoint's own race-condition fallback (the
  lookup path routes `already_checked_in` to the dialog instead, per the
  decision to show party size there too). The camera's
  `scanner.start(..., lookup)` callback and manual-entry button both call
  `lookup` instead of `checkin`.

## Infra (`infra/jesters_rodeo_stack.py`)

- Delete the `tickets_table` construct and its `order_id-index` GSI.
- Delete `"tickets": tickets_table` from the tables dict and
  `"TICKETS_TABLE": tables["tickets"].table_name` from `common_env`.
- The existing `for table in tables.values(): table.grant_read_write_data(app_lambda)`
  loop needs no change -- it already iterates whatever's left in the dict.

## `app/config.py`

Delete `tickets_table: str`.

## Tests

Every test across `tests/test_admin_routes.py`, `tests/test_webhooks.py`,
`tests/test_emails.py`, and `tests/test_clowns_section.py` (incidentally,
via shared fixtures) that constructs a `Ticket`, calls `_put_ticket`, posts
to `/admin/checkin/{ticket_id}`, or asserts on the old per-ticket QR/email
structure or the orders-page N/total column needs updating. This is broad
mechanical fallout from the model removal rather than new design surface --
the implementation plan enumerates the actual file-by-file changes.

New coverage needed beyond today's equivalents:
- `checkin_lookup` returns `ready` without mutating state (a second lookup
  still returns `ready`, not `already_checked_in`).
- `checkin_lookup` on an already-checked-in order returns `quantity` and
  `checked_in_at` alongside `already_checked_in`.
- `checkin_order` is idempotent: first call commits (`checked_in`), second
  call returns `already_checked_in` with the original `checked_in_at`.
- The conditional-write race path (two concurrent commits, only one wins)
  carries over from today's equivalent ticket-level test.
- One QR code (not N) is attached to the confirmation email regardless of
  `quantity`, encoding `order.order_id`.
- Orders page Check-in column reads `o.checked_in` directly, no extra
  per-order ticket query.

## Out of scope

- Any backward compatibility with already-issued per-ticket QR codes (none
  exist outside test data; clean cutover per the decision above).
- Partial/per-attendee admission tracking.
- A dedicated check-in token distinct from `order_id`.
