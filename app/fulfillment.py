from __future__ import annotations

import logging

from app.db import EVENTS, ORDERS, TICKETS
from app.emails import send_confirmation_email
from app.models import Event, Order, Ticket
from app.tickets import generate_ticket_id

logger = logging.getLogger(__name__)


def fulfill_order(order_id: str, order_item: dict) -> None:
    """Create one ticket per unit and email the buyer their tickets.

    Shared by the Stripe webhook (a completed checkout) and the admin
    "give tickets" page (a comped order). The caller is responsible for having
    already written the order and reserved capacity.
    """
    tickets: list[Ticket] = []
    for _ in range(int(order_item["quantity"])):
        ticket_item = {
            "ticket_id": generate_ticket_id(),
            "order_id": order_id,
            "event_id": order_item["event_id"],
            "attendee_name": None,
            "checked_in": False,
            "checked_in_at": None,
            "voided": False,
            "voided_at": None,
        }
        TICKETS().put_item(Item=ticket_item)
        tickets.append(Ticket(**ticket_item))

    # Tolerates a missing event (e.g. deleted after the order was placed):
    # send_confirmation_email treats event=None as "omit the event-details
    # section" rather than failing the whole send.
    event_item = EVENTS().get_item(Key={"event_id": order_item["event_id"]}).get("Item")
    event = Event(**event_item) if event_item else None
    send_confirmation_email(Order(**order_item), tickets, event)


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
