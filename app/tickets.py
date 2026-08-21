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
