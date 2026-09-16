# Card processing fee

Ticket buyers cover Stripe's processing fee — it's charged on top of the
ticket price (and any donation) as its own line item, not folded silently
into the price.

## The math

`app/pricing.py`'s `compute_processing_fee()` grosses up the charge so the
organizer nets the full subtotal after Stripe takes its cut:

```
fee = ceil((subtotal + $0.30) / (1 - 0.029)) - subtotal
```

This is applied to the **whole charge** — tickets plus any optional
donation — not just the ticket portion, since that's what Stripe actually
takes its percentage of. Rounds up, so the organizer's net is never short by
a fraction of a cent.

## It's an estimate, on purpose

`STRIPE_PERCENT_FEE` (2.9%) and `STRIPE_FIXED_FEE_CENTS` ($0.30) are Stripe's
**standard US card rate** — not the actual rate for every charge. Amex,
international cards, and manually-keyed transactions run higher, and Stripe
doesn't reveal the real per-charge rate until it settles, well after
checkout. Every platform that passes the fee on to the buyer (Eventbrite
included) accepts this same estimate and eats small variances either way.
If actual fees consistently run higher than this recovers, adjust the two
constants in `app/pricing.py` — nothing else needs to change.

## Refunds

Stripe does **not** refund its own processing fee when a charge is refunded,
even a full refund. `refund_order` in `app/routes/admin.py` refunds the
buyer's full `total_cents` (fee included) via `stripe.Refund.create`, but the
organizer's Stripe account still eats the original fee on that transaction —
covering the fee at checkout only helps on orders that go through cleanly to
the door, not on ones later refunded.

## Where it shows up

- A third Stripe Checkout line item, "Card processing fee" — visible at the
  actual payment step, same as the donation line item.
- A note on the registration form before checkout even starts.
- The order confirmation page and the admin orders CSV export
  (`processing_fee_usd` column), for reconciliation.

Comped orders (`/admin/give-tickets`) never touch Stripe, so they carry no
processing fee — `processing_fee_cents` stays 0 there.
