import io
import qrcode


def generate_qr_code_png(value: str) -> bytes:
    img = qrcode.make(value)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
