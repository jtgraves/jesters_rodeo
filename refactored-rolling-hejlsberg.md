# Jester's Rodeo Event Registration Site — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a cost-minimal, Stripe-powered event registration site (Eventbrite-style) for an annual krewe ball, deployable entirely within AWS's free tier.

**Architecture:** A single Python FastAPI application, deployed as one Lambda function behind API Gateway, serves both server-rendered public registration pages (Jinja2 templates) and an admin dashboard. Data lives in DynamoDB. Stripe Checkout (hosted page) handles payment. SES sends confirmation emails with QR-code tickets. Cognito Hosted UI authenticates admins, who then carry an HttpOnly session cookie that protects `/admin/*`. All infrastructure is defined in AWS CDK (Python) for a single-command `cdk deploy`. A custom domain (Route 53 + ACM) is supported but optional — without it the site runs on the raw API Gateway endpoint.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, Mangum (ASGI-to-Lambda adapter), boto3, Stripe Python SDK, `qrcode` library, AWS CDK (Python), DynamoDB, Lambda, API Gateway (HTTP API), Cognito User Pool + Hosted UI, SES, SSM Parameter Store, EventBridge, optional Route 53 + ACM, pytest, moto.

**Repository:** `jesters_rodeo` (git@github.com:jtgraves/jesters_rodeo.git), already initialized with a Python `.gitignore` at `/Users/jtg688/Library/Mobile Documents/com~apple~CloudDocs/babylon/jesters_rodeo`. All paths below are relative to this repo root.

## Context

This replaces the manual, paper-based registration process (order forms, hotel reservation forms, liability releases, invitation lists) currently used for the krewe's annual ball with a self-serve web registration flow. Liability waivers and hotel booking remain outside the site's scope (handled separately, per owner's decision) — this system covers ticket sales, payment, QR-code check-in, and attendee management only.

**Key product decisions from design discussion:**
- **Access model:** Hybrid — the event page is public, but a discount/access code can unlock member pricing (not a separate ticket tier — there is only one ticket price per event, adjusted by an optional discount code).
- **Orders vs. tickets:** One order (checkout) can include any quantity of tickets (no upper cap other than remaining event capacity). Only the buyer's name is required; individual attendee names on a multi-ticket order are optional.
- **Multi-year support:** The system supports creating a new `Event` each year; past years' data stays queryable/archived, not deleted or overwritten.
- **Stripe webhook is the source of truth** for payment success — never the browser redirect — so a closed tab or failed redirect never loses a sale or double-books capacity.
- **Capacity is *reserved* at checkout, not sold.** `tickets_sold_count` counts reserved-or-sold seats. Reservations expire in 30 minutes (matching the Stripe Checkout session TTL) and are released by a cleanup Lambda running every 15 minutes and by the `checkout.session.expired` webhook, whichever fires first.
- **Pending orders are never hard-deleted** — they transition to `expired`. A hard delete can race a late webhook and silently strand a customer who actually paid.
- **No CI/CD in v1** — deploys are a manual `cdk deploy` run locally, appropriate for a solo-admin, once-a-year deploy cadence (YAGNI).
- **No autoscaling/pooled concerns** — traffic is 100–500 tickets/year in short bursts; Lambda + DynamoDB on-demand handle this natively at near-zero idle cost.

## Global Constraints

- Every AWS service choice must stay within (or close to) AWS's free tier at 100–500 tickets/year: DynamoDB on-demand (always-free 25GB), Lambda (always-free 1M requests/mo), API Gateway HTTP API, Cognito (always-free 50k MAUs, Hosted UI included), SES (must request production access — sandbox by default), SSM Parameter Store SecureString (always free; chosen over Secrets Manager specifically because Secrets Manager bills ~$0.40/secret/month). The only fixed cost is an optional Route 53 hosted zone (~$0.50/mo) if a custom domain is used.
- Never trust client-submitted totals, quantities, or discount calculations — always recompute and re-validate server-side before charging or before trusting a webhook payload beyond its verified signature.
- Stripe webhook handling must be idempotent under *concurrent* retries. A read-then-write `if status == "paid"` guard is not sufficient — the state transition itself must be a DynamoDB conditional write.
- All money amounts are stored and computed in integer cents, never floats. Values read back from DynamoDB arrive as `Decimal` and must be cast to `int` before arithmetic or before being sent to Stripe.
- Every DynamoDB `scan`/`query` must paginate via `LastEvaluatedKey`. A single call returns at most 1MB and silently truncates — use the `app.db.paginate` helper, never a bare `resp["Items"]`.
- Python 3.12 throughout; type-hint all function signatures in application code, including return types.
- Secrets (Stripe keys) must never appear in source control, in CDK source, or in synthesized CloudFormation. They are created out-of-band as SSM SecureString parameters and read at Lambda cold start.

---

## File Structure

```
jesters_rodeo/
├── app/
│   ├── __init__.py
│   ├── main.py                     # FastAPI app instance, route registration, Mangum handler
│   ├── config.py                   # settings: table names, SES sender, Stripe keys via SSM
│   ├── db.py                       # boto3 DynamoDB resource/table accessors + paginate helper
│   ├── models.py                   # pydantic models: Event, Order, Ticket, DiscountCode, WaitlistEntry
│   ├── pricing.py                  # pure functions: compute order total, apply discount code
│   ├── tickets.py                  # ticket ID generation, QR code image generation
│   ├── emails.py                   # SES send helpers (confirmation, resend)
│   ├── auth.py                     # Cognito JWKS verification + session-cookie admin dependency
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── public.py               # GET /, POST /checkout, confirmation, waitlist signup
│   │   ├── webhooks.py             # POST /webhooks/stripe
│   │   ├── auth_routes.py          # GET /admin/login, /admin/callback, /admin/logout (unauthenticated)
│   │   └── admin.py                # /admin/* — events, orders, codes, waitlist, check-in, refunds, export
│   ├── static/
│   │   └── html5-qrcode.min.js     # vendored QR scanner (no CDN dependency at the door)
│   └── templates/
│       ├── base.html
│       ├── event.html              # public event/registration page
│       ├── no_event.html
│       ├── confirmation.html
│       ├── waitlist_signup.html
│       ├── waitlist_confirmed.html
│       └── admin/
│           ├── login.html
│           ├── events.html
│           ├── orders.html
│           ├── checkin.html
│           ├── discount_codes.html
│           └── waitlist.html
├── tests/
│   ├── __init__.py                 # makes `tests` a package so module names don't collide
│   ├── conftest.py                 # env vars, moto DynamoDB fixture, RSA/JWKS fixtures
│   ├── test_db.py
│   ├── test_pricing.py
│   ├── test_tickets.py
│   ├── test_emails.py
│   ├── test_auth.py                # real JWT signature/expiry/audience verification
│   ├── test_public_routes.py
│   ├── test_webhooks.py
│   ├── test_admin_routes.py
│   └── test_cleanup.py
├── infra/
│   ├── app.py                      # CDK app entrypoint
│   ├── jesters_rodeo_stack.py      # CDK Stack: DynamoDB, Lambda, HTTP API, Cognito, SES,
│   │                               #   EventBridge rule, optional Route 53/ACM custom domain
│   └── requirements.txt
├── scripts/
│   ├── __init__.py
│   └── cleanup_pending_orders.py   # Lambda handler for EventBridge scheduled cleanup
├── docs/
│   └── DEPLOYMENT.md
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
├── .gitignore                      # already present
└── README.md
```

**Design rationale:** `routes/` splits by trust boundary (public/unauthenticated, Stripe-signed webhook, Cognito-authenticated admin) rather than by resource, since the auth requirement is the thing most likely to cause a security bug if muddled. `auth_routes.py` is separate from `admin.py` precisely because the login and OAuth-callback routes must *not* carry the admin auth dependency — putting them in the same router as the protected routes is how you accidentally lock yourself out or accidentally expose everything. `pricing.py` and `tickets.py` are pure-function modules deliberately kept free of AWS calls so they're trivially unit-testable without mocks.

---

## Task 1: Project Scaffolding & CDK Skeleton

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `pytest.ini`
- Create: `app/__init__.py` (empty), `app/routes/__init__.py` (empty), `tests/__init__.py` (empty), `scripts/__init__.py` (empty)
- Create: `app/config.py`
- Create: `infra/app.py`, `infra/jesters_rodeo_stack.py`, `infra/requirements.txt`
- Create: `README.md` (overwrite placeholder)

**Interfaces:**
- Produces: `app.config.Settings` — a pydantic-settings `BaseSettings` class with fields `stripe_secret_key: str`, `stripe_webhook_secret: str`, `stripe_publishable_key: str`, `events_table: str`, `orders_table: str`, `tickets_table: str`, `discount_codes_table: str`, `waitlist_table: str`, `ses_sender_email: str`, `cognito_user_pool_id: str`, `cognito_app_client_id: str`, `cognito_domain: str`, `session_cookie_name: str`, `aws_region: str`, `base_url: str`. Loaded from environment variables (Lambda env vars in production, `.env` locally).
- Produces: `app.config.settings` — a module-level singleton instance. **Task 13 modifies this module by adding an SSM loader; it must not remove or rename any field defined here.**
- Produces: `infra.jesters_rodeo_stack.JestersRodeoStack` — a CDK `Stack` subclass, empty scaffold for now (no resources yet — those come in later tasks), taking `(scope, construct_id, **kwargs)`.

- [ ] **Step 1: Create Python dependency files**

`requirements.txt`:
```
fastapi==0.115.0
mangum==0.19.0
jinja2==3.1.4
boto3==1.35.0
stripe==11.1.0
qrcode[pil]==7.4.2
pydantic==2.9.2
pydantic-settings==2.5.2
python-multipart==0.0.12
python-jose[cryptography]==3.3.0
itsdangerous==2.2.0
```

`requirements-dev.txt`:
```
-r requirements.txt
pytest==8.3.3
moto[dynamodb]==5.0.16
httpx==0.27.2
```

`infra/requirements.txt`:
```
aws-cdk-lib==2.160.0
constructs>=10.0.0,<11.0.0
```

*`itsdangerous` signs the admin session cookie (Task 8). `pytest-asyncio` is deliberately absent — every test in this plan is synchronous, because FastAPI's `TestClient` drives the async app from a sync interface.*

- [ ] **Step 2: Create `pytest.ini`**

```ini
[pytest]
testpaths = tests
pythonpath = .
```

*`pythonpath = .` is load-bearing and easy to omit. Under pytest's default `prepend` import mode, the directory added to `sys.path` is the test file's own directory (`tests/`), **not** the repo root — being the rootdir does not put a directory on the import path. Without this line, `from app.models import Event` raises `ModuleNotFoundError` in every test file, and because the first TDD step of each task legitimately expects `ModuleNotFoundError`, the failure masquerades as correct red-phase output and you only notice at the green step. Requires pytest ≥ 7.*

- [ ] **Step 3: Create the empty package markers**

```bash
mkdir -p app/routes tests scripts
touch app/__init__.py app/routes/__init__.py tests/__init__.py scripts/__init__.py
```

*`tests/__init__.py` prevents basename collisions between test modules once the suite grows, and `scripts/__init__.py` is what makes `from scripts.cleanup_pending_orders import ...` work in Task 11.*

- [ ] **Step 4: Write `app/config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_publishable_key: str = ""
    events_table: str
    orders_table: str
    tickets_table: str
    discount_codes_table: str
    waitlist_table: str
    ses_sender_email: str
    cognito_user_pool_id: str
    cognito_app_client_id: str
    cognito_domain: str = ""
    session_cookie_name: str = "jr_session"
    session_secret: str = "dev-only-insecure-secret"
    aws_region: str = "us-east-1"
    base_url: str = "http://localhost:8000"


settings = Settings()
```

*Three notes on this file, each of which prevents a specific failure later:*

*1. `model_config = SettingsConfigDict(...)` is the pydantic v2 idiom. The pydantic v1 `class Config:` inner class still works under pydantic-settings 2.x but emits deprecation warnings.*

*2. `aws_region` is read from the `AWS_REGION` environment variable, which **AWS Lambda sets automatically**. Do not add `AWS_REGION` to the CDK `environment` dict in Task 12 — it is a reserved Lambda environment variable and CloudFormation rejects any function that tries to set it.*

*3. `base_url` is the public URL Stripe redirects back to after checkout. It defaults to a local dev value and is overridden by the `BASE_URL` Lambda environment variable set in Task 12 once the API Gateway endpoint is known. **Task 13 must preserve this field** — dropping it breaks every checkout with an `AttributeError` at request time.*

- [ ] **Step 5: Write CDK app entrypoint `infra/app.py`**

```python
#!/usr/bin/env python3
import aws_cdk as cdk

from jesters_rodeo_stack import JestersRodeoStack

app = cdk.App()
JestersRodeoStack(app, "JestersRodeoStack")
app.synth()
```

- [ ] **Step 6: Write empty CDK stack `infra/jesters_rodeo_stack.py`**

```python
from aws_cdk import Stack
from constructs import Construct


class JestersRodeoStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # Resources added in later tasks: DynamoDB tables, Lambda, API Gateway,
        # Cognito User Pool, SES identity, EventBridge cleanup rule.
```

- [ ] **Step 7: Verify CDK synthesizes cleanly**

Run:
```bash
cd infra
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk synth
```
Expected: synthesizes an (empty) CloudFormation template with no errors. (Requires `cdk` CLI installed globally: `npm install -g aws-cdk` if not already present.)

*All later `cdk` commands in this plan are run from `infra/` with this virtualenv active. Note for Task 12: because the CDK app's working directory is `infra/`, every relative asset path in the stack resolves against `infra/`, not the repo root.*

- [ ] **Step 8: Write README.md**

```markdown
# Jester's Rodeo

Annual krewe ball registration site. Serverless Python (FastAPI on Lambda),
DynamoDB, Stripe Checkout, SES email, Cognito admin auth. Deployed via AWS CDK.

## Local development

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements-dev.txt
    pytest

## Deploy

Requires Docker running (the Lambda bundle is built in a container) and three
CDK context values. See `docs/DEPLOYMENT.md` for the full procedure.

    cd infra
    pip install -r requirements.txt
    cdk deploy \
      -c site_url=https://register.example.com \
      -c cognito_domain_prefix=jesters-rodeo-admin \
      -c ses_sender_email=noreply@example.com
```

- [ ] **Step 9: Commit**

```bash
git add requirements.txt requirements-dev.txt pytest.ini \
        app/__init__.py app/routes/__init__.py app/config.py \
        tests/__init__.py scripts/__init__.py \
        infra/app.py infra/jesters_rodeo_stack.py infra/requirements.txt README.md
git commit -m "Scaffold project: dependencies, config, empty CDK stack"
```

---

## Task 2: Data Models & DynamoDB Access Layer

**Files:**
- Create: `app/models.py`
- Create: `app/db.py`
- Test: `tests/conftest.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: `app.config.settings` (Task 1) for table names.
- Produces:
  - `app.models.Event` (pydantic model): `event_id: str`, `year: int`, `name: str`, `date: str`, `location: str`, `description: str`, `ticket_price_cents: int`, `capacity: int`, `tickets_sold_count: int = 0`, `registration_open: bool = False`, `registration_opens_at: str | None`, `registration_closes_at: str | None`, `status: Literal["draft","open","closed","archived"] = "draft"`
  - `app.models.Order`: `order_id: str`, `event_id: str`, `buyer_name: str`, `buyer_email: str`, `attendees: list[dict]` (each `{"name": str | None}`), `quantity: int`, `unit_price_cents: int`, `discount_code: str | None`, `total_cents: int`, `stripe_checkout_session_id: str | None`, `stripe_payment_intent_id: str | None`, `status: Literal["pending","paid","refunded","canceled","expired"] = "pending"`, `created_at: str`
  - `app.models.Ticket`: `ticket_id: str`, `order_id: str`, `event_id: str`, `attendee_name: str | None`, `checked_in: bool = False`, `checked_in_at: str | None`, `voided: bool = False`, `voided_at: str | None`
  - `app.models.DiscountCode`: `code: str`, `event_id: str`, `discount_type: Literal["percent","fixed"]`, `discount_value: int`, `max_uses: int | None`, `uses_count: int = 0`, `active: bool = True`
  - `app.models.WaitlistEntry`: `waitlist_id: str`, `event_id: str`, `name: str`, `email: str`, `requested_quantity: int`, `created_at: str`, `notified: bool = False`
  - `app.models.normalize_code(raw: str) -> str` — uppercases and strips a discount code so lookup is case-insensitive. Every read *and* write of a discount code goes through this.
  - `app.db.get_table(name: str) -> Any` — returns a boto3 DynamoDB `Table` resource for the given table name (looked up via `settings`).
  - `app.db.EVENTS`, `app.db.ORDERS`, `app.db.TICKETS`, `app.db.DISCOUNT_CODES`, `app.db.WAITLIST` — lazy accessor functions returning the respective `Table`, e.g. `app.db.EVENTS() -> Any`.
  - `app.db.paginate(method: Callable[..., dict], **kwargs) -> list[dict]` — calls a `Table.scan`/`Table.query` bound method repeatedly, following `LastEvaluatedKey`, and returns every item. **All scans and queries in this codebase go through this.**
  - `app.db.reset_clients() -> None` — clears the cached boto3 resource; used by the test fixture so each `mock_aws` context gets a fresh client.

*Two model notes. `Order.status` includes `expired` because stale reservations are transitioned, never deleted — see Global Constraints. `Ticket.voided` exists so a refund can invalidate tickets at the door without deleting rows the CSV export still needs to account for.*

- [ ] **Step 1: Write the failing test for models**

`tests/test_db.py`:
```python
from app.models import Event, Order, Ticket, DiscountCode, WaitlistEntry


def test_event_defaults():
    event = Event(
        event_id="evt_2026",
        year=2026,
        name="Jester's Rodeo Ball",
        date="2026-03-14",
        location="New Orleans, LA",
        description="Annual krewe ball",
        ticket_price_cents=15000,
        capacity=300,
    )
    assert event.tickets_sold_count == 0
    assert event.registration_open is False
    assert event.status == "draft"


def test_order_attendees_list():
    order = Order(
        order_id="ord_1",
        event_id="evt_2026",
        buyer_name="Jane Doe",
        buyer_email="jane@example.com",
        attendees=[{"name": "Jane Doe"}, {"name": None}],
        quantity=2,
        unit_price_cents=15000,
        discount_code=None,
        total_cents=30000,
        stripe_checkout_session_id=None,
        stripe_payment_intent_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    assert order.status == "pending"
    assert len(order.attendees) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models'`

- [ ] **Step 3: Write `app/models.py`**

```python
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


class Order(BaseModel):
    order_id: str
    event_id: str
    buyer_name: str
    buyer_email: str
    attendees: list[dict]
    quantity: int
    unit_price_cents: int
    discount_code: str | None = None
    total_cents: int
    stripe_checkout_session_id: str | None = None
    stripe_payment_intent_id: str | None = None
    status: Literal["pending", "paid", "refunded", "canceled", "expired"] = "pending"
    created_at: str


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Write `app/db.py`**

```python
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
```

- [ ] **Step 6: Write `tests/conftest.py` with a moto DynamoDB fixture**

Environment variables are set at module import, before any `app.*` module is imported, because `app/config.py` instantiates `Settings()` at import time and will raise on missing required fields. pytest imports `conftest.py` before collecting test modules, so this ordering holds.

```python
import os

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_dummy")
os.environ.setdefault("STRIPE_PUBLISHABLE_KEY", "pk_test_dummy")
os.environ.setdefault("EVENTS_TABLE", "Events")
os.environ.setdefault("ORDERS_TABLE", "Orders")
os.environ.setdefault("TICKETS_TABLE", "Tickets")
os.environ.setdefault("DISCOUNT_CODES_TABLE", "DiscountCodes")
os.environ.setdefault("WAITLIST_TABLE", "Waitlist")
os.environ.setdefault("SES_SENDER_EMAIL", "noreply@example.com")
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-1_dummy")
os.environ.setdefault("COGNITO_APP_CLIENT_ID", "dummy_client_id")
os.environ.setdefault("COGNITO_DOMAIN", "jr-admin-test.auth.us-east-1.amazoncognito.com")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("BASE_URL", "http://testserver")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def dynamodb_tables():
    from app import db

    db.reset_clients()
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="Events",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="Orders",
            KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "order_id", "AttributeType": "S"},
                {"AttributeName": "event_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "event_id-index",
                "KeySchema": [{"AttributeName": "event_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="Tickets",
            KeySchema=[{"AttributeName": "ticket_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "ticket_id", "AttributeType": "S"},
                {"AttributeName": "order_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "order_id-index",
                "KeySchema": [{"AttributeName": "order_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="DiscountCodes",
            KeySchema=[{"AttributeName": "code", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "code", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="Waitlist",
            KeySchema=[{"AttributeName": "waitlist_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "waitlist_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield
    db.reset_clients()
```

*The two `db.reset_clients()` calls matter. `app/db.py` caches its boto3 resource in a module global, and moto only intercepts calls while a `mock_aws` context is active. Without the reset, the resource built during the first test leaks into every later test's mock context, which produces failures that look like data bleeding between tests.*

- [ ] **Step 7: Add db integration tests using the fixture**

Append to `tests/test_db.py`:
```python
from app.db import EVENTS, WAITLIST, paginate
from app.models import normalize_code


def test_events_table_roundtrip(dynamodb_tables):
    table = EVENTS()
    table.put_item(Item={"event_id": "evt_2026", "year": 2026, "name": "Test"})
    resp = table.get_item(Key={"event_id": "evt_2026"})
    assert resp["Item"]["name"] == "Test"


def test_normalize_code_is_case_and_space_insensitive():
    assert normalize_code(" member20 ") == "MEMBER20"
    assert normalize_code("Member20") == "MEMBER20"


def test_paginate_follows_last_evaluated_key(dynamodb_tables):
    table = WAITLIST()
    for i in range(7):
        table.put_item(Item={
            "waitlist_id": f"wl_{i}", "event_id": "evt_2026",
            "name": f"Person {i}", "email": f"p{i}@example.com",
            "requested_quantity": 1, "created_at": "2026-01-01T00:00:00Z",
            "notified": False,
        })

    # Limit=2 forces DynamoDB to return LastEvaluatedKey on every page but
    # the last, which is exactly the truncation a bare resp["Items"] hides.
    items = paginate(table.scan, Limit=2)
    assert len(items) == 7
    assert {i["waitlist_id"] for i in items} == {f"wl_{n}" for n in range(7)}

    truncated = table.scan(Limit=2)["Items"]
    assert len(truncated) == 2, "sanity check: a single scan really does truncate"
```

- [ ] **Step 8: Run full test file, verify pass**

Run: `pytest tests/test_db.py -v`
Expected: PASS (5 tests)

- [ ] **Step 9: Commit**

```bash
git add app/models.py app/db.py tests/conftest.py tests/test_db.py
git commit -m "Add data models, pagination helper, and DynamoDB access layer"
```

---

## Task 3: Pricing & Discount Code Logic

**Files:**
- Create: `app/pricing.py`
- Test: `tests/test_pricing.py`

**Interfaces:**
- Consumes: `app.models.DiscountCode` (Task 2)
- Produces:
  - `app.pricing.MAX_TICKETS_PER_ORDER: int` — hard server-side cap (`20`).
  - `app.pricing.compute_total(unit_price_cents: int, quantity: int, discount_code: DiscountCode | None) -> int` — returns total price in cents, discount applied once to the whole order (not per-ticket), floored at 0.
  - `app.pricing.validate_discount_code(discount_code: DiscountCode | None, event_id: str) -> tuple[bool, str]` — returns `(is_valid, error_message)`. Checks `active`, `event_id` match, and `max_uses`/`uses_count`.
  - `app.pricing.validate_quantity(quantity: int, remaining: int) -> tuple[bool, str]` — returns `(is_valid, error_message)`. Rejects zero, negative, above `MAX_TICKETS_PER_ORDER`, and above remaining capacity.

*`validate_quantity` exists because `quantity` arrives from an HTML form and the `min="1"` attribute on the input is client-side decoration that any HTTP client ignores. A negative quantity would otherwise **decrement** `tickets_sold_count` through the capacity reservation in Task 6, manufacturing capacity out of nothing.*

- [ ] **Step 1: Write failing tests**

`tests/test_pricing.py`:
```python
from app.models import DiscountCode
from app.pricing import (
    MAX_TICKETS_PER_ORDER,
    compute_total,
    validate_discount_code,
    validate_quantity,
)


def test_compute_total_no_discount():
    assert compute_total(15000, 2, None) == 30000


def test_compute_total_percent_discount():
    code = DiscountCode(code="MEMBER20", event_id="evt_2026", discount_type="percent", discount_value=20)
    assert compute_total(15000, 2, code) == 24000


def test_compute_total_fixed_discount():
    code = DiscountCode(code="SAVE10", event_id="evt_2026", discount_type="fixed", discount_value=1000)
    assert compute_total(15000, 1, code) == 14000


def test_compute_total_fixed_discount_floors_at_zero():
    code = DiscountCode(code="HUGE", event_id="evt_2026", discount_type="fixed", discount_value=99999)
    assert compute_total(15000, 1, code) == 0


def test_validate_discount_code_none():
    valid, err = validate_discount_code(None, "evt_2026")
    assert valid is True
    assert err == ""


def test_validate_discount_code_wrong_event():
    code = DiscountCode(code="X", event_id="evt_2025", discount_type="fixed", discount_value=100)
    valid, err = validate_discount_code(code, "evt_2026")
    assert valid is False
    assert "not valid" in err.lower()


def test_validate_discount_code_exhausted():
    code = DiscountCode(code="X", event_id="evt_2026", discount_type="fixed", discount_value=100, max_uses=5, uses_count=5)
    valid, err = validate_discount_code(code, "evt_2026")
    assert valid is False
    assert "exhausted" in err.lower() or "used" in err.lower()


def test_validate_discount_code_inactive():
    code = DiscountCode(code="X", event_id="evt_2026", discount_type="fixed", discount_value=100, active=False)
    valid, err = validate_discount_code(code, "evt_2026")
    assert valid is False


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
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/test_pricing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.pricing'`

- [ ] **Step 3: Implement `app/pricing.py`**

```python
from app.models import DiscountCode

MAX_TICKETS_PER_ORDER = 20


def compute_total(unit_price_cents: int, quantity: int, discount_code: DiscountCode | None) -> int:
    subtotal = unit_price_cents * quantity
    if discount_code is None:
        return subtotal
    if discount_code.discount_type == "percent":
        discounted = subtotal - (subtotal * discount_code.discount_value // 100)
    else:
        discounted = subtotal - discount_code.discount_value
    return max(discounted, 0)


def validate_discount_code(discount_code: DiscountCode | None, event_id: str) -> tuple[bool, str]:
    if discount_code is None:
        return True, ""
    if not discount_code.active:
        return False, "This code is no longer active."
    if discount_code.event_id != event_id:
        return False, "This code is not valid for this event."
    if discount_code.max_uses is not None and discount_code.uses_count >= discount_code.max_uses:
        return False, "This code has been exhausted."
    return True, ""


def validate_quantity(quantity: int, remaining: int) -> tuple[bool, str]:
    if quantity < 1:
        return False, "Please order at least 1 ticket."
    if quantity > MAX_TICKETS_PER_ORDER:
        return False, f"You can order at most {MAX_TICKETS_PER_ORDER} tickets in one order."
    if quantity > remaining:
        return False, f"Only {remaining} ticket(s) remain."
    return True, ""
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/test_pricing.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add app/pricing.py tests/test_pricing.py
git commit -m "Add pricing and discount code validation logic"
```

---

## Task 4: Ticket ID & QR Code Generation

**Files:**
- Create: `app/tickets.py`
- Test: `tests/test_tickets.py`

**Interfaces:**
- Produces:
  - `app.tickets.generate_ticket_id() -> str` — returns a unique ticket ID, format `tkt_<uuid4 hex>`.
  - `app.tickets.generate_qr_code_png(ticket_id: str) -> bytes` — returns PNG image bytes encoding the ticket ID as the QR payload.

- [ ] **Step 1: Write failing tests**

`tests/test_tickets.py`:
```python
from app.tickets import generate_ticket_id, generate_qr_code_png


def test_generate_ticket_id_format():
    tid = generate_ticket_id()
    assert tid.startswith("tkt_")
    assert len(tid) == len("tkt_") + 32


def test_generate_ticket_id_unique():
    assert generate_ticket_id() != generate_ticket_id()


def test_generate_qr_code_png_returns_bytes():
    png_bytes = generate_qr_code_png("tkt_abc123")
    assert isinstance(png_bytes, bytes)
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/test_tickets.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.tickets'`

- [ ] **Step 3: Implement `app/tickets.py`**

```python
import io
import uuid
import qrcode


def generate_ticket_id() -> str:
    return f"tkt_{uuid.uuid4().hex}"


def generate_qr_code_png(ticket_id: str) -> bytes:
    img = qrcode.make(ticket_id)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
```

- [ ] **Step 4: Run tests, verify pass**

Run: `pytest tests/test_tickets.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/tickets.py tests/test_tickets.py
git commit -m "Add ticket ID and QR code generation"
```

---

## Task 5: Email Sending (SES)

**Files:**
- Create: `app/emails.py`
- Test: `tests/test_emails.py`

**Interfaces:**
- Consumes: `app.config.settings.ses_sender_email` (Task 1), `app.tickets.generate_qr_code_png` (Task 4), `app.models.Order`, `app.models.Ticket` (Task 2)
- Produces:
  - `app.emails.send_confirmation_email(order: Order, tickets: list[Ticket]) -> None` — sends an SES raw MIME email with QR code PNGs inline, one per ticket.

- [ ] **Step 1: Write failing test using moto SES mock**

`tests/test_emails.py`:
```python
import email

import boto3
import pytest
from moto import mock_aws
from moto.core import DEFAULT_ACCOUNT_ID
from moto.ses.models import ses_backends

from app import emails as emails_module
from app.emails import send_confirmation_email
from app.models import Order, Ticket


@pytest.fixture
def ses_backend():
    emails_module.reset_clients()
    with mock_aws():
        client = boto3.client("ses", region_name="us-east-1")
        client.verify_email_identity(EmailAddress="noreply@example.com")
        yield ses_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    emails_module.reset_clients()


def _order(quantity: int = 2) -> Order:
    return Order(
        order_id="ord_1",
        event_id="evt_2026",
        buyer_name="Jane Doe",
        buyer_email="jane@example.com",
        attendees=[{"name": "Jane Doe"}, {"name": None}][:quantity],
        quantity=quantity,
        unit_price_cents=15000,
        total_cents=15000 * quantity,
        created_at="2026-01-01T00:00:00Z",
        status="paid",
    )


def test_send_confirmation_email_delivers_to_buyer(ses_backend):
    order = _order(quantity=1)
    tickets = [Ticket(ticket_id="tkt_1", order_id="ord_1", event_id="evt_2026", attendee_name="Jane Doe")]

    send_confirmation_email(order, tickets)

    assert len(ses_backend.sent_messages) == 1
    sent = ses_backend.sent_messages[0]
    assert sent.destinations == ["jane@example.com"]


def test_send_confirmation_email_has_valid_related_alternative_structure(ses_backend):
    order = _order(quantity=2)
    tickets = [
        Ticket(ticket_id="tkt_1", order_id="ord_1", event_id="evt_2026", attendee_name="Jane Doe"),
        Ticket(ticket_id="tkt_2", order_id="ord_1", event_id="evt_2026", attendee_name=None),
    ]

    send_confirmation_email(order, tickets)

    raw = ses_backend.sent_messages[0].raw_data
    msg = email.message_from_string(raw)

    # multipart/related wrapping a multipart/alternative is the structure that
    # renders correctly in Gmail, Apple Mail and Outlook. Attaching the plain
    # and HTML parts directly to the `related` container makes some clients
    # show the plain-text body *and* the HTML body stacked together.
    assert msg.get_content_type() == "multipart/related"
    subtypes = [p.get_content_type() for p in msg.get_payload()]
    assert subtypes[0] == "multipart/alternative"
    alternative = msg.get_payload(0)
    assert [p.get_content_type() for p in alternative.get_payload()] == ["text/plain", "text/html"]

    images = [p for p in msg.get_payload() if p.get_content_type() == "image/png"]
    assert len(images) == 2, "one inline QR code per ticket"
    assert {p["Content-ID"] for p in images} == {"<qr0>", "<qr1>"}

    html = alternative.get_payload(1).get_payload(decode=True).decode()
    assert 'src="cid:qr0"' in html and 'src="cid:qr1"' in html
    assert "tkt_1" in html and "tkt_2" in html
```

*This replaces a test that only asserted `"noreply@example.com" in list_identities()` — which re-asserts the fixture's own setup and passes even if `send_confirmation_email` has an empty body.*

- [ ] **Step 2: Run test, verify failure**

Run: `pytest tests/test_emails.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.emails'`

- [ ] **Step 3: Implement `app/emails.py`**

```python
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Any

import boto3

from app.config import settings
from app.models import Order, Ticket
from app.tickets import generate_qr_code_png

_ses_client: Any = None


def _ses() -> Any:
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("ses", region_name=settings.aws_region)
    return _ses_client


def reset_clients() -> None:
    """Drop the cached SES client so tests get one per `mock_aws` context."""
    global _ses_client
    _ses_client = None


def send_confirmation_email(order: Order, tickets: list[Ticket]) -> None:
    # multipart/related
    #   +-- multipart/alternative
    #   |     +-- text/plain
    #   |     +-- text/html   (references the images below by cid:)
    #   +-- image/png (qr0), image/png (qr1), ...
    #
    # The alternative part MUST be nested inside the related part. Attaching
    # text/plain and text/html as direct siblings of the images makes clients
    # treat them as two separate body parts to display, not as alternatives.
    msg = MIMEMultipart("related")
    msg["Subject"] = "Your Jester's Rodeo tickets"
    msg["From"] = settings.ses_sender_email
    msg["To"] = order.buyer_email

    text_lines = [f"Thanks, {order.buyer_name}! Here are your {len(tickets)} ticket(s).", ""]
    html_parts = [
        f"<p>Thanks, {escape(order.buyer_name)}! "
        f"Here are your {len(tickets)} ticket(s). Show a QR code at the door.</p>"
    ]
    for i, ticket in enumerate(tickets):
        who = ticket.attendee_name or order.buyer_name
        text_lines.append(f"- Ticket for {who} (ID: {ticket.ticket_id})")
        html_parts.append(
            f'<div style="margin-bottom:24px">'
            f"<p><strong>{escape(who)}</strong><br>"
            f"<code>{escape(ticket.ticket_id)}</code></p>"
            f'<img src="cid:qr{i}" alt="QR code for {escape(ticket.ticket_id)}" '
            f'width="200" height="200">'
            f"</div>"
        )

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText("\n".join(text_lines), "plain", "utf-8"))
    alternative.attach(
        MIMEText("<html><body>" + "".join(html_parts) + "</body></html>", "html", "utf-8")
    )
    msg.attach(alternative)

    for i, ticket in enumerate(tickets):
        image = MIMEImage(generate_qr_code_png(ticket.ticket_id), _subtype="png")
        image.add_header("Content-ID", f"<qr{i}>")
        image.add_header("Content-Disposition", "inline", filename=f"{ticket.ticket_id}.png")
        msg.attach(image)

    _ses().send_raw_email(
        Source=settings.ses_sender_email,
        Destinations=[order.buyer_email],
        RawMessage={"Data": msg.as_string()},
    )
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_emails.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/emails.py tests/test_emails.py
git commit -m "Add SES confirmation email sending with inline QR codes"
```

---

## Task 6: Public Routes — Event Page & Checkout Initiation

**Files:**
- Create: `app/main.py`
- Create: `app/routes/public.py` (the package `__init__.py` already exists from Task 1)
- Create: `app/templates/base.html`, `app/templates/event.html`, `app/templates/no_event.html`, `app/templates/confirmation.html`
- Test: `tests/test_public_routes.py`

**Interfaces:**
- Consumes: `app.db.EVENTS`, `app.db.ORDERS`, `app.db.DISCOUNT_CODES`, `app.db.paginate` (Task 2), `app.models.normalize_code` (Task 2), `app.pricing.compute_total`, `app.pricing.validate_discount_code`, `app.pricing.validate_quantity` (Task 3), `app.config.settings` (Task 1)
- Produces:
  - `app.main.app` — the FastAPI application instance, importable by Mangum and by tests via `TestClient`.
  - `app.routes.public.RESERVATION_TTL_MINUTES: int` — `30`. The Stripe Checkout session's `expires_at` and the cleanup job's staleness cutoff are both derived from this one constant so they can never drift apart.
  - `GET /` — renders the current open event (looked up by paginated scan of `Events` for `status == "open"`) or a "no event open" page.
  - `POST /checkout` — form fields `event_id`, `quantity`, `buyer_name`, `buyer_email`, `attendee_names` (optional, newline-separated), `discount_code` (optional). Re-validates quantity and discount server-side, reserves capacity via a DynamoDB conditional update, recomputes the total, creates a `pending` `Order`, creates a Stripe Checkout Session **priced at the recomputed total**, and redirects to the Stripe-hosted URL. Releases the reservation if Stripe session creation fails.
  - `GET /order/{order_id}/confirmation` — renders order status (does not mutate anything).

**Three behaviours here are load-bearing and each replaces a specific defect:**

1. **The Stripe line item is priced from `compute_total`, not from `unit_price × quantity`.** Sending `unit_amount=unit_price, quantity=quantity` charges the customer the *undiscounted* subtotal while the database records the discounted total — the customer overpays and every downstream report disagrees with Stripe. The session is therefore built as a single line item of `unit_amount=total, quantity=1`, with the ticket count in the product name.
2. **An unrecognised discount code is an error, not a silent no-op.** `validate_discount_code(None, ...)` returns valid-by-design so that "no code supplied" is legal. That means a *failed lookup* must be caught separately, or a typo'd code quietly charges full price with no message.
3. **Capacity is reserved before payment and released on every failure path.** The reservation, the Stripe session TTL, and the cleanup job all use `RESERVATION_TTL_MINUTES`.

- [ ] **Step 1: Write failing route tests**

`tests/test_public_routes.py`:
```python
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db import DISCOUNT_CODES, EVENTS, ORDERS
from app.main import app

client = TestClient(app)

OPEN_EVENT = {
    "event_id": "evt_2026", "year": 2026, "name": "Jester's Rodeo Ball",
    "date": "2026-03-14", "location": "New Orleans", "description": "Fun",
    "ticket_price_cents": 15000, "capacity": 300, "tickets_sold_count": 0,
    "registration_open": True, "status": "open",
}


def _put_event(**overrides):
    EVENTS().put_item(Item={**OPEN_EVENT, **overrides})


def _checkout(**overrides):
    data = {
        "event_id": "evt_2026", "quantity": "2", "buyer_name": "Jane Doe",
        "buyer_email": "jane@example.com", "attendee_names": "", "discount_code": "",
    }
    data.update(overrides)
    return client.post("/checkout", data=data, follow_redirects=False)


def _stripe_session():
    session = MagicMock()
    session.url = "https://checkout.stripe.com/fake-session"
    session.id = "cs_test_123"
    return session


def test_get_event_page_shows_open_event(dynamodb_tables):
    _put_event()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Jester's Rodeo Ball" in resp.text


def test_get_event_page_no_open_event(dynamodb_tables):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "no event" in resp.text.lower() or "not currently open" in resp.text.lower()


@patch("app.routes.public.stripe.checkout.Session.create")
def test_checkout_creates_pending_order_and_redirects(mock_create, dynamodb_tables):
    _put_event()
    mock_create.return_value = _stripe_session()

    resp = _checkout(quantity="2")

    assert resp.status_code == 303
    assert resp.headers["location"] == "https://checkout.stripe.com/fake-session"

    _, kwargs = mock_create.call_args
    line_item = kwargs["line_items"][0]
    assert line_item["price_data"]["unit_amount"] == 30000
    assert line_item["quantity"] == 1
    assert "expires_at" in kwargs, "unclaimed reservations must expire on Stripe's side too"

    orders = ORDERS().scan()["Items"]
    assert len(orders) == 1
    assert orders[0]["status"] == "pending"
    assert int(orders[0]["total_cents"]) == 30000
    assert orders[0]["stripe_checkout_session_id"] == "cs_test_123"

    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 2, "capacity is reserved at checkout"


@patch("app.routes.public.stripe.checkout.Session.create")
def test_checkout_charges_the_discounted_total_not_the_subtotal(mock_create, dynamodb_tables):
    """The regression that matters most: Stripe must be told the discounted price."""
    _put_event()
    DISCOUNT_CODES().put_item(Item={
        "code": "MEMBER20", "event_id": "evt_2026", "discount_type": "percent",
        "discount_value": 20, "max_uses": None, "uses_count": 0, "active": True,
    })
    mock_create.return_value = _stripe_session()

    resp = _checkout(quantity="2", discount_code="member20")

    assert resp.status_code == 303
    _, kwargs = mock_create.call_args
    line_item = kwargs["line_items"][0]
    # 2 x $150.00 = $300.00, less 20% = $240.00. Charging 30000 here means the
    # buyer paid full price while the order recorded the discount.
    assert line_item["price_data"]["unit_amount"] == 24000
    assert line_item["quantity"] == 1

    order = ORDERS().scan()["Items"][0]
    assert int(order["total_cents"]) == 24000
    assert order["discount_code"] == "MEMBER20", "codes are stored normalized"


@patch("app.routes.public.stripe.checkout.Session.create")
def test_checkout_rejects_unknown_discount_code(mock_create, dynamodb_tables):
    _put_event()
    resp = _checkout(discount_code="NOPE-NOT-A-CODE")

    assert resp.status_code == 200
    assert "don't recognize" in resp.text.lower() or "not recognized" in resp.text.lower()
    mock_create.assert_not_called()
    assert ORDERS().scan()["Items"] == []
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 0, "a rejected order reserves nothing"


def test_checkout_rejects_when_sold_out(dynamodb_tables):
    _put_event(capacity=1, tickets_sold_count=1)
    resp = _checkout(quantity="1")
    assert resp.status_code == 200
    assert "sold out" in resp.text.lower()


@patch("app.routes.public.stripe.checkout.Session.create")
def test_checkout_rejects_non_positive_quantity(mock_create, dynamodb_tables):
    """min="1" on the form input is client-side decoration; curl ignores it."""
    _put_event(tickets_sold_count=10)
    for bad in ("0", "-5"):
        resp = _checkout(quantity=bad)
        assert resp.status_code == 200
        assert "at least 1" in resp.text
    mock_create.assert_not_called()
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 10, "a negative quantity must not mint capacity"


@patch("app.routes.public.stripe.checkout.Session.create")
def test_checkout_rejects_quantity_above_per_order_cap(mock_create, dynamodb_tables):
    _put_event()
    resp = _checkout(quantity="500")
    assert resp.status_code == 200
    assert "at most 20" in resp.text
    mock_create.assert_not_called()


@patch("app.routes.public.stripe.checkout.Session.create")
def test_checkout_releases_reservation_when_stripe_fails(mock_create, dynamodb_tables):
    _put_event()
    mock_create.side_effect = RuntimeError("stripe is down")

    resp = _checkout(quantity="3")

    assert resp.status_code == 200
    assert "could not start checkout" in resp.text.lower()
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 0, "reservation must be rolled back"
    orders = ORDERS().scan()["Items"]
    assert orders[0]["status"] == "canceled"


def test_checkout_unknown_event_renders_error_not_500(dynamodb_tables):
    resp = _checkout(event_id="evt_does_not_exist")
    assert resp.status_code == 200
    assert "no longer available" in resp.text.lower()
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/test_public_routes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 3: Write templates**

`app/templates/base.html`:
```html
<!DOCTYPE html>
<html>
<head><title>{% block title %}Jester's Rodeo{% endblock %}</title></head>
<body>
{% block content %}{% endblock %}
</body>
</html>
```

`app/templates/event.html`:
```html
{% extends "base.html" %}
{% block title %}{{ event.name }}{% endblock %}
{% block content %}
<h1>{{ event.name }}</h1>
<p>{{ event.date }} — {{ event.location }}</p>
<p>{{ event.description }}</p>
<p>${{ "%.2f"|format(event.ticket_price_cents / 100) }} per ticket</p>

{% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}

{% if event.tickets_sold_count >= event.capacity %}
  <p>This event is sold out. <a href="/waitlist?event_id={{ event.event_id }}">Join the waitlist</a>.</p>
{% elif not event.registration_open %}
  <p>Registration is not currently open.</p>
{% else %}
<form method="post" action="/checkout">
  <input type="hidden" name="event_id" value="{{ event.event_id }}">
  <label>Quantity <input type="number" name="quantity" value="1" min="1" max="20" required></label>
  <label>Your name <input type="text" name="buyer_name" required></label>
  <label>Your email <input type="email" name="buyer_email" required></label>
  <label>Attendee names (one per line, optional)<textarea name="attendee_names"></textarea></label>
  <label>Discount code (optional) <input type="text" name="discount_code"></label>
  <button type="submit">Register</button>
</form>
{% endif %}
{% endblock %}
```

`app/templates/confirmation.html`:
```html
{% extends "base.html" %}
{% block content %}
{% if not order %}
  <h1>We couldn't find that order.</h1>
  <p>If you were charged, contact us and we'll sort it out.</p>
{% else %}
<h1>Order {{ order.status }}</h1>
<p>Buyer: {{ order.buyer_name }}</p>
<p>Total: ${{ "%.2f"|format(order.total_cents / 100) }}</p>
{% if order.status == "paid" %}
  <p>Check your email for your tickets.</p>
{% elif order.status == "pending" %}
  <p>We're still confirming your payment — refresh in a moment.</p>
{% elif order.status == "expired" %}
  <p>This checkout expired before payment completed. Nothing was charged —
     please <a href="/">start again</a>.</p>
{% elif order.status == "canceled" %}
  <p>This order was canceled. Nothing was charged.</p>
{% elif order.status == "refunded" %}
  <p>This order has been refunded.</p>
{% endif %}
{% endif %}
{% endblock %}
```

*The `{% if not order %}` guard matters: `/order/{id}/confirmation` is a public URL that anyone can type, and `order` is `None` for an unknown ID. Without the guard, Jinja evaluates `order.status` on `None` and the page 500s.*

`app/templates/no_event.html`:
```html
{% extends "base.html" %}
{% block content %}
{% if error %}
  <h1>{{ error }}</h1>
{% else %}
  <h1>No event is currently open for registration.</h1>
{% endif %}
<p>Check back soon.</p>
{% endblock %}
```

- [ ] **Step 4: Implement `app/routes/public.py`**

```python
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import stripe
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db import DISCOUNT_CODES, EVENTS, ORDERS, paginate
from app.models import DiscountCode, normalize_code
from app.pricing import compute_total, validate_discount_code, validate_quantity

stripe.api_key = settings.stripe_secret_key
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Stripe requires checkout session `expires_at` to be at least 30 minutes out;
# 35 leaves room for clock skew. The cleanup job in Task 11 uses a *longer*
# window (45 minutes) so Stripe's session is certainly dead before we reclaim
# the seat, and the `checkout.session.expired` webhook normally beats it there.
RESERVATION_TTL_MINUTES = 35


def _find_open_event() -> dict | None:
    events = paginate(EVENTS().scan, FilterExpression=Attr("status").eq("open"))
    return events[0] if events else None


def _reserve_capacity(event_id: str, quantity: int, capacity: int) -> bool:
    """Atomically claim `quantity` seats. Returns False if they aren't available.

    The condition is written as a raw expression string with explicit
    placeholders rather than a `boto3.dynamodb.conditions.Attr` object,
    because mixing an `Attr` condition with a caller-supplied
    `ExpressionAttributeValues` dict in one call makes the interaction
    between generated and supplied placeholders non-obvious. One style, one
    dict, no ambiguity.
    """
    try:
        EVENTS().update_item(
            Key={"event_id": event_id},
            UpdateExpression="SET tickets_sold_count = tickets_sold_count + :q",
            ConditionExpression=(
                "tickets_sold_count <= :max_start AND registration_open = :open"
            ),
            ExpressionAttributeValues={
                ":q": quantity,
                ":max_start": capacity - quantity,
                ":open": True,
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _release_capacity(event_id: str, quantity: int) -> None:
    EVENTS().update_item(
        Key={"event_id": event_id},
        UpdateExpression="SET tickets_sold_count = tickets_sold_count - :q",
        ExpressionAttributeValues={":q": quantity},
    )


def _event_page(request: Request, event: dict, error: str | None):
    return templates.TemplateResponse(request, "event.html", {"event": event, "error": error})


@router.get("/")
def event_page(request: Request):
    event = _find_open_event()
    if not event:
        return templates.TemplateResponse(request, "no_event.html", {"error": None})
    return _event_page(request, event, None)


@router.post("/checkout")
def checkout(
    request: Request,
    event_id: str = Form(...),
    quantity: int = Form(...),
    buyer_name: str = Form(...),
    buyer_email: str = Form(...),
    attendee_names: str = Form(""),
    discount_code: str = Form(""),
):
    event = EVENTS().get_item(Key={"event_id": event_id}).get("Item")
    if not event:
        return templates.TemplateResponse(
            request, "no_event.html", {"error": "That event is no longer available."}
        )

    unit_price = int(event["ticket_price_cents"])
    capacity = int(event["capacity"])
    remaining = capacity - int(event["tickets_sold_count"])

    valid, err = validate_quantity(quantity, remaining)
    if not valid:
        return _event_page(request, event, err)

    # Resolve the discount code BEFORE reserving capacity, so a rejected code
    # never leaves a phantom reservation behind.
    code_obj: DiscountCode | None = None
    if discount_code.strip():
        code_key = normalize_code(discount_code)
        code_item = DISCOUNT_CODES().get_item(Key={"code": code_key}).get("Item")
        if code_item is None:
            # validate_discount_code(None, ...) is valid-by-design so that "no
            # code supplied" is legal. A failed *lookup* must be caught here or
            # a typo silently charges full price with no message shown.
            return _event_page(request, event, "We don't recognize that discount code.")
        code_obj = DiscountCode(**code_item)
        valid, err = validate_discount_code(code_obj, event_id)
        if not valid:
            return _event_page(request, event, err)

    if not _reserve_capacity(event_id, quantity, capacity):
        fresh = EVENTS().get_item(Key={"event_id": event_id}).get("Item", event)
        return _event_page(request, fresh, "Sorry — those tickets were just claimed.")

    total = compute_total(unit_price, quantity, code_obj)

    names = [n.strip() for n in attendee_names.splitlines() if n.strip()]
    attendees: list[dict[str, Any]] = [
        {"name": names[i] if i < len(names) else None} for i in range(quantity)
    ]

    order_id = f"ord_{uuid.uuid4().hex}"
    ORDERS().put_item(Item={
        "order_id": order_id,
        "event_id": event_id,
        "buyer_name": buyer_name,
        "buyer_email": buyer_email,
        "attendees": attendees,
        "quantity": quantity,
        "unit_price_cents": unit_price,
        "discount_code": code_obj.code if code_obj else None,
        "total_cents": total,
        "stripe_checkout_session_id": None,
        "stripe_payment_intent_id": None,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    ticket_word = "ticket" if quantity == 1 else "tickets"
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            customer_email=buyer_email,
            # ONE line item priced at the recomputed total. Sending
            # unit_amount=unit_price with quantity=N would charge the
            # undiscounted subtotal while the order records the discount.
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"{event['name']} — {quantity} {ticket_word}"},
                    "unit_amount": total,
                },
                "quantity": 1,
            }],
            metadata={"order_id": order_id},
            expires_at=int(time.time()) + RESERVATION_TTL_MINUTES * 60,
            success_url=f"{settings.base_url}/order/{order_id}/confirmation",
            cancel_url=f"{settings.base_url}/",
        )
    except Exception:
        # Roll back the reservation immediately rather than waiting for the
        # cleanup job — otherwise a Stripe outage burns real capacity.
        _release_capacity(event_id, quantity)
        ORDERS().update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET #s = :canceled",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":canceled": "canceled"},
        )
        fresh = EVENTS().get_item(Key={"event_id": event_id}).get("Item", event)
        return _event_page(
            request, fresh, "We could not start checkout just now. Please try again."
        )

    ORDERS().update_item(
        Key={"order_id": order_id},
        UpdateExpression="SET stripe_checkout_session_id = :s",
        ExpressionAttributeValues={":s": session.id},
    )
    return RedirectResponse(session.url, status_code=303)


@router.get("/order/{order_id}/confirmation")
def confirmation(request: Request, order_id: str):
    order = ORDERS().get_item(Key={"order_id": order_id}).get("Item")
    return templates.TemplateResponse(request, "confirmation.html", {"order": order})
```

*Note: `success_url`/`cancel_url` are built from `settings.base_url`, which defaults to `http://localhost:8000` locally and is overridden by the `BASE_URL` Lambda environment variable wired up in Task 12 once the API Gateway endpoint exists.*

*Note on the `registration_open = :open` clause in the reservation condition: it closes the window where an admin closes registration between the page load and the form POST. Without it, the capacity check and the open check are two separate reads and a late submitter can slip through.*

- [ ] **Step 5: Write `app/main.py`**

```python
from fastapi import FastAPI
from mangum import Mangum

from app.routes import public

app = FastAPI(title="Jester's Rodeo")
app.include_router(public.router)

handler = Mangum(app)
```

- [ ] **Step 6: Run tests, verify pass**

Run: `pytest tests/test_public_routes.py -v`
Expected: PASS (10 tests)

- [ ] **Step 7: Commit**

```bash
git add app/main.py app/routes/public.py app/templates/base.html app/templates/event.html \
        app/templates/no_event.html app/templates/confirmation.html tests/test_public_routes.py
git commit -m "Add public event page and checkout with server-side pricing and capacity reservation"
```

---

## Task 7: Stripe Webhook Handler

**Files:**
- Create: `app/routes/webhooks.py`
- Test: `tests/test_webhooks.py`
- Modify: `app/main.py`

**Interfaces:**
- Consumes: `app.db.ORDERS`, `app.db.TICKETS`, `app.db.DISCOUNT_CODES`, `app.db.EVENTS` (Task 2), `app.tickets.generate_ticket_id` (Task 4), `app.emails.send_confirmation_email` (Task 5), `app.config.settings` (Task 1)
- Produces: `POST /webhooks/stripe` — verifies the Stripe signature, then handles two event types:
  - `checkout.session.completed` — atomically transitions the order to `paid`, creates `Tickets`, increments discount code usage, sends the confirmation email.
  - `checkout.session.expired` — atomically transitions a `pending` order to `expired` and releases its reserved capacity.

  Every other event type is acknowledged with `{"received": True}` and ignored.

**The idempotency guard must be the conditional write itself.** Stripe retries webhooks, and retries can overlap. A read-then-write guard —

```python
if order_item["status"] == "paid":      # WRONG
    return {"received": True}
```

— has both deliveries read `pending` before either writes, so both proceed: duplicate tickets, two confirmation emails, and the discount counter incremented twice. Instead the status transition carries a `ConditionExpression`, and a `ConditionalCheckFailedException` *is* the "already handled" signal. There is no window between the check and the write because they are the same operation.

- [ ] **Step 1: Write failing tests**

`tests/test_webhooks.py`:
```python
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db import DISCOUNT_CODES, EVENTS, ORDERS, TICKETS
from app.main import app

client = TestClient(app)


def _stripe_event(order_id: str, event_type: str = "checkout.session.completed") -> dict:
    return {
        "type": event_type,
        "data": {"object": {
            "id": "cs_test_123",
            "payment_intent": "pi_test_456",
            "metadata": {"order_id": order_id},
        }},
    }


def _put_order(order_id: str, **overrides) -> None:
    item = {
        "order_id": order_id, "event_id": "evt_2026", "buyer_name": "Jane",
        "buyer_email": "jane@example.com", "attendees": [{"name": "Jane"}, {"name": None}],
        "quantity": 2, "unit_price_cents": 15000, "total_cents": 30000,
        "status": "pending", "created_at": "2026-01-01T00:00:00Z",
        "discount_code": None, "stripe_checkout_session_id": "cs_test_123",
        "stripe_payment_intent_id": None,
    }
    item.update(overrides)
    ORDERS().put_item(Item=item)


def _put_event(tickets_sold_count: int = 2) -> None:
    EVENTS().put_item(Item={
        "event_id": "evt_2026", "year": 2026, "name": "Test", "date": "2026-03-14",
        "location": "NOLA", "description": "d", "ticket_price_cents": 15000,
        "capacity": 300, "tickets_sold_count": tickets_sold_count,
        "registration_open": True, "status": "open",
    })


def _post_webhook():
    return client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "fake"})


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_webhook_marks_order_paid_and_creates_tickets(mock_construct, dynamodb_tables):
    _put_event()
    _put_order("ord_1")
    mock_construct.return_value = _stripe_event("ord_1")

    with patch("app.routes.webhooks.send_confirmation_email") as mock_email:
        resp = _post_webhook()

    assert resp.status_code == 200
    order = ORDERS().get_item(Key={"order_id": "ord_1"})["Item"]
    assert order["status"] == "paid"
    assert order["stripe_payment_intent_id"] == "pi_test_456"

    tickets = TICKETS().scan()["Items"]
    assert len(tickets) == 2
    assert {t["attendee_name"] for t in tickets} == {"Jane", None}
    assert all(t["voided"] is False for t in tickets)
    mock_email.assert_called_once()


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_webhook_ignores_replay_of_an_already_paid_order(mock_construct, dynamodb_tables):
    _put_event()
    _put_order("ord_2", status="paid", stripe_payment_intent_id="pi_existing")
    mock_construct.return_value = _stripe_event("ord_2")

    with patch("app.routes.webhooks.send_confirmation_email") as mock_email:
        resp = _post_webhook()

    assert resp.status_code == 200
    assert TICKETS().scan()["Items"] == []
    mock_email.assert_not_called()


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_webhook_delivered_twice_fulfils_exactly_once(mock_construct, dynamodb_tables):
    """Stripe retries. Two deliveries of the same event must produce one fulfilment.

    This is the test a read-then-write guard passes only by luck: it works
    when the deliveries are strictly sequential, and fails in production when
    they overlap. The conditional write makes it hold either way.
    """
    _put_event()
    _put_order("ord_dup")
    mock_construct.return_value = _stripe_event("ord_dup")

    with patch("app.routes.webhooks.send_confirmation_email") as mock_email:
        first = _post_webhook()
        second = _post_webhook()

    assert first.status_code == 200 and second.status_code == 200
    assert len(TICKETS().scan()["Items"]) == 2, "2 attendees, not 4"
    assert mock_email.call_count == 1


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_webhook_increments_discount_code_usage(mock_construct, dynamodb_tables):
    _put_event()
    DISCOUNT_CODES().put_item(Item={
        "code": "MEMBER20", "event_id": "evt_2026", "discount_type": "percent",
        "discount_value": 20, "max_uses": None, "uses_count": 3, "active": True,
    })
    _put_order("ord_3", quantity=1, attendees=[{"name": "Jane"}],
               total_cents=12000, discount_code="MEMBER20")
    mock_construct.return_value = _stripe_event("ord_3")

    with patch("app.routes.webhooks.send_confirmation_email"):
        _post_webhook()

    code = DISCOUNT_CODES().get_item(Key={"code": "MEMBER20"})["Item"]
    assert int(code["uses_count"]) == 4


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_expired_session_releases_reserved_capacity(mock_construct, dynamodb_tables):
    _put_event(tickets_sold_count=10)
    _put_order("ord_exp", quantity=2)
    mock_construct.return_value = _stripe_event("ord_exp", "checkout.session.expired")

    resp = _post_webhook()

    assert resp.status_code == 200
    assert ORDERS().get_item(Key={"order_id": "ord_exp"})["Item"]["status"] == "expired"
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 8


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_expired_session_does_not_release_capacity_twice(mock_construct, dynamodb_tables):
    _put_event(tickets_sold_count=10)
    _put_order("ord_exp2", quantity=2)
    mock_construct.return_value = _stripe_event("ord_exp2", "checkout.session.expired")

    _post_webhook()
    _post_webhook()

    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 8, "released once, not twice"


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_late_payment_on_an_expired_order_still_fulfils_and_reclaims_capacity(
    mock_construct, dynamodb_tables
):
    """A payment that lands after we gave up must never strand the customer.

    We would rather oversell by two seats — visibly, in the admin dashboard —
    than take someone's money and send them no ticket.
    """
    _put_event(tickets_sold_count=8)
    _put_order("ord_late", status="expired")
    mock_construct.return_value = _stripe_event("ord_late")

    with patch("app.routes.webhooks.send_confirmation_email") as mock_email:
        resp = _post_webhook()

    assert resp.status_code == 200
    assert ORDERS().get_item(Key={"order_id": "ord_late"})["Item"]["status"] == "paid"
    assert len(TICKETS().scan()["Items"]) == 2
    mock_email.assert_called_once()
    event = EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]
    assert int(event["tickets_sold_count"]) == 10, "seats re-claimed for a paid order"


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_webhook_rejects_bad_signature(mock_construct, dynamodb_tables):
    mock_construct.side_effect = ValueError("bad signature")
    resp = _post_webhook()
    assert resp.status_code == 400


@patch("app.routes.webhooks.stripe.Webhook.construct_event")
def test_webhook_ignores_unrelated_event_types(mock_construct, dynamodb_tables):
    _put_event()
    _put_order("ord_other")
    mock_construct.return_value = _stripe_event("ord_other", "payment_intent.created")

    resp = _post_webhook()

    assert resp.status_code == 200
    assert ORDERS().get_item(Key={"order_id": "ord_other"})["Item"]["status"] == "pending"
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/test_webhooks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.routes.webhooks'`

- [ ] **Step 3: Implement `app/routes/webhooks.py`**

```python
import logging

import stripe
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.db import DISCOUNT_CODES, EVENTS, ORDERS, TICKETS
from app.emails import send_confirmation_email
from app.models import Order, Ticket
from app.tickets import generate_ticket_id

logger = logging.getLogger(__name__)
router = APIRouter()


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


def _handle_completed(session: dict) -> None:
    order_id = session["metadata"]["order_id"]

    # The transition IS the idempotency guard. Only an order that is still
    # `pending` (or that we prematurely `expired`) can move to `paid`, and only
    # one caller can win that race. ALL_OLD tells us which case we were in.
    try:
        resp = ORDERS().update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET #s = :paid, stripe_payment_intent_id = :pi",
            ConditionExpression="attribute_exists(order_id) AND #s IN (:pending, :expired)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":paid": "paid",
                ":pi": session.get("payment_intent"),
                ":pending": "pending",
                ":expired": "expired",
            },
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            # Already fulfilled, refunded, or never existed. Either way this
            # delivery has nothing left to do — acknowledge so Stripe stops
            # retrying.
            logger.info("Ignoring duplicate/irrelevant completion for %s", order_id)
            return
        raise

    order_item = resp["Attributes"]
    previous_status = order_item["status"]
    order_item["status"] = "paid"
    order_item["stripe_payment_intent_id"] = session.get("payment_intent")

    if previous_status == "expired":
        # We released these seats early and the payment arrived anyway. Take
        # the seats back even if that pushes the event over capacity: an
        # oversell is visible to the admin and fixable, whereas a paying
        # customer with no ticket is neither.
        logger.warning(
            "Order %s paid after expiring; re-claiming %s seat(s)",
            order_id, order_item["quantity"],
        )
        EVENTS().update_item(
            Key={"event_id": order_item["event_id"]},
            UpdateExpression="SET tickets_sold_count = tickets_sold_count + :q",
            ExpressionAttributeValues={":q": int(order_item["quantity"])},
        )

    tickets: list[Ticket] = []
    for attendee in order_item["attendees"]:
        ticket_item = {
            "ticket_id": generate_ticket_id(),
            "order_id": order_id,
            "event_id": order_item["event_id"],
            "attendee_name": attendee.get("name"),
            "checked_in": False,
            "checked_in_at": None,
            "voided": False,
            "voided_at": None,
        }
        TICKETS().put_item(Item=ticket_item)
        tickets.append(Ticket(**ticket_item))

    if order_item.get("discount_code"):
        DISCOUNT_CODES().update_item(
            Key={"code": order_item["discount_code"]},
            UpdateExpression="SET uses_count = uses_count + :one",
            ExpressionAttributeValues={":one": 1},
        )

    send_confirmation_email(Order(**order_item), tickets)


def _handle_expired(session: dict) -> None:
    order_id = session["metadata"]["order_id"]

    try:
        resp = ORDERS().update_item(
            Key={"order_id": order_id},
            UpdateExpression="SET #s = :expired",
            ConditionExpression="attribute_exists(order_id) AND #s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":expired": "expired", ":pending": "pending"},
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            # Already paid, already expired, or already cleaned up. Releasing
            # capacity here would double-release.
            return
        raise

    order_item = resp["Attributes"]
    EVENTS().update_item(
        Key={"event_id": order_item["event_id"]},
        UpdateExpression="SET tickets_sold_count = tickets_sold_count - :q",
        ExpressionAttributeValues={":q": int(order_item["quantity"])},
    )


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    handlers = {
        "checkout.session.completed": _handle_completed,
        "checkout.session.expired": _handle_expired,
    }
    handler = handlers.get(event["type"])
    if handler:
        handler(event["data"]["object"])

    return {"received": True}
```

*`attribute_exists(order_id)` is part of each condition so that a webhook naming an order we've never heard of fails the condition rather than silently creating a half-formed item — `update_item` upserts by default.*

- [ ] **Step 4: Wire router into `app/main.py`**

```python
from fastapi import FastAPI
from mangum import Mangum

from app.routes import public, webhooks

app = FastAPI(title="Jester's Rodeo")
app.include_router(public.router)
app.include_router(webhooks.router)

handler = Mangum(app)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `pytest tests/test_webhooks.py -v`
Expected: PASS (9 tests)

- [ ] **Step 6: Run full test suite to check for regressions**

Run: `pytest -v`
Expected: All prior tests plus these still PASS.

- [ ] **Step 7: Commit**

```bash
git add app/routes/webhooks.py app/main.py tests/test_webhooks.py
git commit -m "Add Stripe webhook handler with conditional-write idempotency and expiry handling"
```

---

## Task 8: Admin Authentication — Cognito Hosted UI, Verified JWTs, Session Cookie

**Files:**
- Create: `app/auth.py`
- Create: `app/routes/auth_routes.py`
- Test: `tests/test_auth.py`
- Modify: `app/main.py`

**Interfaces:**
- Consumes: `app.config.settings.cognito_user_pool_id`, `.cognito_app_client_id`, `.cognito_domain`, `.session_secret`, `.session_cookie_name`, `.base_url`, `.aws_region` (Task 1)
- Produces:
  - `app.auth.verify_cognito_token(token: str) -> dict` — verifies a Cognito **ID token** against the pool's JWKS: RS256 signature, `exp`, `aud`, `iss`, and `token_use == "id"`. Raises `HTTPException(401)` on any failure; returns claims on success.
  - `app.auth.issue_session(id_token: str) -> str` / `app.auth.read_session(request: Request) -> str | None` — sign and recover the session payload with `itsdangerous`.
  - `app.auth.set_session_cookie(response: Response, id_token: str) -> None` / `app.auth.clear_session_cookie(response: Response) -> None`
  - `app.auth.require_admin(request: Request) -> dict` — FastAPI dependency for every `/admin/*` route in Task 9.
  - `app.auth.reset_jwks_cache() -> None` — test hook.
  - `GET /admin/login`, `GET /admin/callback`, `GET /admin/logout` — the OAuth flow, on an **unauthenticated** router.

**Why this task exists as its own task.** The obvious shortcut — read a bearer token from the `Authorization` header — cannot work here, because every admin screen is server-rendered HTML whose `<form method="post">` submissions and `fetch()` calls send no such header. An admin UI built that way returns 401 on every click and there is no route anywhere that issues a token in the first place. So the flow has to be: Cognito Hosted UI → authorization code → our callback → verified ID token → HttpOnly session cookie.

**Three decisions worth stating up front:**

1. **PKCE with a public client, not a client secret.** A confidential client would mean carrying a Cognito-issued client secret through CDK into a Lambda environment variable, which puts it in CloudFormation in plaintext. PKCE removes the secret entirely; the short-lived code verifier lives in its own signed cookie between `/admin/login` and `/admin/callback`.
2. **`SameSite=Lax` on the session cookie is the CSRF defence.** Every state-changing admin route is a POST, and Lax withholds the cookie from cross-site POSTs, so a form on an attacker's page cannot act as the logged-in admin. This is why the cookie attributes below are not optional decoration — drop `SameSite` and every admin form becomes CSRF-vulnerable.
3. **ID tokens, not access tokens.** Cognito access tokens carry `client_id` and no `aud` claim, so validating `audience=` against an access token always fails. `token_use` is checked explicitly so an access token can never be substituted for an ID token.

- [ ] **Step 1: Write failing tests for real token verification**

These tests sign real RS256 tokens with a locally generated keypair and serve a matching JWKS, so the signature path actually executes. `cryptography` is already available via `python-jose[cryptography]`.

`tests/test_auth.py`:
```python
import time
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request
from jose import jwt
from jose.backends import RSAKey
from jose.constants import ALGORITHMS

from app import auth
from app.config import settings

ISSUER = f"https://cognito-idp.us-east-1.amazonaws.com/{settings.cognito_user_pool_id}"


def _new_key(kid: str):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    jwk = RSAKey(pem, ALGORITHMS.RS256).public_key().to_dict()
    jwk = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in jwk.items()}
    jwk["kid"] = kid
    return pem, jwk


@pytest.fixture
def signing_key():
    auth.reset_jwks_cache()
    pem, jwk = _new_key("test-kid")
    with patch.object(auth, "_get_jwks", return_value={"keys": [jwk]}):
        yield pem
    auth.reset_jwks_cache()


def _token(pem: str, kid: str = "test-kid", **overrides) -> str:
    claims = {
        "sub": "admin-1",
        "aud": settings.cognito_app_client_id,
        "iss": ISSUER,
        "token_use": "id",
        "email": "admin@example.com",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    claims.update(overrides)
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


def test_valid_id_token_is_accepted(signing_key):
    claims = auth.verify_cognito_token(_token(signing_key))
    assert claims["sub"] == "admin-1"
    assert claims["email"] == "admin@example.com"


def test_token_signed_by_a_different_key_is_rejected(signing_key):
    """The whole point of JWKS verification: a well-formed forgery must fail."""
    attacker_pem, _ = _new_key("test-kid")  # same kid, wrong key
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token(_token(attacker_pem))
    assert exc.value.status_code == 401


def test_expired_token_is_rejected(signing_key):
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token(_token(signing_key, exp=int(time.time()) - 60))
    assert exc.value.status_code == 401


def test_token_for_another_app_client_is_rejected(signing_key):
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token(_token(signing_key, aud="some-other-client"))
    assert exc.value.status_code == 401


def test_token_from_another_user_pool_is_rejected(signing_key):
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token(
            _token(signing_key, iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_evil")
        )
    assert exc.value.status_code == 401


def test_access_token_is_rejected(signing_key):
    """Cognito access tokens have no `aud`; accepting them widens the gate."""
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token(_token(signing_key, token_use="access"))
    assert exc.value.status_code == 401


def test_unknown_kid_is_rejected(signing_key):
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token(_token(signing_key, kid="not-in-jwks"))
    assert exc.value.status_code == 401


def test_garbage_token_is_rejected(signing_key):
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token("this is not a jwt")
    assert exc.value.status_code == 401


def test_jwks_fetch_uses_a_timeout_and_caches():
    auth.reset_jwks_cache()
    payload = b'{"keys": []}'
    fake = MagicMock()
    fake.__enter__.return_value.read.return_value = payload
    with patch.object(auth.urllib.request, "urlopen", return_value=fake) as mock_open:
        auth._get_jwks()
        auth._get_jwks()
    # A Lambda with a 15s timeout cannot afford an untimed network call.
    assert mock_open.call_count == 1, "JWKS must be cached across calls"
    assert mock_open.call_args.kwargs["timeout"] == auth.JWKS_TIMEOUT_SECONDS
    auth.reset_jwks_cache()


def _request(cookies: dict | None = None, accept: str = "text/html") -> Request:
    headers = [(b"accept", accept.encode())]
    if cookies:
        raw = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", raw.encode()))
    return Request({"type": "http", "headers": headers, "method": "GET", "path": "/admin/events"})


def test_session_cookie_round_trips():
    signed = auth.issue_session("the-id-token")
    request = _request({settings.session_cookie_name: signed})
    assert auth.read_session(request) == "the-id-token"


def test_tampered_session_cookie_is_rejected():
    signed = auth.issue_session("the-id-token")
    request = _request({settings.session_cookie_name: signed[:-3] + "aaa"})
    assert auth.read_session(request) is None


def test_require_admin_redirects_browsers_to_login():
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(_request(accept="text/html"))
    assert exc.value.status_code == 303
    assert exc.value.headers["Location"] == "/admin/login"


def test_require_admin_401s_api_clients():
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(_request(accept="application/json"))
    assert exc.value.status_code == 401
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.auth'`

- [ ] **Step 3: Implement `app/auth.py`**

```python
import json
import logging
import time
import urllib.request
from typing import Any

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from jose import jwt

from app.config import settings

logger = logging.getLogger(__name__)

JWKS_TIMEOUT_SECONDS = 5
JWKS_CACHE_TTL_SECONDS = 3600
SESSION_MAX_AGE_SECONDS = 3600

_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0.0


def issuer() -> str:
    return (
        f"https://cognito-idp.{settings.aws_region}.amazonaws.com/"
        f"{settings.cognito_user_pool_id}"
    )


def reset_jwks_cache() -> None:
    global _jwks_cache, _jwks_fetched_at
    _jwks_cache = None
    _jwks_fetched_at = 0.0


def _get_jwks() -> dict:
    """Fetch and cache the user pool's public keys.

    The timeout is not optional: this runs inside a Lambda with a 15 second
    budget, and `urlopen` without one blocks until the socket gives up, which
    turns a Cognito blip into a hung function.
    """
    global _jwks_cache, _jwks_fetched_at
    now = time.time()
    if _jwks_cache is None or now - _jwks_fetched_at > JWKS_CACHE_TTL_SECONDS:
        with urllib.request.urlopen(
            f"{issuer()}/.well-known/jwks.json", timeout=JWKS_TIMEOUT_SECONDS
        ) as resp:
            _jwks_cache = json.loads(resp.read())
        _jwks_fetched_at = now
    return _jwks_cache


def verify_cognito_token(token: str) -> dict:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    key = next((k for k in _get_jwks().get("keys", []) if k.get("kid") == kid), None)
    if key is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.cognito_app_client_id,
            issuer=issuer(),
            options={"verify_exp": True, "verify_aud": True, "verify_iss": True},
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Access tokens carry `client_id` rather than `aud` and grant a different
    # scope of authority. Only an ID token proves "this human signed in".
    if claims.get("token_use") != "id":
        raise HTTPException(status_code=401, detail="Invalid token")

    return claims


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="jr-admin-session")


def issue_session(id_token: str) -> str:
    return _serializer().dumps({"id_token": id_token})


def read_session(request: Request) -> str | None:
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None
    try:
        data: Any = _serializer().loads(raw, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("id_token") if isinstance(data, dict) else None


def set_session_cookie(response: Response, id_token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        issue_session(id_token),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=settings.base_url.startswith("https"),
        # SameSite=Lax is what stops a form on another site from POSTing to
        # /admin/... as the logged-in admin. It is this app's CSRF defence.
        samesite="lax",
        path="/admin",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/admin")


def _not_authenticated(request: Request) -> HTTPException:
    if "text/html" in request.headers.get("accept", ""):
        return HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return HTTPException(status_code=401, detail="Not authenticated")


def require_admin(request: Request) -> dict:
    token = read_session(request)
    if token is None:
        raise _not_authenticated(request)
    try:
        return verify_cognito_token(token)
    except HTTPException:
        raise _not_authenticated(request)
```

- [ ] **Step 4: Implement `app/routes/auth_routes.py`**

```python
import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.auth import clear_session_cookie, set_session_cookie, verify_cognito_token
from app.config import settings

router = APIRouter()

PKCE_COOKIE = "jr_pkce"
PKCE_MAX_AGE_SECONDS = 600
TOKEN_TIMEOUT_SECONDS = 10


def _pkce_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="jr-pkce")


def _redirect_uri() -> str:
    return f"{settings.base_url}/admin/callback"


@router.get("/admin/login")
def login():
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    params = urllib.parse.urlencode({
        "client_id": settings.cognito_app_client_id,
        "response_type": "code",
        "scope": "openid email",
        "redirect_uri": _redirect_uri(),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    response = RedirectResponse(
        f"https://{settings.cognito_domain}/oauth2/authorize?{params}", status_code=303
    )
    response.set_cookie(
        PKCE_COOKIE,
        _pkce_serializer().dumps(verifier),
        max_age=PKCE_MAX_AGE_SECONDS,
        httponly=True,
        secure=settings.base_url.startswith("https"),
        samesite="lax",
        path="/admin",
    )
    return response


@router.get("/admin/callback")
def callback(request: Request, code: str = "", error: str = ""):
    if error or not code:
        raise HTTPException(status_code=400, detail="Sign-in failed. Please try again.")

    raw = request.cookies.get(PKCE_COOKIE)
    if not raw:
        raise HTTPException(status_code=400, detail="Sign-in expired. Please start again.")
    try:
        verifier = _pkce_serializer().loads(raw, max_age=PKCE_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=400, detail="Sign-in failed. Please try again.")

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": settings.cognito_app_client_id,
        "code": code,
        "redirect_uri": _redirect_uri(),
        "code_verifier": verifier,
    }).encode()
    token_request = urllib.request.Request(
        f"https://{settings.cognito_domain}/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(token_request, timeout=TOKEN_TIMEOUT_SECONDS) as resp:
        tokens = json.loads(resp.read())

    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="Sign-in failed. Please try again.")

    # Verify before trusting it, even though it came straight from Cognito.
    verify_cognito_token(id_token)

    response = RedirectResponse("/admin/events", status_code=303)
    set_session_cookie(response, id_token)
    response.delete_cookie(PKCE_COOKIE, path="/admin")
    return response


@router.get("/admin/logout")
def logout():
    params = urllib.parse.urlencode({
        "client_id": settings.cognito_app_client_id,
        "logout_uri": f"{settings.base_url}/",
    })
    response = RedirectResponse(
        f"https://{settings.cognito_domain}/logout?{params}", status_code=303
    )
    clear_session_cookie(response)
    return response
```

- [ ] **Step 5: Wire the auth router into `app/main.py`**

```python
from fastapi import FastAPI
from mangum import Mangum

from app.routes import auth_routes, public, webhooks

app = FastAPI(title="Jester's Rodeo")
app.include_router(public.router)
app.include_router(webhooks.router)
app.include_router(auth_routes.router)

handler = Mangum(app)
```

*`auth_routes.router` is included **without** the `require_admin` dependency. Adding it here would make `/admin/login` require an existing login, locking every admin out permanently. This is exactly why the login routes live in a separate module from Task 9's protected router.*

- [ ] **Step 6: Run tests, verify pass**

Run: `pytest tests/test_auth.py -v`
Expected: PASS (13 tests)

- [ ] **Step 7: Run full suite**

Run: `pytest -v`
Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
git add app/auth.py app/routes/auth_routes.py app/main.py tests/test_auth.py
git commit -m "Add Cognito Hosted UI login with PKCE and verified session cookies"
```

---

## Task 9: Admin Dashboard — Events, Orders, Export, Discount Codes, Waitlist, Check-in, Refunds

**Files:**
- Create: `app/routes/admin.py`
- Create: `app/templates/admin/login.html`, `events.html`, `orders.html`, `discount_codes.html`, `waitlist.html`, `checkin.html`
- Create: `app/static/html5-qrcode.min.js` (vendored)
- Test: `tests/test_admin_routes.py`
- Modify: `app/main.py`

**Interfaces:**
- Consumes: `app.auth.require_admin` (Task 8), `app.db.*` and `app.db.paginate` (Task 2), `app.emails.send_confirmation_email` (Task 5), `app.models.normalize_code` (Task 2), `app.config.settings` (Task 1)
- Produces:
  - `GET /admin/events`, `POST /admin/events`, `POST /admin/events/{event_id}/open`, `POST /admin/events/{event_id}/close`
  - `GET /admin/orders?event_id=...&q=...&status=...`
  - `GET /admin/orders/export?event_id=...` — `text/csv` of orders and attendees
  - `POST /admin/orders/{order_id}/resend-email`
  - `POST /admin/orders/{order_id}/refund` — refunds via Stripe, voids the order's tickets, releases capacity
  - `GET /admin/discount-codes?event_id=...`, `POST /admin/discount-codes`, `POST /admin/discount-codes/{code}/deactivate`
  - `GET /admin/waitlist?event_id=...`, `POST /admin/waitlist/{waitlist_id}/notify`
  - `GET /admin/checkin?event_id=...` (scanner page), `POST /admin/checkin/{ticket_id}?event_id=...` (JSON API returning `status` ∈ `checked_in | already_checked_in | wrong_event | voided | invalid`)

**Two behaviours here are easy to get wrong and expensive to get wrong:**

1. **Refunding must void the tickets.** Releasing capacity while leaving `Tickets` rows untouched means the refunded guest's QR code still scans clean at the door, because check-in reads only the ticket row and never looks at the order. Refund therefore sets `voided = True` on every ticket in the order, and check-in refuses voided tickets.
2. **Check-in must be scoped to the event being scanned.** The system deliberately retains past years' data, so without an `event_id` comparison a 2026 ticket scans successfully at the 2027 door.

- [ ] **Step 1: Write failing tests**

`tests/test_admin_routes.py`:
```python
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.db import DISCOUNT_CODES, EVENTS, ORDERS, TICKETS, WAITLIST
from app.main import app


@pytest.fixture
def admin_client(dynamodb_tables):
    """A client carrying a valid session cookie, with token verification stubbed.

    Token verification itself is covered end-to-end in tests/test_auth.py
    against real RS256 signatures; stubbing it here keeps these tests about
    the routes.
    """
    with patch.object(auth, "verify_cognito_token", return_value={"sub": "admin-1"}):
        client = TestClient(app)
        client.cookies.set(settings.session_cookie_name, auth.issue_session("fake-id-token"))
        yield client


def _put_event(event_id="evt_2026", **overrides):
    item = {
        "event_id": event_id, "year": 2026, "name": "Test", "date": "2026-03-14",
        "location": "NOLA", "description": "d", "ticket_price_cents": 15000,
        "capacity": 300, "tickets_sold_count": 5, "registration_open": True,
        "status": "open", "registration_opens_at": None, "registration_closes_at": None,
    }
    item.update(overrides)
    EVENTS().put_item(Item=item)


def _put_ticket(ticket_id, event_id="evt_2026", **overrides):
    item = {
        "ticket_id": ticket_id, "order_id": "ord_1", "event_id": event_id,
        "attendee_name": "Jane", "checked_in": False, "checked_in_at": None,
        "voided": False, "voided_at": None,
    }
    item.update(overrides)
    TICKETS().put_item(Item=item)


def _put_order(order_id="ord_1", **overrides):
    item = {
        "order_id": order_id, "event_id": "evt_2026", "buyer_name": "Jane",
        "buyer_email": "jane@example.com", "attendees": [{"name": "Jane"}, {"name": None}],
        "quantity": 2, "unit_price_cents": 15000, "total_cents": 30000,
        "status": "paid", "created_at": "2026-01-01T00:00:00Z", "discount_code": None,
        "stripe_checkout_session_id": "cs_1", "stripe_payment_intent_id": "pi_1",
    }
    item.update(overrides)
    ORDERS().put_item(Item=item)


def test_admin_redirects_unauthenticated_browsers_to_login(dynamodb_tables):
    client = TestClient(app)
    resp = client.get("/admin/events", headers={"accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_admin_401s_unauthenticated_api_clients(dynamodb_tables):
    client = TestClient(app)
    resp = client.post(
        "/admin/checkin/tkt_x?event_id=evt_2026", headers={"accept": "application/json"}
    )
    assert resp.status_code == 401


def test_admin_rejects_a_forged_session_cookie(dynamodb_tables):
    """The cookie is signed; editing it must not grant access."""
    client = TestClient(app)
    client.cookies.set(settings.session_cookie_name, "not-a-valid-signed-value")
    resp = client.get("/admin/events", headers={"accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 303


def test_admin_can_create_event(admin_client):
    resp = admin_client.post("/admin/events", data={
        "year": "2027", "name": "Next Year Ball", "date": "2027-03-06",
        "location": "NOLA", "description": "d", "ticket_price_cents": "15000",
        "capacity": "300",
    }, follow_redirects=False)
    assert resp.status_code == 303
    items = EVENTS().scan()["Items"]
    assert any(int(e["year"]) == 2027 for e in items)


def test_opening_an_event_closes_any_other_open_event(admin_client):
    """The public page shows `the` open event; two would make it arbitrary."""
    _put_event("evt_2026", status="open", registration_open=True)
    _put_event("evt_2027", status="draft", registration_open=False)

    admin_client.post("/admin/events/evt_2027/open", follow_redirects=False)

    assert EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["status"] == "closed"
    assert EVENTS().get_item(Key={"event_id": "evt_2027"})["Item"]["status"] == "open"


def test_checkin_marks_ticket_checked_in(admin_client):
    _put_ticket("tkt_abc")
    resp = admin_client.post("/admin/checkin/tkt_abc?event_id=evt_2026")
    assert resp.status_code == 200
    assert resp.json()["status"] == "checked_in"
    assert TICKETS().get_item(Key={"ticket_id": "tkt_abc"})["Item"]["checked_in"] is True


def test_checkin_rejects_unknown_ticket(admin_client):
    resp = admin_client.post("/admin/checkin/tkt_nonexistent?event_id=evt_2026")
    assert resp.status_code == 200
    assert resp.json()["status"] == "invalid"


def test_checkin_flags_duplicate(admin_client):
    _put_ticket("tkt_dup", checked_in=True, checked_in_at="2026-03-14T20:00:00Z")
    resp = admin_client.post("/admin/checkin/tkt_dup?event_id=evt_2026")
    assert resp.json()["status"] == "already_checked_in"


def test_checkin_rejects_ticket_from_a_different_event(admin_client):
    """Past years stay queryable, so last year's QR code is a live object."""
    _put_ticket("tkt_lastyear", event_id="evt_2025")
    resp = admin_client.post("/admin/checkin/tkt_lastyear?event_id=evt_2026")
    assert resp.json()["status"] == "wrong_event"
    assert TICKETS().get_item(Key={"ticket_id": "tkt_lastyear"})["Item"]["checked_in"] is False


def test_checkin_rejects_voided_ticket(admin_client):
    _put_ticket("tkt_refunded", voided=True, voided_at="2026-02-01T00:00:00Z")
    resp = admin_client.post("/admin/checkin/tkt_refunded?event_id=evt_2026")
    assert resp.json()["status"] == "voided"
    assert TICKETS().get_item(Key={"ticket_id": "tkt_refunded"})["Item"]["checked_in"] is False


@patch("app.routes.admin.stripe.Refund.create")
def test_refund_marks_refunded_voids_tickets_and_frees_capacity(mock_refund, admin_client):
    _put_event(tickets_sold_count=5)
    _put_order("ord_refund")
    _put_ticket("tkt_r1")
    _put_ticket("tkt_r2")

    resp = admin_client.post("/admin/orders/ord_refund/refund", follow_redirects=False)

    assert resp.status_code == 303
    mock_refund.assert_called_once_with(payment_intent="pi_1")
    assert ORDERS().get_item(Key={"order_id": "ord_refund"})["Item"]["status"] == "refunded"
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 3

    for tid in ("tkt_r1", "tkt_r2"):
        ticket = TICKETS().get_item(Key={"ticket_id": tid})["Item"]
        assert ticket["voided"] is True, "a refunded ticket must not scan at the door"


@patch("app.routes.admin.stripe.Refund.create")
def test_refund_is_idempotent(mock_refund, admin_client):
    """Double-clicking Refund must not refund twice or free capacity twice."""
    _put_event(tickets_sold_count=5)
    _put_order("ord_refund")

    admin_client.post("/admin/orders/ord_refund/refund", follow_redirects=False)
    admin_client.post("/admin/orders/ord_refund/refund", follow_redirects=False)

    assert mock_refund.call_count == 1
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 3


@patch("app.routes.admin.stripe.Refund.create", side_effect=RuntimeError("stripe down"))
def test_failed_refund_leaves_the_order_paid(mock_refund, admin_client):
    _put_event(tickets_sold_count=5)
    _put_order("ord_refund")

    resp = admin_client.post("/admin/orders/ord_refund/refund", follow_redirects=False)

    assert resp.status_code == 502
    assert ORDERS().get_item(Key={"order_id": "ord_refund"})["Item"]["status"] == "paid"
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 5


def test_refund_unknown_order_404s(admin_client):
    resp = admin_client.post("/admin/orders/ord_missing/refund")
    assert resp.status_code == 404


def test_orders_export_returns_csv(admin_client):
    _put_order("ord_csv", quantity=1, attendees=[{"name": "Jane"}], total_cents=15000)
    resp = admin_client.get("/admin/orders/export?event_id=evt_2026")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "jane@example.com" in resp.text


def test_orders_export_neutralizes_spreadsheet_formulas(admin_client):
    """Buyer names are attacker-controlled free text that lands in a spreadsheet."""
    _put_order("ord_evil", buyer_name="=cmd|'/c calc'!A1")
    resp = admin_client.get("/admin/orders/export?event_id=evt_2026")
    assert "=cmd" not in resp.text
    assert "'=cmd" in resp.text


def test_discount_code_is_stored_normalized(admin_client):
    admin_client.post("/admin/discount-codes", data={
        "code": " member20 ", "event_id": "evt_2026",
        "discount_type": "percent", "discount_value": "20", "max_uses": "",
    }, follow_redirects=False)
    assert DISCOUNT_CODES().get_item(Key={"code": "MEMBER20"}).get("Item") is not None


def test_waitlist_notify_marks_entry(admin_client):
    WAITLIST().put_item(Item={
        "waitlist_id": "wl_1", "event_id": "evt_2026", "name": "Sam",
        "email": "sam@example.com", "requested_quantity": 2,
        "created_at": "2026-01-01T00:00:00Z", "notified": False,
    })
    admin_client.post("/admin/waitlist/wl_1/notify", data={"event_id": "evt_2026"},
                      follow_redirects=False)
    assert WAITLIST().get_item(Key={"waitlist_id": "wl_1"})["Item"]["notified"] is True
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pytest tests/test_admin_routes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.routes.admin'`

- [ ] **Step 3: Implement `app/routes/admin.py`**

```python
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

from app.auth import require_admin
from app.config import settings
from app.db import DISCOUNT_CODES, EVENTS, ORDERS, TICKETS, WAITLIST, paginate
from app.emails import send_confirmation_email
from app.models import Order, Ticket, normalize_code

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


@router.get("/events")
def list_events(request: Request):
    events = sorted(paginate(EVENTS().scan), key=lambda e: int(e["year"]), reverse=True)
    return templates.TemplateResponse(request, "admin/events.html", {"events": events})


@router.post("/events")
def create_event(
    year: int = Form(...),
    name: str = Form(...),
    date: str = Form(...),
    location: str = Form(...),
    description: str = Form(...),
    ticket_price_cents: int = Form(...),
    capacity: int = Form(...),
):
    EVENTS().put_item(Item={
        "event_id": f"evt_{year}", "year": year, "name": name, "date": date,
        "location": location, "description": description,
        "ticket_price_cents": ticket_price_cents, "capacity": capacity,
        "tickets_sold_count": 0, "registration_open": False, "status": "draft",
        "registration_opens_at": None, "registration_closes_at": None,
    })
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/open")
def open_event(event_id: str):
    # The public page renders "the" open event, so close any other first —
    # otherwise which event the public sees depends on scan ordering.
    for other in paginate(EVENTS().scan, FilterExpression=Attr("status").eq("open")):
        if other["event_id"] != event_id:
            _set_event_open(other["event_id"], False)
    _set_event_open(event_id, True)
    return RedirectResponse("/admin/events", status_code=303)


@router.post("/events/{event_id}/close")
def close_event(event_id: str):
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
def list_orders(request: Request, event_id: str, q: str = "", status: str = ""):
    orders = _orders_for_event(event_id, q, status)
    return templates.TemplateResponse(
        request, "admin/orders.html",
        {"orders": orders, "event_id": event_id, "q": q, "status": status},
    )


@router.get("/orders/export")
def export_orders(event_id: str):
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
def resend_email(order_id: str):
    order_item = _get_order_or_404(order_id)
    tickets = [Ticket(**t) for t in _tickets_for_order(order_id)]
    send_confirmation_email(Order(**order_item), tickets)
    return RedirectResponse(f"/admin/orders?event_id={order_item['event_id']}", status_code=303)


@router.post("/orders/{order_id}/refund")
def refund_order(order_id: str):
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


@router.get("/discount-codes")
def list_discount_codes(request: Request, event_id: str):
    codes = paginate(DISCOUNT_CODES().scan, FilterExpression=Attr("event_id").eq(event_id))
    return templates.TemplateResponse(
        request, "admin/discount_codes.html", {"codes": codes, "event_id": event_id}
    )


@router.post("/discount-codes")
def create_discount_code(
    code: str = Form(...),
    event_id: str = Form(...),
    discount_type: str = Form(...),
    discount_value: int = Form(...),
    max_uses: str = Form(""),
):
    DISCOUNT_CODES().put_item(Item={
        # Stored normalized so lookup at checkout is case-insensitive.
        "code": normalize_code(code),
        "event_id": event_id,
        "discount_type": discount_type,
        "discount_value": discount_value,
        "max_uses": int(max_uses) if max_uses.strip() else None,
        "uses_count": 0,
        "active": True,
    })
    return RedirectResponse(f"/admin/discount-codes?event_id={event_id}", status_code=303)


@router.post("/discount-codes/{code}/deactivate")
def deactivate_discount_code(code: str, event_id: str = Form(...)):
    DISCOUNT_CODES().update_item(
        Key={"code": normalize_code(code)},
        UpdateExpression="SET active = :f",
        ExpressionAttributeValues={":f": False},
    )
    return RedirectResponse(f"/admin/discount-codes?event_id={event_id}", status_code=303)


@router.get("/waitlist")
def list_waitlist(request: Request, event_id: str):
    entries = paginate(WAITLIST().scan, FilterExpression=Attr("event_id").eq(event_id))
    return templates.TemplateResponse(
        request, "admin/waitlist.html", {"entries": entries, "event_id": event_id}
    )


@router.post("/waitlist/{waitlist_id}/notify")
def notify_waitlist_entry(waitlist_id: str, event_id: str = Form(...)):
    WAITLIST().update_item(
        Key={"waitlist_id": waitlist_id},
        UpdateExpression="SET notified = :t",
        ExpressionAttributeValues={":t": True},
    )
    return RedirectResponse(f"/admin/waitlist?event_id={event_id}", status_code=303)


@router.get("/checkin")
def checkin_page(request: Request, event_id: str):
    return templates.TemplateResponse(request, "admin/checkin.html", {"event_id": event_id})


@router.post("/checkin/{ticket_id}")
def checkin_ticket(ticket_id: str, event_id: str):
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
```

- [ ] **Step 4: Vendor the QR scanner library**

```bash
mkdir -p app/static
curl -fsSL https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js \
  -o app/static/html5-qrcode.min.js
test -s app/static/html5-qrcode.min.js && echo "vendored OK"
```

*Check-in happens once a year, at a venue, on whatever network the venue has. Loading the scanner from a CDN at that moment makes the door dependent on unpkg.com being reachable and unchanged. Vendoring it into the deployment bundle removes that dependency and pins the exact code being run.*

- [ ] **Step 5: Write admin templates**

`app/templates/admin/login.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>Admin Login</h1>
<p><a href="/admin/login">Sign in with Cognito</a></p>
{% endblock %}
```

`app/templates/admin/events.html`:
```html
{% extends "base.html" %}
{% block content %}
<p style="text-align:right"><a href="/admin/logout">Sign out</a></p>
<h1>Events</h1>
<ul>
{% for e in events %}
  <li>{{ e.year }} — {{ e.name }} ({{ e.status }}) — {{ e.tickets_sold_count }}/{{ e.capacity }}
    <a href="/admin/orders?event_id={{ e.event_id }}">Orders</a>
    <a href="/admin/discount-codes?event_id={{ e.event_id }}">Codes</a>
    <a href="/admin/waitlist?event_id={{ e.event_id }}">Waitlist</a>
    <a href="/admin/checkin?event_id={{ e.event_id }}">Check-in</a>
    <form method="post" action="/admin/events/{{ e.event_id }}/open" style="display:inline"><button>Open</button></form>
    <form method="post" action="/admin/events/{{ e.event_id }}/close" style="display:inline"><button>Close</button></form>
  </li>
{% endfor %}
</ul>
<h2>Create Event</h2>
<form method="post" action="/admin/events">
  <input name="year" placeholder="Year" required>
  <input name="name" placeholder="Name" required>
  <input name="date" placeholder="Date (YYYY-MM-DD)" required>
  <input name="location" placeholder="Location" required>
  <input name="description" placeholder="Description" required>
  <input name="ticket_price_cents" placeholder="Price (cents)" required>
  <input name="capacity" placeholder="Capacity" required>
  <button type="submit">Create</button>
</form>
{% endblock %}
```

`app/templates/admin/orders.html`:
```html
{% extends "base.html" %}
{% block content %}
<p style="text-align:right"><a href="/admin/events">Events</a> · <a href="/admin/logout">Sign out</a></p>
<h1>Orders</h1>
<form method="get" action="/admin/orders">
  <input type="hidden" name="event_id" value="{{ event_id }}">
  <input name="q" value="{{ q }}" placeholder="Search name or email">
  <select name="status">
    <option value="">any status</option>
    {% for s in ["pending", "paid", "refunded", "canceled", "expired"] %}
      <option value="{{ s }}" {% if status == s %}selected{% endif %}>{{ s }}</option>
    {% endfor %}
  </select>
  <button type="submit">Filter</button>
</form>
<p><a href="/admin/orders/export?event_id={{ event_id }}">Export CSV</a></p>
<table>
<tr><th>Buyer</th><th>Email</th><th>Qty</th><th>Total</th><th>Status</th><th>Actions</th></tr>
{% for o in orders %}
<tr>
  <td>{{ o.buyer_name }}</td><td>{{ o.buyer_email }}</td><td>{{ o.quantity }}</td>
  <td>${{ "%.2f"|format(o.total_cents / 100) }}</td><td>{{ o.status }}</td>
  <td>
    {% if o.status == "paid" %}
    <form method="post" action="/admin/orders/{{ o.order_id }}/resend-email" style="display:inline"><button>Resend</button></form>
    <form method="post" action="/admin/orders/{{ o.order_id }}/refund" style="display:inline"
          onsubmit="return confirm('Refund {{ o.buyer_name }} ${{ "%.2f"|format(o.total_cents / 100) }} and void their tickets?')">
      <button>Refund</button></form>
    {% endif %}
  </td>
</tr>
{% endfor %}
</table>
{% endblock %}
```

`app/templates/admin/discount_codes.html`:
```html
{% extends "base.html" %}
{% block content %}
<p style="text-align:right"><a href="/admin/events">Events</a> · <a href="/admin/logout">Sign out</a></p>
<h1>Discount Codes</h1>
<table>
<tr><th>Code</th><th>Type</th><th>Value</th><th>Uses</th><th>Active</th><th></th></tr>
{% for c in codes %}
<tr>
  <td>{{ c.code }}</td><td>{{ c.discount_type }}</td><td>{{ c.discount_value }}</td>
  <td>{{ c.uses_count }}{% if c.max_uses %}/{{ c.max_uses }}{% endif %}</td>
  <td>{{ c.active }}</td>
  <td><form method="post" action="/admin/discount-codes/{{ c.code }}/deactivate">
    <input type="hidden" name="event_id" value="{{ event_id }}">
    <button>Deactivate</button></form></td>
</tr>
{% endfor %}
</table>
<h2>Create Code</h2>
<form method="post" action="/admin/discount-codes">
  <input type="hidden" name="event_id" value="{{ event_id }}">
  <input name="code" placeholder="CODE" required>
  <select name="discount_type"><option value="percent">percent</option><option value="fixed">fixed</option></select>
  <input name="discount_value" placeholder="Value (percent, or cents if fixed)" required>
  <input name="max_uses" placeholder="Max uses (optional)">
  <button type="submit">Create</button>
</form>
{% endblock %}
```

`app/templates/admin/waitlist.html`:
```html
{% extends "base.html" %}
{% block content %}
<p style="text-align:right"><a href="/admin/events">Events</a> · <a href="/admin/logout">Sign out</a></p>
<h1>Waitlist</h1>
<table>
<tr><th>Name</th><th>Email</th><th>Qty</th><th>Notified</th><th></th></tr>
{% for w in entries %}
<tr>
  <td>{{ w.name }}</td><td>{{ w.email }}</td><td>{{ w.requested_quantity }}</td><td>{{ w.notified }}</td>
  <td><form method="post" action="/admin/waitlist/{{ w.waitlist_id }}/notify">
    <input type="hidden" name="event_id" value="{{ event_id }}">
    <button>Mark Notified</button></form></td>
</tr>
{% endfor %}
</table>
{% endblock %}
```

`app/templates/admin/checkin.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>Check-in</h1>
<p>Event: {{ event_id }} · <a href="/admin/events">Events</a></p>
<div id="reader" style="width:min(400px, 100%)"></div>
<p id="result" role="status" style="font-size:1.5rem; padding:0.5rem"></p>
<p>
  <input id="manual-ticket-id" placeholder="Ticket ID (manual entry)">
  <button id="manual-submit">Check In</button>
</p>
<script src="/static/html5-qrcode.min.js"></script>
<script>
  const EVENT_ID = {{ event_id | tojson }};
  const MESSAGES = {
    checked_in:        (d) => ["✅ Welcome, " + (d.attendee_name || "guest"), "#137333"],
    already_checked_in:(d) => ["⚠️ Already checked in: " + (d.attendee_name || "guest"), "#b06000"],
    wrong_event:       ()  => ["⛔ Ticket is for a different event", "#b3261e"],
    voided:            ()  => ["⛔ Ticket was refunded", "#b3261e"],
    invalid:           ()  => ["⛔ Unrecognized ticket", "#b3261e"],
  };
  let busy = false;

  async function checkin(ticketId) {
    if (busy || !ticketId) return;
    busy = true;
    try {
      const url = "/admin/checkin/" + encodeURIComponent(ticketId)
                + "?event_id=" + encodeURIComponent(EVENT_ID);
      const resp = await fetch(url, {method: "POST", headers: {"Accept": "application/json"}});
      if (resp.status === 401) {
        document.getElementById("result").textContent = "Session expired — reload and sign in.";
        return;
      }
      const data = await resp.json();
      const [text, color] = (MESSAGES[data.status] || MESSAGES.invalid)(data);
      const el = document.getElementById("result");
      el.textContent = text;
      el.style.color = color;
    } finally {
      // Debounce so one QR code in front of the camera is not scanned 10x/sec.
      setTimeout(() => { busy = false; }, 1500);
    }
  }

  document.getElementById("manual-submit").onclick = () =>
    checkin(document.getElementById("manual-ticket-id").value.trim());

  const scanner = new Html5Qrcode("reader");
  scanner.start({facingMode: "environment"}, {fps: 10, qrbox: 250}, checkin)
    .catch(() => {
      document.getElementById("result").textContent =
        "Camera unavailable — use manual entry below.";
    });
</script>
{% endblock %}
```

*The `busy` debounce is not cosmetic: `html5-qrcode` fires its success callback on every decoded frame, so a code held in front of the lens produces a burst of requests and the guest sees "already checked in" a beat after "welcome".*

- [ ] **Step 6: Wire the admin router and static files into `app/main.py`**

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from mangum import Mangum

from app.routes import admin, auth_routes, public, webhooks

app = FastAPI(title="Jester's Rodeo")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(public.router)
app.include_router(webhooks.router)
app.include_router(auth_routes.router)
app.include_router(admin.router)

handler = Mangum(app)
```

- [ ] **Step 7: Run tests, verify pass**

Run: `pytest tests/test_admin_routes.py -v`
Expected: PASS (18 tests)

- [ ] **Step 8: Run full test suite**

Run: `pytest -v`
Expected: All tests across all files PASS.

- [ ] **Step 9: Commit**

```bash
git add app/routes/admin.py app/templates/admin/ app/static/ app/main.py tests/test_admin_routes.py
git commit -m "Add admin dashboard: events, orders, export, codes, waitlist, scoped check-in, refunds"
```

---

## Task 10: Waitlist Signup Route (Public)

**Files:**
- Modify: `app/routes/public.py`
- Create: `app/templates/waitlist_signup.html`, `app/templates/waitlist_confirmed.html`
- Test: `tests/test_public_routes.py` (append)

**Interfaces:**
- Consumes: `app.db.WAITLIST` (Task 2)
- Produces: `GET /waitlist?event_id=...` (form page), `POST /waitlist` (creates a `WaitlistEntry`), `GET /order-confirmation-waitlist?event_id=...` (acknowledgement page)

*The waitlist deliberately reserves nothing and charges nothing — it is a list of people to email by hand when a refund frees a seat. Keeping it dumb is what keeps it out of the capacity accounting in Tasks 6, 7 and 11.*

- [ ] **Step 1: Write failing test**

Append to `tests/test_public_routes.py`:
```python
from app.db import WAITLIST


def test_waitlist_signup_creates_entry(dynamodb_tables):
    resp = client.post("/waitlist", data={
        "event_id": "evt_2026", "name": "Sam Smith",
        "email": "sam@example.com", "requested_quantity": "2",
    }, follow_redirects=False)
    assert resp.status_code == 303
    items = WAITLIST().scan()["Items"]
    assert len(items) == 1
    assert items[0]["name"] == "Sam Smith"
    assert items[0]["notified"] is False
```

- [ ] **Step 2: Run test, verify failure**

Run: `pytest tests/test_public_routes.py -v -k waitlist`
Expected: FAIL with 404 (route doesn't exist yet)

- [ ] **Step 3: Add waitlist routes to `app/routes/public.py`**

`uuid`, `datetime`/`timezone`, `Form` and `RedirectResponse` are already imported by Task 6. Add `WAITLIST` to the existing `from app.db import ...` line, then append:
```python
@router.get("/waitlist")
def waitlist_signup_page(request: Request, event_id: str):
    return templates.TemplateResponse(request, "waitlist_signup.html", {"event_id": event_id})


@router.post("/waitlist")
def waitlist_signup(
    event_id: str = Form(...), name: str = Form(...),
    email: str = Form(...), requested_quantity: int = Form(...),
):
    WAITLIST().put_item(Item={
        "waitlist_id": f"wl_{uuid.uuid4().hex}",
        "event_id": event_id, "name": name, "email": email,
        "requested_quantity": requested_quantity,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notified": False,
    })
    return RedirectResponse(f"/order-confirmation-waitlist?event_id={event_id}", status_code=303)


@router.get("/order-confirmation-waitlist")
def waitlist_confirmation(request: Request, event_id: str):
    return templates.TemplateResponse(request, "waitlist_confirmed.html", {"event_id": event_id})
```

- [ ] **Step 4: Add templates**

`app/templates/waitlist_signup.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>Join the Waitlist</h1>
<form method="post" action="/waitlist">
  <input type="hidden" name="event_id" value="{{ event_id }}">
  <label>Name <input type="text" name="name" required></label>
  <label>Email <input type="email" name="email" required></label>
  <label>How many tickets? <input type="number" name="requested_quantity" value="1" min="1" required></label>
  <button type="submit">Join Waitlist</button>
</form>
{% endblock %}
```

`app/templates/waitlist_confirmed.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>You're on the waitlist!</h1>
<p>We'll email you if a spot opens up.</p>
{% endblock %}
```

- [ ] **Step 5: Run test, verify pass**

Run: `pytest tests/test_public_routes.py -v -k waitlist`
Expected: PASS

- [ ] **Step 6: Run full suite**

Run: `pytest -v`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add app/routes/public.py app/templates/waitlist_signup.html app/templates/waitlist_confirmed.html tests/test_public_routes.py
git commit -m "Add public waitlist signup route"
```

---

## Task 11: Reservation Cleanup Lambda

**Files:**
- Create: `scripts/cleanup_pending_orders.py`
- Test: `tests/test_cleanup.py`

**Interfaces:**
- Consumes: `app.db.ORDERS`, `app.db.EVENTS`, `app.db.paginate` (Task 2), `app.routes.public.RESERVATION_TTL_MINUTES` (Task 6)
- Produces:
  - `scripts.cleanup_pending_orders.CLEANUP_GRACE_MINUTES: int` — `45`.
  - `scripts.cleanup_pending_orders.expire_stale_reservations(max_age_minutes: int = CLEANUP_GRACE_MINUTES) -> int` — returns the number of reservations expired. Testable directly, without a Lambda `event`/`context`.
  - `scripts.cleanup_pending_orders.handler(event: dict, context: object) -> dict` — the Lambda entrypoint, returns `{"expired": n}`.

**This job is a backstop, not the primary release path.** The `checkout.session.expired` webhook (Task 7) normally frees a seat at `RESERVATION_TTL_MINUTES` (35). This runs every 15 minutes and catches reservations whose webhook never arrived, using a *longer* window (45 minutes) so it can never race a Stripe session that is still live.

**Stale orders are expired, never deleted.** Hard-deleting a `pending` order looks tidy and is quietly dangerous: if the payment webhook is merely delayed, the order disappears, the late webhook finds nothing (Task 7's condition includes `attribute_exists(order_id)`), and a customer who paid gets no ticket, no email, and leaves no record anyone can trace. Transitioning to `expired` keeps the row, and Task 7 knows how to fulfil a late payment on an `expired` order.

- [ ] **Step 1: Write failing test**

`tests/test_cleanup.py`:
```python
from datetime import datetime, timedelta, timezone

from app.db import EVENTS, ORDERS
from scripts.cleanup_pending_orders import expire_stale_reservations


def _ts(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _put_event(tickets_sold_count: int = 3) -> None:
    EVENTS().put_item(Item={
        "event_id": "evt_2026", "year": 2026, "name": "Test", "date": "2026-03-14",
        "location": "NOLA", "description": "d", "ticket_price_cents": 15000,
        "capacity": 300, "tickets_sold_count": tickets_sold_count,
        "registration_open": True, "status": "open",
    })


def _put_order(order_id: str, minutes_ago: int, quantity: int = 1, status: str = "pending") -> None:
    ORDERS().put_item(Item={
        "order_id": order_id, "event_id": "evt_2026", "buyer_name": order_id,
        "buyer_email": f"{order_id}@example.com", "attendees": [{"name": None}] * quantity,
        "quantity": quantity, "unit_price_cents": 15000, "total_cents": 15000 * quantity,
        "status": status, "created_at": _ts(minutes_ago), "discount_code": None,
        "stripe_checkout_session_id": None, "stripe_payment_intent_id": None,
    })


def test_expires_stale_reservations_and_frees_capacity(dynamodb_tables):
    _put_event(tickets_sold_count=3)
    _put_order("ord_old", minutes_ago=120, quantity=1)
    _put_order("ord_recent", minutes_ago=5, quantity=2)

    assert expire_stale_reservations(max_age_minutes=45) == 1

    assert ORDERS().get_item(Key={"order_id": "ord_old"})["Item"]["status"] == "expired"
    assert ORDERS().get_item(Key={"order_id": "ord_recent"})["Item"]["status"] == "pending"
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 2


def test_stale_orders_are_kept_not_deleted(dynamodb_tables):
    """A late webhook must still find the order — see Task 7's late-payment path."""
    _put_event()
    _put_order("ord_old", minutes_ago=120)

    expire_stale_reservations(max_age_minutes=45)

    assert ORDERS().get_item(Key={"order_id": "ord_old"}).get("Item") is not None


def test_paid_and_refunded_orders_are_untouched(dynamodb_tables):
    _put_event(tickets_sold_count=5)
    _put_order("ord_paid", minutes_ago=5000, status="paid", quantity=2)
    _put_order("ord_refunded", minutes_ago=5000, status="refunded", quantity=1)

    assert expire_stale_reservations(max_age_minutes=45) == 0
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 5


def test_running_twice_releases_capacity_only_once(dynamodb_tables):
    """EventBridge can invoke a schedule more than once; so can a manual retry."""
    _put_event(tickets_sold_count=3)
    _put_order("ord_old", minutes_ago=120, quantity=2)

    first = expire_stale_reservations(max_age_minutes=45)
    second = expire_stale_reservations(max_age_minutes=45)

    assert (first, second) == (1, 0)
    assert int(EVENTS().get_item(Key={"event_id": "evt_2026"})["Item"]["tickets_sold_count"]) == 1
```

- [ ] **Step 2: Run test, verify failure**

Run: `pytest tests/test_cleanup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.cleanup_pending_orders'`

- [ ] **Step 3: Implement `scripts/cleanup_pending_orders.py`**

```python
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
```

- [ ] **Step 4: Run test, verify pass**

Run: `pytest tests/test_cleanup.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run full suite**

Run: `pytest -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/cleanup_pending_orders.py tests/test_cleanup.py
git commit -m "Expire stale reservations on a schedule instead of deleting pending orders"
```

---

## Task 12: CDK Infrastructure — DynamoDB, Lambda, API Gateway, Cognito, SES, EventBridge

**Files:**
- Modify: `infra/jesters_rodeo_stack.py`

**Interfaces:**
- Consumes: `app.main.handler` (Task 6), `scripts.cleanup_pending_orders.handler` (Task 11) — as Lambda handler entrypoints.
- Produces: a deployable CDK stack exposing `CfnOutput`s named `ApiUrl`, `SiteUrl`, `UserPoolId`, `UserPoolClientId`, and `CognitoDomain`.
- Required CDK context values (passed as `-c key=value` or in `cdk.json`):
  - `site_url` — the public base URL, e.g. `https://register.example.com` or the execute-api URL. **Required.**
  - `cognito_domain_prefix` — globally unique prefix for the Cognito Hosted UI domain, e.g. `jesters-rodeo-admin`. **Required.**
  - `ses_sender_email` — the verified From address. **Required.**
  - `domain_name` + `hosted_zone_id` — optional; when both are present the stack provisions an ACM certificate, an API Gateway custom domain, and a Route 53 alias record.

**Four things in this stack are easy to get wrong in ways that only surface at deploy or at runtime:**

1. **`AWS_REGION` must not appear in any Lambda `environment` dict.** It is a reserved Lambda environment variable; CloudFormation rejects the whole stack with *"the environment variables you have provided contains reserved keys."* Lambda sets it automatically, and `app/config.py` reads it from there.
2. **The Lambda asset must be built, and its path is relative to `infra/`.** `Code.from_asset(".")` while the CDK app runs from `infra/` packages the CDK directory, not the application — and even pointed at the repo root it ships no third-party packages, so the function dies on `import fastapi`. The bundling step below pip-installs `requirements.txt` into the artifact.
3. **`site_url` is context, not a reference to the API endpoint.** The Cognito app client needs the callback URL, the Lambda needs `BASE_URL`, and the HTTP API needs the Lambda — deriving the callback from `http_api.api_endpoint` closes that loop into a circular dependency CloudFormation will refuse. Supplying the URL as context breaks the cycle. Without a custom domain this means deploying once to learn the URL, then redeploying with it (documented in Task 14).
4. **Both functions get the same environment.** The cleanup function imports `app.db`, which imports `app.config`, which requires every setting — so it needs the full env dict and, after Task 13, read access to the same SSM parameters.

- [ ] **Step 1: Write the full stack**

Replace `infra/jesters_rodeo_stack.py`:
```python
from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_certificatemanager as acm,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_ses as ses,
)
from constructs import Construct

# Everything the deployment artifact does not need. Without this the asset
# also carries .git, the local virtualenv, and the plan document.
ASSET_EXCLUDES = [
    ".git", ".github", ".venv", "venv", "infra", "tests", "docs",
    "*.md", "__pycache__", "*.pyc", ".pytest_cache", ".DS_Store", "cdk.out",
]


class JestersRodeoStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        site_url = self._require_context("site_url")
        cognito_domain_prefix = self._require_context("cognito_domain_prefix")
        sender_email = self._require_context("ses_sender_email")
        domain_name = self.node.try_get_context("domain_name")
        hosted_zone_id = self.node.try_get_context("hosted_zone_id")

        tables = self._create_tables()
        user_pool, user_pool_client, user_pool_domain = self._create_auth(
            cognito_domain_prefix, site_url
        )

        ses.EmailIdentity(
            self, "SesSenderIdentity", identity=ses.Identity.email(sender_email)
        )

        common_env = {
            "EVENTS_TABLE": tables["events"].table_name,
            "ORDERS_TABLE": tables["orders"].table_name,
            "TICKETS_TABLE": tables["tickets"].table_name,
            "DISCOUNT_CODES_TABLE": tables["discount_codes"].table_name,
            "WAITLIST_TABLE": tables["waitlist"].table_name,
            "SES_SENDER_EMAIL": sender_email,
            "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
            "COGNITO_APP_CLIENT_ID": user_pool_client.user_pool_client_id,
            "COGNITO_DOMAIN": f"{cognito_domain_prefix}.auth.{self.region}.amazoncognito.com",
            "BASE_URL": site_url,
            # NOTE: no AWS_REGION here. It is a reserved Lambda environment
            # variable — setting it fails the deployment outright — and Lambda
            # populates it for us.
        }

        app_lambda = _lambda.Function(
            self, "AppFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="app.main.handler",
            code=self._bundled_code(),
            timeout=Duration.seconds(15),
            memory_size=512,
            environment=common_env,
        )
        cleanup_lambda = _lambda.Function(
            self, "CleanupFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="scripts.cleanup_pending_orders.handler",
            code=self._bundled_code(),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment=common_env,
        )

        for table in tables.values():
            table.grant_read_write_data(app_lambda)
        tables["orders"].grant_read_write_data(cleanup_lambda)
        tables["events"].grant_read_write_data(cleanup_lambda)

        app_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendRawEmail"],
                resources=["*"],
                conditions={"StringEquals": {"ses:FromAddress": sender_email}},
            )
        )

        http_api = apigwv2.HttpApi(
            self, "HttpApi",
            default_integration=apigwv2_integrations.HttpLambdaIntegration(
                "AppIntegration", app_lambda
            ),
        )

        # Every 15 minutes: a backstop behind the checkout.session.expired
        # webhook, so an unclaimed seat is never held for more than an hour.
        rule = events.Rule(
            self, "CleanupScheduleRule",
            schedule=events.Schedule.rate(Duration.minutes(15)),
        )
        rule.add_target(targets.LambdaFunction(cleanup_lambda))

        if domain_name and hosted_zone_id:
            self._create_custom_domain(http_api, domain_name, hosted_zone_id)

        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "SiteUrl", value=site_url)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "CognitoDomain", value=user_pool_domain.base_url())

    def _require_context(self, key: str) -> str:
        value = self.node.try_get_context(key)
        if not value:
            raise ValueError(
                f"Missing required CDK context '{key}'. "
                f"Pass it with -c {key}=... or add it to infra/cdk.json."
            )
        return value

    def _bundled_code(self) -> _lambda.Code:
        """Package app/ and scripts/ together with their dependencies.

        The path is ".." because the CDK app runs from infra/. Bundling runs
        pip inside the official Python 3.12 Lambda image, so Docker must be
        running for `cdk deploy`.
        """
        return _lambda.Code.from_asset(
            "..",
            exclude=ASSET_EXCLUDES,
            bundling=BundlingOptions(
                image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                command=[
                    "bash", "-c",
                    "pip install --no-cache-dir -r requirements.txt -t /asset-output "
                    "&& cp -r app scripts /asset-output/",
                ],
            ),
        )

    def _create_tables(self) -> dict:
        events_table = dynamodb.Table(
            self, "EventsTable",
            partition_key=dynamodb.Attribute(name="event_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        orders_table = dynamodb.Table(
            self, "OrdersTable",
            partition_key=dynamodb.Attribute(name="order_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        orders_table.add_global_secondary_index(
            index_name="event_id-index",
            partition_key=dynamodb.Attribute(name="event_id", type=dynamodb.AttributeType.STRING),
        )
        tickets_table = dynamodb.Table(
            self, "TicketsTable",
            partition_key=dynamodb.Attribute(name="ticket_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        tickets_table.add_global_secondary_index(
            index_name="order_id-index",
            partition_key=dynamodb.Attribute(name="order_id", type=dynamodb.AttributeType.STRING),
        )
        discount_codes_table = dynamodb.Table(
            self, "DiscountCodesTable",
            partition_key=dynamodb.Attribute(name="code", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        waitlist_table = dynamodb.Table(
            self, "WaitlistTable",
            partition_key=dynamodb.Attribute(name="waitlist_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        return {
            "events": events_table,
            "orders": orders_table,
            "tickets": tickets_table,
            "discount_codes": discount_codes_table,
            "waitlist": waitlist_table,
        }

    def _create_auth(self, domain_prefix: str, site_url: str):
        user_pool = cognito.UserPool(
            self, "AdminUserPool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            password_policy=cognito.PasswordPolicy(min_length=12),
            removal_policy=RemovalPolicy.RETAIN,
        )
        user_pool_domain = user_pool.add_domain(
            "AdminHostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
        )
        # generate_secret=False: the app uses the authorization-code flow with
        # PKCE (Task 8), so there is no client secret to smuggle into a Lambda
        # environment variable and therefore none to leak via CloudFormation.
        user_pool_client = user_pool.add_client(
            "AdminUserPoolClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"{site_url}/admin/callback"],
                logout_urls=[f"{site_url}/"],
            ),
        )
        return user_pool, user_pool_client, user_pool_domain

    def _create_custom_domain(self, http_api, domain_name: str, hosted_zone_id: str) -> None:
        zone = route53.HostedZone.from_hosted_zone_attributes(
            self, "HostedZone",
            hosted_zone_id=hosted_zone_id,
            zone_name=".".join(domain_name.split(".")[-2:]),
        )
        certificate = acm.Certificate(
            self, "SiteCertificate",
            domain_name=domain_name,
            validation=acm.CertificateValidation.from_dns(zone),
        )
        api_domain = apigwv2.DomainName(
            self, "ApiDomainName", domain_name=domain_name, certificate=certificate
        )
        apigwv2.ApiMapping(self, "ApiMapping", api=http_api, domain_name=api_domain)
        route53.ARecord(
            self, "ApiAliasRecord",
            zone=zone,
            record_name=domain_name,
            target=route53.RecordTarget.from_alias(
                route53_targets.ApiGatewayv2DomainProperties(
                    api_domain.regional_domain_name, api_domain.regional_hosted_zone_id
                )
            ),
        )
```

*The SES policy is scoped with a `ses:FromAddress` condition rather than left as a bare `Resource: "*"`, so a bug in the app cannot send mail as an arbitrary identity in the account.*

*`point_in_time_recovery=True` on the three tables that hold money-adjacent records is within the free tier's storage allowance at this scale and is the difference between a recoverable mistake and a permanent one.*

- [ ] **Step 2: Verify synth**

Ensure Docker is running, then:
```bash
cd infra
cdk synth \
  -c site_url=https://example.invalid \
  -c cognito_domain_prefix=jesters-rodeo-admin-test \
  -c ses_sender_email=noreply@example.com
```
Expected: synthesizes without errors. The first run pulls the Python 3.12 bundling image and pip-installs the dependencies, so allow a few minutes; later runs reuse the cached asset.

- [ ] **Step 3: Confirm the bundled artifact actually contains the dependencies**

```bash
cd infra
ls cdk.out/asset.*/ | head -20
test -d cdk.out/asset.*/fastapi && echo "dependencies bundled OK"
test -d cdk.out/asset.*/app && echo "application code bundled OK"
```
Expected: both echo. If `fastapi/` is missing, the function will fail at import with `ModuleNotFoundError` the first time it is invoked — and nothing before this point would have caught it.

- [ ] **Step 4: Commit**

```bash
git add infra/jesters_rodeo_stack.py
git commit -m "Add full CDK infrastructure with bundled Lambda assets and Cognito Hosted UI"
```

---

## Task 13: Stripe Keys and Session Secret in SSM Parameter Store

**Files:**
- Modify: `app/config.py`
- Modify: `infra/jesters_rodeo_stack.py`

**Interfaces:**
- Produces: `app.config.settings` now resolves `stripe_secret_key`, `stripe_webhook_secret`, `stripe_publishable_key`, and `session_secret` from SSM Parameter Store at Lambda cold start when `SECURE_PARAM_PREFIX` is set, falling back to plain environment variables otherwise (locally, and under pytest).
- No field is removed or renamed. Task 1's `Settings` is extended, not rewritten.

**Why Parameter Store rather than Secrets Manager.** SecureString parameters are free; Secrets Manager bills about $0.40 per secret per month, which is comparable to the entire rest of this system's running cost. Rotation and cross-account sharing would justify the price, and this app needs neither.

**Why the parameters are created out-of-band.** Defining them in CDK means either a plaintext placeholder in the synthesized CloudFormation template or the real key in source control. Creating them with the AWS CLI keeps the secret out of both, and the stack only ever refers to them by path.

- [ ] **Step 1: Extend `app/config.py` — add, do not replace**

Keep the existing `Settings` class exactly as written in Task 1 and add the loader below it. The whole point of the additive shape is that a field cannot go missing: an earlier draft of this plan rewrote the class wholesale and silently dropped `base_url`, which broke every checkout with an `AttributeError` while the test suite it claimed to run would have caught it.

```python
import os
from typing import Any

import boto3
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_publishable_key: str = ""
    events_table: str
    orders_table: str
    tickets_table: str
    discount_codes_table: str
    waitlist_table: str
    ses_sender_email: str
    cognito_user_pool_id: str
    cognito_app_client_id: str
    cognito_domain: str = ""
    session_cookie_name: str = "jr_session"
    session_secret: str = "dev-only-insecure-secret"
    aws_region: str = "us-east-1"
    base_url: str = "http://localhost:8000"


SECURE_FIELDS = (
    "stripe_secret_key",
    "stripe_webhook_secret",
    "stripe_publishable_key",
    "session_secret",
)


def _load_secure_params() -> dict[str, str]:
    """Fetch SecureString parameters in one call, or nothing when unset.

    `SECURE_PARAM_PREFIX` is set only by the CDK stack, so locally and under
    pytest this short-circuits and the plain environment variables win.
    """
    prefix = os.environ.get("SECURE_PARAM_PREFIX")
    if not prefix:
        return {}
    client = boto3.client("ssm")
    resp = client.get_parameters(
        Names=[f"{prefix}/{name}" for name in SECURE_FIELDS], WithDecryption=True
    )
    return {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in resp["Parameters"]}


def _apply_secure_params(target: Settings) -> None:
    for key, value in _load_secure_params().items():
        if value and hasattr(target, key):
            setattr(target, key, value)


settings = Settings()
_apply_secure_params(settings)
```

- [ ] **Step 2: Grant both functions read access in the CDK stack**

In `infra/jesters_rodeo_stack.py`, after `common_env` is defined and before the two `_lambda.Function` constructions, add:
```python
        secure_param_prefix = f"/jesters-rodeo/{self.stack_name}"
        common_env["SECURE_PARAM_PREFIX"] = secure_param_prefix
```

Then after both functions exist, alongside the other grants:
```python
        for function in (app_lambda, cleanup_lambda):
            function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["ssm:GetParameters", "ssm:GetParameter"],
                    resources=[
                        f"arn:aws:ssm:{self.region}:{self.account}:parameter"
                        f"{secure_param_prefix}/*"
                    ],
                )
            )
            function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["kms:Decrypt"],
                    resources=["*"],
                    conditions={
                        "StringEquals": {"kms:ViaService": f"ssm.{self.region}.amazonaws.com"}
                    },
                )
            )

        CfnOutput(self, "SecureParamPrefix", value=secure_param_prefix)
```

*Both functions, not just the app function. The cleanup handler imports `app.db`, which imports `app.config`, which runs the SSM lookup at module import — granting only the app function means every scheduled cleanup invocation dies with `AccessDeniedException` before it does any work, and nothing in the test suite would reveal it.*

- [ ] **Step 3: Create the parameters (one-time, per environment)**

These are real credentials, so they are created by hand and never committed. Start in Stripe **test** mode:
```bash
STACK=JestersRodeoStack
PREFIX="/jesters-rodeo/${STACK}"

aws ssm put-parameter --type SecureString --overwrite \
  --name "${PREFIX}/stripe_secret_key"      --value "sk_test_..."
aws ssm put-parameter --type SecureString --overwrite \
  --name "${PREFIX}/stripe_publishable_key" --value "pk_test_..."
aws ssm put-parameter --type SecureString --overwrite \
  --name "${PREFIX}/stripe_webhook_secret"  --value "whsec_..."
aws ssm put-parameter --type SecureString --overwrite \
  --name "${PREFIX}/session_secret"         --value "$(openssl rand -base64 48)"
```

*Rotating any of these takes effect on the next Lambda cold start; force one immediately with an empty configuration update if you need it applied now.*

- [ ] **Step 4: Run the full test suite to confirm no regressions**

Run: `pytest -v`
Expected: All tests still PASS. `SECURE_PARAM_PREFIX` is unset locally, so `_load_secure_params` returns `{}` and the dummy values from `tests/conftest.py` remain in effect.

- [ ] **Step 5: Verify CDK synth still succeeds**

```bash
cd infra
cdk synth \
  -c site_url=https://example.invalid \
  -c cognito_domain_prefix=jesters-rodeo-admin-test \
  -c ses_sender_email=noreply@example.com
```
Expected: synthesizes without errors.

- [ ] **Step 6: Confirm no secret material is in the synthesized template**

```bash
cd infra
grep -iE "sk_(test|live)|whsec_|pk_(test|live)" cdk.out/*.template.json && \
  echo "FAIL: secret material in template" || echo "OK: no secrets in template"
```
Expected: `OK: no secrets in template`.

- [ ] **Step 7: Commit**

```bash
git add app/config.py infra/jesters_rodeo_stack.py
git commit -m "Load Stripe keys and session secret from SSM Parameter Store"
```

---

## Task 14: Deployment & Manual AWS Console Setup Steps

**Files:**
- Create: `docs/DEPLOYMENT.md`

This task has no automated tests — it documents the one-time manual steps outside of code (AWS account setup, domain, SES production access, Stripe dashboard configuration) so next year's deploy is repeatable.

- [ ] **Step 1: Write `docs/DEPLOYMENT.md`**

````markdown
# Deployment Guide

## Prerequisites
1. An AWS account, with the AWS CLI configured (`aws configure`) for an IAM
   principal that can deploy CloudFormation.
2. Node and the CDK CLI: `npm install -g aws-cdk`
3. **Docker running.** The Lambda asset is built by pip-installing
   `requirements.txt` inside the Python 3.12 build image. Without Docker,
   `cdk deploy` fails at the bundling step.
4. One-time CDK bootstrap: `cd infra && cdk bootstrap`

## Required context values

Every deploy needs three, supplied as `-c key=value` or committed to
`infra/cdk.json` (none of them are secret):

| Key | Meaning |
| --- | --- |
| `site_url` | Public base URL, no trailing slash |
| `cognito_domain_prefix` | Globally unique Hosted UI prefix, e.g. `jesters-rodeo-admin` |
| `ses_sender_email` | The From address for confirmation emails |

Two more are optional and must be supplied together to enable a custom domain:
`domain_name` and `hosted_zone_id`.

## The chicken-and-egg on `site_url`

Cognito needs the callback URL before the API exists, so `site_url` is an input
rather than something the stack derives. That gives two paths:

- **With a custom domain (recommended):** you already know the URL. Create the
  Route 53 hosted zone first, note its zone ID, and deploy once.
- **Without one:** deploy with a placeholder, read the `ApiUrl` output, then
  deploy a second time with `-c site_url=<that URL>`. Only the second deploy
  produces a working login.

## First deploy

```bash
cd infra
source .venv/bin/activate
cdk deploy \
  -c site_url=https://register.example.com \
  -c cognito_domain_prefix=jesters-rodeo-admin \
  -c ses_sender_email=noreply@example.com \
  -c domain_name=register.example.com \
  -c hosted_zone_id=Z0123456789ABCDEFGHIJ
```

Note the `ApiUrl`, `SiteUrl`, `UserPoolId`, `UserPoolClientId`, `CognitoDomain`,
and `SecureParamPrefix` outputs.

## Secrets (Task 13)

Create the four SecureString parameters under `SecureParamPrefix` before the
first real checkout. Start with Stripe **test** keys.

## SES production access

New accounts start in the SES sandbox and can only send to verified addresses.
Before go-live:
1. Confirm the `ses_sender_email` identity (CDK requests verification; click the
   link in the email AWS sends).
2. In the SES console choose **Request production access**, describing the use
   case as transactional order-confirmation email for an event registration
   site. It is free and usually approved within a day.

Until this is granted, only verified recipients receive tickets — which is fine
for testing and fatal on sale day, so do it early.

## Stripe setup

1. Create a Stripe account and start in test mode.
2. Add a webhook endpoint at `https://<site_url>/webhooks/stripe`, subscribed to
   **`checkout.session.completed`** and **`checkout.session.expired`**. Both are
   required: without the expiry event, abandoned checkouts hold their seats
   until the cleanup job catches them.
3. Copy the signing secret into the `stripe_webhook_secret` parameter.
4. Complete a full test purchase before switching to live keys. Going live means
   new live-mode keys *and* a new live-mode webhook endpoint with its own
   signing secret — the test-mode secret will not validate live events.

## Create the first admin user

Cognito self-signup is disabled, so admins are created explicitly:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <UserPoolId> \
  --username admin@example.com \
  --user-attributes Name=email,Value=admin@example.com Name=email_verified,Value=true \
  --temporary-password 'ChangeMe-Temp-123!'
```

Then visit `<site_url>/admin/login`, which redirects to the Cognito Hosted UI.
You will be prompted to set a permanent password on first sign-in, and land back
on `/admin/events` with a session cookie.

## Pre-event checklist (run before opening registration each year)

1. Create the new year's event in the admin dashboard; opening it automatically
   closes last year's.
2. Confirm Stripe is in the intended mode and the webhook endpoint points at the
   current `site_url`.
3. Run a full test purchase end to end: checkout → webhook → confirmation email
   with a scannable QR code.
4. Scan that ticket at `/admin/checkin?event_id=...` and confirm a second scan
   reports "already checked in".
5. Refund the test order and confirm the ticket then reports "voided" at
   check-in and capacity is returned.
6. Export the CSV and open it in a spreadsheet.
7. Switch to live Stripe keys, then open registration.
````

- [ ] **Step 2: Commit**

```bash
git add docs/DEPLOYMENT.md
git commit -m "Add deployment and manual AWS/Stripe setup documentation"
```

---

## Verification (End-to-End)

After Task 14, verify the whole system works together.

**1. Full automated suite**

Run `pytest -v` from the repo root. Expect every test to pass across
`tests/test_db.py`, `test_pricing.py`, `test_tickets.py`, `test_emails.py`,
`test_auth.py`, `test_public_routes.py`, `test_webhooks.py`,
`test_admin_routes.py`, and `test_cleanup.py`.

**2. Synthesis and packaging**

```bash
cd infra
cdk synth -c site_url=https://example.invalid \
          -c cognito_domain_prefix=jr-verify-test \
          -c ses_sender_email=noreply@example.com
test -d cdk.out/asset.*/fastapi && echo "dependencies present"
grep -iE "sk_(test|live)|whsec_" cdk.out/*.template.json || echo "no secrets in template"
```

**3. Deploy to a test account and walk the flow**

Following `docs/DEPLOYMENT.md`, deploy and then check each of these by hand.
The first four are the behaviours that unit tests can only approximate, because
they involve Stripe's real webhook delivery and a real phone camera:

- Sign in at `/admin/login`, confirming the Hosted UI redirect and the return to
  `/admin/events`. Then delete the session cookie and confirm `/admin/events`
  bounces you back to login.
- Create and open an event; confirm `/` renders it at the right price.
- Complete a test-mode checkout with card `4242 4242 4242 4242`. Confirm the
  order flips to `paid` in CloudWatch logs and the dashboard, and that the
  confirmation email arrives with a scannable QR code.
- **Apply a discount code and verify the Stripe dashboard shows the discounted
  amount**, matching `total_cents` on the order. This is the check that catches
  a regression in how the checkout session is priced.
- Start a checkout and abandon it. Confirm the seat is released within about an
  hour — by the `checkout.session.expired` webhook, or failing that by the
  15-minute cleanup rule — and that the order reads `expired`, not deleted.
- Scan the ticket at `/admin/checkin?event_id=...` from a phone; scan again and
  confirm "already checked in".
- Refund the order. Confirm capacity returns, and that re-scanning the refunded
  ticket now reports "voided" rather than admitting the guest.
- Scan a ticket belonging to a different event and confirm "wrong event".
- Export the CSV and open it in a spreadsheet application.

**4. Tear down or keep**

`cdk destroy` removes the stack, but the DynamoDB tables and the user pool are
`RETAIN` by design and survive it — delete them by hand if you truly want them
gone. Leaving a test deploy running costs effectively nothing beyond the Route 53
hosted zone, so keeping it as a staging environment is a reasonable choice.
