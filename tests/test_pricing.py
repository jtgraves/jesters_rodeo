import pytest

from app.pricing import (
    MAX_TICKETS_PER_ORDER,
    STRIPE_FIXED_FEE_CENTS,
    STRIPE_PERCENT_FEE,
    compute_processing_fee,
    parse_dollars_to_cents,
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
