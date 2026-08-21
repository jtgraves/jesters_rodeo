from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


def normalize_code(raw: str) -> str:
    """Discount codes are case-insensitive and whitespace-tolerant.

    Applied on both write and lookup so `member20`, ` MEMBER20 `, and
    `Member20` all resolve to the same stored item.
    """
    return raw.strip().upper()


class Event(BaseModel):
    event_id: str
    year: int
    name: str
    date: str
    location: str
    description: str
    ticket_price_cents: int
    capacity: int
    tickets_sold_count: int = 0
    registration_open: bool = False
    registration_opens_at: str | None = None
    registration_closes_at: str | None = None
    status: Literal["draft", "open", "closed", "archived"] = "draft"


class Order(BaseModel):
    order_id: str
    event_id: str
    buyer_name: str
    buyer_email: str
    attendees: list[dict]
    quantity: int
    unit_price_cents: int
    discount_code: str | None = None
    total_cents: int
    stripe_checkout_session_id: str | None = None
    stripe_payment_intent_id: str | None = None
    status: Literal["pending", "paid", "refunded", "canceled", "expired"] = "pending"
    created_at: str
    # Set by the webhook when the payment succeeded but fulfilment (tickets,
    # discount tally, confirmation email) did not. Absent on every order that
    # went through cleanly.
    fulfillment_error: bool = False


class Ticket(BaseModel):
    ticket_id: str
    order_id: str
    event_id: str
    attendee_name: str | None = None
    checked_in: bool = False
    checked_in_at: str | None = None
    voided: bool = False
    voided_at: str | None = None


class DiscountCode(BaseModel):
    code: str
    event_id: str
    discount_type: Literal["percent", "fixed"]
    discount_value: int
    max_uses: int | None = None
    uses_count: int = 0
    active: bool = True


class WaitlistEntry(BaseModel):
    waitlist_id: str
    event_id: str
    name: str
    email: str
    requested_quantity: int
    created_at: str
    notified: bool = False
