from __future__ import annotations

import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

from app.config import settings
from app.models import Event, Order
from app.tickets import generate_qr_code_png

# A bare address in From: ("jestersrodeo@gmail.com") reads more like an
# automated/anonymous sender -- to both spam filters and a human skimming
# their inbox -- than a properly named one does. formataddr handles the
# RFC 2822 quoting/encoding correctly (e.g. if this name ever needs a comma
# or non-ASCII character), which naive string formatting wouldn't.
FROM_DISPLAY_NAME = "Jester's Reaux-de-Eaux"

# Gmail's SMTP submission endpoint -- see docs/DEPLOYMENT.md for the account
# setup (2-Step Verification + an App Password) this authenticates with.
# Implicit TLS (port 465), not STARTTLS (587) -- tried while chasing an
# earlier production failure here that turned out to be unrelated to the
# port at all (see _send_mime_message's password-cleaning comment below for
# the actual root cause). Left on 465 since it works fine and there's no
# reason to churn back to 587.
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def reset_clients() -> None:
    """No-op kept for the test fixtures that already call it between cases
    (tests/test_emails.py, tests/test_send_announcement.py): _send_mime_message
    opens a fresh connection per call rather than caching one (see its own
    docstring for why), so there's nothing left to actually reset."""


def _send_mime_message(msg: MIMEMultipart, to_email: str) -> None:
    """Open a connection, authenticate, send, close -- every call, no cached
    connection reused across sends.

    This app sends at most a few emails a minute even at its busiest (a
    ticket confirmation per completed Stripe webhook; an announcement blast
    paces itself at one send per SEND_PACE_SECONDS), always from an async
    webhook/Lambda handler, never in a buyer's synchronous request path --
    so the ~200-300ms a fresh TLS handshake costs is not a latency problem
    worth caching a connection to avoid. Caching one instead would mean
    handling it going stale: Gmail closes idle SMTP connections after a few
    minutes, and a warm Lambda execution environment can easily sit between
    sends for longer than that, so a cached connection would intermittently
    fail with SMTPServerDisconnected on otherwise-healthy sends. Opening
    fresh each time sidesteps that whole failure mode.
    """
    # Google displays an App Password as four space-separated groups (e.g.
    # "abcd efgh ijkl mnop") for readability; copying it off that page can
    # carry a NON-BREAKING space (U+00A0), not a plain one, into wherever
    # it's pasted -- confirmed in production as the actual root cause of an
    # SMTPServerDisconnected failure that looked, for a while, like Gmail
    # rejecting connections from this Lambda's IP entirely: smtplib's own
    # base64-encoding step chokes on that character with a bare
    # UnicodeEncodeError client-side if it's plain ASCII-unsafe, but a
    # subtly malformed AUTH payload otherwise just gets the connection
    # dropped by Gmail's server, with nothing in the error pointing at "your
    # password has a stray character in it". split()+join() removes every
    # whitespace run (not just leading/trailing, and not just plain spaces)
    # wherever it falls, so this is safe regardless of which kind of
    # whitespace ended up in there or where.
    password = "".join(settings.smtp_password.split())
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
        smtp.login(settings.ses_sender_email, password)
        smtp.sendmail(settings.ses_sender_email, [to_email], msg.as_string())


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _event_details_sections(event: Event | None, base_url: str) -> tuple[list[str], list[str]]:
    """Plain-text lines and HTML fragments describing the event itself --
    what it is, when, where -- so the email makes sense on its own without
    the reader needing to already know what this order was for. Tolerates
    event being None (e.g. the event was deleted after the order was
    placed) by simply omitting this section rather than failing the send.
    """
    if event is None:
        return [], []

    text_lines = [
        event.name,
        event.date,
        event.address or event.location,
    ]
    html_parts = [
        '<div style="margin:16px 0;padding:16px;background:#f7f7f7;border-radius:8px">',
    ]
    if event.logo_url:
        # Only max-height was constrained here before -- fine in a browser,
        # but confirmed stretched/warped in iOS Mail: several mail-client
        # rendering engines don't reliably infer the other axis from the
        # image's own aspect ratio when just one dimension is constrained,
        # and can default to stretching to the container's full width while
        # still honoring the height clamp. Constraining both axes (plus the
        # auto pair, belt-and-suspenders for engines that only honor one
        # form) is the standard email-HTML fix.
        html_parts.append(
            f'<div style="text-align:center;margin-bottom:12px">'
            f'<img src="{escape(event.logo_url)}" alt="" '
            f'style="max-height:80px;max-width:300px;width:auto;height:auto" '
            f'height="80"></div>'
        )
    html_parts.append(f"<p><strong>{escape(event.name)}</strong></p>")
    html_parts.append(f"<p>\U0001f5d3 {escape(event.date)}</p>")
    html_parts.append(f"<p>\U0001f4cd {escape(event.address or event.location)}</p>")

    if event.timeline:
        text_lines.append("")
        text_lines.append("Timeline:")
        html_parts.append("<p><strong>Timeline:</strong></p><ul>")
        for item in event.timeline:
            time, activity = item.get("time"), item.get("activity", "")
            line = f"{time} - {activity}" if time else activity
            text_lines.append(f"- {line}")
            html_parts.append(f"<li>{escape(time) + ' - ' if time else ''}{escape(activity)}</li>")
        html_parts.append("</ul>")

    html_parts.append("</div>")
    text_lines.append("")
    text_lines.append(f"Full event details: {base_url}")
    html_parts.append(f'<p><a href="{escape(base_url)}">View event details</a></p>')
    return text_lines, html_parts


def _order_summary_sections(order: Order) -> tuple[list[str], list[str]]:
    """Plain-text lines and HTML fragments for the price breakdown -- always
    in dollars, never raw cents, matching how every other price is already
    shown to buyers (e.g. the web order-confirmation page)."""
    subtotal_cents = order.unit_price_cents * order.quantity
    text_lines = [
        f"{order.quantity} ticket(s) at {_dollars(order.unit_price_cents)} each"
        f" = {_dollars(subtotal_cents)}",
    ]
    html_parts = [
        "<div>",
        f"<p>{order.quantity} ticket(s) at {_dollars(order.unit_price_cents)} each"
        f" = {_dollars(subtotal_cents)}</p>",
    ]
    if order.processing_fee_cents:
        text_lines.append(f"Includes a {_dollars(order.processing_fee_cents)} card processing fee.")
        html_parts.append(f"<p>Includes a {_dollars(order.processing_fee_cents)} card processing fee.</p>")
    if order.donation_cents:
        text_lines.append(f"Includes a {_dollars(order.donation_cents)} donation. Thank you!")
        html_parts.append(f"<p>Includes a {_dollars(order.donation_cents)} donation. Thank you!</p>")
    text_lines.append(f"Total: {_dollars(order.total_cents)}")
    html_parts.append(f"<p><strong>Total: {_dollars(order.total_cents)}</strong></p>")
    html_parts.append("</div>")
    return text_lines, html_parts


def send_confirmation_email(order: Order, event: Event | None = None) -> None:
    # multipart/related
    #   +-- multipart/alternative
    #   |     +-- text/plain
    #   |     +-- text/html   (references the QR image below by cid:; the
    #   |                      event logo, if any, is a normal remote <img>
    #   |                      since it's already hosted in S3)
    #   +-- image/png (qr)
    #
    # The alternative part MUST be nested inside the related part. Attaching
    # text/plain and text/html as direct siblings of the image makes clients
    # treat them as two separate body parts to display, not as alternatives.
    msg = MIMEMultipart("related")
    msg["Subject"] = "Your Jester's Reaux-de-Eaux tickets"
    msg["From"] = formataddr((FROM_DISPLAY_NAME, settings.ses_sender_email))
    msg["To"] = order.buyer_email

    event_text, event_html = _event_details_sections(event, settings.base_url)
    order_text, order_html = _order_summary_sections(order)
    party_word = "person" if order.quantity == 1 else "people"

    text_lines = [
        f"Thanks, {order.buyer_name}! You're confirmed -- party of {order.quantity}.", "",
        *event_text, "",
        *order_text, "",
        f"Show this QR code at the door ({order.quantity} {party_word}, one scan):",
        f"Order ID: {order.order_id}",
    ]
    html_parts = [
        f"<p>Thanks, {escape(order.buyer_name)}! "
        f"You're confirmed &mdash; party of {order.quantity}.</p>",
        *event_html,
        *order_html,
        '<div style="text-align:center;margin-top:16px">'
        f"<p><strong>{escape(order.buyer_name)}</strong><br>"
        f"Party of {order.quantity}<br>"
        f"<code>{escape(order.order_id)}</code></p>"
        '<img src="cid:qr" alt="Check-in QR code" width="200" height="200">'
        "</div>",
    ]

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText("\n".join(text_lines), "plain", "utf-8"))
    alternative.attach(
        MIMEText("<html><body>" + "".join(html_parts) + "</body></html>", "html", "utf-8")
    )
    msg.attach(alternative)

    image = MIMEImage(generate_qr_code_png(order.order_id), _subtype="png")
    image.add_header("Content-ID", "<qr>")
    image.add_header("Content-Disposition", "inline", filename=f"{order.order_id}.png")
    msg.attach(image)

    _send_mime_message(msg, order.buyer_email)


def send_announcement_email(to_email: str, subject: str, body: str) -> None:
    """A plain admin-composed blast -- no attachments, so unlike the
    confirmation email this needs only multipart/alternative, not a related
    part nesting it.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((FROM_DISPLAY_NAME, settings.ses_sender_email))
    msg["To"] = to_email

    html_body = escape(body).replace("\n", "<br>")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(f"<html><body>{html_body}</body></html>", "html", "utf-8"))

    _send_mime_message(msg, to_email)
