import io
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError

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
    p = admin_routes._my_profile(
        {"sub": "sub-new", "email": "RETURNING@example.com", "email_verified": True}
    )
    assert p["clown_id"] == "clown_hist"
    assert p["cognito_sub"] == "sub-new"
    stored = CLOWN_PROFILES().get_item(Key={"clown_id": "clown_hist"})["Item"]
    assert stored["cognito_sub"] == "sub-new"
    assert len(CLOWN_PROFILES().scan()["Items"]) == 1  # not duplicated


def _cognito_fake(live_subs: set[str]):
    """A fake Cognito client whose admin_get_user raises UserNotFoundException
    for anything outside `live_subs` -- for exercising _cognito_sub_is_live
    without a real pool."""
    def admin_get_user(self, UserPoolId, Username):
        if Username in live_subs:
            return {"Username": Username}
        raise ClientError(
            {"Error": {"Code": "UserNotFoundException", "Message": "not found"}},
            "AdminGetUser",
        )
    return type("C", (), {"admin_get_user": admin_get_user})()


def test_my_profile_reclaims_a_profile_whose_sub_is_stale(dynamodb_tables):
    # The pool-migration scenario: a profile carries a cognito_sub from a
    # retired user pool. It LOOKS linked, but that sub no longer resolves in
    # the current pool -- reclaimable by email, same as no login at all.
    CLOWN_PROFILES().put_item(Item={
        "clown_id": "clown_stale", "cognito_sub": "sub-old-pool", "email": "mover@example.com",
        "years_ridden": [2020], "is_lieutenant": False, "active": True,
        "created_at": "2026-01-01T00:00:00Z",
    })
    with patch("app.routes.admin._cognito", return_value=_cognito_fake(set())):
        p = admin_routes._my_profile(
            {"sub": "sub-new-pool", "email": "mover@example.com", "email_verified": True}
        )
    assert p["clown_id"] == "clown_stale"
    assert p["cognito_sub"] == "sub-new-pool"
    assert len(CLOWN_PROFILES().scan()["Items"]) == 1  # not duplicated


def test_my_profile_does_not_reclaim_a_profile_whose_sub_is_still_live(dynamodb_tables):
    CLOWN_PROFILES().put_item(Item={
        "clown_id": "clown_taken", "cognito_sub": "sub-real", "email": "shared@example.com",
        "years_ridden": [2020], "is_lieutenant": False, "active": True,
        "created_at": "2026-01-01T00:00:00Z",
    })
    with patch("app.routes.admin._cognito", return_value=_cognito_fake({"sub-real"})):
        p = admin_routes._my_profile(
            {"sub": "sub-someone-else", "email": "shared@example.com", "email_verified": True}
        )
    assert p["clown_id"] != "clown_taken"  # a fresh profile, not stolen from the live one
    assert len(CLOWN_PROFILES().scan()["Items"]) == 2


def _client(sub="member-1", email="member@example.com", admin=False):
    groups = ["admins"] if admin else []
    ctx = patch.object(auth, "verify_cognito_token",
                       return_value={"sub": sub, "email": email, "cognito:groups": groups})
    ctx.start()
    c = TestClient(app)
    c.cookies.set(settings.session_cookie_name, auth.issue_session("fake-id-token"))
    return c, ctx


def test_clowns_landing_redirects_to_resources_and_creates_profile(dynamodb_tables):
    c, ctx = _client()
    try:
        resp = c.get("/admin/clowns", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/clowns/resources"
        assert len(CLOWN_PROFILES().scan()["Items"]) == 1
    finally:
        ctx.stop()


def test_clowns_nav_links_visible_to_members(dynamodb_tables):
    c, ctx = _client()
    try:
        resp = c.get("/admin/clowns/resources")
        assert 'href="/admin/clowns/resources"' in resp.text
        assert 'href="/admin/clowns/roster"' in resp.text
        assert 'href="/admin/clowns/manage"' not in resp.text
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


def test_my_profile_edit_can_set_years_ridden(dynamodb_tables):
    c, ctx = _client(sub="sub-me", email="me@example.com")
    try:
        c.post("/admin/clowns/profile", data={
            "display_name": "Me", "years_ridden": "2019 2021-2023", "bio": "", "phone": "",
            "address": "", "emergency_contact_name": "", "emergency_contact_phone": "",
        }, follow_redirects=False)
    finally:
        ctx.stop()
    mine = next(p for p in CLOWN_PROFILES().scan()["Items"] if p["cognito_sub"] == "sub-me")
    assert [int(y) for y in mine["years_ridden"]] == [2019, 2021, 2022, 2023]


def test_my_profile_edit_can_set_swag_name(dynamodb_tables):
    c, ctx = _client(sub="sub-me", email="me@example.com")
    try:
        c.post("/admin/clowns/profile", data={
            "display_name": "Jonathan Smith", "swag_name": "Jonny", "bio": "", "phone": "",
            "address": "", "emergency_contact_name": "", "emergency_contact_phone": "",
        }, follow_redirects=False)
    finally:
        ctx.stop()
    mine = next(p for p in CLOWN_PROFILES().scan()["Items"] if p["cognito_sub"] == "sub-me")
    assert mine["swag_name"] == "Jonny"


def test_my_profile_edit_cannot_set_lieutenant_or_active(dynamodb_tables):
    # years_ridden is now self-service (see above); lieutenant status and
    # active/inactive remain admin-only.
    c, ctx = _client(sub="sub-me", email="me@example.com")
    try:
        c.post("/admin/clowns/profile", data={
            "display_name": "Me", "bio": "", "phone": "", "address": "",
            "emergency_contact_name": "", "emergency_contact_phone": "",
            "is_lieutenant": "1", "active": "",
        }, follow_redirects=False)
    finally:
        ctx.stop()
    mine = next(p for p in CLOWN_PROFILES().scan()["Items"] if p["cognito_sub"] == "sub-me")
    assert mine["is_lieutenant"] is False
    assert mine["active"] is True


def _put_profile(clown_id, name, years, **extra):
    item = {
        "clown_id": clown_id, "cognito_sub": clown_id + "-sub", "email": clown_id + "@x.com",
        "display_name": name, "years_ridden": years,
        "is_lieutenant": False, "active": True,
        "photo_url": None, "bio": None, "phone": None, "address": None,
        "emergency_contact_name": None, "emergency_contact_phone": None,
        "created_at": "2026-01-01T00:00:00Z",
    }
    item.update(extra)
    CLOWN_PROFILES().put_item(Item=item)


def test_roster_defaults_to_latest_roster_year_even_without_a_matching_event(dynamodb_tables):
    # Roster data for a new year (e.g. bulk-imported ahead of time) can
    # exist before that year's Event record does -- the roster link must
    # reflect the roster's own highest year, not lag behind on the Events
    # table's. If the page defaulted to 2026 (the Events table's year),
    # 2026-only-rider would show and 2027-only-rider would not -- the
    # opposite of what's asserted here.
    from app.db import EVENTS
    EVENTS().put_item(Item={"event_id": "evt_2026", "year": 2026, "name": "x", "date": "d",
                            "location": "l", "description": "d", "ticket_price_cents": 1,
                            "capacity": 1, "tickets_sold_count": 0, "registration_open": False,
                            "status": "open"})
    _put_profile("clown_a", "Twenty Six Only", [2026])
    _put_profile("clown_b", "Twenty Seven Only", [2027])  # no Event record for 2027 yet
    c, ctx = _client(admin=True)
    try:
        resp = c.get("/admin/clowns/roster", follow_redirects=False)
        assert resp.status_code == 200
        assert "Twenty Seven Only" in resp.text
        assert "Twenty Six Only" not in resp.text
    finally:
        ctx.stop()


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
    _put_profile("clown_c", "Cyd", [2015, 2016, 2017, 2018, 2019])  # 5 years total
    c, ctx = _client(admin=True)
    try:
        # Tenure is as of the SELECTED year, not a running total: on their
        # 5th year overall, viewing an earlier year shows an earlier tenure.
        resp = c.get("/admin/clowns/roster?year=2017")
        assert "Cyd" in resp.text and "3rd year" in resp.text
        assert "5-year rider" not in resp.text
        latest = c.get("/admin/clowns/roster?year=2019")
        assert "5-year rider" in latest.text
        empty = c.get("/admin/clowns/roster?year=1999")
        assert "Cyd" not in empty.text
    finally:
        ctx.stop()


def test_roster_floats_lieutenants_to_top_with_a_badge(dynamodb_tables):
    _put_profile("clown_reg", "Amy", [2025])
    _put_profile("clown_lt", "Zeke", [2025], is_lieutenant=True)
    c, ctx = _client()
    try:
        resp = c.get("/admin/clowns/roster?year=2025")
        assert "Float Lieutenants" not in resp.text  # no separate section anymore
        assert resp.text.index("Zeke") < resp.text.index("Amy")  # lieutenant floats first
        assert "🎖 Float Lieutenant" in resp.text
    finally:
        ctx.stop()


def test_roster_inactive_lieutenant_is_not_badged_but_still_listed(dynamodb_tables):
    # An inactive former lieutenant still has a historical ride on record --
    # that stays in the year grid -- but isn't badged as if still serving.
    _put_profile("clown_gone_lt", "Gone", [2020], is_lieutenant=True, active=False)
    _put_profile("clown_reg", "Reg", [2020])
    c, ctx = _client()
    try:
        resp = c.get("/admin/clowns/roster?year=2020")
        assert "Gone" in resp.text  # still on the 2020 roster grid
        assert "Reg" in resp.text
        assert "🎖 Float Lieutenant" not in resp.text
    finally:
        ctx.stop()


def test_roster_card_links_to_directory_entry_for_active_riders(dynamodb_tables):
    _put_profile("clown_active", "Ann", [2025])
    c, ctx = _client()
    try:
        resp = c.get("/admin/clowns/roster?year=2025")
        assert 'href="/admin/clowns/directory#clown-clown_active"' in resp.text
    finally:
        ctx.stop()


def test_roster_card_is_not_a_link_for_inactive_riders(dynamodb_tables):
    # No directory row exists for an inactive rider (the directory only
    # lists active people) -- so their card doesn't link to a dead anchor.
    _put_profile("clown_hist", "Hank", [2020], active=False)
    c, ctx = _client()
    try:
        resp = c.get("/admin/clowns/roster?year=2020")
        assert "Hank" in resp.text
        assert "#clown-clown_hist" not in resp.text
    finally:
        ctx.stop()


def test_directory_row_has_a_stable_anchor_id(dynamodb_tables):
    _put_profile("clown_anchor", "Ida", [2025])
    c, ctx = _client()
    try:
        resp = c.get("/admin/clowns/directory")
        assert 'id="clown-clown_anchor"' in resp.text
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


def test_directory_lists_lieutenants_first_with_a_badge(dynamodb_tables):
    _put_profile("clown_a", "Amy", [2025])
    _put_profile("clown_z", "Zeke", [2025], is_lieutenant=True)
    c, ctx = _client()
    try:
        resp = c.get("/admin/clowns/directory")
        assert resp.text.index("Zeke") < resp.text.index("Amy")  # lieutenant sorts first
        assert "🎖 Float Lieutenant" in resp.text
    finally:
        ctx.stop()


def test_parse_years_ranges_and_dedup():
    assert admin_routes._parse_years("2018-2021, 2023 2023") == [2018, 2019, 2020, 2021, 2023]
    assert admin_routes._parse_years("") == []


def test_parse_bool_accepts_spreadsheet_checkbox_style_x():
    # "X" (any case) is at least as common a way to mark a checked
    # spreadsheet column as typing the word "yes".
    for truthy in ("x", "X", "1", "true", "TRUE", "yes", "y", "on"):
        assert admin_routes._parse_bool(truthy) is True, truthy
    for falsy in ("", "  ", "no", "n", "0", "false"):
        assert admin_routes._parse_bool(falsy) is False, falsy


def test_manage_sets_official_fields(dynamodb_tables):
    _put_profile("clown_m", "Mo", [])
    c, ctx = _client(admin=True)
    try:
        resp = c.post("/admin/clowns/manage/clown_m", data={
            "years_ridden": "2019-2021", "is_lieutenant": "1", "active": "1",
        }, follow_redirects=False)
        assert resp.status_code == 303
    finally:
        ctx.stop()
    p = CLOWN_PROFILES().get_item(Key={"clown_id": "clown_m"})["Item"]
    assert [int(y) for y in p["years_ridden"]] == [2019, 2020, 2021]
    assert p["is_lieutenant"] is True
    assert p["active"] is True


def test_manage_roster_page_lays_out_each_field_in_its_own_column(dynamodb_tables):
    # Regression guard: years/lieutenant/active/actions used to be crammed
    # into one flex-wrapping colspan=4 cell that didn't line up with the
    # header row at all. Each now gets its own <td>, joined to one logical
    # form via the HTML5 form="..." attribute rather than nesting.
    _put_profile("clown_m", "Mo", [2019, 2020, 2021])
    c, ctx = _client(admin=True)
    try:
        resp = c.get("/admin/clowns/manage")
    finally:
        ctx.stop()
    body = resp.text
    assert 'id="roster-clown_m"' in body
    assert 'class="roster-years-input"' in body
    assert body.count('form="roster-clown_m"') == 4  # lieutenant, active, Save, Delete
    assert "colspan=" not in body


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


def _csv(rows_text):
    return {"file": ("clowns.csv", io.BytesIO(rows_text.encode()), "text/csv")}


def test_import_creates_loginless_profile_for_unknown_email(dynamodb_tables):
    fake = type("C", (), {"list_users": lambda self, **kw: {"Users": []}})()
    c, ctx = _client(admin=True)
    try:
        with patch("app.routes.admin._cognito", return_value=fake):
            resp = c.post("/admin/clowns/import", files=_csv(
                "email,display_name,years_ridden\n"
                "ghost@example.com,Ghost Rider,2014-2016\n"
            ), data={"invite_missing": ""}, follow_redirects=False)
        assert resp.status_code == 200
        p = CLOWN_PROFILES().scan()["Items"][0]
        assert p["display_name"] == "Ghost Rider"
        assert p.get("cognito_sub") is None
        assert [int(y) for y in p["years_ridden"]] == [2014, 2015, 2016]
        assert "created 1" in resp.text.lower() or "created: 1" in resp.text.lower()
    finally:
        ctx.stop()


def test_import_sets_lieutenant_and_active_from_x_marked_columns(dynamodb_tables):
    fake = type("C", (), {"list_users": lambda self, **kw: {"Users": []}})()
    c, ctx = _client(admin=True)
    try:
        with patch("app.routes.admin._cognito", return_value=fake):
            c.post("/admin/clowns/import", files=_csv(
                "email,display_name,years_ridden,is_lieutenant,active\n"
                "lt@example.com,LT,2020-2021,X,X\n"
                "rider@example.com,Rider,2020-2021,,X\n"
            ), data={"invite_missing": ""}, follow_redirects=False)
    finally:
        ctx.stop()
    profiles = {p["email"]: p for p in CLOWN_PROFILES().scan()["Items"]}
    assert profiles["lt@example.com"]["is_lieutenant"] is True
    assert profiles["lt@example.com"]["active"] is True
    assert profiles["rider@example.com"]["is_lieutenant"] is False
    assert profiles["rider@example.com"]["active"] is True


def test_import_updates_existing_and_leaves_blank_cells(dynamodb_tables):
    _put_profile("clown_u", "Original", [2020], phone="5045551111", email="u@example.com",
                 cognito_sub=None)
    fake = type("C", (), {"list_users": lambda self, **kw: {"Users": []}})()
    c, ctx = _client(admin=True)
    try:
        with patch("app.routes.admin._cognito", return_value=fake):
            c.post("/admin/clowns/import", files=_csv(
                "email,display_name,phone,years_ridden\n"
                "u@example.com,,,2020 2021\n"
            ), data={"invite_missing": ""}, follow_redirects=False)
    finally:
        ctx.stop()
    p = CLOWN_PROFILES().get_item(Key={"clown_id": "clown_u"})["Item"]
    assert p["display_name"] == "Original"      # blank cell -> unchanged
    assert p["phone"] == "5045551111"           # blank cell -> unchanged
    assert [int(y) for y in p["years_ridden"]] == [2020, 2021]


def test_import_invite_path_creates_and_links(dynamodb_tables):
    calls = {}
    class Fake:
        def list_users(self, **kw): return {"Users": []}
        def admin_create_user(self, **kw):
            calls["email"] = kw["Username"]
            return {"User": {"Username": "sub-invited"}}
    c, ctx = _client(admin=True)
    try:
        with patch("app.routes.admin._cognito", return_value=Fake()):
            c.post("/admin/clowns/import", files=_csv(
                "email,display_name\nnew@example.com,New Clown\n"
            ), data={"invite_missing": "1"}, follow_redirects=False)
    finally:
        ctx.stop()
    assert calls["email"] == "new@example.com"
    p = CLOWN_PROFILES().scan()["Items"][0]
    assert p["cognito_sub"] == "sub-invited"


def test_import_skips_blank_email_rows(dynamodb_tables):
    fake = type("C", (), {"list_users": lambda self, **kw: {"Users": []}})()
    c, ctx = _client(admin=True)
    try:
        with patch("app.routes.admin._cognito", return_value=fake):
            resp = c.post("/admin/clowns/import", files=_csv(
                "email,display_name\n,No Email\n"
            ), data={"invite_missing": ""}, follow_redirects=False)
        assert CLOWN_PROFILES().scan()["Items"] == []
        assert "skipped" in resp.text.lower()
    finally:
        ctx.stop()


def test_import_rejects_csv_without_email_column(dynamodb_tables):
    c, ctx = _client(admin=True)
    try:
        resp = c.post("/admin/clowns/import", files=_csv("name\nBob\n"),
                      data={"invite_missing": ""}, follow_redirects=False)
        assert resp.status_code == 400
    finally:
        ctx.stop()


def test_export_round_trips(dynamodb_tables):
    _put_profile("clown_x", "Xtra", [2021, 2022], is_lieutenant=True,
                 email="x@example.com", phone="+15045551212", cognito_sub=None,
                 swag_name="Lil X")
    fake = type("C", (), {"list_users": lambda self, **kw: {"Users": []}})()
    c, ctx = _client(admin=True)
    try:
        export = c.get("/admin/clowns/export.csv")
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/csv")
        # wipe and re-import
        CLOWN_PROFILES().delete_item(Key={"clown_id": "clown_x"})
        with patch("app.routes.admin._cognito", return_value=fake):
            c.post("/admin/clowns/import",
                   files={"file": ("clowns.csv", io.BytesIO(export.text.encode()), "text/csv")},
                   data={"invite_missing": ""}, follow_redirects=False)
    finally:
        ctx.stop()
    p = CLOWN_PROFILES().scan()["Items"][0]
    assert p["display_name"] == "Xtra"
    assert p["swag_name"] == "Lil X"
    assert p["is_lieutenant"] is True
    assert [int(y) for y in p["years_ridden"]] == [2021, 2022]
    assert p["phone"] == "+15045551212"  # M2: no permanent leading apostrophe


# ---- Resources links ----

def _put_link(link_id, label="Roster sheet", url="https://docs.google.com/x", sort_order=0):
    KREWE_LINKS().put_item(Item={
        "link_id": link_id, "label": label, "url": url, "description": None,
        "sort_order": sort_order, "created_at": "2026-01-01T00:00:00Z",
    })


def test_resources_page_shows_the_banner_image(dynamodb_tables):
    c, ctx = _client(admin=False)
    try:
        resp = c.get("/admin/clowns/resources")
        assert 'src="/static/img/RdE%20Clowns.png"' in resp.text
    finally:
        ctx.stop()


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


def test_resources_admin_edit_form_is_behind_a_disclosure(dynamodb_tables):
    _put_link("lnk_1", label="Throw budget")
    c, ctx = _client(admin=True)
    try:
        resp = c.get("/admin/clowns/resources")
        # The edit fields live inside a <details>, not on the page by default.
        assert "<details" in resp.text
        assert "<summary>Edit</summary>" in resp.text
        assert 'action="/admin/clowns/resources/lnk_1"' in resp.text  # still present, just tucked away
        # Reorder stays reachable without opening the disclosure.
        assert 'action="/admin/clowns/resources/lnk_1/move"' in resp.text
    finally:
        ctx.stop()


def test_resources_manage_toggle_markup_is_admin_only(dynamodb_tables):
    m, mctx = _client(admin=False)
    try:
        body = m.get("/admin/clowns/resources").text
        assert 'id="resources-manage-toggle"' not in body
        assert "Manage resources" not in body
    finally:
        mctx.stop()
    a, actx = _client(admin=True)
    try:
        body = a.get("/admin/clowns/resources").text
        assert 'id="resources-manage-toggle"' in body
        assert "Manage resources" in body
    finally:
        actx.stop()


def test_resources_can_add_a_link_less_information_entry(dynamodb_tables):
    c, ctx = _client(admin=True)
    try:
        resp = c.post("/admin/clowns/resources",
                       data={"label": "Bring your own throw bag", "url": "", "description": "No link needed."},
                       follow_redirects=False)
        assert resp.status_code == 303
        items = KREWE_LINKS().scan()["Items"]
        assert len(items) == 1
        assert items[0]["url"] is None
        assert items[0]["label"] == "Bring your own throw bag"

        page = c.get("/admin/clowns/resources").text
        assert "Bring your own throw bag" in page
        # It shows as plain text, not a link.
        assert 'class="krewe-link-label"' in page
    finally:
        ctx.stop()


def test_resources_create_rejects_blank_label(dynamodb_tables):
    c, ctx = _client(admin=True)
    try:
        resp = c.post("/admin/clowns/resources", data={"label": "  ", "url": "", "description": ""},
                       follow_redirects=False)
        assert resp.status_code == 400
        assert KREWE_LINKS().scan()["Items"] == []
    finally:
        ctx.stop()


def test_resources_update_can_clear_the_url_to_make_it_info_only(dynamodb_tables):
    _put_link("lnk_1", label="Old rules doc", url="https://docs.google.com/old")
    c, ctx = _client(admin=True)
    try:
        resp = c.post("/admin/clowns/resources/lnk_1",
                       data={"label": "Old rules doc", "url": "", "description": "Retired, ask a lieutenant."},
                       follow_redirects=False)
        assert resp.status_code == 303
    finally:
        ctx.stop()
    item = KREWE_LINKS().get_item(Key={"link_id": "lnk_1"})["Item"]
    assert item["url"] is None
    assert item["description"] == "Retired, ask a lieutenant."


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


def test_nav_shows_clown_admin_links_only_to_admins(dynamodb_tables):
    m, mctx = _client(admin=False)
    try:
        body = m.get("/admin/clowns/resources").text
        assert 'href="/admin/clowns/roster"' in body
        assert 'href="/admin/clowns/manage"' not in body
        assert 'href="/admin/clowns/import"' not in body
        assert 'href="/admin/clown_mgmt"' not in body
    finally:
        mctx.stop()
    a, actx = _client(sub="admin-2", admin=True)
    try:
        body = a.get("/admin/clowns/resources").text
        assert 'href="/admin/clowns/manage"' in body
        assert 'href="/admin/clowns/import"' in body
        assert 'href="/admin/clown_mgmt"' in body
    finally:
        actx.stop()


def test_resources_shows_incomplete_profile_nudge(dynamodb_tables):
    c, ctx = _client(sub="sub-new", email="new@example.com")
    try:
        resp = c.get("/admin/clowns/resources")
        assert "Your profile is incomplete" in resp.text
    finally:
        ctx.stop()


def test_resources_hides_nudge_when_profile_complete(dynamodb_tables):
    CLOWN_PROFILES().put_item(Item={
        "clown_id": "clown_done", "cognito_sub": "sub-done", "email": "done@example.com",
        "display_name": "All Set", "photo_url": "https://img.example/x.jpg",
        "years_ridden": [2025], "is_lieutenant": False, "active": True,
        "created_at": "2026-01-01T00:00:00Z",
    })
    c, ctx = _client(sub="sub-done", email="done@example.com")
    try:
        resp = c.get("/admin/clowns/resources")
        assert "Your profile is incomplete" not in resp.text
    finally:
        ctx.stop()


def test_my_profile_skips_adoption_when_email_unverified(dynamodb_tables):
    CLOWN_PROFILES().put_item(Item={
        "clown_id": "clown_hist2", "cognito_sub": None, "email": "old@example.com",
        "years_ridden": [2015], "is_lieutenant": False, "active": False,
        "created_at": "2026-01-01T00:00:00Z",
    })
    p = admin_routes._my_profile({"sub": "sub-z", "email": "old@example.com"})
    assert p["clown_id"] != "clown_hist2"
    assert len(CLOWN_PROFILES().scan()["Items"]) == 2  # a fresh profile, no adoption


def test_import_adopts_unlinked_profile_when_email_has_cognito_account(dynamodb_tables):
    # The seed workflow: import historical CSV -> invite riders (now in Cognito)
    # -> re-import. The row must adopt the historical profile, not duplicate it.
    _put_profile("clown_alice", "Alice", [2015, 2016], cognito_sub=None,
                 email="alice@example.com")
    fake = type("C", (), {"list_users": lambda self, **kw: {"Users": [
        {"Username": "sub-alice",
         "Attributes": [{"Name": "email", "Value": "alice@example.com"}]}
    ]}})()
    c, ctx = _client(admin=True)
    try:
        with patch("app.routes.admin._cognito", return_value=fake):
            resp = c.post("/admin/clowns/import", files=_csv(
                "email,display_name,years_ridden\n"
                "alice@example.com,Alice Updated,2015-2017\n"
            ), data={"invite_missing": ""}, follow_redirects=False)
        assert resp.status_code == 200
    finally:
        ctx.stop()
    items = CLOWN_PROFILES().scan()["Items"]
    assert len(items) == 1
    p = items[0]
    assert p["clown_id"] == "clown_alice"
    assert p["cognito_sub"] == "sub-alice"
    assert p["display_name"] == "Alice Updated"
    assert [int(y) for y in p["years_ridden"]] == [2015, 2016, 2017]


def test_import_reclaims_profile_with_a_stale_sub_instead_of_duplicating(dynamodb_tables):
    # The pool-migration scenario, hit via CSV import instead of self-login:
    # Bob's profile still carries his sub from the retired pool. Re-importing
    # his row -- now that he has a real login in the CURRENT pool under a
    # different sub -- must update his existing profile, not create a
    # second one.
    _put_profile("clown_bob", "Bob", [2018, 2019], cognito_sub="sub-old-pool",
                 email="bob@example.com", is_lieutenant=True, active=True)

    def admin_get_user(self, UserPoolId, Username):
        raise ClientError(
            {"Error": {"Code": "UserNotFoundException", "Message": "not found"}}, "AdminGetUser",
        )

    fake = type("C", (), {
        "list_users": lambda self, **kw: {"Users": [
            {"Username": "sub-new-pool",
             "Attributes": [{"Name": "email", "Value": "bob@example.com"}]}
        ]},
        "admin_get_user": admin_get_user,
    })()
    c, ctx = _client(admin=True)
    try:
        with patch("app.routes.admin._cognito", return_value=fake):
            resp = c.post("/admin/clowns/import", files=_csv(
                "email,years_ridden\nbob@example.com,2018-2020\n"
            ), data={"invite_missing": ""}, follow_redirects=False)
        assert resp.status_code == 200
    finally:
        ctx.stop()
    items = CLOWN_PROFILES().scan()["Items"]
    assert len(items) == 1  # not duplicated
    p = items[0]
    assert p["clown_id"] == "clown_bob"
    assert p["cognito_sub"] == "sub-new-pool"
    # status carried over from the pre-existing row, untouched by this CSV
    # (its is_lieutenant/active columns were left blank)
    assert p["is_lieutenant"] is True
    assert p["active"] is True


def test_import_row_wider_than_header_is_not_a_500(dynamodb_tables):
    fake = type("C", (), {"list_users": lambda self, **kw: {"Users": []}})()
    c, ctx = _client(admin=True)
    try:
        with patch("app.routes.admin._cognito", return_value=fake):
            resp = c.post("/admin/clowns/import", files=_csv(
                "email,display_name\n"
                "wide@example.com,Wide,extra,more extra\n"
            ), data={"invite_missing": ""}, follow_redirects=False)
        assert resp.status_code == 200
    finally:
        ctx.stop()
    assert [p["email"] for p in CLOWN_PROFILES().scan()["Items"]] == ["wide@example.com"]


def test_manage_delete_removes_profile(dynamodb_tables):
    _put_profile("clown_del", "Deletable", [2019])
    c, ctx = _client(admin=True)
    try:
        resp = c.post("/admin/clowns/manage/clown_del/delete", follow_redirects=False)
        assert resp.status_code == 303
    finally:
        ctx.stop()
    assert "clown_del" not in [p["clown_id"] for p in CLOWN_PROFILES().scan()["Items"]]


def _assert_s3_object_gone(url):
    key = url.split(".amazonaws.com/", 1)[1]
    with pytest.raises(ClientError):
        boto3.client("s3", region_name="us-east-1").get_object(Bucket="event-images-test", Key=key)


def test_deleting_a_clown_profile_deletes_its_photo_from_s3(dynamodb_tables):
    c, ctx = _client(sub="sub-photo", admin=True)
    try:
        c.post(
            "/admin/clowns/profile", data={"display_name": "Photo Clown"},
            files={"photo": ("p.jpg", b"photo-bytes", "image/jpeg")}, follow_redirects=False,
        )
        profile = CLOWN_PROFILES().scan()["Items"][0]
        url = profile["photo_url"]
        assert url
        resp = c.post(f"/admin/clowns/manage/{profile['clown_id']}/delete", follow_redirects=False)
        assert resp.status_code == 303
    finally:
        ctx.stop()
    _assert_s3_object_gone(url)


def test_replacing_a_clown_photo_deletes_the_old_one_from_s3(dynamodb_tables):
    c, ctx = _client(sub="sub-photo2")
    try:
        c.post(
            "/admin/clowns/profile", data={"display_name": "Old Photo"},
            files={"photo": ("old.jpg", b"old-bytes", "image/jpeg")}, follow_redirects=False,
        )
        old_url = CLOWN_PROFILES().scan()["Items"][0]["photo_url"]
        c.post(
            "/admin/clowns/profile", data={"display_name": "New Photo"},
            files={"photo": ("new.jpg", b"new-bytes", "image/jpeg")}, follow_redirects=False,
        )
        new_url = CLOWN_PROFILES().scan()["Items"][0]["photo_url"]
        assert new_url != old_url
    finally:
        ctx.stop()
    _assert_s3_object_gone(old_url)


def test_manage_delete_is_admin_only(dynamodb_tables):
    _put_profile("clown_del2", "Nope", [2019])
    c, ctx = _client(admin=False)
    try:
        resp = c.post("/admin/clowns/manage/clown_del2/delete",
                      headers={"accept": "application/json"})
        assert resp.status_code == 403
    finally:
        ctx.stop()
    assert "clown_del2" in [p["clown_id"] for p in CLOWN_PROFILES().scan()["Items"]]


def test_manage_update_on_stale_id_does_not_500_the_page(dynamodb_tables):
    c, ctx = _client(admin=True)
    try:
        resp = c.post("/admin/clowns/manage/clown_missing", data={
            "years_ridden": "2019", "is_lieutenant": "", "active": "1",
        }, follow_redirects=False)
        assert resp.status_code in (303, 400)
        assert c.get("/admin/clowns/manage").status_code == 200
    finally:
        ctx.stop()
    assert CLOWN_PROFILES().scan()["Items"] == []  # no ghost row upserted


def test_roster_milestone_badge_for_any_multiple_of_five(dynamodb_tables):
    _put_profile("clown_vet", "Vet", list(range(1990, 2020)))  # 30 years, 1990-2019
    c, ctx = _client(admin=True)
    try:
        # Tenure is as of the selected year -- viewing the LAST of those 30
        # years is what reaches the 30-year milestone, not an earlier one.
        resp = c.get("/admin/clowns/roster?year=2019")
        assert "Vet" in resp.text
        assert "30-year rider" in resp.text
    finally:
        ctx.stop()


def test_manage_link_rejects_login_already_linked_to_another_profile(dynamodb_tables):
    _put_profile("clown_first", "First", [2010], cognito_sub="sub-dupe",
                 email="first@example.com")
    _put_profile("clown_second", "Second", [2011], cognito_sub=None,
                 email="second@example.com")
    fake = type("C", (), {"list_users": lambda self, **kw: {"Users": [
        {"Username": "sub-dupe",
         "Attributes": [{"Name": "email", "Value": "reuse@example.com"}]}
    ]}})()
    c, ctx = _client(admin=True)
    try:
        with patch("app.routes.admin._cognito", return_value=fake):
            resp = c.post("/admin/clowns/manage/clown_second/link",
                          data={"email": "reuse@example.com"}, follow_redirects=False)
        assert resp.status_code == 400
    finally:
        ctx.stop()
    p = CLOWN_PROFILES().get_item(Key={"clown_id": "clown_second"})["Item"]
    assert p.get("cognito_sub") is None


def test_every_clowns_view_page_renders_for_a_member(dynamodb_tables):
    m, mctx = _client(admin=False)
    try:
        for path in ("/admin/clowns", "/admin/clowns/roster",
                     "/admin/clowns/directory", "/admin/clowns/resources", "/admin/clowns/profile"):
            assert m.get(path).status_code == 200, path
    finally:
        mctx.stop()
