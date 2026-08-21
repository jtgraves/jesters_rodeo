from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

import stripe
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.auth import require_admin
from app.config import settings
from app.db import DISCOUNT_CODES, EVENTS, ORDERS, TICKETS, WAITLIST, paginate
from app.emails import send_confirmation_email
from app.models import DiscountCode, Order, Ticket, normalize_code

stripe.api_key = settings.stripe_secret_key
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
templates = Jinja2Templates(directory="app/templates")

CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


def _csv_safe(value: Any) -> Any:
    """Neutralize spreadsheet formula injection.

    Buyer names and attendee names are free text typed by the public, and the
    export is opened in Excel/Numbers by an admin. A leading =, +, - or @ makes
    the cell an executable formula.
    """
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(CSV_FORMULA_PREFIXES) else text


def _events_page(request: Request, error: str | None = None, status_code: int = 200) -> Response:
    events = sorted(paginate(EVENTS().scan), key=lambda e: int(e["year"]), reverse=True)
    return templates.TemplateResponse(
        request, "admin/events.html", {"events": events, "error": error},
        status_code=status_code,
    )


@router.get("/events")
def list_events(request: Request) -> Response:
    return _events_page(request)


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
) -> Response:
    try:
        EVENTS().put_item(
            Item={
                "event_id": f"evt_{year}", "year": year, "name": name, "date": date,
                "location": location, "description": description,
                "ticket_price_cents": ticket_price_cents, "capacity": capacity,
                "tickets_sold_count": 0, "registration_open": False, "status": "draft",
                "registration_opens_at": None, "registration_closes_at": None,
            },
            # An unconditional put on an existing year resets tickets_sold_count
            # to 0 and status to draft while the paid orders for that event
            # remain — a double-submit would silently corrupt live capacity
            # accounting. The year IS the identity, so refuse to re-create it.
            ConditionExpression="attribute_not_exists(event_id)",
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return _events_page(
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


@router.get("/orders")
def list_orders(request: Request, event_id: str, q: str = "", status: str = "") -> Response:
    orders = _orders_for_event(event_id, q, status)
    return templates.TemplateResponse(
        request, "admin/orders.html",
        {"orders": orders, "event_id": event_id, "q": q, "status": status},
    )


@router.get("/orders/export")
def export_orders(event_id: str) -> Response:
    orders = _orders_for_event(event_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "order_id", "buyer_name", "buyer_email", "quantity",
        "total_cents", "status", "created_at", "attendee_names",
    ])
    for o in orders:
        attendees = "; ".join(a.get("name") or "" for a in o["attendees"])
        writer.writerow([
            o["order_id"], _csv_safe(o["buyer_name"]), _csv_safe(o["buyer_email"]),
            int(o["quantity"]), int(o["total_cents"]), o["status"],
            o["created_at"], _csv_safe(attendees),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="orders-{event_id}.csv"'},
    )


@router.post("/orders/{order_id}/resend-email")
def resend_email(order_id: str) -> RedirectResponse:
    order_item = _get_order_or_404(order_id)
    tickets = [Ticket(**t) for t in _tickets_for_order(order_id)]
    send_confirmation_email(Order(**order_item), tickets)
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
        raise HTTPException(status_code=502, detail="Stripe refund failed. Nothing was changed.")

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
def list_discount_codes(request: Request, event_id: str) -> Response:
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


@router.get("/waitlist")
def list_waitlist(request: Request, event_id: str) -> Response:
    entries = paginate(WAITLIST().scan, FilterExpression=Attr("event_id").eq(event_id))
    return templates.TemplateResponse(
        request, "admin/waitlist.html", {"entries": entries, "event_id": event_id}
    )


@router.post("/waitlist/{waitlist_id}/notify")
def notify_waitlist_entry(waitlist_id: str, event_id: str = Form(...)) -> RedirectResponse:
    WAITLIST().update_item(
        Key={"waitlist_id": waitlist_id},
        UpdateExpression="SET notified = :t",
        ExpressionAttributeValues={":t": True},
    )
    return RedirectResponse(f"/admin/waitlist?event_id={event_id}", status_code=303)


@router.get("/checkin")
def checkin_page(request: Request, event_id: str) -> Response:
    return templates.TemplateResponse(request, "admin/checkin.html", {"event_id": event_id})


@router.post("/checkin/{ticket_id}")
def checkin_ticket(ticket_id: str, event_id: str) -> JSONResponse:
    ticket = TICKETS().get_item(Key={"ticket_id": ticket_id}).get("Item")
    if not ticket:
        return JSONResponse({"status": "invalid"})

    # Tickets from previous years are still live rows in this table.
    if ticket["event_id"] != event_id:
        return JSONResponse({"status": "wrong_event", "attendee_name": ticket["attendee_name"]})

    if ticket.get("voided"):
        return JSONResponse({"status": "voided", "attendee_name": ticket["attendee_name"]})

    if ticket["checked_in"]:
        return JSONResponse({
            "status": "already_checked_in",
            "attendee_name": ticket["attendee_name"],
            "checked_in_at": ticket["checked_in_at"],
        })

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
            return JSONResponse({
                "status": "already_checked_in",
                "attendee_name": ticket["attendee_name"],
            })
        raise

    return JSONResponse({"status": "checked_in", "attendee_name": ticket["attendee_name"]})
