from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.models import DiscountCode

MAX_TICKETS_PER_ORDER = 20


def parse_dollars_to_cents(value: str) -> int:
    """Parse an admin-typed dollar amount ("15", "15.5", "15.50") into whole
    cents. Decimal, not float: currency math has no business rounding
    through binary floating point. Raises ValueError -- callers already have
    an established pattern for turning that into a form re-render with an
    error -- for anything that isn't a valid, non-negative amount.
    """
    try:
        dollars = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("not a valid dollar amount")
    if dollars < 0:
        raise ValueError("amount can't be negative")
    return int((dollars * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

# Stripe's standard US card rate. An ESTIMATE, not the actual per-charge rate:
# Amex, international, and manually-keyed cards run higher, and Stripe doesn't
# reveal the real rate until a charge settles. Every platform that "passes on"
# the fee (Eventbrite included) accepts this same estimate and eats small
# variances either way -- adjust here if it's ever wrong enough to matter.
STRIPE_PERCENT_FEE = 0.029
STRIPE_FIXED_FEE_CENTS = 30


def compute_processing_fee(subtotal_cents: int) -> int:
    """The amount to add on top of `subtotal_cents` so that, after Stripe
    takes its cut of the WHOLE charge (subtotal + this fee), the organizer
    still nets the full subtotal -- the standard "buyer covers the fee"
    gross-up: total = (subtotal + fixed) / (1 - rate).

    Rounds up: undercharging by even a fraction of a cent would mean the
    organizer doesn't quite net the target.
    """
    if subtotal_cents <= 0:
        return 0
    total_charged = math.ceil((subtotal_cents + STRIPE_FIXED_FEE_CENTS) / (1 - STRIPE_PERCENT_FEE))
    return total_charged - subtotal_cents


def current_ticket_price_cents(event: dict, today: str) -> int:
    """The ticket price actually in effect right now.

    `today` is an ISO "YYYY-MM-DD" string, passed in rather than read from
    the clock here so this stays a pure, easily-tested function -- callers
    use datetime.now(timezone.utc).date().isoformat(). If the event has a
    scheduled increase (price_increase_date/price_increase_cents, both admin-
    set and both required together) and today has reached that date, the
    increased price applies; otherwise the base ticket_price_cents does.
    This is the ONE place that decides the effective price -- both the
    display on the ticket card and the amount actually charged at checkout
    call it, so they can never disagree.
    """
    increase_date = event.get("price_increase_date")
    increase_cents = event.get("price_increase_cents")
    if increase_date and increase_cents is not None and today >= increase_date:
        return int(increase_cents)
    return int(event["ticket_price_cents"])


def price_increase_is_upcoming(event: dict, today: str) -> bool:
    """True when a scheduled price increase is configured and hasn't taken
    effect yet -- used to show/hide the "increases to $X on <date>" notice
    without duplicating current_ticket_price_cents's date comparison."""
    increase_date = event.get("price_increase_date")
    increase_cents = event.get("price_increase_cents")
    return bool(increase_date and increase_cents is not None and today < increase_date)


def compute_total(unit_price_cents: int, quantity: int, discount_code: DiscountCode | None) -> int:
    subtotal = unit_price_cents * quantity
    if discount_code is None:
        return subtotal
    if discount_code.discount_type == "percent":
        discounted = subtotal - (subtotal * discount_code.discount_value // 100)
    else:
        discounted = subtotal - discount_code.discount_value
    return max(discounted, 0)


def validate_discount_code(discount_code: DiscountCode | None, event_id: str) -> tuple[bool, str]:
    if discount_code is None:
        return True, ""
    if not discount_code.active:
        return False, "This code is no longer active."
    if discount_code.event_id != event_id:
        return False, "This code is not valid for this event."
    if discount_code.max_uses is not None and discount_code.uses_count >= discount_code.max_uses:
        return False, "This code has been exhausted."
    return True, ""


def validate_quantity(quantity: int, remaining: int) -> tuple[bool, str]:
    if quantity < 1:
        return False, "Please order at least 1 ticket."
    if quantity > MAX_TICKETS_PER_ORDER:
        return False, f"You can order at most {MAX_TICKETS_PER_ORDER} tickets in one order."
    if quantity > remaining:
        return False, f"Only {remaining} ticket(s) remain."
    return True, ""
