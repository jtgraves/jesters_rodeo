from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi.templating import Jinja2Templates

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

# Cache-busting token for /static/style.css. The <link> URL is otherwise
# identical across deploys, so a browser that cached the stylesheet can serve
# a stale copy indefinitely; keying it to the file's bytes forces a re-fetch
# only when the CSS actually changed.
_css = Path("app/static/style.css")
templates.env.globals["static_version"] = (
    hashlib.sha1(_css.read_bytes()).hexdigest()[:8] if _css.exists() else "dev"
)
