from __future__ import annotations

import logging
import time

from boto3.dynamodb.conditions import Attr

from app.db import ANNOUNCEMENTS, ORDERS, WAITLIST, paginate
from app.emails import send_announcement_email

logger = logging.getLogger(__name__)

# A real SES account has a per-second sending-rate ceiling (sandbox: ~1/s;
# a fresh production account: often ~14/s). Firing sends in a tight loop risks
# self-inflicted Throttling errors that land in the loop's except-and-continue
# and get reported as "failed to send" for perfectly good addresses -- exactly
# backwards on the one occasion (a cancellation notice) reliability matters
# most. A small fixed pace keeps this well under any default limit; at this
# app's scale (100-500 recipients) the total added delay is trivial.
SEND_PACE_SECONDS = 0.2


def _resolve_recipients(event_id: str, audience: list[str]) -> list[str]:
    # Dedup by lowercased email -- a person can be both a buyer and on the
    # waitlist, or hold multiple paid orders. First-seen original casing wins.
    emails: dict[str, str] = {}

    if "attendees" in audience:
        orders = paginate(
            ORDERS().query,
            IndexName="event_id-index",
            KeyConditionExpression="event_id = :e",
            ExpressionAttributeValues={":e": event_id},
        )
        for order in orders:
            if order.get("status") != "paid":
                continue
            emails.setdefault(order["buyer_email"].strip().lower(), order["buyer_email"])

    if "waitlist" in audience:
        entries = paginate(WAITLIST().scan, FilterExpression=Attr("event_id").eq(event_id))
        for entry in entries:
            emails.setdefault(entry["email"].strip().lower(), entry["email"])

    return list(emails.values())


def send_announcement(announcement_id: str) -> None:
    item = ANNOUNCEMENTS().get_item(Key={"announcement_id": announcement_id}).get("Item")
    if item is None:
        logger.warning("Announcement %s not found; nothing to send", announcement_id)
        return

    ANNOUNCEMENTS().update_item(
        Key={"announcement_id": announcement_id},
        UpdateExpression="SET #s = :sending",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":sending": "sending"},
    )

    recipients = _resolve_recipients(item["event_id"], item["audience"])
    ANNOUNCEMENTS().update_item(
        Key={"announcement_id": announcement_id},
        UpdateExpression="SET recipient_count = :n",
        ExpressionAttributeValues={":n": len(recipients)},
    )

    sent = failed = 0
    for i, to_email in enumerate(recipients):
        if i:
            time.sleep(SEND_PACE_SECONDS)
        try:
            send_announcement_email(to_email, item["subject"], item["body"])
            sent += 1
        except Exception:
            failed += 1
            logger.exception("Failed to send announcement %s to %s", announcement_id, to_email)
        # One write per recipient: tiny scale, and a stuck/slow send stays
        # visible as live progress in the admin UI rather than an opaque wait.
        ANNOUNCEMENTS().update_item(
            Key={"announcement_id": announcement_id},
            UpdateExpression="SET sent_count = :n",
            ExpressionAttributeValues={":n": sent},
        )

    error = f"{failed} of {len(recipients)} failed to send" if failed else None
    ANNOUNCEMENTS().update_item(
        Key={"announcement_id": announcement_id},
        UpdateExpression="SET #s = :status, #e = :error",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={":status": "sent", ":error": error},
    )


def handler(event: dict, context: object) -> dict:
    announcement_id = event["announcement_id"]
    try:
        send_announcement(announcement_id)
    except Exception:
        logger.exception("send_announcement crashed for %s", announcement_id)
        ANNOUNCEMENTS().update_item(
            Key={"announcement_id": announcement_id},
            UpdateExpression="SET #s = :failed, #e = :error",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={
                ":failed": "failed", ":error": "Unexpected error; see CloudWatch logs",
            },
        )
        raise
    return {"announcement_id": announcement_id}
