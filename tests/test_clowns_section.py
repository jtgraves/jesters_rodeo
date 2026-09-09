from unittest.mock import patch

from app import auth
from app.config import settings
from app.db import CLOWN_PROFILES, KREWE_LINKS
from app.models import ClownProfile, KreweLink
from app.routes import admin as admin_routes
from fastapi.testclient import TestClient
from app.main import app


def test_clown_profile_defaults():
    p = ClownProfile(clown_id="clown_1", created_at="2026-01-01T00:00:00Z")
    assert p.cognito_sub is None
    assert p.email is None
    assert p.years_ridden == []
    assert p.is_lieutenant is False
    assert p.active is True


def test_krewe_link_defaults():
    lk = KreweLink(link_id="lnk_1", label="Roster sheet",
                   url="https://docs.google.com/x", created_at="2026-01-01T00:00:00Z")
    assert lk.sort_order == 0
    assert lk.description is None


def test_clown_tables_exist_and_start_empty(dynamodb_tables):
    assert CLOWN_PROFILES().scan()["Items"] == []
    assert KREWE_LINKS().scan()["Items"] == []


def test_my_profile_creates_a_linked_profile_on_first_call(dynamodb_tables):
    p = admin_routes._my_profile({"sub": "sub-abc", "email": "Rider@Example.com"})
    assert p["cognito_sub"] == "sub-abc"
    assert p["email"] == "Rider@Example.com"
    assert p["active"] is True
    assert p["years_ridden"] == []
    # idempotent
    again = admin_routes._my_profile({"sub": "sub-abc", "email": "Rider@Example.com"})
    assert again["clown_id"] == p["clown_id"]
    assert len(CLOWN_PROFILES().scan()["Items"]) == 1


def test_my_profile_links_an_unlinked_profile_by_email(dynamodb_tables):
    CLOWN_PROFILES().put_item(Item={
        "clown_id": "clown_hist", "cognito_sub": None, "email": "returning@example.com",
        "years_ridden": [2015, 2016], "is_lieutenant": False, "active": False,
        "created_at": "2026-01-01T00:00:00Z",
    })
    p = admin_routes._my_profile({"sub": "sub-new", "email": "RETURNING@example.com"})
    assert p["clown_id"] == "clown_hist"
    assert p["cognito_sub"] == "sub-new"
    stored = CLOWN_PROFILES().get_item(Key={"clown_id": "clown_hist"})["Item"]
    assert stored["cognito_sub"] == "sub-new"
    assert len(CLOWN_PROFILES().scan()["Items"]) == 1  # not duplicated


def _client(sub="member-1", email="member@example.com", admin=False):
    groups = ["admins"] if admin else []
    ctx = patch.object(auth, "verify_cognito_token",
                       return_value={"sub": sub, "email": email, "cognito:groups": groups})
    ctx.start()
    c = TestClient(app)
    c.cookies.set(settings.session_cookie_name, auth.issue_session("fake-id-token"))
    return c, ctx


def test_clowns_hub_creates_profile_and_renders(dynamodb_tables):
    c, ctx = _client()
    try:
        resp = c.get("/admin/clowns")
        assert resp.status_code == 200
        assert "Clowns" in resp.text
        assert 'href="/admin/clowns/roster"' in resp.text
        assert len(CLOWN_PROFILES().scan()["Items"]) == 1
    finally:
        ctx.stop()


def test_clowns_hub_nav_link_visible_to_members(dynamodb_tables):
    c, ctx = _client()
    try:
        resp = c.get("/admin/clowns")
        assert 'href="/admin/clowns"' in resp.text
    finally:
        ctx.stop()


def test_my_profile_edit_updates_only_my_row(dynamodb_tables):
    CLOWN_PROFILES().put_item(Item={
        "clown_id": "clown_other", "cognito_sub": "sub-other", "email": "o@example.com",
        "display_name": "Other", "years_ridden": [], "is_lieutenant": False, "active": True,
        "created_at": "2026-01-01T00:00:00Z",
    })
    c, ctx = _client(sub="sub-me", email="me@example.com")
    try:
        resp = c.post("/admin/clowns/profile", data={
            "display_name": "  Me the Clown  ", "bio": "Rode since forever.",
            "phone": "5045551234", "address": "", "emergency_contact_name": "Pat",
            "emergency_contact_phone": "5045559999",
        }, follow_redirects=False)
        assert resp.status_code == 303
    finally:
        ctx.stop()
    mine = next(p for p in CLOWN_PROFILES().scan()["Items"] if p["cognito_sub"] == "sub-me")
    assert mine["display_name"] == "Me the Clown"
    assert mine["bio"] == "Rode since forever."
    assert mine["emergency_contact_name"] == "Pat"
    other = CLOWN_PROFILES().get_item(Key={"clown_id": "clown_other"})["Item"]
    assert other["display_name"] == "Other"  # untouched


def test_my_profile_edit_cannot_set_official_fields(dynamodb_tables):
    c, ctx = _client(sub="sub-me", email="me@example.com")
    try:
        c.post("/admin/clowns/profile", data={
            "display_name": "Me", "bio": "", "phone": "", "address": "",
            "emergency_contact_name": "", "emergency_contact_phone": "",
            "years_ridden": "2019 2020", "is_lieutenant": "1", "active": "",
        }, follow_redirects=False)
    finally:
        ctx.stop()
    mine = next(p for p in CLOWN_PROFILES().scan()["Items"] if p["cognito_sub"] == "sub-me")
    assert mine["years_ridden"] == []
    assert mine["is_lieutenant"] is False
    assert mine["active"] is True


def _put_profile(clown_id, name, years, **extra):
    item = {
        "clown_id": clown_id, "cognito_sub": clown_id + "-sub", "email": clown_id + "@x.com",
        "display_name": name, "years_ridden": years,
        "is_lieutenant": False, "lieutenant_title": None, "active": True,
        "photo_url": None, "bio": None, "phone": None, "address": None,
        "emergency_contact_name": None, "emergency_contact_phone": None,
        "created_at": "2026-01-01T00:00:00Z",
    }
    item.update(extra)
    CLOWN_PROFILES().put_item(Item=item)


def test_roster_defaults_to_latest_event_year_and_filters(dynamodb_tables):
    from app.db import EVENTS
    EVENTS().put_item(Item={"event_id": "evt_2026", "year": 2026, "name": "x", "date": "d",
                            "location": "l", "description": "d", "ticket_price_cents": 1,
                            "capacity": 1, "tickets_sold_count": 0, "registration_open": False,
                            "status": "open"})
    _put_profile("clown_a", "Abby", [2024, 2025, 2026])
    _put_profile("clown_b", "Bo", [2020])          # not this year
    c, ctx = _client(admin=True)
    try:
        resp = c.get("/admin/clowns/roster")
        assert resp.status_code == 200
        assert "Abby" in resp.text
        assert "Bo" not in resp.text
        assert "3rd year" in resp.text
    finally:
        ctx.stop()


def test_roster_year_param_and_milestone(dynamodb_tables):
    _put_profile("clown_c", "Cyd", [2015, 2016, 2017, 2018, 2019])  # 5 years
    c, ctx = _client(admin=True)
    try:
        resp = c.get("/admin/clowns/roster?year=2017")
        assert "Cyd" in resp.text and "5-year rider" in resp.text
        empty = c.get("/admin/clowns/roster?year=1999")
        assert "Cyd" not in empty.text
    finally:
        ctx.stop()


def test_lieutenants_page_shows_only_lieutenants(dynamodb_tables):
    _put_profile("clown_lt", "Lou", [2025], is_lieutenant=True,
                 lieutenant_title="Float 3 Lieutenant", bio="Been steering since 2009.")
    _put_profile("clown_reg", "Reg", [2025])
    c, ctx = _client()
    try:
        resp = c.get("/admin/clowns/lieutenants")
        assert "Lou" in resp.text and "Float 3 Lieutenant" in resp.text
        assert "Been steering since 2009." in resp.text
        assert "Reg" not in resp.text
    finally:
        ctx.stop()


def test_directory_shows_active_contact_rows(dynamodb_tables):
    _put_profile("clown_x", "Xena", [2025], phone="5045551234", email="xena@x.com",
                 emergency_contact_name="Gabby", emergency_contact_phone="5045550000")
    _put_profile("clown_gone", "Gone", [2019], active=False, phone="5045559999")
    c, ctx = _client()
    try:
        resp = c.get("/admin/clowns/directory")
        assert "Xena" in resp.text and "xena@x.com" in resp.text
        assert "(504)555-1234" in resp.text
        assert "Gabby" in resp.text
        assert "Gone" not in resp.text
    finally:
        ctx.stop()
