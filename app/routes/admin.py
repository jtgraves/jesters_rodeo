from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import boto3
import stripe
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import ValidationError

from app.auth import ADMIN_GROUP, require_admin, require_member
from app.config import settings
from app.db import ANNOUNCEMENTS, DISCOUNT_CODES, EVENTS, ORDERS, TICKETS, WAITLIST, paginate
from app.emails import send_confirmation_email
from app.fulfillment import fulfill_order, flag_fulfillment_error
from app.models import Announcement, DiscountCode, Order, Ticket, normalize_code
from app.pricing import MAX_TICKETS_PER_ORDER
from app.templating import templates

logger = logging.getLogger(__name__)

stripe.api_key = settings.stripe_secret_key
# Admin-only by default -- a new route is locked down unless it's deliberately
# put on member_router below.
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
# Pages members ("clowns" without admin rights) share with admins.
member_router = APIRouter(prefix="/admin", dependencies=[Depends(require_member)])

CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


def _format_cents(cents: int) -> str:
    """Integer cents -> a decimal dollar string, e.g. 30000 -> "300.00".

    Integer arithmetic throughout (never cents / 100) so this can't introduce
    the float rounding the rest of the app deliberately avoids by storing
    money as integer cents.
    """
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def _csv_safe(value: Any) -> Any:
    """Neutralize spreadsheet formula injection.

    Buyer names and attendee names are free text typed by the public, and the
    export is opened in Excel/Numbers by an admin. A leading =, +, - or @ makes
    the cell an executable formula.
    """
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(CSV_FORMULA_PREFIXES) else text


IMAGE_EXTENSIONS_BY_CONTENT_TYPE = {
    "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
}
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2MB: generous for a compressed banner/logo, and Create
                                    # Event can upload two of these in one request, so this
                                    # keeps that combined well under Lambda's payload ceiling.


class _ImageUploadError(Exception):
    pass


def _upload_event_image(file: UploadFile, event_id: str, label: str) -> str:
    if file.content_type not in IMAGE_EXTENSIONS_BY_CONTENT_TYPE:
        raise _ImageUploadError(f"{label} must be a JPEG, PNG, GIF, or WebP image.")
    data = file.file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise _ImageUploadError(f"{label} must be under 2MB.")

    ext = IMAGE_EXTENSIONS_BY_CONTENT_TYPE[file.content_type]
    key = f"{event_id}/{label.lower().replace(' ', '-')}-{uuid.uuid4().hex}.{ext}"
    boto3.client("s3", region_name=settings.aws_region).put_object(
        Bucket=settings.event_images_bucket, Key=key, Body=data, ContentType=file.content_type,
    )
    return f"https://{settings.event_images_bucket}.s3.{settings.aws_region}.amazonaws.com/{key}"


def _maybe_upload_image(
    file: UploadFile | None, event_id: str, label: str
) -> tuple[str | None, str | None]:
    """Returns (url, error). Both None means "no file was chosen" -- distinct
    from an upload that failed validation, and from a deliberate removal,
    which callers handle separately (a file input can't submit "please clear
    the existing image", only "no new file").
    """
    if not file or not file.filename:
        return None, None
    try:
        return _upload_event_image(file, event_id, label), None
    except _ImageUploadError as exc:
        return None, str(exc)


def _parse_timeline(
    times: list[str], activities: list[str], details: list[str]
) -> list[dict]:
    """Zip the three parallel form arrays into timeline rows, dropping any row
    with no activity (that's the trailing blank the 'Add activity' UI leaves)."""
    rows = []
    for time, activity, detail in zip(times, activities, details):
        activity = activity.strip()
        if not activity:
            continue
        rows.append({
            "time": time.strip(),
            "activity": activity,
            "details": detail.strip(),
        })
    return rows


def _events_page(request: Request, error: str | None = None, status_code: int = 200) -> Response:
    events = sorted(paginate(EVENTS().scan), key=lambda e: int(e["year"]), reverse=True)
    return templates.TemplateResponse(
        request, "admin/events.html", {"events": events, "error": error},
        status_code=status_code,
    )


def _new_event_page(request: Request, error: str | None = None, status_code: int = 200) -> Response:
    return templates.TemplateResponse(
        request, "admin/event_new.html", {"error": error}, status_code=status_code,
    )


@router.get("/events")
def list_events(request: Request) -> Response:
    return _events_page(request)


@router.get("/events/new")
def new_event_page(request: Request) -> Response:
    return _new_event_page(request)


@router.post("/events")
def create_event(
    request: Request,
    year: int = Form(...),
    name: str = Form(...),
    date: str = Form(...),
    location: str = Form(...),
    description: str = Form(...),
    ticket_price_cents: int = Form(...),
    capacity: int = Form(...),
    address: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    banner_image: UploadFile | None = File(None),
    banner_image_2: UploadFile | None = File(None),
    banner_image_3: UploadFile | None = File(None),
    logo_image: UploadFile | None = File(None),
    timeline_time: list[str] = Form([]),
    timeline_activity: list[str] = Form([]),
    timeline_details: list[str] = Form([]),
) -> Response:
    event_id = f"evt_{year}"
    banner_url, banner_error = _maybe_upload_image(banner_image, event_id, "Banner image")
    banner_url_2, banner_error_2 = _maybe_upload_image(banner_image_2, event_id, "Banner image 2")
    banner_url_3, banner_error_3 = _maybe_upload_image(banner_image_3, event_id, "Banner image 3")
    logo_url, logo_error = _maybe_upload_image(logo_image, event_id, "Logo")
    upload_error = banner_error or banner_error_2 or banner_error_3 or logo_error
    if upload_error:
        return _new_event_page(request, error=upload_error, status_code=400)

    try:
        EVENTS().put_item(
            Item={
                "event_id": event_id, "year": year, "name": name, "date": date,
                "location": location, "description": description,
                "ticket_price_cents": ticket_price_cents, "capacity": capacity,
                "tickets_sold_count": 0, "registration_open": False, "status": "draft",
                "registration_opens_at": None, "registration_closes_at": None,
                "banner_image_url": banner_url,
                "banner_image_url_2": banner_url_2,
                "banner_image_url_3": banner_url_3,
                "logo_url": logo_url,
                "address": address.strip() or None,
                "contact_name": contact_name.strip() or None,
                "contact_email": contact_email.strip() or None,
                "contact_phone": contact_phone.strip() or None,
                "timeline": _parse_timeline(timeline_time, timeline_activity, timeline_details),
            },
            # An unconditional put on an existing year resets tickets_sold_count
            # to 0 and status to draft while the paid orders for that event
            # remain — a double-submit would silently corrupt live capacity
            # accounting. The year IS the identity, so refuse to re-create it.
            ConditionExpression="attribute_not_exists(event_id)",
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return _new_event_page(
                request,
                error=f"An event for {year} already exists. Edit it instead of re-creating it.",
                status_code=409,
            )
        raise
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/open")
def open_event(event_id: str) -> RedirectResponse:
    # The public page renders "the" open event, so close any other first —
    # otherwise which event the public sees depends on scan ordering.
    for other in paginate(EVENTS().scan, FilterExpression=Attr("status").eq("open")):
        if other["event_id"] != event_id:
            _set_event_open(other["event_id"], False)
    _set_event_open(event_id, True)
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/close")
def close_event(event_id: str) -> RedirectResponse:
    _set_event_open(event_id, False)
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/images")
def update_event_images(
    request: Request,
    event_id: str,
    banner_image: UploadFile | None = File(None),
    banner_image_2: UploadFile | None = File(None),
    banner_image_3: UploadFile | None = File(None),
    logo_image: UploadFile | None = File(None),
    remove_banner_image: str = Form(""),
    remove_banner_image_2: str = Form(""),
    remove_banner_image_3: str = Form(""),
    remove_logo_image: str = Form(""),
) -> Response:
    banner_url, banner_error = _maybe_upload_image(banner_image, event_id, "Banner image")
    banner_url_2, banner_error_2 = _maybe_upload_image(banner_image_2, event_id, "Banner image 2")
    banner_url_3, banner_error_3 = _maybe_upload_image(banner_image_3, event_id, "Banner image 3")
    logo_url, logo_error = _maybe_upload_image(logo_image, event_id, "Logo")
    upload_error = banner_error or banner_error_2 or banner_error_3 or logo_error
    if upload_error:
        return _events_page(request, error=upload_error, status_code=400)

    # A file input can't be pre-filled with "the current image", so "no new
    # file chosen" has to mean leave-as-is, not clear -- clearing needs the
    # explicit Remove checkbox instead. Only touch fields that are actually
    # changing: a bare SET on every field, unconditionally, would silently
    # wipe an existing image every time the admin only meant to change the
    # other one.
    updates: dict[str, Any] = {}
    for field, new_url, remove in (
        ("banner_image_url", banner_url, remove_banner_image),
        ("banner_image_url_2", banner_url_2, remove_banner_image_2),
        ("banner_image_url_3", banner_url_3, remove_banner_image_3),
        ("logo_url", logo_url, remove_logo_image),
    ):
        if new_url:
            updates[field] = new_url
        elif remove:
            updates[field] = None

    if updates:
        EVENTS().update_item(
            Key={"event_id": event_id},
            UpdateExpression="SET " + ", ".join(f"{k} = :{k}" for k in updates),
            ExpressionAttributeValues={f":{k}": v for k, v in updates.items()},
        )
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/details")
def update_event_details(
    request: Request,
    event_id: str,
    name: str = Form(...),
    description: str = Form(...),
    location: str = Form(...),
    address: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
) -> Response:
    name, description, location = name.strip(), description.strip(), location.strip()
    if not name or not description or not location:
        return _events_page(
            request, error="Name, description, and location can't be empty.", status_code=400
        )
    EVENTS().update_item(
        Key={"event_id": event_id},
        # name and location are both DynamoDB reserved words, hence the aliases.
        UpdateExpression=(
            "SET #n = :n, description = :d, #l = :l, address = :addr, "
            "contact_name = :cn, contact_email = :ce, contact_phone = :cp"
        ),
        ExpressionAttributeNames={"#n": "name", "#l": "location"},
        ExpressionAttributeValues={
            ":n": name, ":d": description, ":l": location,
            ":addr": address.strip() or None,
            ":cn": contact_name.strip() or None,
            ":ce": contact_email.strip() or None,
            ":cp": contact_phone.strip() or None,
        },
    )
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/banner")
def update_event_banner(
    event_id: str,
    banner_message: str = Form(""),
    banner_style: str = Form("notice"),
) -> RedirectResponse:
    # No condition needed: cosmetic metadata, not capacity/status. A blank
    # message clears the banner.
    EVENTS().update_item(
        Key={"event_id": event_id},
        UpdateExpression="SET banner_message = :m, banner_style = :s",
        ExpressionAttributeValues={
            ":m": banner_message.strip() or None,
            ":s": banner_style if banner_style in ("notice", "urgent") else "notice",
        },
    )
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/timeline")
def update_event_timeline(
    event_id: str,
    timeline_time: list[str] = Form([]),
    timeline_activity: list[str] = Form([]),
    timeline_details: list[str] = Form([]),
) -> RedirectResponse:
    EVENTS().update_item(
        Key={"event_id": event_id},
        UpdateExpression="SET timeline = :t",
        ExpressionAttributeValues={
            ":t": _parse_timeline(timeline_time, timeline_activity, timeline_details),
        },
    )
    return RedirectResponse("/admin/events", status_code=303)


def _set_event_open(event_id: str, is_open: bool) -> None:
    EVENTS().update_item(
        Key={"event_id": event_id},
        UpdateExpression="SET registration_open = :flag, #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":flag": is_open,
            ":status": "open" if is_open else "closed",
        },
    )


def _default_event_id() -> str | None:
    """The event an admin almost certainly means when none was specified.

    Prefers the currently open event; falls back to the most recent by year
    so browsing between events (or after one closes) still lands somewhere
    useful instead of nowhere.
    """
    events = paginate(EVENTS().scan)
    if not events:
        return None
    open_events = [e for e in events if e.get("status") == "open"]
    if open_events:
        return open_events[0]["event_id"]
    return max(events, key=lambda e: int(e["year"]))["event_id"]


def _resolve_event_id(request: Request, event_id: str, path: str) -> Response | None:
    """Redirect to `path` with a sensible event_id filled in when it's missing.

    event_id is a plain query param with no default on every list/checkin
    route below, all reached via links that always supply it -- but a typed
    URL, an old bookmark, or an edited address bar on a phone doesn't. Without
    this, FastAPI's own required-param validation returns a raw JSON 422 that
    an admin fumbling with their phone at the door has no way to act on.
    Returns a redirect to follow, or None if event_id was already given.
    """
    if event_id:
        return None
    default = _default_event_id()
    if default is None:
        return _events_page(request, error="No events exist yet. Create one first.")
    return RedirectResponse(f"{path}?event_id={default}", status_code=303)


def _orders_for_event(event_id: str, q: str = "", status: str = "") -> list[dict]:
    orders = paginate(
        ORDERS().query,
        IndexName="event_id-index",
        KeyConditionExpression="event_id = :e",
        ExpressionAttributeValues={":e": event_id},
    )
    if q:
        needle = q.lower()
        orders = [
            o for o in orders
            if needle in o["buyer_name"].lower() or needle in o["buyer_email"].lower()
        ]
    if status:
        orders = [o for o in orders if o["status"] == status]
    return orders


def _tickets_for_order(order_id: str) -> list[dict]:
    return paginate(
        TICKETS().query,
        IndexName="order_id-index",
        KeyConditionExpression="order_id = :o",
        ExpressionAttributeValues={":o": order_id},
    )


def _get_order_or_404(order_id: str) -> dict:
    order = ORDERS().get_item(Key={"order_id": order_id}).get("Item")
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@member_router.get("/orders")
def list_orders(request: Request, event_id: str = "", q: str = "", status: str = "") -> Response:
    redirect = _resolve_event_id(request, event_id, "/admin/orders")
    if redirect is not None:
        return redirect
    orders = _orders_for_event(event_id, q, status)
    return templates.TemplateResponse(
        request, "admin/orders.html",
        {"orders": orders, "event_id": event_id, "q": q, "status": status},
    )


@member_router.get("/orders/export")
def export_orders(request: Request, event_id: str = "") -> Response:
    # Redirect to the human-readable page rather than back to /export itself,
    # so a missing event_id doesn't turn into a download loop.
    redirect = _resolve_event_id(request, event_id, "/admin/orders")
    if redirect is not None:
        return redirect
    orders = _orders_for_event(event_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "order_id", "buyer_name", "buyer_email", "quantity",
        "donation_usd", "total_usd", "status", "created_at",
    ])
    for o in orders:
        writer.writerow([
            o["order_id"], _csv_safe(o["buyer_name"]), _csv_safe(o["buyer_email"]),
            int(o["quantity"]), _format_cents(int(o.get("donation_cents", 0))),
            _format_cents(int(o["total_cents"])), o["status"], o["created_at"],
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="orders-{event_id}.csv"'},
    )


def _resend_confirmation_email(order_id: str) -> dict:
    order_item = _get_order_or_404(order_id)
    tickets = [Ticket(**t) for t in _tickets_for_order(order_id)]
    send_confirmation_email(Order(**order_item), tickets)
    return order_item


@member_router.post("/orders/{order_id}/resend-email")
def resend_email(order_id: str) -> RedirectResponse:
    order_item = _resend_confirmation_email(order_id)
    return RedirectResponse(f"/admin/orders?event_id={order_item['event_id']}", status_code=303)


@router.post("/orders/{order_id}/refund")
def refund_order(order_id: str) -> Response:
    order_item = _get_order_or_404(order_id)

    # Claim the right to refund BEFORE calling Stripe. Whoever wins this
    # conditional write is the only caller that will issue a refund, so a
    # double-click cannot refund twice or release capacity twice.
    try:
        ORDERS().update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET #s = :refunded",
            ConditionExpression="#s = :paid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":refunded": "refunded", ":paid": "paid"},
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return RedirectResponse(
                f"/admin/orders?event_id={order_item['event_id']}", status_code=303
            )
        raise

    # A comped order has no Stripe payment behind it -- "refunding" it just
    # voids the tickets and frees the seats, which the code below already does.
    if not order_item.get("comp"):
        try:
            stripe.Refund.create(payment_intent=order_item["stripe_payment_intent_id"])
        except Exception:
            # Put the order back so the admin can retry; nothing else has changed.
            ORDERS().update_item(
                Key={"order_id": order_id},
                UpdateExpression="SET #s = :paid",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":paid": "paid"},
            )
            raise HTTPException(
                status_code=502, detail="Stripe refund failed. Nothing was changed."
            )

    now = datetime.now(timezone.utc).isoformat()
    for ticket in _tickets_for_order(order_id):
        TICKETS().update_item(
            Key={"ticket_id": ticket["ticket_id"]},
            UpdateExpression="SET voided = :t, voided_at = :now",
            ExpressionAttributeValues={":t": True, ":now": now},
        )

    EVENTS().update_item(
        Key={"event_id": order_item["event_id"]},
        UpdateExpression="SET tickets_sold_count = tickets_sold_count - :q",
        ExpressionAttributeValues={":q": int(order_item["quantity"])},
    )
    return RedirectResponse(f"/admin/orders?event_id={order_item['event_id']}", status_code=303)


# ---- Give tickets (admin-issued comps) ----

def _give_tickets_page(
    request: Request, preselect: str = "", error: str | None = None, status_code: int = 200
) -> Response:
    events = sorted(paginate(EVENTS().scan), key=lambda e: int(e["year"]), reverse=True)
    return templates.TemplateResponse(
        request, "admin/give_tickets.html",
        {"events": events, "preselect": preselect, "error": error},
        status_code=status_code,
    )


@router.get("/give-tickets")
def give_tickets_page(request: Request, event_id: str = "") -> Response:
    return _give_tickets_page(request, preselect=event_id)


@router.post("/give-tickets")
def give_tickets(
    request: Request,
    event_id: str = Form(...),
    quantity: int = Form(...),
    buyer_name: str = Form(...),
    buyer_email: str = Form(...),
    discount_code: str = Form(""),
) -> Response:
    event = EVENTS().get_item(Key={"event_id": event_id}).get("Item")
    if not event:
        return _give_tickets_page(request, error="Choose a valid event.", status_code=400)

    buyer_name, buyer_email = buyer_name.strip(), buyer_email.strip()
    if not 1 <= quantity <= MAX_TICKETS_PER_ORDER:
        return _give_tickets_page(
            request, event_id,
            f"Quantity must be between 1 and {MAX_TICKETS_PER_ORDER}.", status_code=400,
        )
    if not buyer_name or not buyer_email:
        return _give_tickets_page(
            request, event_id, "Recipient name and email are required.", status_code=400
        )

    # Comped seats still count against capacity so the event can't oversell
    # without the admin seeing it. No open-registration check -- an admin can
    # comp tickets whenever they like.
    EVENTS().update_item(
        Key={"event_id": event_id},
        UpdateExpression="SET tickets_sold_count = tickets_sold_count + :q",
        ExpressionAttributeValues={":q": quantity},
    )

    order_id = f"ord_{uuid.uuid4().hex}"
    order_item = {
        "order_id": order_id, "event_id": event_id,
        "buyer_name": buyer_name, "buyer_email": buyer_email,
        "quantity": quantity, "unit_price_cents": 0,
        "discount_code": discount_code.strip() or None,
        "donation_cents": 0, "total_cents": 0,
        "stripe_checkout_session_id": None, "stripe_payment_intent_id": None,
        "status": "paid", "comp": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    ORDERS().put_item(Item=order_item)

    try:
        fulfill_order(order_id, order_item)
    except Exception:
        logger.exception("Comp fulfilment failed for order %s", order_id)
        flag_fulfillment_error(order_id)
        return _give_tickets_page(
            request, event_id,
            error="The tickets were recorded, but the email may not have sent. "
                  "Use Resend on the Orders page.",
            status_code=500,
        )
    return RedirectResponse(f"/admin/orders?event_id={event_id}", status_code=303)


def _discount_codes_page(
    request: Request, event_id: str, error: str | None = None, status_code: int = 200
) -> Response:
    codes = paginate(DISCOUNT_CODES().scan, FilterExpression=Attr("event_id").eq(event_id))
    return templates.TemplateResponse(
        request, "admin/discount_codes.html",
        {"codes": codes, "event_id": event_id, "error": error},
        status_code=status_code,
    )


@router.get("/discount-codes")
def list_discount_codes(request: Request, event_id: str = "") -> Response:
    redirect = _resolve_event_id(request, event_id, "/admin/discount-codes")
    if redirect is not None:
        return redirect
    return _discount_codes_page(request, event_id)


def _validate_discount_value(discount_type: str, discount_value: int) -> str | None:
    """Reject values that would misprice tickets. Returns an error, or None.

    A percent over 100 makes tickets free once compute_total's max(_, 0) floor
    kicks in; a negative percent makes them cost MORE than the subtotal. Neither
    is ever intended, and neither is visible until a customer hits checkout.
    """
    if discount_type == "percent" and not 0 <= discount_value <= 100:
        return "A percent discount must be between 0 and 100."
    if discount_type == "fixed" and discount_value < 0:
        return "A fixed discount cannot be negative."
    return None


@router.post("/discount-codes")
def create_discount_code(
    request: Request,
    code: str = Form(...),
    event_id: str = Form(...),
    discount_type: str = Form(...),
    discount_value: int = Form(...),
    max_uses: str = Form(""),
) -> Response:
    item = {
        # Stored normalized so lookup at checkout is case-insensitive.
        "code": normalize_code(code),
        "event_id": event_id,
        "discount_type": discount_type,
        "discount_value": discount_value,
        "max_uses": int(max_uses) if max_uses.strip() else None,
        "uses_count": 0,
        "active": True,
    }

    # Checkout reads codes back through DiscountCode(**item). Validating with
    # the same model HERE turns "every customer who types this code gets a 500"
    # into "the admin who typed it wrong sees why", at the moment they typed it.
    try:
        DiscountCode(**item)
    except ValidationError:
        return _discount_codes_page(
            request, event_id,
            error="Discount type must be either 'percent' or 'fixed'.",
            status_code=400,
        )

    error = _validate_discount_value(discount_type, discount_value)
    if error:
        return _discount_codes_page(request, event_id, error=error, status_code=400)

    DISCOUNT_CODES().put_item(Item=item)
    return RedirectResponse(f"/admin/discount-codes?event_id={event_id}", status_code=303)


@router.post("/discount-codes/{code}/deactivate")
def deactivate_discount_code(code: str, event_id: str = Form(...)) -> RedirectResponse:
    DISCOUNT_CODES().update_item(
        Key={"code": normalize_code(code)},
        UpdateExpression="SET active = :f",
        ExpressionAttributeValues={":f": False},
    )
    return RedirectResponse(f"/admin/discount-codes?event_id={event_id}", status_code=303)


@member_router.get("/waitlist")
def list_waitlist(request: Request, event_id: str = "") -> Response:
    redirect = _resolve_event_id(request, event_id, "/admin/waitlist")
    if redirect is not None:
        return redirect
    entries = paginate(WAITLIST().scan, FilterExpression=Attr("event_id").eq(event_id))
    return templates.TemplateResponse(
        request, "admin/waitlist.html", {"entries": entries, "event_id": event_id}
    )


@member_router.post("/waitlist/{waitlist_id}/notify")
def notify_waitlist_entry(waitlist_id: str, event_id: str = Form(...)) -> RedirectResponse:
    WAITLIST().update_item(
        Key={"waitlist_id": waitlist_id},
        UpdateExpression="SET notified = :t",
        ExpressionAttributeValues={":t": True},
    )
    return RedirectResponse(f"/admin/waitlist?event_id={event_id}", status_code=303)


def _search_checkin(event_id: str, q: str) -> list[dict]:
    """Every ticket for this event whose attendee name, or whose order's
    buyer name/email, matches -- for door staff working from a name or email
    instead of a scannable QR code or a written-down ticket ID.
    """
    needle = q.strip().lower()
    if not needle:
        return []

    orders_by_id = {
        o["order_id"]: o
        for o in paginate(
            ORDERS().query,
            IndexName="event_id-index",
            KeyConditionExpression="event_id = :e",
            ExpressionAttributeValues={":e": event_id},
        )
    }
    tickets = paginate(TICKETS().scan, FilterExpression=Attr("event_id").eq(event_id))

    matches = []
    for ticket in tickets:
        order = orders_by_id.get(ticket["order_id"])
        if not order:
            continue
        haystack = " ".join(filter(None, [
            order.get("buyer_name"), order.get("buyer_email"), ticket.get("attendee_name"),
        ])).lower()
        if needle in haystack:
            matches.append({
                **ticket,
                "buyer_name": order["buyer_name"],
                "buyer_email": order["buyer_email"],
            })
    return matches


@member_router.get("/checkin")
def checkin_page(request: Request, event_id: str = "", q: str = "") -> Response:
    redirect = _resolve_event_id(request, event_id, "/admin/checkin")
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(
        request, "admin/checkin.html",
        {"event_id": event_id, "q": q, "matches": _search_checkin(event_id, q)},
    )


def _checkin_response(request: Request, result: dict, event_id: str, q: str) -> Response:
    # Same content-negotiation convention as app.auth._not_authenticated:
    # a browser requesting an HTML page (the name/email search results' Check
    # In button, a plain form submit) gets redirected back to a fresh
    # rendering; anything else -- the JS scanner's fetch, which sets
    # Accept: application/json, and any client that sends no Accept header at
    # all -- gets the JSON result directly.
    if "text/html" in request.headers.get("accept", ""):
        suffix = f"&q={quote(q)}" if q else ""
        return RedirectResponse(f"/admin/checkin?event_id={event_id}{suffix}", status_code=303)
    return JSONResponse(result)


@member_router.post("/checkin/{ticket_id}")
def checkin_ticket(request: Request, ticket_id: str, event_id: str, q: str = "") -> Response:
    ticket = TICKETS().get_item(Key={"ticket_id": ticket_id}).get("Item")
    if not ticket:
        return _checkin_response(request, {"status": "invalid"}, event_id, q)

    # Tickets from previous years are still live rows in this table.
    if ticket["event_id"] != event_id:
        return _checkin_response(
            request, {"status": "wrong_event", "attendee_name": ticket["attendee_name"]}, event_id, q
        )

    if ticket.get("voided"):
        return _checkin_response(
            request, {"status": "voided", "attendee_name": ticket["attendee_name"]}, event_id, q
        )

    if ticket["checked_in"]:
        return _checkin_response(request, {
            "status": "already_checked_in",
            "attendee_name": ticket["attendee_name"],
            "checked_in_at": ticket["checked_in_at"],
        }, event_id, q)

    now = datetime.now(timezone.utc).isoformat()
    try:
        TICKETS().update_item(
            Key={"ticket_id": ticket_id},
            UpdateExpression="SET checked_in = :t, checked_in_at = :now",
            # Two doors, two phones, one guest: only one scan may win.
            ConditionExpression="checked_in = :f",
            ExpressionAttributeValues={":t": True, ":now": now, ":f": False},
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return _checkin_response(
                request, {"status": "already_checked_in", "attendee_name": ticket["attendee_name"]},
                event_id, q,
            )
        raise

    return _checkin_response(
        request, {"status": "checked_in", "attendee_name": ticket["attendee_name"]}, event_id, q
    )


@member_router.post("/checkin/{order_id}/resend-email")
def resend_email_from_checkin(order_id: str, q: str = "") -> RedirectResponse:
    order_item = _resend_confirmation_email(order_id)
    suffix = f"&q={quote(q)}" if q else ""
    return RedirectResponse(f"/admin/checkin?event_id={order_item['event_id']}{suffix}", status_code=303)


def _invoke_announcement_lambda(announcement_id: str) -> None:
    boto3.client("lambda", region_name=settings.aws_region).invoke(
        FunctionName=settings.announcement_lambda_name,
        InvocationType="Event",
        Payload=json.dumps({"announcement_id": announcement_id}).encode("utf-8"),
    )


def _announcements_page(
    request: Request, event_id: str, error: str | None = None, status_code: int = 200
) -> Response:
    items = paginate(ANNOUNCEMENTS().scan, FilterExpression=Attr("event_id").eq(event_id))
    items.sort(key=lambda a: a["created_at"], reverse=True)
    return templates.TemplateResponse(
        request, "admin/announcements.html",
        {"announcements": items, "event_id": event_id, "error": error},
        status_code=status_code,
    )


@router.get("/announcements")
def list_announcements(request: Request, event_id: str = "") -> Response:
    redirect = _resolve_event_id(request, event_id, "/admin/announcements")
    if redirect is not None:
        return redirect
    return _announcements_page(request, event_id)


@router.post("/announcements")
def create_announcement(
    request: Request,
    event_id: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    audience: list[str] = Form([]),
) -> Response:
    subject, body = subject.strip(), body.strip()
    if not audience:
        return _announcements_page(
            request, event_id, error="Pick at least one audience.", status_code=400
        )
    if not subject or not body:
        return _announcements_page(
            request, event_id, error="Subject and message can't be empty.", status_code=400
        )

    announcement_id = f"ann_{uuid.uuid4().hex}"
    item = {
        "announcement_id": announcement_id, "event_id": event_id,
        "subject": subject, "body": body, "audience": audience,
        "status": "queued", "recipient_count": None, "sent_count": 0,
        "error": None, "created_at": datetime.now(timezone.utc).isoformat(),
    }
    # Same "validate with the model that reads it back" guard as discount
    # codes: a tampered/malformed audience value gets a friendly 400 instead
    # of an unhandled 500 from pydantic.
    try:
        Announcement(**item)
    except ValidationError:
        return _announcements_page(request, event_id, error="Invalid announcement.", status_code=400)

    ANNOUNCEMENTS().put_item(Item=item)
    _invoke_announcement_lambda(announcement_id)
    return RedirectResponse(f"/admin/announcements?event_id={event_id}", status_code=303)


# ---- Charity benefit (per-event) ----

def _charity_page(
    request: Request, event_id: str, error: str | None = None, status_code: int = 200
) -> Response:
    event = EVENTS().get_item(Key={"event_id": event_id}).get("Item")
    return templates.TemplateResponse(
        request, "admin/charity.html",
        {"event": event, "event_id": event_id, "error": error},
        status_code=status_code,
    )


@router.get("/charity")
def charity_admin_page(request: Request, event_id: str = "") -> Response:
    redirect = _resolve_event_id(request, event_id, "/admin/charity")
    if redirect is not None:
        return redirect
    return _charity_page(request, event_id)


@router.post("/charity")
def update_charity(
    request: Request,
    event_id: str = Form(...),
    charity_name: str = Form(""),
    charity_description: str = Form(""),
    charity_website_url: str = Form(""),
    charity_contact_name: str = Form(""),
    charity_contact_email: str = Form(""),
    charity_contact_phone: str = Form(""),
    charity_logo: UploadFile | None = File(None),
    remove_charity_logo: str = Form(""),
    charity_banner_image: UploadFile | None = File(None),
    charity_banner_image_2: UploadFile | None = File(None),
    charity_banner_image_3: UploadFile | None = File(None),
    remove_charity_banner_image: str = Form(""),
    remove_charity_banner_image_2: str = Form(""),
    remove_charity_banner_image_3: str = Form(""),
    charity_banner_caption: str = Form(""),
    charity_banner_caption_2: str = Form(""),
    charity_banner_caption_3: str = Form(""),
) -> Response:
    website = charity_website_url.strip()
    if website and not website.startswith(("http://", "https://")):
        return _charity_page(
            request, event_id,
            error="The website link must start with http:// or https://.", status_code=400,
        )

    logo_url, logo_error = _maybe_upload_image(charity_logo, event_id, "Charity logo")
    banner_1_url, banner_1_err = _maybe_upload_image(charity_banner_image, event_id, "Charity banner")
    banner_2_url, banner_2_err = _maybe_upload_image(charity_banner_image_2, event_id, "Charity banner")
    banner_3_url, banner_3_err = _maybe_upload_image(charity_banner_image_3, event_id, "Charity banner")
    upload_error = logo_error or banner_1_err or banner_2_err or banner_3_err
    if upload_error:
        return _charity_page(request, event_id, error=upload_error, status_code=400)

    values = {
        "charity_name": charity_name.strip() or None,
        "charity_description": charity_description.strip() or None,
        "charity_website_url": website or None,
        "charity_contact_name": charity_contact_name.strip() or None,
        "charity_contact_email": charity_contact_email.strip() or None,
        "charity_contact_phone": charity_contact_phone.strip() or None,
        # Captions always overwrite: clearing the box clears the caption.
        "charity_banner_caption": charity_banner_caption.strip() or None,
        "charity_banner_caption_2": charity_banner_caption_2.strip() or None,
        "charity_banner_caption_3": charity_banner_caption_3.strip() or None,
    }
    if logo_url:
        values["charity_logo_url"] = logo_url
    elif remove_charity_logo:
        values["charity_logo_url"] = None
    # A file input can't say "keep the current image", so only touch a banner
    # slot when a new file came in or Remove was ticked -- mirrors
    # update_event_images.
    for field, new_url, remove in (
        ("charity_banner_image_url", banner_1_url, remove_charity_banner_image),
        ("charity_banner_image_url_2", banner_2_url, remove_charity_banner_image_2),
        ("charity_banner_image_url_3", banner_3_url, remove_charity_banner_image_3),
    ):
        if new_url:
            values[field] = new_url
        elif remove:
            values[field] = None

    EVENTS().update_item(
        Key={"event_id": event_id},
        UpdateExpression="SET " + ", ".join(f"{k} = :{k}" for k in values),
        ExpressionAttributeValues={f":{k}": v for k, v in values.items()},
    )
    return RedirectResponse(f"/admin/charity?event_id={event_id}", status_code=303)


# ---- Clown management (Cognito users) ----
# Everyone in the pool is a clown (member). The ones in the ADMIN_GROUP are
# clowns with admin privileges.

def _cognito():
    return boto3.client("cognito-idp", region_name=settings.aws_region)


def _admin_usernames() -> set[str]:
    resp = _cognito().list_users_in_group(
        UserPoolId=settings.cognito_user_pool_id, GroupName=ADMIN_GROUP
    )
    return {u["Username"] for u in resp.get("Users", [])}


def _list_clowns() -> list[dict]:
    users = _cognito().list_users(UserPoolId=settings.cognito_user_pool_id).get("Users", [])
    admin_usernames = _admin_usernames()
    clowns = []
    for u in users:
        attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
        clowns.append({
            # Username is the opaque sub on this pool (sign-in is by email alias).
            "username": u["Username"],
            "email": attrs.get("email", u["Username"]),
            "status": u.get("UserStatus", ""),
            "created": u.get("UserCreateDate"),
            "is_admin": u["Username"] in admin_usernames,
        })
    clowns.sort(key=lambda c: c["email"].lower())
    return clowns


def _create_clown(email: str, make_admin: bool) -> None:
    # No temporary password / no SUPPRESS: Cognito emails the invitation with a
    # generated password, and the new clown sets a real one on first sign-in.
    resp = _cognito().admin_create_user(
        UserPoolId=settings.cognito_user_pool_id,
        Username=email,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
    )
    if make_admin:
        _promote_clown(resp["User"]["Username"])


def _promote_clown(username: str) -> None:
    _cognito().admin_add_user_to_group(
        UserPoolId=settings.cognito_user_pool_id,
        Username=username,
        GroupName=ADMIN_GROUP,
    )


def _delete_clown(username: str) -> None:
    _cognito().admin_delete_user(
        UserPoolId=settings.cognito_user_pool_id, Username=username
    )


def _clowns_page(
    request: Request, current_sub: str, error: str | None = None, status_code: int = 200
) -> Response:
    return templates.TemplateResponse(
        request, "admin/clowns.html",
        {"clowns": _list_clowns(), "current_sub": current_sub, "error": error},
        status_code=status_code,
    )


@router.get("/clowns")
def list_clowns(
    request: Request, current_admin: dict = Depends(require_admin)
) -> Response:
    return _clowns_page(request, current_admin.get("sub"))


@router.post("/clowns")
def create_clown(
    request: Request,
    current_admin: dict = Depends(require_admin),
    email: str = Form(...),
    make_admin: str = Form(""),
) -> Response:
    email = email.strip()
    if not email:
        return _clowns_page(
            request, current_admin.get("sub"), error="Email is required.", status_code=400
        )
    try:
        _create_clown(email, make_admin=bool(make_admin))
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "UsernameExistsException":
            msg = "There's already a clown with that email."
        elif code in ("InvalidParameterException", "InvalidEmailRoleAccessPolicyException"):
            msg = "That doesn't look like a valid email address."
        else:
            raise
        return _clowns_page(request, current_admin.get("sub"), error=msg, status_code=400)
    return RedirectResponse("/admin/clowns", status_code=303)


@router.post("/clowns/{username}/promote")
def promote_clown(
    request: Request,
    username: str,
    current_admin: dict = Depends(require_admin),
) -> Response:
    _promote_clown(username)
    return RedirectResponse("/admin/clowns", status_code=303)


@router.post("/clowns/{username}/delete")
def delete_clown(
    request: Request,
    username: str,
    current_admin: dict = Depends(require_admin),
) -> Response:
    if username == current_admin.get("sub"):
        return _clowns_page(
            request, current_admin.get("sub"),
            error="You can't remove your own account.", status_code=400,
        )
    _delete_clown(username)
    return RedirectResponse("/admin/clowns", status_code=303)
