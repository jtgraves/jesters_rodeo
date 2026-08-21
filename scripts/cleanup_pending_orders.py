from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from app.db import EVENTS, ORDERS, paginate

logger = logging.getLogger(__name__)

# Longer than app.routes.public.RESERVATION_TTL_MINUTES (35) so a Stripe
# checkout session is certainly dead before we reclaim its seats.
CLEANUP_GRACE_MINUTES = 45


def expire_stale_reservations(max_age_minutes: int = CLEANUP_GRACE_MINUTES) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    expired = 0

    # paginate(), not a bare scan: one scan returns at most 1MB, and a
    # truncated cleanup silently leaks capacity with no error anywhere.
    for order in paginate(ORDERS().scan, FilterExpression=Attr("status").eq("pending")):
        if datetime.fromisoformat(order["created_at"]) >= cutoff:
            continue

        # Conditional so a concurrent webhook that just marked this order paid
        # wins, and so a second run of this job cannot release the same seats
        # twice.
        try:
            ORDERS().update_item(
                Key={"order_id": order["order_id"]},
                UpdateExpression="SET #s = :expired",
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":expired": "expired", ":pending": "pending"},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue
            raise

        EVENTS().update_item(
            Key={"event_id": order["event_id"]},
            UpdateExpression="SET tickets_sold_count = tickets_sold_count - :q",
            ExpressionAttributeValues={":q": int(order["quantity"])},
        )
        expired += 1

    logger.info("Expired %s stale reservation(s)", expired)
    return expired


def handler(event: dict, context: object) -> dict:
    return {"expired": expire_stale_reservations()}
