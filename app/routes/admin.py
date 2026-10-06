from __future__ import annotations

import csv
import io
import json
import logging
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable
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
from app.db import (
    ANNOUNCEMENTS,
    CLOWN_PROFILES,
    EVENTS,
    FAQ_ENTRIES,
    KREWE_LINKS,
    ORDERS,
    PAST_BENEFICIARIES,
    WAITLIST,
    paginate,
)
from app.emails import send_confirmation_email
from app.fulfillment import fulfill_order, flag_fulfillment_error
from app.models import (
    Announcement,
    ClownProfile,
    Event,
    FaqEntry,
    KreweLink,
    Order,
    PastBeneficiary,
)
from app.pricing import MAX_TICKETS_PER_ORDER, parse_dollars_to_cents
from app.templating import templates

logger = logging.getLogger(__name__)

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


def _delete_event_image(url: str | None) -> None:
    """Best-effort cleanup of an S3 object this app uploaded, called whenever
    a stored reference to it is about to be removed or replaced. Every
    *_url field only ever points at our own bucket (set exclusively by
    _upload_event_image) or is a pre-existing/legacy value from before this
    cleanup existed -- either way, an unrecognized URL is left alone rather
    than guessed at. Never raises: a failed delete (already gone, a
    transient error) must not block the save/remove action that triggered
    it, and just leaves the object for a future manual sweep.
    """
    if not url:
        return
    prefix = f"https://{settings.event_images_bucket}.s3.{settings.aws_region}.amazonaws.com/"
    if not url.startswith(prefix):
        return
    key = url[len(prefix):]
    try:
        boto3.client("s3", region_name=settings.aws_region).delete_object(
            Bucket=settings.event_images_bucket, Key=key
        )
    except Exception:
        logger.warning("Failed to delete S3 object for a removed image: %s", key)


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
    ticket_price_dollars: str = Form(...),
    capacity: int = Form(...),
    address: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    logo_image: UploadFile | None = File(None),
    timeline_time: list[str] = Form([]),
    timeline_activity: list[str] = Form([]),
    timeline_details: list[str] = Form([]),
) -> Response:
    event_id = f"evt_{year}"
    try:
        ticket_price_cents = parse_dollars_to_cents(ticket_price_dollars)
    except ValueError:
        return _new_event_page(
            request, error="Ticket price must be a valid, non-negative dollar amount.",
            status_code=400,
        )
    logo_url, upload_error = _maybe_upload_image(logo_image, event_id, "Logo")
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
                # The banner pool is built afterward on the Events page (one
                # upload at a time -- see add_banner_pool_image); a brand new
                # event has no banner_image_urls yet.
                "banner_image_urls": [],
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
    logo_image: UploadFile | None = File(None),
    remove_logo_image: str = Form(""),
) -> Response:
    logo_url, upload_error = _maybe_upload_image(logo_image, event_id, "Logo")
    if upload_error:
        return _events_page(request, error=upload_error, status_code=400)

    # A file input can't be pre-filled with "the current image", so "no new
    # file chosen" has to mean leave-as-is, not clear -- clearing needs the
    # explicit Remove checkbox instead.
    if logo_url or remove_logo_image:
        event = EVENTS().get_item(Key={"event_id": event_id}).get("Item") or {}
        _delete_event_image(event.get("logo_url"))
        EVENTS().update_item(
            Key={"event_id": event_id},
            UpdateExpression="SET logo_url = :u",
            ExpressionAttributeValues={":u": logo_url},
        )
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/banner-pool")
def add_banner_pool_image(request: Request, event_id: str, image: UploadFile = File(...)) -> Response:
    url, upload_error = _maybe_upload_image(image, event_id, "Banner image")
    if upload_error:
        return _events_page(request, error=upload_error, status_code=400)
    if not url:
        return _events_page(request, error="Choose an image to add.", status_code=400)

    event = EVENTS().get_item(Key={"event_id": event_id}).get("Item") or {}
    pool = list(event.get("banner_image_urls") or [])
    pool.append(url)
    EVENTS().update_item(
        Key={"event_id": event_id},
        UpdateExpression="SET banner_image_urls = :p",
        ExpressionAttributeValues={":p": pool},
    )
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/banner-pool/remove")
def remove_banner_pool_image(event_id: str, url: str = Form(...)) -> RedirectResponse:
    event = EVENTS().get_item(Key={"event_id": event_id}).get("Item") or {}
    pool = [u for u in (event.get("banner_image_urls") or []) if u != url]
    EVENTS().update_item(
        Key={"event_id": event_id},
        UpdateExpression="SET banner_image_urls = :p",
        ExpressionAttributeValues={":p": pool},
    )
    _delete_event_image(url)
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/details")
def update_event_details(
    request: Request,
    event_id: str,
    year: int = Form(...),
    name: str = Form(...),
    date: str = Form(...),
    description: str = Form(...),
    location: str = Form(...),
    ticket_price_dollars: str = Form(...),
    capacity: int = Form(...),
    address: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    price_increase_date: str = Form(""),
    price_increase_dollars: str = Form(""),
    perks_text: str = Form(""),
) -> Response:
    name, date, description, location = name.strip(), date.strip(), description.strip(), location.strip()
    if not name or not date or not description or not location:
        return _events_page(
            request, error="Name, date, description, and location can't be empty.", status_code=400
        )
    try:
        ticket_price_cents = parse_dollars_to_cents(ticket_price_dollars)
    except ValueError:
        return _events_page(
            request, error="Ticket price must be a valid, non-negative dollar amount.",
            status_code=400,
        )
    if capacity < 0:
        return _events_page(request, error="Capacity can't be negative.", status_code=400)
    if year <= 0:
        return _events_page(request, error="Year must be a positive number.", status_code=400)

    price_increase_date = price_increase_date.strip()
    # Optional pair -- both set or neither. One without the other is
    # ambiguous (a date with no new price, or a price with nothing to
    # trigger it), so it's rejected rather than silently guessed at.
    if bool(price_increase_date) != bool(price_increase_dollars.strip()):
        return _events_page(
            request,
            error="A price increase needs both a date and an amount -- or leave both blank.",
            status_code=400,
        )
    price_increase_cents = None
    if price_increase_date:
        try:
            price_increase_cents = parse_dollars_to_cents(price_increase_dollars)
        except ValueError:
            return _events_page(
                request, error="Price increase amount must be a valid, non-negative dollar amount.",
                status_code=400,
            )

    # One item per line; blank lines dropped so stray extra newlines from
    # copy-pasted text don't turn into empty bullets on the public page.
    perks = [line.strip() for line in perks_text.splitlines() if line.strip()]

    EVENTS().update_item(
        Key={"event_id": event_id},
        # name, location, and capacity are all DynamoDB reserved words, hence
        # the aliases.
        UpdateExpression=(
            "SET #y = :year, #n = :n, #d = :date, description = :desc, #l = :l, "
            "ticket_price_cents = :price, #cap = :cap, address = :addr, "
            "contact_name = :cn, contact_email = :ce, contact_phone = :cp, "
            "price_increase_date = :pid, price_increase_cents = :pic, "
            "perks = :perks"
        ),
        ExpressionAttributeNames={
            "#y": "year", "#n": "name", "#d": "date", "#l": "location", "#cap": "capacity",
        },
        ExpressionAttributeValues={
            ":year": year, ":n": name, ":date": date, ":desc": description, ":l": location,
            ":price": ticket_price_cents, ":cap": capacity,
            ":addr": address.strip() or None,
            ":cn": contact_name.strip() or None,
            ":ce": contact_email.strip() or None,
            ":cp": contact_phone.strip() or None,
            ":pid": price_increase_date or None,
            ":pic": price_increase_cents,
            ":perks": perks,
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
    return RedirectResponse(f"/admin/announcements?event_id={event_id}", status_code=303)


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


# Keyed by the querystring value the "sort" param carries, so a column
# header link can name the field directly (see admin/orders.html's sort_th
# macro). Each extractor returns a plain comparable (lower-cased strings so
# sorting is case-insensitive, ints for the numeric columns since DynamoDB
# hands back Decimal) -- never a raw DynamoDB value, which sort() can't
# reliably compare across rows (e.g. mixed int/Decimal).
ORDER_SORT_KEYS: dict[str, Callable[[dict], object]] = {
    "created_at": lambda o: o["created_at"],
    "buyer_name": lambda o: o["buyer_name"].lower(),
    "buyer_email": lambda o: o["buyer_email"].lower(),
    "quantity": lambda o: int(o["quantity"]),
    "total_cents": lambda o: int(o["total_cents"]),
    "status": lambda o: o["status"],
}


def _orders_for_event(
    event_id: str, q: str = "", status: str = "", sort: str = "created_at", dir: str = "desc",
) -> list[dict]:
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
    key_fn = ORDER_SORT_KEYS.get(sort, ORDER_SORT_KEYS["created_at"])
    orders.sort(key=key_fn, reverse=(dir != "asc"))
    return orders


def _get_order_or_404(order_id: str) -> dict:
    order = ORDERS().get_item(Key={"order_id": order_id}).get("Item")
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@member_router.get("/orders")
def list_orders(
    request: Request, event_id: str = "", q: str = "", status: str = "",
    sort: str = "created_at", dir: str = "desc",
) -> Response:
    redirect = _resolve_event_id(request, event_id, "/admin/orders")
    if redirect is not None:
        return redirect
    # A malformed/forged querystring falls back to the default rather than
    # raising or silently no-op sorting -- same defensive pattern as
    # banner_style elsewhere in this file.
    if sort not in ORDER_SORT_KEYS:
        sort = "created_at"
    if dir not in ("asc", "desc"):
        dir = "desc"
    orders = _orders_for_event(event_id, q, status, sort, dir)
    return templates.TemplateResponse(
        request, "admin/orders.html",
        {"orders": orders, "event_id": event_id, "q": q, "status": status, "sort": sort, "dir": dir},
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
        "donation_usd", "processing_fee_usd", "total_usd", "status", "created_at",
    ])
    for o in orders:
        writer.writerow([
            o["order_id"], _csv_safe(o["buyer_name"]), _csv_safe(o["buyer_email"]),
            int(o["quantity"]), _format_cents(int(o.get("donation_cents", 0))),
            _format_cents(int(o.get("processing_fee_cents", 0))),
            _format_cents(int(o["total_cents"])), o["status"], o["created_at"],
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="orders-{event_id}.csv"'},
    )


def _resend_confirmation_email(order_id: str) -> dict:
    order_item = _get_order_or_404(order_id)
    event_item = EVENTS().get_item(Key={"event_id": order_item["event_id"]}).get("Item")
    event = Event(**event_item) if event_item else None
    send_confirmation_email(Order(**order_item), event)
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
        # require_admin already ran refresh_secure_params_if_stale for this
        # request (see auth._authenticated_claims), so settings.stripe_secret_key
        # is current -- just pick it up before the call.
        stripe.api_key = settings.stripe_secret_key
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
        "donation_cents": 0, "total_cents": 0,
        "stripe_checkout_session_id": None, "stripe_payment_intent_id": None,
        "status": "paid", "comp": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "checked_in": False, "checked_in_at": None,
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
    """Every paid order for this event whose buyer name or email matches --
    for door staff working from a name instead of a scannable QR code."""
    needle = q.strip().lower()
    if not needle:
        return []
    orders = paginate(
        ORDERS().query,
        IndexName="event_id-index",
        KeyConditionExpression="event_id = :e",
        ExpressionAttributeValues={":e": event_id},
    )
    return [
        o for o in orders
        if o["status"] == "paid"
        and (needle in o["buyer_name"].lower() or needle in o["buyer_email"].lower())
    ]


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


def _checkin_status_payload(order: dict) -> dict:
    """The shape every ready/already-checked-in check-in response shares."""
    return {"buyer_name": order["buyer_name"], "quantity": int(order["quantity"])}


def _resolve_checkin_order(order_id: str, event_id: str) -> tuple[dict | None, str | None]:
    """Shared validation for both the lookup and commit endpoints. Returns
    (order, None) if the order is scannable (whether or not already checked
    in), or (None, status) for a short-circuit status with nothing to
    confirm: not found, from a different event, or not a valid paid order
    (pending/refunded/canceled/expired -- "voided" covers all of these, same
    status name the old per-ticket check used for a refunded ticket)."""
    order = ORDERS().get_item(Key={"order_id": order_id}).get("Item")
    if not order:
        return None, "invalid"
    if order["event_id"] != event_id:
        return None, "wrong_event"
    if order["status"] != "paid":
        return None, "voided"
    return order, None


@member_router.get("/checkin/lookup/{order_id}")
def checkin_lookup(order_id: str, event_id: str) -> Response:
    order, status = _resolve_checkin_order(order_id, event_id)
    if status:
        return JSONResponse({"status": status})
    if order["checked_in"]:
        return JSONResponse({
            "status": "already_checked_in",
            "checked_in_at": order["checked_in_at"],
            **_checkin_status_payload(order),
        })
    return JSONResponse({"status": "ready", **_checkin_status_payload(order)})


@member_router.post("/checkin/{order_id}")
def checkin_order(request: Request, order_id: str, event_id: str, q: str = "") -> Response:
    order, status = _resolve_checkin_order(order_id, event_id)
    if status:
        return _checkin_response(request, {"status": status}, event_id, q)

    if order["checked_in"]:
        return _checkin_response(request, {
            "status": "already_checked_in",
            "checked_in_at": order["checked_in_at"],
            **_checkin_status_payload(order),
        }, event_id, q)

    now = datetime.now(timezone.utc).isoformat()
    try:
        ORDERS().update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET checked_in = :t, checked_in_at = :now",
            # Two doors, two phones, one party: only one confirm may win.
            ConditionExpression="attribute_not_exists(checked_in) OR checked_in = :f",
            ExpressionAttributeValues={":t": True, ":now": now, ":f": False},
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return _checkin_response(request, {
                "status": "already_checked_in",
                "checked_in_at": now,  # close enough: the other request just won
                **_checkin_status_payload(order),
            }, event_id, q)
        raise

    return _checkin_response(
        request, {"status": "checked_in", **_checkin_status_payload(order)}, event_id, q
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
    event = EVENTS().get_item(Key={"event_id": event_id}).get("Item")
    return templates.TemplateResponse(
        request, "admin/announcements.html",
        {"announcements": items, "event": event, "event_id": event_id, "error": error},
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
    # Validate with the same model that reads this back: a tampered/malformed
    # audience value gets a friendly 400 instead of an unhandled 500 from
    # pydantic.
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
    giving_back_description: str = Form(""),
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

    event = EVENTS().get_item(Key={"event_id": event_id}).get("Item") or {}

    values = {
        "giving_back_description": giving_back_description.strip() or None,
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
        _delete_event_image(event.get("charity_logo_url"))
        values["charity_logo_url"] = logo_url
    elif remove_charity_logo:
        _delete_event_image(event.get("charity_logo_url"))
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
            _delete_event_image(event.get(field))
            values[field] = new_url
        elif remove:
            _delete_event_image(event.get(field))
            values[field] = None

    EVENTS().update_item(
        Key={"event_id": event_id},
        UpdateExpression="SET " + ", ".join(f"{k} = :{k}" for k in values),
        ExpressionAttributeValues={f":{k}": v for k, v in values.items()},
    )
    return RedirectResponse(f"/admin/charity?event_id={event_id}", status_code=303)


# ---- Past beneficiaries (public "Past Beneficiaries" page) ----

def _sorted_beneficiaries() -> list[dict]:
    items = paginate(PAST_BENEFICIARIES().scan)
    # Most recent year first; entries with no year sink to the bottom, then by name.
    return sorted(
        items,
        key=lambda b: (-(int(b["year"]) if b.get("year") else 0), (b.get("name") or "").lower()),
    )


def _beneficiaries_page(
    request: Request, error: str | None = None, status_code: int = 200
) -> Response:
    return templates.TemplateResponse(
        request, "admin/beneficiaries.html",
        {"beneficiaries": _sorted_beneficiaries(), "error": error},
        status_code=status_code,
    )


@router.get("/beneficiaries")
def list_beneficiaries(request: Request) -> Response:
    return _beneficiaries_page(request)


@router.post("/beneficiaries")
def create_beneficiary(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    website_url: str = Form(""),
    year: str = Form(""),
    amount_dollars: str = Form(""),
    logo: UploadFile | None = File(None),
) -> Response:
    name = name.strip()
    website = website_url.strip()
    if not name:
        return _beneficiaries_page(request, error="Name is required.", status_code=400)
    if website and not website.startswith(("http://", "https://")):
        return _beneficiaries_page(
            request, error="The website link must start with http:// or https://.",
            status_code=400,
        )

    year_value: int | None = None
    if year.strip():
        try:
            year_value = int(year.strip())
        except ValueError:
            return _beneficiaries_page(request, error="Year must be a number.", status_code=400)

    amount_cents = 0
    if amount_dollars.strip():
        try:
            dollars = int(amount_dollars.strip())
        except ValueError:
            return _beneficiaries_page(
                request, error="Amount must be a whole dollar number.", status_code=400
            )
        if dollars < 0:
            return _beneficiaries_page(
                request, error="Amount can't be negative.", status_code=400
            )
        amount_cents = dollars * 100

    logo_url, logo_error = _maybe_upload_image(logo, "beneficiaries", "Beneficiary logo")
    if logo_error:
        return _beneficiaries_page(request, error=logo_error, status_code=400)

    item = {
        "beneficiary_id": f"ben_{uuid.uuid4().hex}",
        "name": name,
        "description": description.strip() or None,
        "website_url": website or None,
        "logo_url": logo_url,
        "amount_cents": amount_cents,
        "year": year_value,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        PastBeneficiary(**item)
    except ValidationError:
        return _beneficiaries_page(request, error="Invalid beneficiary.", status_code=400)

    PAST_BENEFICIARIES().put_item(Item=item)
    return RedirectResponse("/admin/beneficiaries", status_code=303)


@router.post("/beneficiaries/{beneficiary_id}/delete")
def delete_beneficiary(beneficiary_id: str) -> RedirectResponse:
    item = PAST_BENEFICIARIES().get_item(Key={"beneficiary_id": beneficiary_id}).get("Item") or {}
    _delete_event_image(item.get("logo_url"))
    PAST_BENEFICIARIES().delete_item(Key={"beneficiary_id": beneficiary_id})
    return RedirectResponse("/admin/beneficiaries", status_code=303)


# ---- FAQ (public /faq page) ----

def _sorted_faq_entries() -> list[dict]:
    items = paginate(FAQ_ENTRIES().scan)
    return sorted(items, key=lambda f: (int(f.get("sort_order") or 0), f.get("created_at") or ""))


def _faq_page(request: Request, error: str | None = None, status_code: int = 200) -> Response:
    return templates.TemplateResponse(
        request, "admin/faq.html",
        {"entries": _sorted_faq_entries(), "error": error},
        status_code=status_code,
    )


@router.get("/faq")
def list_faq(request: Request) -> Response:
    return _faq_page(request)


def _write_faq_order(ids: list[str]) -> None:
    """Renumber sort_order to match list position (0, 1, 2, ...)."""
    for position, faq_id in enumerate(ids):
        FAQ_ENTRIES().update_item(
            Key={"faq_id": faq_id},
            UpdateExpression="SET sort_order = :s",
            ExpressionAttributeValues={":s": position},
        )


@router.post("/faq")
def create_faq(
    request: Request,
    question: str = Form(...),
    answer: str = Form(...),
) -> Response:
    question, answer = question.strip(), answer.strip()
    if not question or not answer:
        return _faq_page(request, error="Question and answer are both required.", status_code=400)

    item = {
        "faq_id": f"faq_{uuid.uuid4().hex}",
        "question": question,
        "answer": answer,
        # New entries land at the bottom; reorder with the arrows.
        "sort_order": len(_sorted_faq_entries()),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        FaqEntry(**item)
    except ValidationError:
        return _faq_page(request, error="Invalid FAQ entry.", status_code=400)

    FAQ_ENTRIES().put_item(Item=item)
    return RedirectResponse("/admin/faq", status_code=303)


@router.post("/faq/{faq_id}")
def update_faq(
    request: Request,
    faq_id: str,
    question: str = Form(...),
    answer: str = Form(...),
) -> Response:
    question, answer = question.strip(), answer.strip()
    if not question or not answer:
        return _faq_page(request, error="Question and answer are both required.", status_code=400)
    FAQ_ENTRIES().update_item(
        Key={"faq_id": faq_id},
        UpdateExpression="SET question = :q, answer = :a",
        ExpressionAttributeValues={":q": question, ":a": answer},
    )
    return RedirectResponse("/admin/faq", status_code=303)


@router.post("/faq/{faq_id}/move")
def move_faq(faq_id: str, direction: str = Form(...)) -> RedirectResponse:
    ids = [e["faq_id"] for e in _sorted_faq_entries()]
    if faq_id in ids:
        i = ids.index(faq_id)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            _write_faq_order(ids)
    return RedirectResponse("/admin/faq", status_code=303)


@router.post("/faq/{faq_id}/delete")
def delete_faq(faq_id: str) -> RedirectResponse:
    FAQ_ENTRIES().delete_item(Key={"faq_id": faq_id})
    _write_faq_order([e["faq_id"] for e in _sorted_faq_entries()])
    return RedirectResponse("/admin/faq", status_code=303)


# ---- Clown management (Cognito users) ----
# Everyone in the pool is a clown (member). The ones in the ADMIN_GROUP are
# clowns with admin privileges.

def _cognito():
    return boto3.client("cognito-idp", region_name=settings.aws_region)


def _cognito_sub_is_live(sub: str) -> bool:
    """Whether `sub` is a real, current user in THIS pool -- not a leftover
    value from a retired pool (e.g. after the case-sensitivity migration in
    infra/jesters_rodeo_stack.py, which points settings.cognito_user_pool_id
    at a brand new pool and leaves every existing ClownProfile's cognito_sub
    referring to the old one) or a deleted account. A ClownProfile carrying
    a stale sub looks linked but isn't -- without this check, both
    _my_profile's relink and the CSV importer's adopt-by-email path would
    treat it as someone else's account and create a duplicate instead of
    reclaiming it. Checked directly against Cognito rather than guessed,
    so it can't be fooled by a pool exceeding list_users' one-page limit
    elsewhere in this file.
    """
    try:
        _cognito().admin_get_user(UserPoolId=settings.cognito_user_pool_id, Username=sub)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "UserNotFoundException":
            return False
        raise


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


def _resend_clown_invite(username: str) -> None:
    # MessageAction="RESEND" only works while the account is still
    # FORCE_CHANGE_PASSWORD (invited, never signed in) -- Cognito rejects it
    # once someone's actually set a real password. Generates a fresh
    # temporary password and re-sends the same invitation email template
    # already configured on the pool; no UserAttributes needed since the
    # account already exists.
    #
    # `username` here is the opaque sub (see _list_clowns), which is what
    # Cognito actually stores as this pool's Username -- and what lookups
    # like admin_get_user/admin_add_user_to_group accept fine. But
    # AdminCreateUser's Username argument is validated against the pool's
    # UsernameAttributes schema (email) regardless of MessageAction,
    # confirmed in production: passing the sub there failed with
    # "Username should be an email." for a clown who hadn't signed in yet,
    # not an already-confirmed one. Resolve to the real email first.
    user = _cognito().admin_get_user(UserPoolId=settings.cognito_user_pool_id, Username=username)
    email = next(a["Value"] for a in user["UserAttributes"] if a["Name"] == "email")
    _cognito().admin_create_user(
        UserPoolId=settings.cognito_user_pool_id,
        Username=email,
        MessageAction="RESEND",
    )


def _promote_clown(username: str) -> None:
    _cognito().admin_add_user_to_group(
        UserPoolId=settings.cognito_user_pool_id,
        Username=username,
        GroupName=ADMIN_GROUP,
    )


def _demote_clown(username: str) -> None:
    _cognito().admin_remove_user_from_group(
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
        request, "admin/clown_mgmt.html",
        {"clowns": _list_clowns(), "current_sub": current_sub, "error": error},
        status_code=status_code,
    )


@router.get("/clown_mgmt")
def list_clowns(
    request: Request, current_admin: dict = Depends(require_admin)
) -> Response:
    return _clowns_page(request, current_admin.get("sub"))


@router.post("/clown_mgmt")
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
    return RedirectResponse("/admin/clown_mgmt", status_code=303)


@router.post("/clown_mgmt/{username}/resend-invite")
def resend_clown_invite(
    request: Request,
    username: str,
    current_admin: dict = Depends(require_admin),
) -> Response:
    try:
        _resend_clown_invite(username)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        # Logged regardless of which branch below applies: a prior version
        # of this mapped InvalidParameterException straight to "already
        # confirmed" on the assumption that's the only thing Cognito guards
        # here, and that assumption turned out to be wrong for a still-
        # pending clown -- logging the real message is what actually
        # revealed that, rather than guessing again.
        logger.warning(
            "Resend invite failed for %s: %s - %s",
            username, code, exc.response["Error"].get("Message"),
        )
        if code == "InvalidParameterException":
            # Cognito's own guard: RESEND only works pre-confirmation.
            msg = "This clown has already signed in and set a password -- nothing to resend."
        elif code == "UserNotFoundException":
            msg = "That clown no longer exists."
        else:
            raise
        return _clowns_page(request, current_admin.get("sub"), error=msg, status_code=400)
    return RedirectResponse("/admin/clown_mgmt", status_code=303)


@router.post("/clown_mgmt/{username}/promote")
def promote_clown(
    request: Request,
    username: str,
    current_admin: dict = Depends(require_admin),
) -> Response:
    _promote_clown(username)
    return RedirectResponse("/admin/clown_mgmt", status_code=303)


@router.post("/clown_mgmt/{username}/demote")
def demote_clown(
    request: Request,
    username: str,
    current_admin: dict = Depends(require_admin),
) -> Response:
    if username == current_admin.get("sub"):
        return _clowns_page(
            request, current_admin.get("sub"),
            error="You can't remove your own admin rights.", status_code=400,
        )
    _demote_clown(username)
    return RedirectResponse("/admin/clown_mgmt", status_code=303)


@router.post("/clown_mgmt/{username}/delete")
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
    return RedirectResponse("/admin/clown_mgmt", status_code=303)


# ---- Clowns section (members-only krewe area) ----

def _my_profile(claims: dict) -> dict:
    """Return the calling clown's ClownProfile, creating or email-linking it.

    1. by cognito_sub -> return it
    2. a profile whose email matches, and whose own cognito_sub is either
       unset or stale (see _cognito_sub_is_live) -> attach cognito_sub,
       return it (a pre-seeded / returning rider, or a rider carried over
       from a retired user pool, connecting to their current account)
    3. otherwise create a fresh linked profile
    """
    sub = claims.get("sub", "")
    email = (claims.get("email") or "").strip()
    email_key = email.lower()
    profiles = paginate(CLOWN_PROFILES().scan)

    for p in profiles:
        if p.get("cognito_sub") == sub:
            return p

    # Step 2 adopts a profile off the token's email, so only trust an
    # address Cognito has verified.
    if email_key and claims.get("email_verified"):
        for p in profiles:
            if (p.get("email") or "").strip().lower() != email_key:
                continue
            stored_sub = p.get("cognito_sub")
            if stored_sub and _cognito_sub_is_live(stored_sub):
                continue  # a different, currently-real account -- not ours to adopt
            CLOWN_PROFILES().update_item(
                Key={"clown_id": p["clown_id"]},
                UpdateExpression="SET cognito_sub = :s",
                ExpressionAttributeValues={":s": sub},
            )
            p["cognito_sub"] = sub
            return p

    item = {
        "clown_id": f"clown_{uuid.uuid4().hex}",
        "cognito_sub": sub,
        "email": email or None,
        "display_name": None, "swag_name": None, "photo_url": None, "bio": None,
        "phone": None, "address": None,
        "emergency_contact_name": None, "emergency_contact_phone": None,
        "years_ridden": [], "is_lieutenant": False,
        "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    CLOWN_PROFILES().put_item(Item=item)
    return item


@member_router.get("/clowns")
def clowns_landing(claims: dict = Depends(require_member)) -> RedirectResponse:
    """The Clowns area has no hub page -- navigation is the menu. Anyone landing
    here (post-login, an old bookmark, the nav's group link) gets their profile
    created if needed and is dropped on Resources."""
    _my_profile(claims)
    return RedirectResponse("/admin/clowns/resources", status_code=303)


# ---- Resources links (krewe links) ----

def _sorted_krewe_links() -> list[dict]:
    items = paginate(KREWE_LINKS().scan)
    return sorted(items, key=lambda x: (int(x.get("sort_order") or 0), (x.get("label") or "").lower()))


def _write_krewe_link_order(ids: list[str]) -> None:
    """Renumber sort_order to match list position (0, 1, 2, ...)."""
    for position, link_id in enumerate(ids):
        KREWE_LINKS().update_item(
            Key={"link_id": link_id},
            UpdateExpression="SET sort_order = :s",
            ExpressionAttributeValues={":s": position},
        )


def _resources_page(
    request: Request, error: str | None = None, status_code: int = 200,
    profile: dict | None = None,
) -> Response:
    return templates.TemplateResponse(
        request, "admin/clowns_resources.html",
        {"links": _sorted_krewe_links(), "error": error, "profile": profile},
        status_code=status_code,
    )


@member_router.get("/clowns/resources")
def clowns_resources(request: Request, claims: dict = Depends(require_member)) -> Response:
    # Resources is the members' landing page, so this is where the calling
    # clown's profile is created on first visit and where the "finish your
    # profile" nudge lives.
    return _resources_page(request, profile=_my_profile(claims))


@router.post("/clowns/resources")
def create_krewe_link(
    request: Request, label: str = Form(...), url: str = Form(""), description: str = Form(""),
) -> Response:
    label, url = label.strip(), url.strip()
    if not label:
        return _resources_page(request, error="Label is required.", status_code=400)
    if url and not url.startswith(("http://", "https://")):
        return _resources_page(request, error="The URL must start with http:// or https://.", status_code=400)
    KREWE_LINKS().put_item(Item={
        "link_id": f"lnk_{uuid.uuid4().hex}",
        "label": label, "url": url or None, "description": description.strip() or None,
        "sort_order": len(_sorted_krewe_links()),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return RedirectResponse("/admin/clowns/resources", status_code=303)


@router.post("/clowns/resources/{link_id}")
def update_krewe_link(
    request: Request, link_id: str,
    label: str = Form(...), url: str = Form(""), description: str = Form(""),
) -> Response:
    label, url = label.strip(), url.strip()
    if not label:
        return _resources_page(request, error="Label is required.", status_code=400)
    if url and not url.startswith(("http://", "https://")):
        return _resources_page(request, error="The URL must start with http:// or https://.", status_code=400)
    KREWE_LINKS().update_item(
        Key={"link_id": link_id},
        UpdateExpression="SET label = :l, #u = :u, description = :d",
        ExpressionAttributeNames={"#u": "url"},
        ExpressionAttributeValues={":l": label, ":u": url or None, ":d": description.strip() or None},
    )
    return RedirectResponse("/admin/clowns/resources", status_code=303)


@router.post("/clowns/resources/{link_id}/move")
def move_krewe_link(link_id: str, direction: str = Form(...)) -> RedirectResponse:
    ids = [x["link_id"] for x in _sorted_krewe_links()]
    if link_id in ids:
        i = ids.index(link_id)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            _write_krewe_link_order(ids)
    return RedirectResponse("/admin/clowns/resources", status_code=303)


@router.post("/clowns/resources/{link_id}/delete")
def delete_krewe_link(link_id: str) -> RedirectResponse:
    KREWE_LINKS().delete_item(Key={"link_id": link_id})
    _write_krewe_link_order([x["link_id"] for x in _sorted_krewe_links()])
    return RedirectResponse("/admin/clowns/resources", status_code=303)


def _update_clown_fields(clown_id: str, fields: dict, *, require_exists: bool = False) -> None:
    if not fields:
        return
    kwargs: dict = dict(
        Key={"clown_id": clown_id},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
        ExpressionAttributeNames={f"#{k}": k for k in fields},
        ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
    )
    if require_exists:
        # update_item upserts; without this a stale id writes a bare ghost row.
        kwargs["ConditionExpression"] = Attr("clown_id").exists()
    CLOWN_PROFILES().update_item(**kwargs)


@member_router.get("/clowns/profile")
def my_profile_page(request: Request, claims: dict = Depends(require_member)) -> Response:
    return templates.TemplateResponse(
        request, "admin/clowns_profile.html", {"profile": _my_profile(claims)}
    )


@member_router.post("/clowns/profile")
def update_my_profile(
    request: Request,
    claims: dict = Depends(require_member),
    display_name: str = Form(""),
    swag_name: str = Form(""),
    years_ridden: str = Form(""),
    bio: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    emergency_contact_name: str = Form(""),
    emergency_contact_phone: str = Form(""),
    photo: UploadFile | None = File(None),
) -> Response:
    profile = _my_profile(claims)
    fields = {
        "display_name": display_name.strip() or None,
        "swag_name": swag_name.strip() or None,
        # Self-service, same as the admin roster-manage form: a clown owns
        # their own ride history now too, not just admins.
        "years_ridden": _parse_years(years_ridden),
        "bio": bio.strip() or None,
        "phone": phone.strip() or None,
        "address": address.strip() or None,
        "emergency_contact_name": emergency_contact_name.strip() or None,
        "emergency_contact_phone": emergency_contact_phone.strip() or None,
    }
    photo_url, photo_error = _maybe_upload_image(photo, "clowns", "Clown photo")
    if photo_error:
        return templates.TemplateResponse(
            request, "admin/clowns_profile.html",
            {"profile": profile, "error": photo_error}, status_code=400,
        )
    if photo_url:
        _delete_event_image(profile.get("photo_url"))
        fields["photo_url"] = photo_url
    _update_clown_fields(profile["clown_id"], fields)
    return RedirectResponse("/admin/clowns/profile", status_code=303)


def _all_profiles() -> list[dict]:
    profiles = paginate(CLOWN_PROFILES().scan)
    profiles.sort(key=lambda p: (p.get("display_name") or p.get("email") or p["clown_id"]).lower())
    return profiles


def _profile_years(p: dict) -> list[int]:
    return sorted(int(y) for y in (p.get("years_ridden") or []))


def _current_krewe_year() -> int:
    years = [int(e["year"]) for e in paginate(EVENTS().scan) if e.get("year")]
    return max(years) if years else date.today().year


@member_router.get("/clowns/roster")
def clowns_roster(request: Request, year: int | None = None) -> Response:
    profiles = _all_profiles()
    all_years = sorted({y for p in profiles for y in _profile_years(p)}, reverse=True)
    # Default to the roster's own highest year, not the Events table's --
    # roster data for a new year (e.g. bulk-imported ahead of time) can
    # legitimately exist before that year's Event record does, and the
    # roster link should reflect the roster, not lag behind it. Only when
    # no profile has any years_ridden at all is there nothing in `all_years`
    # to default to, so fall back to the current krewe year then.
    selected = year if year is not None else (all_years[0] if all_years else _current_krewe_year())
    riders = [
        {
            **p,
            # Tenure as of the year being viewed, not a running total to
            # today -- a rider on their 7th year in 2026 shows as on their
            # 6th when 2025 is the selected year, not their 7th.
            "tenure": len([y for y in _profile_years(p) if y <= selected]),
        }
        for p in profiles if selected in _profile_years(p)
    ]
    # Lieutenant is a current-standing flag, not tracked per year -- an
    # inactive former lieutenant isn't floated/badged as if still serving,
    # even in a year they did ride. Stable sort: riders is already
    # alphabetical (from _all_profiles()), so this only promotes
    # lieutenants, same pattern as the directory page.
    riders.sort(key=lambda r: not (r.get("is_lieutenant") and r.get("active", True)))
    return templates.TemplateResponse(
        request, "admin/clowns_roster.html",
        {"riders": riders, "year": selected, "all_years": all_years},
    )


@member_router.get("/clowns/directory")
def clowns_directory(request: Request) -> Response:
    people = [p for p in _all_profiles() if p.get("active", True)]
    # Lieutenants first (alphabetical within each group) -- _all_profiles()
    # already sorted alphabetically, so this only needs to promote lieutenants.
    people.sort(key=lambda p: not p.get("is_lieutenant"))
    return templates.TemplateResponse(
        request, "admin/clowns_directory.html", {"people": people}
    )


# ---- Manage roster (admin) ----

def _parse_years(text: str) -> list[int]:
    """'2018-2021, 2023' -> [2018, 2019, 2020, 2021, 2023]. Splits on any run
    of non-digit/non-hyphen; expands A-B inclusive; de-dupes; sorts."""
    years: set[int] = set()
    for token in re.split(r"[^\d-]+", (text or "").strip()):
        if not token:
            continue
        if "-" in token.strip("-"):
            lo, hi = token.split("-", 1)
            if lo.isdigit() and hi.isdigit() and int(lo) <= int(hi):
                years.update(range(int(lo), int(hi) + 1))
        elif token.isdigit():
            years.add(int(token))
    return sorted(years)


def _parse_bool(value: str) -> bool:
    # "x" (any case) included for spreadsheet-style checkbox columns -- a
    # blank cell means false, and marking a checked one with an X is at
    # least as common a convention as typing the word "yes".
    return (value or "").strip().lower() in ("1", "true", "yes", "y", "on", "x")


def _clowns_manage_page(
    request: Request, error: str | None = None, status_code: int = 200
) -> Response:
    return templates.TemplateResponse(
        request, "admin/clowns_manage.html",
        {"profiles": _all_profiles(), "error": error}, status_code=status_code,
    )


@router.get("/clowns/manage")
def clowns_manage(request: Request) -> Response:
    return _clowns_manage_page(request)


@router.post("/clowns/manage")
def add_historical_rider(
    request: Request, display_name: str = Form(...), years_ridden: str = Form(""),
) -> Response:
    name = display_name.strip()
    if not name:
        return _clowns_manage_page(request, error="Name is required.", status_code=400)
    CLOWN_PROFILES().put_item(Item={
        "clown_id": f"clown_{uuid.uuid4().hex}",
        "cognito_sub": None, "email": None,
        "display_name": name, "photo_url": None, "bio": None,
        "phone": None, "address": None,
        "emergency_contact_name": None, "emergency_contact_phone": None,
        "years_ridden": _parse_years(years_ridden),
        "is_lieutenant": False, "active": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return RedirectResponse("/admin/clowns/manage", status_code=303)


@router.post("/clowns/manage/{clown_id}")
def update_clown_official(
    request: Request, clown_id: str,
    years_ridden: str = Form(""), is_lieutenant: str = Form(""),
    active: str = Form(""),
) -> Response:
    try:
        _update_clown_fields(clown_id, {
            "years_ridden": _parse_years(years_ridden),
            "is_lieutenant": _parse_bool(is_lieutenant),
            "active": _parse_bool(active),
        }, require_exists=True)
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return RedirectResponse("/admin/clowns/manage", status_code=303)
        raise
    return RedirectResponse("/admin/clowns/manage", status_code=303)


@router.post("/clowns/manage/{clown_id}/link")
def link_clown_account(request: Request, clown_id: str, email: str = Form(...)) -> Response:
    wanted = email.strip().lower()
    # NOTE: reads only the first Cognito page (~60 users); email-matching
    # silently fails past that. Switch to a paginated list_users if the rider
    # pool ever exceeds ~60 (see app/db.py `paginate`).
    users = _cognito().list_users(UserPoolId=settings.cognito_user_pool_id).get("Users", [])
    sub = None
    for u in users:
        attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
        if (attrs.get("email") or "").strip().lower() == wanted:
            sub = u["Username"]
            break
    if sub is None:
        return _clowns_manage_page(request, error="No login found with that email.", status_code=400)
    for other in paginate(CLOWN_PROFILES().scan):
        if other.get("cognito_sub") == sub and other["clown_id"] != clown_id:
            name = other.get("display_name") or other.get("email") or other["clown_id"]
            return _clowns_manage_page(
                request,
                error=f"That login is already linked to another profile ({name}).",
                status_code=400,
            )
    try:
        CLOWN_PROFILES().update_item(
            Key={"clown_id": clown_id},
            UpdateExpression="SET cognito_sub = :s",
            ExpressionAttributeValues={":s": sub},
            ConditionExpression=Attr("clown_id").exists(),
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return _clowns_manage_page(
                request, error="That profile no longer exists.", status_code=400
            )
        raise
    return RedirectResponse("/admin/clowns/manage", status_code=303)


@router.post("/clowns/manage/{clown_id}/unlink")
def unlink_clown_account(clown_id: str) -> RedirectResponse:
    try:
        CLOWN_PROFILES().update_item(
            Key={"clown_id": clown_id}, UpdateExpression="REMOVE cognito_sub",
            ConditionExpression=Attr("clown_id").exists(),
        )
    except ClientError as exc:
        if not _is_conditional_failure(exc):
            raise
    return RedirectResponse("/admin/clowns/manage", status_code=303)


@router.post("/clowns/manage/{clown_id}/delete")
def delete_clown_profile(clown_id: str) -> RedirectResponse:
    profile = CLOWN_PROFILES().get_item(Key={"clown_id": clown_id}).get("Item") or {}
    _delete_event_image(profile.get("photo_url"))
    CLOWN_PROFILES().delete_item(Key={"clown_id": clown_id})
    return RedirectResponse("/admin/clowns/manage", status_code=303)


# ---- CSV import + export (admin) ----

CLOWN_CSV_COLUMNS = [
    "email", "display_name", "swag_name", "phone", "address", "bio",
    "emergency_contact_name", "emergency_contact_phone",
    "years_ridden", "is_lieutenant", "active",
]
_CLOWN_CSV_TEXT_COLUMNS = (
    "display_name", "swag_name", "phone", "address", "bio",
    "emergency_contact_name", "emergency_contact_phone",
)


def _clowns_import_page(
    request: Request, summary: dict | None = None,
    error: str | None = None, status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request, "admin/clowns_import.html",
        {"summary": summary, "error": error, "columns": CLOWN_CSV_COLUMNS},
        status_code=status_code,
    )


@router.get("/clowns/import")
def clowns_import_form(request: Request) -> Response:
    return _clowns_import_page(request)


@router.get("/clowns/import/template")
def clowns_import_template() -> Response:
    buf = io.StringIO()
    csv.writer(buf).writerow(CLOWN_CSV_COLUMNS)
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="clowns-template.csv"'},
    )


@router.get("/clowns/export.csv")
def clowns_export() -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CLOWN_CSV_COLUMNS)
    for p in _all_profiles():
        writer.writerow([
            _csv_safe(p.get("email")),
            _csv_safe(p.get("display_name")),
            _csv_safe(p.get("swag_name")),
            _csv_safe(p.get("phone")),
            _csv_safe(p.get("address")),
            _csv_safe(p.get("bio")),
            _csv_safe(p.get("emergency_contact_name")),
            _csv_safe(p.get("emergency_contact_phone")),
            " ".join(str(int(y)) for y in (p.get("years_ridden") or [])),
            "yes" if p.get("is_lieutenant") else "no",
            "yes" if p.get("active", True) else "no",
        ])
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="clowns.csv"'},
    )


@router.post("/clowns/import")
def clowns_import(
    request: Request,
    file: UploadFile = File(...),
    invite_missing: str = Form(""),
) -> Response:
    raw = file.file.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    headers = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    if "email" not in headers:
        return _clowns_import_page(request, error="The CSV needs an 'email' column.", status_code=400)

    profiles = paginate(CLOWN_PROFILES().scan)
    by_sub = {p["cognito_sub"]: p for p in profiles if p.get("cognito_sub")}
    # A profile's stored cognito_sub can be stale -- pointing at a retired
    # user pool (see _cognito_sub_is_live) -- in which case it's just as
    # reclaimable by email as one with no login at all. Only profiles that
    # already carry a sub incur the extra AdminGetUser call, and only once
    # each, here.
    by_email = {}
    for p in profiles:
        key = (p.get("email") or "").strip().lower()
        if not key:
            continue
        stored_sub = p.get("cognito_sub")
        if stored_sub and _cognito_sub_is_live(stored_sub):
            continue  # a different, currently-real account -- not reclaimable
        by_email[key] = p
    cognito_by_email: dict[str, str] = {}
    # NOTE: reads only the first Cognito page (~60 users); an email past that
    # page looks unknown here, so the importer would treat a returning rider as
    # new. Switch to a paginated list_users if the pool ever exceeds ~60
    # (see app/db.py `paginate`).
    for u in _cognito().list_users(UserPoolId=settings.cognito_user_pool_id).get("Users", []):
        attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
        if attrs.get("email"):
            cognito_by_email[attrs["email"].strip().lower()] = u["Username"]

    do_invite = bool(invite_missing)
    created = updated = invited = 0
    skipped: list[str] = []

    for row_num, row in enumerate(reader, start=2):
        # csv.DictReader parks surplus values under key None as a list -> guard
        # against a row with more columns than the header.
        norm = {(k or "").strip().lower(): (v if isinstance(v, str) else "").strip()
                for k, v in row.items()}
        email = norm.get("email", "")
        if not email:
            skipped.append(f"row {row_num}: missing email")
            continue
        key = email.lower()
        sub = cognito_by_email.get(key)
        profile = (by_sub.get(sub) if sub else None) or by_email.get(key)

        fields: dict = {}
        for col in _CLOWN_CSV_TEXT_COLUMNS:
            if norm.get(col):
                value = norm[col]
                if value.startswith("'"):
                    value = value[1:]  # undo the _csv_safe formula-injection guard
                fields[col] = value
        if profile and sub and profile.get("cognito_sub") != sub:
            fields["cognito_sub"] = sub  # adopt/re-link the login onto this row
        if norm.get("years_ridden"):
            fields["years_ridden"] = _parse_years(norm["years_ridden"])
        if norm.get("is_lieutenant"):
            fields["is_lieutenant"] = _parse_bool(norm["is_lieutenant"])
        if norm.get("active"):
            fields["active"] = _parse_bool(norm["active"])

        if profile:
            _update_clown_fields(profile["clown_id"], fields)
            updated += 1
            continue

        if sub is None and do_invite:
            try:
                resp = _cognito().admin_create_user(
                    UserPoolId=settings.cognito_user_pool_id, Username=email,
                    UserAttributes=[
                        {"Name": "email", "Value": email},
                        {"Name": "email_verified", "Value": "true"},
                    ],
                )
                sub = resp["User"]["Username"]
                invited += 1
            except ClientError:
                skipped.append(f"row {row_num}: could not invite {email}")
                continue

        CLOWN_PROFILES().put_item(Item={
            "clown_id": f"clown_{uuid.uuid4().hex}",
            "cognito_sub": sub, "email": email,
            "display_name": fields.get("display_name"),
            "swag_name": fields.get("swag_name"),
            "photo_url": None,
            "bio": fields.get("bio"),
            "phone": fields.get("phone"),
            "address": fields.get("address"),
            "emergency_contact_name": fields.get("emergency_contact_name"),
            "emergency_contact_phone": fields.get("emergency_contact_phone"),
            "years_ridden": fields.get("years_ridden", []),
            "is_lieutenant": fields.get("is_lieutenant", False),
            "active": fields.get("active", True),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        created += 1

    return _clowns_import_page(request, summary={
        "created": created, "updated": updated, "invited": invited, "skipped": skipped,
    })
