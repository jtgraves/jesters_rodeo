from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import stripe
from botocore.exceptions import ClientError
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db import DISCOUNT_CODES, EVENTS, ORDERS, WAITLIST, paginate
from app.models import DiscountCode, normalize_code
from app.pricing import (
    MAX_TICKETS_PER_ORDER,
    compute_total,
    validate_discount_code,
    validate_quantity,
)

logger = logging.getLogger(__name__)
stripe.api_key = settings.stripe_secret_key
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Stripe requires checkout session `expires_at` to be at least 30 minutes out;
# 35 leaves room for clock skew. The cleanup job in Task 11 uses a *longer*
# window (45 minutes) so Stripe's session is certainly dead before we reclaim
# the seat, and the `checkout.session.expired` webhook normally beats it there.
RESERVATION_TTL_MINUTES = 35


def _find_open_event() -> dict | None:
    events = paginate(EVENTS().scan)
    open_events = [e for e in events if e.get("status") == "open"]
    if open_events:
        return open_events[0]
    # A cancelled event's banner must stay visible even after registration is
    # closed -- otherwise "Close" silently hides the notice instead of
    # communicating it.
    banner_events = [e for e in events if e.get("banner_message")]
    if banner_events:
        return max(banner_events, key=lambda e: int(e["year"]))
    return None


def _reserve_capacity(event_id: str, quantity: int, capacity: int) -> bool:
    """Atomically claim `quantity` seats. Returns False if they aren't available.

    The condition is written as a raw expression string with explicit
    placeholders rather than a `boto3.dynamodb.conditions.Attr` object,
    because mixing an `Attr` condition with a caller-supplied
    `ExpressionAttributeValues` dict in one call makes the interaction
    between generated and supplied placeholders non-obvious. One style, one
    dict, no ambiguity.
    """
    try:
        EVENTS().update_item(
            Key={"event_id": event_id},
            UpdateExpression="SET tickets_sold_count = tickets_sold_count + :q",
            ConditionExpression=(
                "tickets_sold_count <= :max_start AND registration_open = :open"
            ),
            ExpressionAttributeValues={
                ":q": quantity,
                ":max_start": capacity - quantity,
                ":open": True,
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _release_capacity(event_id: str, quantity: int) -> None:
    EVENTS().update_item(
        Key={"event_id": event_id},
        UpdateExpression="SET tickets_sold_count = tickets_sold_count - :q",
        ExpressionAttributeValues={":q": quantity},
    )


def _event_page(request: Request, event: dict, error: str | None):
    return templates.TemplateResponse(request, "event.html", {"event": event, "error": error})


@router.get("/")
def event_page(request: Request):
    event = _find_open_event()
    if not event:
        return templates.TemplateResponse(request, "no_event.html", {"error": None})
    return _event_page(request, event, None)


@router.post("/checkout")
def checkout(
    request: Request,
    event_id: str = Form(...),
    quantity: int = Form(...),
    buyer_name: str = Form(...),
    buyer_email: str = Form(...),
    attendee_names: str = Form(""),
    discount_code: str = Form(""),
):
    event = EVENTS().get_item(Key={"event_id": event_id}).get("Item")
    if not event:
        return templates.TemplateResponse(
            request, "no_event.html", {"error": "That event is no longer available."}
        )

    unit_price = int(event["ticket_price_cents"])
    capacity = int(event["capacity"])
    remaining = capacity - int(event["tickets_sold_count"])

    valid, err = validate_quantity(quantity, remaining)
    if not valid:
        return _event_page(request, event, err)

    # Resolve the discount code BEFORE reserving capacity, so a rejected code
    # never leaves a phantom reservation behind.
    code_obj: DiscountCode | None = None
    if discount_code.strip():
        code_key = normalize_code(discount_code)
        code_item = DISCOUNT_CODES().get_item(Key={"code": code_key}).get("Item")
        if code_item is None:
            # validate_discount_code(None, ...) is valid-by-design so that "no
            # code supplied" is legal. A failed *lookup* must be caught here or
            # a typo silently charges full price with no message shown.
            return _event_page(request, event, "We don't recognize that discount code.")
        code_obj = DiscountCode(**code_item)
        valid, err = validate_discount_code(code_obj, event_id)
        if not valid:
            return _event_page(request, event, err)

    if not _reserve_capacity(event_id, quantity, capacity):
        fresh = EVENTS().get_item(Key={"event_id": event_id}).get("Item", event)
        return _event_page(request, fresh, "Sorry — those tickets were just claimed.")

    total = compute_total(unit_price, quantity, code_obj)

    names = [n.strip() for n in attendee_names.splitlines() if n.strip()]
    attendees: list[dict[str, Any]] = [
        {"name": names[i] if i < len(names) else None} for i in range(quantity)
    ]

    order_id = f"ord_{uuid.uuid4().hex}"
    ORDERS().put_item(Item={
        "order_id": order_id,
        "event_id": event_id,
        "buyer_name": buyer_name,
        "buyer_email": buyer_email,
        "attendees": attendees,
        "quantity": quantity,
        "unit_price_cents": unit_price,
        "discount_code": code_obj.code if code_obj else None,
        "total_cents": total,
        "stripe_checkout_session_id": None,
        "stripe_payment_intent_id": None,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    ticket_word = "ticket" if quantity == 1 else "tickets"
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            customer_email=buyer_email,
            # ONE line item priced at the recomputed total. Sending
            # unit_amount=unit_price with quantity=N would charge the
            # undiscounted subtotal while the order records the discount.
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"{event['name']} — {quantity} {ticket_word}"},
                    "unit_amount": total,
                },
                "quantity": 1,
            }],
            metadata={"order_id": order_id},
            expires_at=int(time.time()) + RESERVATION_TTL_MINUTES * 60,
            success_url=f"{settings.base_url}/order/{order_id}/confirmation",
            cancel_url=f"{settings.base_url}/",
        )
    except Exception:
        # Previously swallowed silently: a bad key, a Stripe outage, a
        # malformed param all looked identical to the customer (bounced back
        # to the form with a generic message) and left nothing to find in the
        # logs. Log the traceback so a real cause is diagnosable.
        logger.exception("stripe.checkout.Session.create failed")
        # Roll back the reservation immediately rather than waiting for the
        # cleanup job — otherwise a Stripe outage burns real capacity.
        _release_capacity(event_id, quantity)
        ORDERS().update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET #s = :canceled",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":canceled": "canceled"},
        )
        fresh = EVENTS().get_item(Key={"event_id": event_id}).get("Item", event)
        return _event_page(
            request, fresh, "We could not start checkout just now. Please try again."
        )

    ORDERS().update_item(
        Key={"order_id": order_id},
        UpdateExpression="SET stripe_checkout_session_id = :s",
        ExpressionAttributeValues={":s": session.id},
    )
    return RedirectResponse(session.url, status_code=303)


@router.get("/order/{order_id}/confirmation")
def confirmation(request: Request, order_id: str):
    order = ORDERS().get_item(Key={"order_id": order_id}).get("Item")
    return templates.TemplateResponse(request, "confirmation.html", {"order": order})


def _waitlist_page(request: Request, event_id: str, error: str | None = None,
                   status_code: int = 200) -> Any:
    return templates.TemplateResponse(
        request, "waitlist_signup.html", {"event_id": event_id, "error": error},
        status_code=status_code,
    )


@router.get("/waitlist")
def waitlist_signup_page(request: Request, event_id: str) -> Any:
    return _waitlist_page(request, event_id)


@router.post("/waitlist")
def waitlist_signup(
    request: Request,
    event_id: str = Form(...), name: str = Form(...),
    email: str = Form(...), requested_quantity: int = Form(...),
) -> Any:
    # This endpoint is public and unauthenticated: the form's min="1" and the
    # event_id in the hidden field are both suggestions as far as curl is
    # concerned. Check them here or the table fills with orphaned rows for
    # events that never existed and requests for a billion tickets.
    if not EVENTS().get_item(Key={"event_id": event_id}).get("Item"):
        return _waitlist_page(
            request, event_id, "That event is no longer available.", status_code=404
        )

    if not 1 <= requested_quantity <= MAX_TICKETS_PER_ORDER:
        return _waitlist_page(
            request, event_id,
            f"Please request between 1 and {MAX_TICKETS_PER_ORDER} tickets.",
            status_code=400,
        )

    WAITLIST().put_item(Item={
        "waitlist_id": f"wl_{uuid.uuid4().hex}",
        "event_id": event_id, "name": name, "email": email,
        "requested_quantity": requested_quantity,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notified": False,
    })
    return RedirectResponse(f"/order-confirmation-waitlist?event_id={event_id}", status_code=303)


@router.get("/order-confirmation-waitlist")
def waitlist_confirmation(request: Request, event_id: str) -> Any:
    return templates.TemplateResponse(request, "waitlist_confirmed.html", {"event_id": event_id})
