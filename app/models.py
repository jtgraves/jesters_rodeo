from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Event(BaseModel):
    event_id: str
    year: int
    name: str
    date: str
    location: str
    description: str
    ticket_price_cents: int
    # An optional scheduled early-bird-style price increase: from
    # price_increase_date (an ISO "YYYY-MM-DD" string) onward, the effective
    # price becomes price_increase_cents instead of ticket_price_cents. Both
    # unset (the default) means no increase is configured. Compared as plain
    # date strings -- no timezone math, the increase takes effect at the
    # start of that day UTC. See pricing.current_ticket_price_cents, the one
    # place this pair is ever read.
    price_increase_date: str | None = None
    price_increase_cents: int | None = None
    capacity: int
    tickets_sold_count: int = 0
    registration_open: bool = False
    registration_opens_at: str | None = None
    registration_closes_at: str | None = None
    status: Literal["draft", "open", "closed", "archived"] = "draft"
    # Legacy: up to three fixed banner images, uploaded via the admin event
    # forms. Superseded by banner_image_urls (an admin-managed pool of any
    # size); kept only so events that predate the pool still show a banner --
    # see _event_hero.html's fallback. Never written to by current code.
    banner_image_url: str | None = None
    banner_image_url_2: str | None = None
    banner_image_url_3: str | None = None
    # The banner pool: the public event page cross-fades through all of them,
    # in a fresh random order each render (zero = fallback gradient, one =
    # static, two or more = rotating). Stored in the event-images S3 bucket.
    banner_image_urls: list[str] = []
    logo_url: str | None = None
    # A site-wide notice (e.g. cancellation) shown above the hero regardless
    # of registration_open/status -- see _find_open_event's banner fallback.
    banner_message: str | None = None
    banner_style: Literal["notice", "urgent"] = "notice"
    # `location` is the short label ("New Orleans"); `address` is the specific
    # street address the map on the event page is centred on.
    address: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    # The overall "why we give back" story -- distinct from charity_description
    # below, which is specifically about *this year's* chosen charity. This
    # field is the krewe's own evergreen framing (e.g. "we've been doing this
    # for years, last year we raised $X"); it can be set and shown even before
    # this year's charity_name has been decided.
    giving_back_description: str | None = None
    # The charity this event benefits. Managed per-event on the admin Charity
    # page; surfaced on the public event page and the /charity page.
    charity_name: str | None = None
    charity_description: str | None = None
    charity_website_url: str | None = None
    charity_logo_url: str | None = None
    charity_contact_name: str | None = None
    charity_contact_email: str | None = None
    charity_contact_phone: str | None = None
    # Ordered schedule shown in the "Timeline" section of the event page.
    # Each item: {"time": "7:30pm", "activity": "...", "details": "..."}.
    timeline: list[dict] = []
    # The "What's Included" value-prop list on the event page -- one short
    # phrase per item (e.g. "Brass band accompaniment with police escort").
    # A plain list, not timeline's richer time/activity/details shape: these
    # items don't need their own edit-in-place admin rows, so they're edited
    # as one per line in a single textarea -- see update_event_details.
    perks: list[str] = []
    # Up to three banner images for the top of the public /charity page,
    # shown as a clickable carousel; each has an optional caption below it.
    charity_banner_image_url: str | None = None
    charity_banner_image_url_2: str | None = None
    charity_banner_image_url_3: str | None = None
    charity_banner_caption: str | None = None
    charity_banner_caption_2: str | None = None
    charity_banner_caption_3: str | None = None


class Order(BaseModel):
    order_id: str
    event_id: str
    buyer_name: str
    buyer_email: str
    quantity: int
    unit_price_cents: int
    # An optional whole-dollar donation to the event's charity, charged as a
    # separate Stripe line item. Stored in cents for consistency with every
    # other money field; always a multiple of 100.
    donation_cents: int = 0
    # The card processing fee passed on to the buyer -- see
    # pricing.compute_processing_fee. Charged as a third Stripe line item;
    # included in total_cents. Zero on a comped ("give tickets") order, which
    # never touches Stripe.
    processing_fee_cents: int = 0
    total_cents: int
    stripe_checkout_session_id: str | None = None
    stripe_payment_intent_id: str | None = None
    status: Literal["pending", "paid", "refunded", "canceled", "expired"] = "pending"
    created_at: str
    # Set by the webhook when the payment succeeded but fulfilment (tickets,
    # confirmation email) did not. Absent on every order that went through
    # cleanly.
    fulfillment_error: bool = False
    # An order created by an admin from the "give tickets" page: paid, $0, no
    # Stripe payment behind it.
    comp: bool = False
    # Check-in moves here from the (removed) Ticket model -- one scan admits
    # the whole party at once. Every write path that creates an Order MUST
    # set checked_in explicitly (not rely on this default) -- see Global
    # Constraints.
    checked_in: bool = False
    checked_in_at: str | None = None


class WaitlistEntry(BaseModel):
    waitlist_id: str
    event_id: str
    name: str
    email: str
    requested_quantity: int
    created_at: str
    notified: bool = False


class Announcement(BaseModel):
    announcement_id: str
    event_id: str
    subject: str
    body: str
    audience: list[Literal["attendees", "waitlist"]]
    status: Literal["queued", "sending", "sent", "failed"] = "queued"
    recipient_count: int | None = None
    sent_count: int = 0
    error: str | None = None
    created_at: str


class PastBeneficiary(BaseModel):
    """A charity the krewe has donated to in a prior year. Entered by hand on
    the admin side; shown on the public "Past Beneficiaries" page."""
    beneficiary_id: str
    name: str
    description: str | None = None
    website_url: str | None = None
    logo_url: str | None = None
    amount_cents: int = 0
    year: int | None = None
    created_at: str


class FaqEntry(BaseModel):
    """A question/answer pair shown on the public /faq page. sort_order is a
    manual tiebreaker (lower shows first); entries with the same order fall
    back to created_at."""
    faq_id: str
    question: str
    answer: str
    sort_order: int = 0
    created_at: str


class ClownProfile(BaseModel):
    """A Reaux-de-Eaux clown. The source of truth for the roster. A profile may
    have no Cognito login (a historical rider); it is linked to an account on
    first sign-in by matching email, or by an admin."""
    clown_id: str
    cognito_sub: str | None = None
    email: str | None = None
    display_name: str | None = None
    # What this clown wants printed on swag (a shirt, a cup, a name tag) --
    # distinct from display_name, which is used for roster/directory
    # listings and may be a full legal-ish name rather than what someone
    # actually wants on a t-shirt.
    swag_name: str | None = None
    photo_url: str | None = None
    bio: str | None = None
    phone: str | None = None
    address: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    years_ridden: list[int] = []
    # No per-float title/rank -- being a lieutenant is a single yes/no thing;
    # anywhere this is shown, the label is the fixed string "Float
    # Lieutenant", not admin-entered text.
    is_lieutenant: bool = False
    active: bool = True
    created_at: str


class KreweLink(BaseModel):
    """A resource shown on the clowns Resources page: usually a link to a
    Google Doc/Sheet, but url is optional -- some resources are just a piece
    of information (a label + description) with nothing to link to."""
    link_id: str
    label: str
    url: str | None = None
    description: str | None = None
    sort_order: int = 0
    created_at: str
