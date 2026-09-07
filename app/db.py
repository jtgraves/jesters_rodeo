from __future__ import annotations

from typing import Any, Callable

import boto3

from app.config import settings

_resource: Any = None


def _dynamodb() -> Any:
    global _resource
    if _resource is None:
        _resource = boto3.resource("dynamodb", region_name=settings.aws_region)
    return _resource


def reset_clients() -> None:
    """Drop the cached boto3 resource.

    Tests call this between `mock_aws` contexts. moto patches botocore while
    a mock context is active, so a resource built inside one context and
    reused inside the next is a well-known source of confusing cross-test
    failures.
    """
    global _resource
    _resource = None


def get_table(name: str) -> Any:
    return _dynamodb().Table(name)


def paginate(method: Callable[..., dict], **kwargs: Any) -> list[dict]:
    """Run a Table.scan/Table.query to exhaustion and return every item.

    A single DynamoDB scan or query returns at most 1MB and reports the
    cutoff only via `LastEvaluatedKey`. Reading `resp["Items"]` once
    silently truncates — which shows up as a cleanup job that skips orders
    or a CSV export missing rows, with no error anywhere.
    """
    items: list[dict] = []
    start_key: dict | None = None
    while True:
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        resp = method(**kwargs)
        items.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            return items


def EVENTS() -> Any:
    return get_table(settings.events_table)


def ORDERS() -> Any:
    return get_table(settings.orders_table)


def TICKETS() -> Any:
    return get_table(settings.tickets_table)


def DISCOUNT_CODES() -> Any:
    return get_table(settings.discount_codes_table)


def WAITLIST() -> Any:
    return get_table(settings.waitlist_table)


def ANNOUNCEMENTS() -> Any:
    return get_table(settings.announcements_table)


def PAST_BENEFICIARIES() -> Any:
    return get_table(settings.past_beneficiaries_table)
