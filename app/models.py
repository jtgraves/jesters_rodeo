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
    # Uploaded via the admin event forms, stored in the event-images S3 bucket.
    # Up to three banner images: the public event page cross-fades between
    # whichever ones are set (one = static, two or three = rotating).
    banner_image_url: str | None = None
    banner_image_url_2: str | None = None
    banner_image_url_3: str | None = None
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
    discount_code: str | None = None
    # An optional whole-dollar donation to the event's charity, charged as a
    # separate Stripe line item. Stored in cents for consistency with every
    # other money field; always a multiple of 100.
    donation_cents: int = 0
    total_cents: int
    stripe_checkout_session_id: str | None = None
    stripe_payment_intent_id: str | None = None
    status: Literal["pending", "paid", "refunded", "canceled", "expired"] = "pending"
    created_at: str
    # Set by the webhook when the payment succeeded but fulfilment (tickets,
    # discount tally, confirmation email) did not. Absent on every order that
    # went through cleanly.
    fulfillment_error: bool = False
    # An order created by an admin from the "give tickets" page: paid, $0, no
    # Stripe payment behind it.
    comp: bool = False


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
    photo_url: str | None = None
    bio: str | None = None
    phone: str | None = None
    address: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    years_ridden: list[int] = []
    is_lieutenant: bool = False
    lieutenant_title: str | None = None
    active: bool = True
    created_at: str


class KreweLink(BaseModel):
    """A link to a Google Doc/Sheet, shown on the clowns Resources page."""
    link_id: str
    label: str
    url: str
    description: str | None = None
    sort_order: int = 0
    created_at: str
