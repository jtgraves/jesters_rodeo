from __future__ import annotations

import logging

import stripe
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.db import DISCOUNT_CODES, EVENTS, ORDERS, TICKETS
from app.emails import send_confirmation_email
from app.models import Order, Ticket
from app.tickets import generate_ticket_id

logger = logging.getLogger(__name__)
router = APIRouter()


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


def _order_id_from(session: dict) -> str | None:
    """This app's own checkouts always set metadata.order_id. Anything else
    reaching this endpoint -- a `stripe trigger`, a session created in the
    dashboard, another integration on the same account -- has no order for us
    to act on. Return None so the caller can acknowledge and move on rather
    than raise a KeyError that Stripe would retry for days.
    """
    metadata = session.get("metadata") or {}
    return metadata.get("order_id")


def _handle_completed(session: dict) -> None:
    order_id = _order_id_from(session)
    if not order_id:
        logger.info("checkout.session.completed with no order_id metadata; ignoring")
        return

    # The transition IS the idempotency guard. Only an order that is still
    # `pending` (or that we prematurely `expired`) can move to `paid`, and only
    # one caller can win that race. ALL_OLD tells us which case we were in.
    try:
        resp = ORDERS().update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET #s = :paid, stripe_payment_intent_id = :pi",
            ConditionExpression="attribute_exists(order_id) AND #s IN (:pending, :expired)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":paid": "paid",
                ":pi": session.get("payment_intent"),
                ":pending": "pending",
                ":expired": "expired",
            },
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            # Already fulfilled, refunded, or never existed. Either way this
            # delivery has nothing left to do — acknowledge so Stripe stops
            # retrying.
            logger.info("Ignoring duplicate/irrelevant completion for %s", order_id)
            return
        raise

    order_item = resp["Attributes"]
    previous_status = order_item["status"]
    order_item["status"] = "paid"
    order_item["stripe_payment_intent_id"] = session.get("payment_intent")

    if previous_status == "expired":
        # We released these seats early and the payment arrived anyway. Take
        # the seats back even if that pushes the event over capacity: an
        # oversell is visible to the admin and fixable, whereas a paying
        # customer with no ticket is neither.
        logger.warning(
            "Order %s paid after expiring; re-claiming %s seat(s)",
            order_id, order_item["quantity"],
        )
        EVENTS().update_item(
            Key={"event_id": order_item["event_id"]},
            UpdateExpression="SET tickets_sold_count = tickets_sold_count + :q",
            ExpressionAttributeValues={":q": int(order_item["quantity"])},
        )

    # Everything below runs AFTER the order is already `paid`, so a retry from
    # Stripe would hit the conditional guard above and no-op — the retry cannot
    # repair a partial failure here. Letting the exception escape would give
    # Stripe a 500 to retry uselessly and leave nothing behind to find later.
    # So: swallow it, log it loudly, and flag the order for the admin.
    try:
        _fulfill(order_id, order_item)
    except Exception:
        logger.error(
            "Fulfilment failed for paid order %s; payment stands but tickets/email may be "
            "missing. Flagging fulfillment_error for admin follow-up.",
            order_id, exc_info=True,
        )
        _flag_fulfillment_error(order_id)


def _fulfill(order_id: str, order_item: dict) -> None:
    tickets: list[Ticket] = []
    for attendee in order_item["attendees"]:
        ticket_item = {
            "ticket_id": generate_ticket_id(),
            "order_id": order_id,
            "event_id": order_item["event_id"],
            "attendee_name": attendee.get("name"),
            "checked_in": False,
            "checked_in_at": None,
            "voided": False,
            "voided_at": None,
        }
        TICKETS().put_item(Item=ticket_item)
        tickets.append(Ticket(**ticket_item))

    if order_item.get("discount_code"):
        DISCOUNT_CODES().update_item(
            Key={"code": order_item["discount_code"]},
            UpdateExpression="SET uses_count = uses_count + :one",
            ExpressionAttributeValues={":one": 1},
        )

    send_confirmation_email(Order(**order_item), tickets)


def _flag_fulfillment_error(order_id: str) -> None:
    """Leave a durable marker on the order so the failure is discoverable."""
    try:
        ORDERS().update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET fulfillment_error = :t",
            ExpressionAttributeValues={":t": True},
        )
    except Exception:
        # The log line above is the last line of defence if even this fails.
        logger.error("Could not flag fulfillment_error on order %s", order_id, exc_info=True)


def _handle_expired(session: dict) -> None:
    order_id = _order_id_from(session)
    if not order_id:
        logger.info("checkout.session.expired with no order_id metadata; ignoring")
        return

    try:
        resp = ORDERS().update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET #s = :expired",
            ConditionExpression="attribute_exists(order_id) AND #s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":expired": "expired", ":pending": "pending"},
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            # Already paid, already expired, or already cleaned up. Releasing
            # capacity here would double-release.
            return
        raise

    order_item = resp["Attributes"]
    EVENTS().update_item(
        Key={"event_id": order_item["event_id"]},
        UpdateExpression="SET tickets_sold_count = tickets_sold_count - :q",
        ExpressionAttributeValues={":q": int(order_item["quantity"])},
    )


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except Exception as exc:
        # A wrong/rotated stripe_webhook_secret or a delivery from the wrong
        # Stripe account both land here, and both look identical from the
        # outside (a bare 400). The exception message ("No signatures found
        # matching...", "Timestamp outside the tolerance zone", ...) is the
        # only thing that tells them apart, so it's worth one log line -- just
        # the message, never the payload or the secret itself.
        logger.warning("Webhook signature verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    handlers = {
        "checkout.session.completed": _handle_completed,
        "checkout.session.expired": _handle_expired,
    }
    handler = handlers.get(event["type"])
    if handler:
        handler(event["data"]["object"])

    return {"received": True}
