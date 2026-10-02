import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from app.config import settings
from app.models import Order, Ticket
from app.tickets import generate_qr_code_png

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
    msg["Subject"] = "Your Jester's Reaux-de-Eaux tickets"
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

    _send_mime_message(msg, order.buyer_email)


def send_announcement_email(to_email: str, subject: str, body: str) -> None:
    """A plain admin-composed blast -- no attachments, so unlike the
    confirmation email this needs only multipart/alternative, not a related
    part nesting it.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.ses_sender_email
    msg["To"] = to_email

    html_body = escape(body).replace("\n", "<br>")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(f"<html><body>{html_body}</body></html>", "html", "utf-8"))

    _send_mime_message(msg, to_email)
