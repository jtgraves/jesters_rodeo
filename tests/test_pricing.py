import pytest

from app.models import DiscountCode
from app.pricing import (
    MAX_TICKETS_PER_ORDER,
    STRIPE_FIXED_FEE_CENTS,
    STRIPE_PERCENT_FEE,
    compute_processing_fee,
    compute_total,
    current_ticket_price_cents,
    parse_dollars_to_cents,
    price_increase_is_upcoming,
    validate_discount_code,
    validate_quantity,
)


def test_parse_dollars_to_cents_whole_and_fractional():
    assert parse_dollars_to_cents("15") == 1500
    assert parse_dollars_to_cents("15.00") == 1500
    assert parse_dollars_to_cents("15.5") == 1550
    assert parse_dollars_to_cents("15.99") == 1599
    assert parse_dollars_to_cents("0") == 0


def test_parse_dollars_to_cents_strips_whitespace():
    assert parse_dollars_to_cents("  15.00  ") == 1500


def test_parse_dollars_to_cents_rounds_sub_cent_amounts():
    assert parse_dollars_to_cents("15.005") == 1501  # round-half-up, not banker's rounding
    assert parse_dollars_to_cents("15.004") == 1500


def test_parse_dollars_to_cents_rejects_negative():
    with pytest.raises(ValueError):
        parse_dollars_to_cents("-5")


def test_parse_dollars_to_cents_rejects_garbage():
    for garbage in ("abc", "", "  ", "$15", "15,00"):
        with pytest.raises(ValueError):
            parse_dollars_to_cents(garbage)


def test_compute_total_no_discount():
    assert compute_total(15000, 2, None) == 30000


def test_compute_total_percent_discount():
    code = DiscountCode(code="MEMBER20", event_id="evt_2026", discount_type="percent", discount_value=20)
    assert compute_total(15000, 2, code) == 24000


def test_compute_total_fixed_discount():
    code = DiscountCode(code="SAVE10", event_id="evt_2026", discount_type="fixed", discount_value=1000)
    assert compute_total(15000, 1, code) == 14000


def test_compute_total_fixed_discount_floors_at_zero():
    code = DiscountCode(code="HUGE", event_id="evt_2026", discount_type="fixed", discount_value=99999)
    assert compute_total(15000, 1, code) == 0


def test_compute_processing_fee_zero_and_negative_subtotal():
    assert compute_processing_fee(0) == 0
    assert compute_processing_fee(-100) == 0


def test_compute_processing_fee_known_values():
    # Verified by hand against the gross-up formula; also pins the constants.
    assert compute_processing_fee(30000) == 927
    assert compute_processing_fee(24000) == 748
    assert compute_processing_fee(15000) == 479
    assert compute_processing_fee(100) == 34


def test_compute_processing_fee_organizer_nets_the_full_subtotal():
    """The whole point: after Stripe takes its cut of (subtotal + fee), the
    organizer's net should be >= the original subtotal (rounding up, never
    down, is what guarantees this)."""
    for subtotal in (100, 1500, 15000, 30000, 123456):
        fee = compute_processing_fee(subtotal)
        total_charged = subtotal + fee
        stripes_cut = total_charged * STRIPE_PERCENT_FEE + STRIPE_FIXED_FEE_CENTS
        net_to_organizer = total_charged - stripes_cut
        assert net_to_organizer >= subtotal - 1  # within a rounding cent


def test_current_ticket_price_cents_no_increase_configured():
    event = {"ticket_price_cents": 8500}
    assert current_ticket_price_cents(event, "2026-03-14") == 8500


def test_current_ticket_price_cents_before_increase_date():
    event = {"ticket_price_cents": 8500, "price_increase_date": "2026-03-01", "price_increase_cents": 9500}
    assert current_ticket_price_cents(event, "2026-02-28") == 8500


def test_current_ticket_price_cents_on_and_after_increase_date():
    event = {"ticket_price_cents": 8500, "price_increase_date": "2026-03-01", "price_increase_cents": 9500}
    assert current_ticket_price_cents(event, "2026-03-01") == 9500
    assert current_ticket_price_cents(event, "2026-03-15") == 9500


def test_price_increase_is_upcoming():
    event = {"ticket_price_cents": 8500, "price_increase_date": "2026-03-01", "price_increase_cents": 9500}
    assert price_increase_is_upcoming(event, "2026-02-28") is True
    assert price_increase_is_upcoming(event, "2026-03-01") is False
    assert price_increase_is_upcoming(event, "2026-03-15") is False


def test_price_increase_is_upcoming_false_when_unconfigured():
    assert price_increase_is_upcoming({"ticket_price_cents": 8500}, "2026-03-14") is False


def test_validate_discount_code_none():
    valid, err = validate_discount_code(None, "evt_2026")
    assert valid is True
    assert err == ""


def test_validate_discount_code_wrong_event():
    code = DiscountCode(code="X", event_id="evt_2025", discount_type="fixed", discount_value=100)
    valid, err = validate_discount_code(code, "evt_2026")
    assert valid is False
    assert "not valid" in err.lower()


def test_validate_discount_code_exhausted():
    code = DiscountCode(code="X", event_id="evt_2026", discount_type="fixed", discount_value=100, max_uses=5, uses_count=5)
    valid, err = validate_discount_code(code, "evt_2026")
    assert valid is False
    assert "exhausted" in err.lower() or "used" in err.lower()


def test_validate_discount_code_inactive():
    code = DiscountCode(code="X", event_id="evt_2026", discount_type="fixed", discount_value=100, active=False)
    valid, err = validate_discount_code(code, "evt_2026")
    assert valid is False


def test_validate_quantity_accepts_normal_order():
    valid, err = validate_quantity(2, remaining=300)
    assert valid is True
    assert err == ""


def test_validate_quantity_rejects_zero_and_negative():
    for bad in (0, -1, -50):
        valid, err = validate_quantity(bad, remaining=300)
        assert valid is False, f"quantity {bad} must be rejected"
        assert "at least 1" in err


def test_validate_quantity_rejects_above_per_order_cap():
    valid, err = validate_quantity(MAX_TICKETS_PER_ORDER + 1, remaining=300)
    assert valid is False
    assert str(MAX_TICKETS_PER_ORDER) in err


def test_validate_quantity_rejects_above_remaining_capacity():
    valid, err = validate_quantity(5, remaining=3)
    assert valid is False
    assert "3" in err
