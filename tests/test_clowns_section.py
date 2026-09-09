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


def test_parse_years_ranges_and_dedup():
    assert admin_routes._parse_years("2018-2021, 2023 2023") == [2018, 2019, 2020, 2021, 2023]
    assert admin_routes._parse_years("") == []


def test_manage_sets_official_fields(dynamodb_tables):
    _put_profile("clown_m", "Mo", [])
    c, ctx = _client(admin=True)
    try:
        resp = c.post("/admin/clowns/manage/clown_m", data={
            "years_ridden": "2019-2021", "is_lieutenant": "1",
            "lieutenant_title": "  Float 2 Lieutenant  ", "active": "1",
        }, follow_redirects=False)
        assert resp.status_code == 303
    finally:
        ctx.stop()
    p = CLOWN_PROFILES().get_item(Key={"clown_id": "clown_m"})["Item"]
    assert [int(y) for y in p["years_ridden"]] == [2019, 2020, 2021]
    assert p["is_lieutenant"] is True
    assert p["lieutenant_title"] == "Float 2 Lieutenant"
    assert p["active"] is True


def test_manage_add_historical_rider(dynamodb_tables):
    c, ctx = _client(admin=True)
    try:
        c.post("/admin/clowns/manage", data={"display_name": "  Old Timer  ",
               "years_ridden": "2010 2011"}, follow_redirects=False)
    finally:
        ctx.stop()
    p = CLOWN_PROFILES().scan()["Items"][0]
    assert p["display_name"] == "Old Timer"
    assert p.get("cognito_sub") is None
    assert p["active"] is False
    assert [int(y) for y in p["years_ridden"]] == [2010, 2011]


def test_manage_link_and_unlink_account(dynamodb_tables):
    _put_profile("clown_h", "Hist", [2012], cognito_sub=None, email="hist@example.com")
    fake = type("C", (), {"list_users": lambda self, **kw: {"Users": [
        {"Username": "sub-hist", "Attributes": [{"Name": "email", "Value": "hist@example.com"}]}
    ]}})()
    c, ctx = _client(admin=True)
    try:
        with patch("app.routes.admin._cognito", return_value=fake):
            c.post("/admin/clowns/manage/clown_h/link", data={"email": "hist@example.com"},
                   follow_redirects=False)
        assert CLOWN_PROFILES().get_item(Key={"clown_id": "clown_h"})["Item"]["cognito_sub"] == "sub-hist"
        c.post("/admin/clowns/manage/clown_h/unlink", follow_redirects=False)
        assert CLOWN_PROFILES().get_item(Key={"clown_id": "clown_h"})["Item"].get("cognito_sub") is None
    finally:
        ctx.stop()


def test_manage_is_admin_only(dynamodb_tables):
    c, ctx = _client(admin=False)
    try:
        resp = c.get("/admin/clowns/manage", headers={"accept": "text/html"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/orders"
    finally:
        ctx.stop()


# ---- Resources links ----

def _put_link(link_id, label="Roster sheet", url="https://docs.google.com/x", sort_order=0):
    KREWE_LINKS().put_item(Item={
        "link_id": link_id, "label": label, "url": url, "description": None,
        "sort_order": sort_order, "created_at": "2026-01-01T00:00:00Z",
    })


def test_resources_member_view_has_no_admin_controls(dynamodb_tables):
    _put_link("lnk_1", label="Throw budget")
    c, ctx = _client(admin=False)
    try:
        resp = c.get("/admin/clowns/resources")
        assert "Throw budget" in resp.text
        assert 'href="https://docs.google.com/x"' in resp.text
        assert 'action="/admin/clowns/resources/lnk_1"' not in resp.text  # no edit form
    finally:
        ctx.stop()


def test_resources_admin_can_add_edit_move_delete(dynamodb_tables):
    c, ctx = _client(admin=True)
    try:
        c.post("/admin/clowns/resources", data={"label": "A", "url": "https://a.example",
               "description": "first"}, follow_redirects=False)
        c.post("/admin/clowns/resources", data={"label": "B", "url": "https://b.example",
               "description": ""}, follow_redirects=False)
        ids = [i["link_id"] for i in sorted(KREWE_LINKS().scan()["Items"],
                                            key=lambda x: int(x["sort_order"]))]
        c.post(f"/admin/clowns/resources/{ids[1]}/move", data={"direction": "up"},
               follow_redirects=False)
        order = {i["link_id"]: int(i["sort_order"]) for i in KREWE_LINKS().scan()["Items"]}
        assert order[ids[1]] == 0 and order[ids[0]] == 1
        c.post(f"/admin/clowns/resources/{ids[0]}", data={"label": "A2",
               "url": "https://a2.example", "description": "x"}, follow_redirects=False)
        assert KREWE_LINKS().get_item(Key={"link_id": ids[0]})["Item"]["label"] == "A2"
        c.post(f"/admin/clowns/resources/{ids[0]}/delete", follow_redirects=False)
        remaining = KREWE_LINKS().scan()["Items"]
        assert len(remaining) == 1 and int(remaining[0]["sort_order"]) == 0
    finally:
        ctx.stop()


def test_resources_add_rejects_bad_url(dynamodb_tables):
    c, ctx = _client(admin=True)
    try:
        resp = c.post("/admin/clowns/resources", data={"label": "X", "url": "docs.google.com/x",
               "description": ""}, follow_redirects=False)
        assert resp.status_code == 400
        assert KREWE_LINKS().scan()["Items"] == []
    finally:
        ctx.stop()


def test_resources_mutations_are_admin_only(dynamodb_tables):
    c, ctx = _client(admin=False)
    try:
        resp = c.post("/admin/clowns/resources", data={"label": "X", "url": "https://x.example",
               "description": ""}, headers={"accept": "application/json"})
        assert resp.status_code == 403
    finally:
        ctx.stop()
