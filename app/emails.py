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

    _ses().send_raw_email(
        Source=settings.ses_sender_email,
        RawMessage={"Data": msg.as_string().encode("utf-8")},
    )


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

    _ses().send_raw_email(
        Source=settings.ses_sender_email,
        RawMessage={"Data": msg.as_string().encode("utf-8")},
    )
