from __future__ import annotations

from app.models import DiscountCode

MAX_TICKETS_PER_ORDER = 20


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
