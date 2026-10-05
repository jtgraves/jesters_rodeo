from __future__ import annotations

import hashlib
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import settings
from app.pricing import current_ticket_price_cents, price_increase_is_upcoming
from app.richtext import render_richtext, render_richtext_inline

templates = Jinja2Templates(directory="app/templates")


def _format_phone(value: str | None) -> str:
    """Render a US number as (###)###-####. Anything that isn't a plain
    10-digit number (11 with a leading country code) is shown as the admin
    typed it rather than mangled."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}){digits[3:6]}-{digits[6:]}"
    return value


templates.env.filters["phone"] = _format_phone


def _ordinal(value: object) -> str:
    n = int(value)
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


templates.env.filters["ordinal"] = _ordinal
templates.env.filters["richtext"] = render_richtext
templates.env.filters["richtext_inline"] = render_richtext_inline


def _format_datetime(value: object) -> str:
    """Just the date and a 12-hour time -- no seconds, no timezone offset,
    no microseconds. Accepts either an ISO 8601 string (how created_at is
    stored on every DynamoDB item) or a datetime object (how boto3 hands
    back Cognito's UserCreateDate on the clown management page)."""
    if not value:
        return ""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.strftime("%b %d, %Y %I:%M %p")


templates.env.filters["datetime"] = _format_datetime


def _format_event_date(value: str | None) -> str:
    """Month-day-year for display (e.g. 'Feb 05, 2027') -- event.date is
    stored as a plain YYYY-MM-DD string (see the admin form's "YYYY-MM-DD"
    placeholder, chosen for unambiguous data entry, not display -- the
    stored format is untouched, only how it's shown). Falls back to
    whatever was typed if it doesn't parse as that, same defensive pattern
    as the phone filter, rather than erroring the page over a stray value.
    """
    if not value:
        return ""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return value
    return parsed.strftime("%b %d, %Y")


templates.env.filters["eventdate"] = _format_event_date


def _shuffled(items: list) -> list:
    """A fresh, randomly-ordered copy -- doesn't mutate `items`, unlike
    random.shuffle (which also returns None, unusable as a Jinja filter)."""
    return random.sample(items, len(items))


templates.env.filters["shuffled"] = _shuffled

# A callable, not a value: registered once at import, but auth.py refreshes
# settings.stripe_mode on every admin page view, so calling this at render
# time (stripe_mode() in a template) reads the current mode, not a snapshot
# from whenever this module happened to be imported.
templates.env.globals["stripe_mode"] = lambda: settings.stripe_mode

# Both wrap the pure functions in pricing.py with "today", read fresh on
# every call (not fixed at import time) so a render right at midnight on the
# increase date sees the correct side of it.
templates.env.globals["current_ticket_price_cents"] = lambda event: current_ticket_price_cents(
    event, datetime.now(timezone.utc).date().isoformat()
)
templates.env.globals["price_increase_is_upcoming"] = lambda event: price_increase_is_upcoming(
    event, datetime.now(timezone.utc).date().isoformat()
)

# Cache-busting token for /static/style.css. The <link> URL is otherwise
# identical across deploys, so a browser that cached the stylesheet can serve
# a stale copy indefinitely; keying it to the file's bytes forces a re-fetch
# only when the CSS actually changed.
_css = Path("app/static/style.css")
templates.env.globals["static_version"] = (
    hashlib.sha1(_css.read_bytes()).hexdigest()[:8] if _css.exists() else "dev"
)
