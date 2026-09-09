from app.db import CLOWN_PROFILES, KREWE_LINKS
from app.models import ClownProfile, KreweLink
from app.routes import admin as admin_routes


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
