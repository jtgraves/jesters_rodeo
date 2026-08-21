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
