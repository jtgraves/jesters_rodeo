# Clowns Section Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a members-only "Clowns" section (roster, prior-year rosters, lieutenant bios, directory, a Google-Docs links page, per-clown ride-year tracking, self-service profile editing, and an admin CSV import/export), and move the existing Cognito login/role page from `/admin/clowns` to `/admin/clown_mgmt`.

**Architecture:** A new DynamoDB `ClownProfile` table is the source of truth for the roster; Cognito stays purely auth. Profiles may have no login (historical riders) and are linked to an account on first sign-in (by matching email) or by an admin. All pages live under `/admin/...` because the session cookie is scoped to that path; view pages use the existing `member_router` (any signed-in clown), management uses `router` (admins only). A second `KreweLink` table backs the resources page. Follows the patterns already used for FAQ and Past Beneficiaries.

**Tech Stack:** FastAPI on AWS Lambda (via Mangum), Jinja2 templates, DynamoDB (boto3 resource), AWS CDK (Python) for infra, pytest + moto for tests.

**Spec:** `docs/superpowers/specs/2026-09-08-clowns-section-design.md`

## Global Constraints

- Python 3.9 test runtime — modules that use `X | Y` in runtime-evaluated positions need `from __future__ import annotations` (already present in `app/routes/admin.py`, `app/models.py`, `app/templating.py`).
- No new third-party dependencies. CSV only (stdlib `csv`), no `openpyxl`.
- New DynamoDB tables: `billing_mode=PAY_PER_REQUEST`, `removal_policy=RemovalPolicy.RETAIN`, no GSI — same as `FaqEntriesTable`.
- Money/label copy: the krewe is "Jester's Reaux-de-Eaux"; the float is the "Reaux-de-Eaux clowns" within Knights of Babylon. Don't reintroduce "Rodeo" or "ball".
- Admin-only by default: a route is on `router` unless it is deliberately placed on `member_router`. Never loosen `router`.
- Commit after every task with a `git commit` (message style: lowercase imperative summary; the repo does not use a strict prefix convention, plain summaries are fine).
- Run the full suite (`.venv/bin/python -m pytest -q`) green before each commit.

---

## File Structure

**Modified**
- `app/routes/admin.py` — all new routes + helpers; the clown_mgmt route rename.
- `app/models.py` — `ClownProfile`, `KreweLink` models.
- `app/config.py` — two required table-name settings.
- `app/db.py` — `CLOWN_PROFILES()`, `KREWE_LINKS()` accessors.
- `app/templating.py` — `ordinal` Jinja filter.
- `app/templates/admin/_base.html` — nav: rename "Clown Management" target, add un-gated "Clowns".
- `infra/jesters_rodeo_stack.py` — two `dynamodb.Table`s + `common_env` entries.
- `tests/conftest.py` — env defaults + `create_table` for the two tables.
- `tests/test_admin_routes.py` — repoint clown_mgmt tests; new fixtures email claim.

**Created**
- `app/templates/admin/clown_mgmt.html` — renamed from `admin/clowns.html`.
- `app/templates/admin/clowns_home.html` — section hub.
- `app/templates/admin/clowns_roster.html` — roster grid + year picker.
- `app/templates/admin/clowns_lieutenants.html` — lieutenant bio cards.
- `app/templates/admin/clowns_directory.html` — contact table.
- `app/templates/admin/clowns_profile.html` — "My profile" edit form.
- `app/templates/admin/clowns_manage.html` — admin per-profile controls.
- `app/templates/admin/clowns_resources.html` — resources list + inline admin controls.
- `app/templates/admin/clowns_import.html` — CSV upload + result summary.
- `tests/test_clowns_section.py` — all new tests (keeps `test_admin_routes.py` from ballooning).

---

## Task 1: Move Clown Management to `/admin/clown_mgmt`

Pure rename. No behavior change. The five routes, the template, the nav link, and the tests move together.

**Files:**
- Modify: `app/routes/admin.py` — the `# ---- Clown management` section (routes `GET/POST /clowns`, `POST /clowns/{username}/promote|demote|delete`, `_clowns_page` template name and redirect strings)
- Rename: `app/templates/admin/clowns.html` → `app/templates/admin/clown_mgmt.html` (via `git mv`), update its 6 form `action`s
- Modify: `app/templates/admin/_base.html:24`
- Modify: `tests/test_admin_routes.py` — the `# ---- Clown management` block and three tests in `# ---- Member ... access`

**Interfaces:**
- Produces: routes `GET /admin/clown_mgmt`, `POST /admin/clown_mgmt`, `POST /admin/clown_mgmt/{username}/promote`, `.../demote`, `.../delete`. Template `admin/clown_mgmt.html`. Helper names unchanged (`_list_clowns`, `_create_clown`, `_promote_clown`, `_demote_clown`, `_delete_clown`, `_clowns_page`).

- [ ] **Step 1: Repoint the tests to `/admin/clown_mgmt`**

In `tests/test_admin_routes.py`, in the `# ---- Clown management (Cognito users) ----` block and the member tests, replace every `"/admin/clowns"` and `'action="/admin/clowns/'` with the `clown_mgmt` form:

- `test_clowns_page_lists_clowns_with_roles`: `admin_client.get("/admin/clown_mgmt")`; assertions `'action="/admin/clown_mgmt/sub-2/promote"'`, `'action="/admin/clown_mgmt/admin-2/promote"' not in`, `'action="/admin/clown_mgmt/admin-2/demote"'`, `'action="/admin/clown_mgmt/admin-1/demote"' not in`, `'action="/admin/clown_mgmt/sub-2/delete"'`, `'action="/admin/clown_mgmt/admin-2/delete"'`, `'action="/admin/clown_mgmt/admin-1/delete"' not in`.
- `test_invite_clown_calls_cognito`, `test_invite_clown_as_admin_passes_the_flag`, `test_invite_clown_rejects_blank_email`, `test_invite_clown_handles_duplicate`: `admin_client.post("/admin/clown_mgmt", ...)` and `resp.headers["location"] == "/admin/clown_mgmt"`.
- `test_promote_clown_calls_cognito`: `admin_client.post("/admin/clown_mgmt/sub-2/promote", ...)`, `location == "/admin/clown_mgmt"`.
- `test_demote_clown_calls_cognito`: `.../admin-2/demote`, `location == "/admin/clown_mgmt"`.
- `test_demote_clown_blocks_self`: `.../admin-1/demote`.
- `test_delete_clown_calls_cognito`: `.../sub-2/delete`.
- `test_delete_clown_blocks_self`: `.../admin-1/delete`.

In `test_member_is_bounced_from_admin_only_pages`, change `"/admin/clowns"` in the tuple to `"/admin/clown_mgmt"`.

In `test_member_nav_hides_admin_links`, change the last assertion to:
```python
    assert 'href="/admin/clown_mgmt"' not in resp.text
```

In `test_admin_nav_shows_clowns_link`, rename to `test_admin_nav_shows_clown_mgmt_link` and change the assertion to:
```python
    assert 'href="/admin/clown_mgmt"' in resp.text
```

- [ ] **Step 2: Run the clown tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_admin_routes.py -k "clown"`
Expected: FAIL — routes still at `/admin/clowns`, so redirects/paths 404 or mismatch.

- [ ] **Step 3: Rename the routes and template**

In `app/routes/admin.py`, in the `# ---- Clown management` section:
- `@router.get("/clowns")` → `@router.get("/clown_mgmt")`
- `@router.post("/clowns")` → `@router.post("/clown_mgmt")`
- `@router.post("/clowns/{username}/promote")` → `@router.post("/clown_mgmt/{username}/promote")`
- `@router.post("/clowns/{username}/demote")` → `@router.post("/clown_mgmt/{username}/demote")`
- `@router.post("/clowns/{username}/delete")` → `@router.post("/clown_mgmt/{username}/delete")`
- Every `RedirectResponse("/admin/clowns", ...)` in those five handlers → `RedirectResponse("/admin/clown_mgmt", ...)`
- In `_clowns_page`, `"admin/clowns.html"` → `"admin/clown_mgmt.html"`

Then:
```bash
git mv app/templates/admin/clowns.html app/templates/admin/clown_mgmt.html
```
In `app/templates/admin/clown_mgmt.html`, change all six `action="/admin/clowns..."` to `action="/admin/clown_mgmt..."` (the top invite form's `action="/admin/clowns"` and the five per-row `action="/admin/clowns/{{ c.username }}/..."`).

In `app/templates/admin/_base.html`, line 24:
```html
      {% if admin %}<a href="/admin/clown_mgmt">Clown Management</a>{% endif %}
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all previously-green tests, now repointed).

- [ ] **Step 5: Commit**

```bash
git add app/routes/admin.py app/templates/admin/clown_mgmt.html app/templates/admin/_base.html tests/test_admin_routes.py
git commit -m "Move Clown Management to /admin/clown_mgmt"
```

---

## Task 2: Two DynamoDB tables + config/db/conftest wiring + models

Everything needed for the two tables to exist and be addressable. One task because a required `Settings` field with no matching env default breaks every import.

**Files:**
- Modify: `infra/jesters_rodeo_stack.py` — `_create_tables()` and `common_env`
- Modify: `app/config.py:16-24` — add two fields
- Modify: `app/db.py` — add two accessors after `FAQ_ENTRIES`
- Modify: `tests/conftest.py` — two env defaults + two `create_table`
- Modify: `app/models.py` — add `ClownProfile`, `KreweLink` after `FaqEntry`
- Test: `tests/test_clowns_section.py` (new)

**Interfaces:**
- Produces: `settings.clown_profiles_table`, `settings.krewe_links_table`; `app.db.CLOWN_PROFILES()`, `app.db.KREWE_LINKS()` (return a boto3 `Table`); models `ClownProfile`, `KreweLink`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_clowns_section.py`:
```python
from datetime import datetime, timezone

from app.db import CLOWN_PROFILES, KREWE_LINKS
from app.models import ClownProfile, KreweLink


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_clowns_section.py`
Expected: FAIL — `ImportError: cannot import name 'CLOWN_PROFILES'` (and `Settings` will raise for the missing env var once wired, so do all of step 3 before re-running).

- [ ] **Step 3: Wire everything**

`app/config.py` — after `faq_entries_table: str` (line ~23):
```python
    clown_profiles_table: str
    krewe_links_table: str
```

`app/db.py` — after the `FAQ_ENTRIES` function:
```python
def CLOWN_PROFILES() -> Any:
    return get_table(settings.clown_profiles_table)


def KREWE_LINKS() -> Any:
    return get_table(settings.krewe_links_table)
```

`tests/conftest.py` — after the `FAQ_ENTRIES_TABLE` line:
```python
os.environ.setdefault("CLOWN_PROFILES_TABLE", "ClownProfiles")
os.environ.setdefault("KREWE_LINKS_TABLE", "KreweLinks")
```
and in `dynamodb_tables`, after the `FaqEntries` `create_table` block and before the S3 `create_bucket`:
```python
        client.create_table(
            TableName="ClownProfiles",
            KeySchema=[{"AttributeName": "clown_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "clown_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="KreweLinks",
            KeySchema=[{"AttributeName": "link_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "link_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
```

`app/models.py` — after the `FaqEntry` class:
```python
class ClownProfile(BaseModel):
    """A Reaux-de-Eaux clown. The source of truth for the roster. A profile may
    have no Cognito login (a historical rider); it is linked to an account on
    first sign-in by matching email, or by an admin."""
    clown_id: str
    cognito_sub: str | None = None
    email: str | None = None
    display_name: str | None = None
    photo_url: str | None = None
    bio: str | None = None
    phone: str | None = None
    address: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    years_ridden: list[int] = []
    is_lieutenant: bool = False
    lieutenant_title: str | None = None
    active: bool = True
    created_at: str


class KreweLink(BaseModel):
    """A link to a Google Doc/Sheet, shown on the clowns Resources page."""
    link_id: str
    label: str
    url: str
    description: str | None = None
    sort_order: int = 0
    created_at: str
```

`infra/jesters_rodeo_stack.py` — in `common_env`, after the `FAQ_ENTRIES_TABLE` line:
```python
            "CLOWN_PROFILES_TABLE": tables["clown_profiles"].table_name,
            "KREWE_LINKS_TABLE": tables["krewe_links"].table_name,
```
In `_create_tables()`, after `faq_entries_table = dynamodb.Table(...)`:
```python
        clown_profiles_table = dynamodb.Table(
            self, "ClownProfilesTable",
            partition_key=dynamodb.Attribute(name="clown_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        krewe_links_table = dynamodb.Table(
            self, "KreweLinksTable",
            partition_key=dynamodb.Attribute(name="link_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
```
and in the returned dict, after `"faq_entries": faq_entries_table,`:
```python
            "clown_profiles": clown_profiles_table,
            "krewe_links": krewe_links_table,
```

- [ ] **Step 4: Run tests + infra syntax check**

Run: `.venv/bin/python -m pytest -q tests/test_clowns_section.py`
Expected: PASS (3 tests).

Run: `.venv/bin/python -c "import ast; ast.parse(open('infra/jesters_rodeo_stack.py').read()); print('infra ok')"`
Expected: `infra ok`. (A real `cdk synth` is done in Verification, before deploy.)

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (whole suite still green).

- [ ] **Step 5: Commit**

```bash
git add infra/jesters_rodeo_stack.py app/config.py app/db.py tests/conftest.py app/models.py tests/test_clowns_section.py
git commit -m "Add ClownProfiles and KreweLinks tables + models"
```

---

## Task 3: `_my_profile` helper (auto-create + email-link) + fixtures carry an email claim

**Files:**
- Modify: `app/routes/admin.py` — imports (`date`, `CLOWN_PROFILES`, `ClownProfile`); add `_my_profile` in a new `# ---- Clowns section` block at end of file (before nothing — it can be the last section)
- Modify: `tests/test_admin_routes.py` — add `"email"` to the two fixtures
- Test: `tests/test_clowns_section.py`

**Interfaces:**
- Consumes: `paginate` (from `app.db`), `CLOWN_PROFILES` (Task 2), `datetime`/`timezone`/`uuid` (already imported in `admin.py`).
- Produces: `app.routes.admin._my_profile(claims: dict) -> dict` — returns the caller's `ClownProfile` item (a plain dict), creating or email-linking it as needed. `claims` is the Cognito claims dict from `require_member` (`{"sub": ..., "email": ...}`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_clowns_section.py`:
```python
from app.routes import admin as admin_routes


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_clowns_section.py -k my_profile`
Expected: FAIL — `AttributeError: module 'app.routes.admin' has no attribute '_my_profile'`.

- [ ] **Step 3: Implement `_my_profile`**

In `app/routes/admin.py`:
- Change `from datetime import datetime, timezone` → `from datetime import date, datetime, timezone`
- Add `import re` next to `import json`
- In the `from app.db import (...)` block add `CLOWN_PROFILES,` and `KREWE_LINKS,` (alphabetical: after `ANNOUNCEMENTS,` put `CLOWN_PROFILES,`; `KREWE_LINKS,` after `FAQ_ENTRIES,`)
- In the `from app.models import (...)` block add `ClownProfile,` and `KreweLink,`

At the end of the file, add:
```python
# ---- Clowns section (members-only krewe area) ----

def _my_profile(claims: dict) -> dict:
    """Return the calling clown's ClownProfile, creating or email-linking it.

    1. by cognito_sub -> return it
    2. an unlinked profile whose email matches -> attach cognito_sub, return it
       (a pre-seeded / returning rider connecting to their new account)
    3. otherwise create a fresh linked profile
    """
    sub = claims.get("sub", "")
    email = (claims.get("email") or "").strip()
    email_key = email.lower()
    profiles = paginate(CLOWN_PROFILES().scan)

    for p in profiles:
        if p.get("cognito_sub") == sub:
            return p

    if email_key:
        for p in profiles:
            if not p.get("cognito_sub") and (p.get("email") or "").strip().lower() == email_key:
                CLOWN_PROFILES().update_item(
                    Key={"clown_id": p["clown_id"]},
                    UpdateExpression="SET cognito_sub = :s",
                    ExpressionAttributeValues={":s": sub},
                )
                p["cognito_sub"] = sub
                return p

    item = {
        "clown_id": f"clown_{uuid.uuid4().hex}",
        "cognito_sub": sub,
        "email": email or None,
        "display_name": None, "photo_url": None, "bio": None,
        "phone": None, "address": None,
        "emergency_contact_name": None, "emergency_contact_phone": None,
        "years_ridden": [], "is_lieutenant": False, "lieutenant_title": None,
        "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    CLOWN_PROFILES().put_item(Item=item)
    return item
```

In `tests/test_admin_routes.py`, the `admin_client` fixture `return_value`:
```python
        return_value={"sub": "admin-1", "email": "admin@example.com", "cognito:groups": ["admins"]},
```
and `member_client`:
```python
        return_value={"sub": "member-1", "email": "member@example.com", "cognito:groups": []},
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q tests/test_clowns_section.py && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/admin.py tests/test_clowns_section.py tests/test_admin_routes.py
git commit -m "Add _my_profile: auto-create and email-link a clown profile"
```

---

## Task 4: Clowns hub page + nav link

**Files:**
- Modify: `app/routes/admin.py` — add `member_router.get("/clowns")`
- Create: `app/templates/admin/clowns_home.html`
- Modify: `app/templates/admin/_base.html` — add un-gated `Clowns` link
- Modify: `tests/test_admin_routes.py` — `test_member_nav_hides_admin_links`
- Test: `tests/test_clowns_section.py`

**Interfaces:**
- Consumes: `_my_profile` (Task 3), `member_router`, `templates`.
- Produces: `GET /admin/clowns` (member) → renders `admin/clowns_home.html` with `{"profile": <dict>}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_clowns_section.py`:
```python
from unittest.mock import patch

from app import auth
from app.config import settings
from fastapi.testclient import TestClient
from app.main import app


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_clowns_section.py -k hub`
Expected: FAIL — 404 for `/admin/clowns`.

- [ ] **Step 3: Implement the route, template, and nav**

In `app/routes/admin.py`, after `_my_profile`:
```python
@member_router.get("/clowns")
def clowns_home(request: Request, claims: dict = Depends(require_member)) -> Response:
    profile = _my_profile(claims)
    return templates.TemplateResponse(request, "admin/clowns_home.html", {"profile": profile})
```

Create `app/templates/admin/clowns_home.html`:
```html
{% extends "admin/_base.html" %}
{% block content %}
<h1>Reaux-de-Eaux Clowns</h1>
{% if not profile.display_name or not profile.photo_url %}
<p class="error" role="alert">Your profile is incomplete —
   <a href="/admin/clowns/profile">add your name and photo</a>.</p>
{% endif %}
<ul>
  <li><a href="/admin/clowns/roster">Roster</a></li>
  <li><a href="/admin/clowns/lieutenants">Float lieutenants</a></li>
  <li><a href="/admin/clowns/directory">Directory</a></li>
  <li><a href="/admin/clowns/resources">Resources</a></li>
  <li><a href="/admin/clowns/profile">My profile</a></li>
  {% if request.state.is_admin %}
  <li><a href="/admin/clowns/manage">Manage clowns</a> ·
      <a href="/admin/clowns/import">Import CSV</a></li>
  {% endif %}
</ul>
{% endblock %}
```

In `app/templates/admin/_base.html`, add after the `Check-in` line (line 19), **not** gated:
```html
      <a href="/admin/clowns">Clowns</a>
```

In `tests/test_admin_routes.py`, `test_member_nav_hides_admin_links` — the member nav now legitimately contains `/admin/clowns`, so the only remaining "hidden" assertions are `/admin/events` and `/admin/clown_mgmt`. Ensure the body is:
```python
def test_member_nav_hides_admin_links(member_client):
    _put_event(status="open")
    resp = member_client.get("/admin/orders")
    assert 'href="/admin/checkin"' in resp.text
    assert 'href="/admin/clowns"' in resp.text
    assert 'href="/admin/events"' not in resp.text
    assert 'href="/admin/clown_mgmt"' not in resp.text
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/admin.py app/templates/admin/clowns_home.html app/templates/admin/_base.html tests/test_clowns_section.py tests/test_admin_routes.py
git commit -m "Add the Clowns section hub + nav link"
```

---

## Task 5: My Profile (view + self-edit)

**Files:**
- Modify: `app/routes/admin.py` — `member_router.get/post("/clowns/profile")`, helper `_update_clown_fields`
- Create: `app/templates/admin/clowns_profile.html`
- Test: `tests/test_clowns_section.py`

**Interfaces:**
- Consumes: `_my_profile` (Task 3), `_maybe_upload_image` (existing), `require_member`.
- Produces: `GET /admin/clowns/profile` (member); `POST /admin/clowns/profile` (member) → 303 to `/admin/clowns/profile`. Helper `_update_clown_fields(clown_id: str, fields: dict) -> None` — `SET`s each key on the profile using name aliases; a no-op if `fields` is empty.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_clowns_section.py`:
```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_clowns_section.py -k "my_profile_edit"`
Expected: FAIL — 404 for `POST /admin/clowns/profile`.

- [ ] **Step 3: Implement**

In `app/routes/admin.py`, in the Clowns section:
```python
def _update_clown_fields(clown_id: str, fields: dict) -> None:
    if not fields:
        return
    CLOWN_PROFILES().update_item(
        Key={"clown_id": clown_id},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
        ExpressionAttributeNames={f"#{k}": k for k in fields},
        ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
    )


_PROFILE_TEXT_FIELDS = (
    "display_name", "bio", "phone", "address",
    "emergency_contact_name", "emergency_contact_phone",
)


@member_router.get("/clowns/profile")
def my_profile_page(request: Request, claims: dict = Depends(require_member)) -> Response:
    return templates.TemplateResponse(
        request, "admin/clowns_profile.html", {"profile": _my_profile(claims)}
    )


@member_router.post("/clowns/profile")
def update_my_profile(
    request: Request,
    claims: dict = Depends(require_member),
    display_name: str = Form(""),
    bio: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    emergency_contact_name: str = Form(""),
    emergency_contact_phone: str = Form(""),
    photo: UploadFile | None = File(None),
) -> Response:
    profile = _my_profile(claims)
    fields = {
        "display_name": display_name.strip() or None,
        "bio": bio.strip() or None,
        "phone": phone.strip() or None,
        "address": address.strip() or None,
        "emergency_contact_name": emergency_contact_name.strip() or None,
        "emergency_contact_phone": emergency_contact_phone.strip() or None,
    }
    photo_url, photo_error = _maybe_upload_image(photo, "clowns", "Clown photo")
    if photo_error:
        return templates.TemplateResponse(
            request, "admin/clowns_profile.html",
            {"profile": profile, "error": photo_error}, status_code=400,
        )
    if photo_url:
        fields["photo_url"] = photo_url
    _update_clown_fields(profile["clown_id"], fields)
    return RedirectResponse("/admin/clowns/profile", status_code=303)
```

Create `app/templates/admin/clowns_profile.html`:
```html
{% extends "admin/_base.html" %}
{% block content %}
<h1>My profile</h1>
{% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}
<form method="post" action="/admin/clowns/profile" enctype="multipart/form-data" class="event-images-form">
  <label>Name <input type="text" name="display_name" value="{{ profile.display_name or '' }}"></label>
  <label>
    {% if profile.photo_url %}<img src="{{ profile.photo_url }}" alt="" class="event-image-preview"><br>{% endif %}
    Photo <input type="file" name="photo" accept="image/jpeg,image/png,image/gif,image/webp">
  </label>
  <label>Bio <textarea name="bio" rows="4">{{ profile.bio or '' }}</textarea></label>
  <label>Phone <input type="tel" name="phone" value="{{ profile.phone or '' }}"></label>
  <label>Address <input type="text" name="address" value="{{ profile.address or '' }}"></label>
  <label>Emergency contact name <input type="text" name="emergency_contact_name" value="{{ profile.emergency_contact_name or '' }}"></label>
  <label>Emergency contact phone <input type="tel" name="emergency_contact_phone" value="{{ profile.emergency_contact_phone or '' }}"></label>
  <button type="submit">Save</button>
</form>
{% include "admin/_image_size_guard.html" %}
{% endblock %}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/admin.py app/templates/admin/clowns_profile.html tests/test_clowns_section.py
git commit -m "Add My Profile self-edit for clowns"
```

---

## Task 6: Roster + roster-by-year

**Files:**
- Modify: `app/routes/admin.py` — `_all_profiles`, `_current_krewe_year`, `member_router.get("/clowns/roster")`
- Modify: `app/templating.py` — `ordinal` filter
- Create: `app/templates/admin/clowns_roster.html`
- Test: `tests/test_clowns_section.py`

**Interfaces:**
- Consumes: `paginate`, `EVENTS`, `CLOWN_PROFILES`, `require_member`.
- Produces: `GET /admin/clowns/roster?year=<int>` (member). Helpers `_all_profiles() -> list[dict]` (scan, sorted by `display_name` fallback `email` fallback `clown_id`, case-insensitive), `_current_krewe_year() -> int`. Jinja filter `ordinal` (`1` → `"1st"`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_clowns_section.py`:
```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_clowns_section.py -k roster`
Expected: FAIL — 404 for `/admin/clowns/roster`.

- [ ] **Step 3: Implement**

`app/templating.py` — after `_format_phone` / its filter registration:
```python
def _ordinal(value: object) -> str:
    n = int(value)
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


templates.env.filters["ordinal"] = _ordinal
```

`app/routes/admin.py` — in the Clowns section:
```python
def _all_profiles() -> list[dict]:
    profiles = paginate(CLOWN_PROFILES().scan)
    profiles.sort(key=lambda p: (p.get("display_name") or p.get("email") or p["clown_id"]).lower())
    return profiles


def _profile_years(p: dict) -> list[int]:
    return sorted(int(y) for y in (p.get("years_ridden") or []))


def _current_krewe_year() -> int:
    years = [int(e["year"]) for e in paginate(EVENTS().scan) if e.get("year")]
    return max(years) if years else date.today().year


@member_router.get("/clowns/roster")
def clowns_roster(request: Request, year: int | None = None) -> Response:
    profiles = _all_profiles()
    all_years = sorted({y for p in profiles for y in _profile_years(p)}, reverse=True)
    selected = year if year is not None else _current_krewe_year()
    riders = [
        {**p, "tenure": len(_profile_years(p))}
        for p in profiles if selected in _profile_years(p)
    ]
    return templates.TemplateResponse(
        request, "admin/clowns_roster.html",
        {"riders": riders, "year": selected, "all_years": all_years},
    )
```

Create `app/templates/admin/clowns_roster.html`:
```html
{% extends "admin/_base.html" %}
{% block content %}
<h1>Roster — {{ year }}</h1>
<form method="get" action="/admin/clowns/roster">
  <label>Year
    <select name="year" onchange="this.form.submit()">
      {% if year not in all_years %}<option value="{{ year }}" selected>{{ year }}</option>{% endif %}
      {% for y in all_years %}
      <option value="{{ y }}" {% if y == year %}selected{% endif %}>{{ y }}</option>
      {% endfor %}
    </select>
  </label>
  <noscript><button type="submit">Go</button></noscript>
</form>

{% if not riders %}
<p>No riders recorded for {{ year }} yet.</p>
{% else %}
<ul class="clown-grid">
  {% for r in riders %}
  <li class="clown-card">
    {% if r.photo_url %}
    <img src="{{ r.photo_url }}" alt="{{ r.display_name or r.email }}">
    {% else %}
    <img src="/static/img/fleur.png" alt="" class="clown-card-placeholder">
    {% endif %}
    <span class="clown-card-name">{{ r.display_name or r.email }}</span>
    <span class="clown-card-tenure">{{ r.tenure | ordinal }} year</span>
    {% if r.tenure in [5, 10, 15, 20, 25] %}
    <span class="clown-card-milestone">🏅 {{ r.tenure }}-year rider</span>
    {% endif %}
  </li>
  {% endfor %}
</ul>
{% endif %}
{% endblock %}
```

Add to `app/static/style.css` (end of file):
```css
.clown-grid { list-style: none; padding: 0; display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: var(--space-2); }
.clown-card { display: flex; flex-direction: column; align-items: center; text-align: center; }
.clown-card img { width: 120px; height: 120px; object-fit: cover; border-radius: var(--radius);
  background: var(--color-surface); }
.clown-card-placeholder { object-fit: contain; opacity: 0.4; padding: 1rem; }
.clown-card-name { font-weight: 600; margin-top: 0.4rem; }
.clown-card-tenure { color: var(--color-muted); font-size: 0.85rem; }
.clown-card-milestone { font-size: 0.85rem; }
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/admin.py app/templating.py app/templates/admin/clowns_roster.html app/static/style.css tests/test_clowns_section.py
git commit -m "Add the clowns roster (current + prior years) with tenure badges"
```

---

## Task 7: Lieutenants + Directory

**Files:**
- Modify: `app/routes/admin.py` — two `member_router.get`
- Create: `app/templates/admin/clowns_lieutenants.html`, `app/templates/admin/clowns_directory.html`
- Test: `tests/test_clowns_section.py`

**Interfaces:**
- Consumes: `_all_profiles` (Task 6), `require_member`, the `phone` filter (existing).
- Produces: `GET /admin/clowns/lieutenants` (member), `GET /admin/clowns/directory` (member).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_clowns_section.py`:
```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_clowns_section.py -k "lieutenants or directory"`
Expected: FAIL — 404s.

- [ ] **Step 3: Implement**

`app/routes/admin.py`:
```python
@member_router.get("/clowns/lieutenants")
def clowns_lieutenants(request: Request) -> Response:
    lts = [p for p in _all_profiles() if p.get("is_lieutenant")]
    return templates.TemplateResponse(
        request, "admin/clowns_lieutenants.html", {"lieutenants": lts}
    )


@member_router.get("/clowns/directory")
def clowns_directory(request: Request) -> Response:
    people = [p for p in _all_profiles() if p.get("active", True)]
    return templates.TemplateResponse(
        request, "admin/clowns_directory.html", {"people": people}
    )
```

Create `app/templates/admin/clowns_lieutenants.html`:
```html
{% extends "admin/_base.html" %}
{% block content %}
<h1>Float lieutenants</h1>
{% if not lieutenants %}
<p>No lieutenants listed yet.</p>
{% else %}
{% for l in lieutenants %}
<section class="lieutenant">
  {% if l.photo_url %}<img class="lieutenant-photo" src="{{ l.photo_url }}" alt="{{ l.display_name }}">{% endif %}
  <h2>{{ l.display_name or l.email }}{% if l.lieutenant_title %} — {{ l.lieutenant_title }}{% endif %}</h2>
  {% if l.bio %}<p class="lieutenant-bio">{{ l.bio }}</p>{% endif %}
</section>
{% endfor %}
{% endif %}
{% endblock %}
```

Create `app/templates/admin/clowns_directory.html`:
```html
{% extends "admin/_base.html" %}
{% block content %}
<h1>Directory</h1>
<div class="table-scroll">
<table>
<tr><th>Name</th><th>Phone</th><th>Email</th><th>Emergency contact</th></tr>
{% for p in people %}
<tr>
  <td>{{ p.display_name or p.email }}</td>
  <td>{% if p.phone %}<a href="tel:{{ p.phone }}">{{ p.phone | phone }}</a>{% endif %}</td>
  <td>{% if p.email %}<a href="mailto:{{ p.email }}">{{ p.email }}</a>{% endif %}</td>
  <td>
    {% if p.emergency_contact_name %}{{ p.emergency_contact_name }}{% endif %}
    {% if p.emergency_contact_phone %}<a href="tel:{{ p.emergency_contact_phone }}">{{ p.emergency_contact_phone | phone }}</a>{% endif %}
  </td>
</tr>
{% endfor %}
</table>
</div>
{% endblock %}
```

Add to `app/static/style.css` (end of file):
```css
.lieutenant { margin-top: var(--space-3); display: flex; gap: var(--space-2); align-items: flex-start; }
.lieutenant-photo { width: 96px; height: 96px; object-fit: cover; border-radius: var(--radius); flex: 0 0 auto; }
.lieutenant-bio { white-space: pre-line; }
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/admin.py app/templates/admin/clowns_lieutenants.html app/templates/admin/clowns_directory.html app/static/style.css tests/test_clowns_section.py
git commit -m "Add clowns lieutenants and directory pages"
```

---

## Task 8: Manage page (admin) — official fields, historical riders, account link/unlink

**Files:**
- Modify: `app/routes/admin.py` — `_parse_years`, `_parse_bool`, `_clowns_manage_page`, and five routes
- Create: `app/templates/admin/clowns_manage.html`
- Modify: `tests/test_admin_routes.py` — add `/admin/clowns/manage` to `test_member_is_bounced_from_admin_only_pages`
- Test: `tests/test_clowns_section.py`

**Interfaces:**
- Consumes: `_all_profiles`, `_update_clown_fields`, `_cognito` (existing), `require_admin`.
- Produces:
  - `_parse_years(text: str) -> list[int]` — `"2018-2021, 2023"` → `[2018,2019,2020,2021,2023]`; sorted, de-duped.
  - `_parse_bool(value: str) -> bool` — true for `1/true/yes/y/on` (case-insensitive).
  - `GET /admin/clowns/manage` (admin).
  - `POST /admin/clowns/manage` (admin) — add a historical (login-less) rider.
  - `POST /admin/clowns/manage/{clown_id}` (admin) — set `years_ridden`, `is_lieutenant`, `lieutenant_title`, `active`.
  - `POST /admin/clowns/manage/{clown_id}/link` (admin), `POST /admin/clowns/manage/{clown_id}/unlink` (admin).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_clowns_section.py`:
```python
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
```
(`admin_routes` is imported in `tests/test_clowns_section.py` from Task 3.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_clowns_section.py -k "parse_years or manage"`
Expected: FAIL — `_parse_years` missing, routes 404.

- [ ] **Step 3: Implement**

`app/routes/admin.py`, Clowns section:
```python
def _parse_years(text: str) -> list[int]:
    """'2018-2021, 2023' -> [2018, 2019, 2020, 2021, 2023]. Splits on any run
    of non-digit/non-hyphen; expands A-B inclusive; de-dupes; sorts."""
    years: set[int] = set()
    for token in re.split(r"[^\d-]+", (text or "").strip()):
        if not token:
            continue
        if "-" in token.strip("-"):
            lo, hi = token.split("-", 1)
            if lo.isdigit() and hi.isdigit() and int(lo) <= int(hi):
                years.update(range(int(lo), int(hi) + 1))
        elif token.isdigit():
            years.add(int(token))
    return sorted(years)


def _parse_bool(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _clowns_manage_page(
    request: Request, error: str | None = None, status_code: int = 200
) -> Response:
    return templates.TemplateResponse(
        request, "admin/clowns_manage.html",
        {"profiles": _all_profiles(), "error": error}, status_code=status_code,
    )


@router.get("/clowns/manage")
def clowns_manage(request: Request) -> Response:
    return _clowns_manage_page(request)


@router.post("/clowns/manage")
def add_historical_rider(
    request: Request, display_name: str = Form(...), years_ridden: str = Form(""),
) -> Response:
    name = display_name.strip()
    if not name:
        return _clowns_manage_page(request, error="Name is required.", status_code=400)
    CLOWN_PROFILES().put_item(Item={
        "clown_id": f"clown_{uuid.uuid4().hex}",
        "cognito_sub": None, "email": None,
        "display_name": name, "photo_url": None, "bio": None,
        "phone": None, "address": None,
        "emergency_contact_name": None, "emergency_contact_phone": None,
        "years_ridden": _parse_years(years_ridden),
        "is_lieutenant": False, "lieutenant_title": None, "active": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return RedirectResponse("/admin/clowns/manage", status_code=303)


@router.post("/clowns/manage/{clown_id}")
def update_clown_official(
    request: Request, clown_id: str,
    years_ridden: str = Form(""), is_lieutenant: str = Form(""),
    lieutenant_title: str = Form(""), active: str = Form(""),
) -> Response:
    _update_clown_fields(clown_id, {
        "years_ridden": _parse_years(years_ridden),
        "is_lieutenant": _parse_bool(is_lieutenant),
        "lieutenant_title": lieutenant_title.strip() or None,
        "active": _parse_bool(active),
    })
    return RedirectResponse("/admin/clowns/manage", status_code=303)


@router.post("/clowns/manage/{clown_id}/link")
def link_clown_account(request: Request, clown_id: str, email: str = Form(...)) -> Response:
    wanted = email.strip().lower()
    users = _cognito().list_users(UserPoolId=settings.cognito_user_pool_id).get("Users", [])
    sub = None
    for u in users:
        attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
        if (attrs.get("email") or "").strip().lower() == wanted:
            sub = u["Username"]
            break
    if sub is None:
        return _clowns_manage_page(request, error="No login found with that email.", status_code=400)
    CLOWN_PROFILES().update_item(
        Key={"clown_id": clown_id},
        UpdateExpression="SET cognito_sub = :s", ExpressionAttributeValues={":s": sub},
    )
    return RedirectResponse("/admin/clowns/manage", status_code=303)


@router.post("/clowns/manage/{clown_id}/unlink")
def unlink_clown_account(clown_id: str) -> RedirectResponse:
    CLOWN_PROFILES().update_item(
        Key={"clown_id": clown_id}, UpdateExpression="REMOVE cognito_sub",
    )
    return RedirectResponse("/admin/clowns/manage", status_code=303)
```

Create `app/templates/admin/clowns_manage.html`:
```html
{% extends "admin/_base.html" %}
{% block content %}
<h1>Manage clowns</h1>
{% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}

<div class="table-scroll">
<table>
<tr><th>Name / email</th><th>Account</th><th>Years ridden</th><th>Lieutenant</th><th>Active</th><th></th></tr>
{% for p in profiles %}
<tr>
  <td>{{ p.display_name or "(no name)" }}<br><small>{{ p.email or "no login" }}</small></td>
  <td>
    {% if p.cognito_sub %}
      linked
      <form method="post" action="/admin/clowns/manage/{{ p.clown_id }}/unlink" style="display:inline">
        <button type="submit">unlink</button></form>
    {% else %}
      <form method="post" action="/admin/clowns/manage/{{ p.clown_id }}/link" style="display:inline">
        <input type="email" name="email" placeholder="login email" required>
        <button type="submit">link</button></form>
    {% endif %}
  </td>
  <td colspan="4">
    <form method="post" action="/admin/clowns/manage/{{ p.clown_id }}" class="event-images-form">
      <label>Years <input type="text" name="years_ridden"
             value="{{ p.years_ridden | map('int') | join(' ') }}" placeholder="2019 2021-2023"></label>
      <label><input type="checkbox" name="is_lieutenant" value="1" {% if p.is_lieutenant %}checked{% endif %}> Lieutenant</label>
      <label>Title <input type="text" name="lieutenant_title" value="{{ p.lieutenant_title or '' }}"></label>
      <label><input type="checkbox" name="active" value="1" {% if p.active %}checked{% endif %}> Active</label>
      <button type="submit">Save</button>
    </form>
  </td>
</tr>
{% endfor %}
</table>
</div>

<h2>Add a historical rider (no login)</h2>
<form method="post" action="/admin/clowns/manage" class="event-images-form">
  <label>Name <input type="text" name="display_name" required></label>
  <label>Years <input type="text" name="years_ridden" placeholder="2010 2011"></label>
  <button type="submit">Add</button>
</form>
{% endblock %}
```

In `tests/test_admin_routes.py`, `test_member_is_bounced_from_admin_only_pages` — add `"/admin/clowns/manage"` to the tuple.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/admin.py app/templates/admin/clowns_manage.html tests/test_clowns_section.py tests/test_admin_routes.py
git commit -m "Add the admin clowns manage page (years, lieutenant, active, account link)"
```

---

## Task 9: Resources page (member view + inline admin controls)

**Files:**
- Modify: `app/routes/admin.py` — `_sorted_krewe_links`, `_write_krewe_link_order`, one `member_router.get` + four `router.post`
- Create: `app/templates/admin/clowns_resources.html`
- Test: `tests/test_clowns_section.py`

**Interfaces:**
- Consumes: `paginate`, `KREWE_LINKS`, `KreweLink`, `require_member`, `request.state.is_admin`.
- Produces:
  - `_sorted_krewe_links() -> list[dict]` — by `(sort_order, label.lower())`.
  - `GET /admin/clowns/resources` (member).
  - `POST /admin/clowns/resources` (admin) — add; new links get `sort_order = len(existing)`.
  - `POST /admin/clowns/resources/{link_id}` (admin) — update label/url/description.
  - `POST /admin/clowns/resources/{link_id}/move` (admin) — `direction` form field (`up`/`down`), swap + renumber to `0..n`.
  - `POST /admin/clowns/resources/{link_id}/delete` (admin) — delete + renumber.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_clowns_section.py`:
```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_clowns_section.py -k resources`
Expected: FAIL — 404s.

- [ ] **Step 3: Implement**

`app/routes/admin.py`, Clowns section:
```python
def _sorted_krewe_links() -> list[dict]:
    items = paginate(KREWE_LINKS().scan)
    return sorted(items, key=lambda x: (int(x.get("sort_order") or 0), (x.get("label") or "").lower()))


def _write_krewe_link_order(ids: list[str]) -> None:
    for position, link_id in enumerate(ids):
        KREWE_LINKS().update_item(
            Key={"link_id": link_id},
            UpdateExpression="SET sort_order = :s",
            ExpressionAttributeValues={":s": position},
        )


def _resources_page(request: Request, error: str | None = None, status_code: int = 200) -> Response:
    return templates.TemplateResponse(
        request, "admin/clowns_resources.html",
        {"links": _sorted_krewe_links(), "error": error}, status_code=status_code,
    )


@member_router.get("/clowns/resources")
def clowns_resources(request: Request) -> Response:
    return _resources_page(request)


@router.post("/clowns/resources")
def create_krewe_link(
    request: Request, label: str = Form(...), url: str = Form(...), description: str = Form(""),
) -> Response:
    label, url = label.strip(), url.strip()
    if not label or not url:
        return _resources_page(request, error="Label and URL are required.", status_code=400)
    if not url.startswith(("http://", "https://")):
        return _resources_page(request, error="The URL must start with http:// or https://.", status_code=400)
    KREWE_LINKS().put_item(Item={
        "link_id": f"lnk_{uuid.uuid4().hex}",
        "label": label, "url": url, "description": description.strip() or None,
        "sort_order": len(_sorted_krewe_links()),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return RedirectResponse("/admin/clowns/resources", status_code=303)


@router.post("/clowns/resources/{link_id}")
def update_krewe_link(
    request: Request, link_id: str,
    label: str = Form(...), url: str = Form(...), description: str = Form(""),
) -> Response:
    label, url = label.strip(), url.strip()
    if not label or not url:
        return _resources_page(request, error="Label and URL are required.", status_code=400)
    if not url.startswith(("http://", "https://")):
        return _resources_page(request, error="The URL must start with http:// or https://.", status_code=400)
    KREWE_LINKS().update_item(
        Key={"link_id": link_id},
        UpdateExpression="SET label = :l, #u = :u, description = :d",
        ExpressionAttributeNames={"#u": "url"},
        ExpressionAttributeValues={":l": label, ":u": url, ":d": description.strip() or None},
    )
    return RedirectResponse("/admin/clowns/resources", status_code=303)


@router.post("/clowns/resources/{link_id}/move")
def move_krewe_link(link_id: str, direction: str = Form(...)) -> RedirectResponse:
    ids = [x["link_id"] for x in _sorted_krewe_links()]
    if link_id in ids:
        i = ids.index(link_id)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            _write_krewe_link_order(ids)
    return RedirectResponse("/admin/clowns/resources", status_code=303)


@router.post("/clowns/resources/{link_id}/delete")
def delete_krewe_link(link_id: str) -> RedirectResponse:
    KREWE_LINKS().delete_item(Key={"link_id": link_id})
    _write_krewe_link_order([x["link_id"] for x in _sorted_krewe_links()])
    return RedirectResponse("/admin/clowns/resources", status_code=303)
```
Note: `url` is a DynamoDB reserved word — hence the `#u` alias in `update_krewe_link`. `label` and `description` are not reserved.

Create `app/templates/admin/clowns_resources.html`:
```html
{% extends "admin/_base.html" %}
{% block content %}
<h1>Resources</h1>
{% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}

{% if not links %}
<p>No links yet.</p>
{% else %}
<ul class="krewe-links">
{% for lk in links %}
  <li>
    <a href="{{ lk.url }}" target="_blank" rel="noopener">{{ lk.label }}</a>
    {% if lk.description %}<p class="krewe-link-desc">{{ lk.description }}</p>{% endif %}
    {% if request.state.is_admin %}
    <form method="post" action="/admin/clowns/resources/{{ lk.link_id }}" class="faq-admin-fields">
      <label>Label <input type="text" name="label" value="{{ lk.label }}" required></label>
      <label>URL <input type="url" name="url" value="{{ lk.url }}" required></label>
      <label>Description <input type="text" name="description" value="{{ lk.description or '' }}"></label>
      <div class="faq-admin-actions">
        <span class="faq-admin-num">#{{ loop.index }}</span>
        <button class="icon-btn" type="submit" name="direction" value="up"
                formaction="/admin/clowns/resources/{{ lk.link_id }}/move" formnovalidate
                {% if loop.first %}disabled{% endif %} aria-label="Move up" title="Move up">&#9650;</button>
        <button class="icon-btn" type="submit" name="direction" value="down"
                formaction="/admin/clowns/resources/{{ lk.link_id }}/move" formnovalidate
                {% if loop.last %}disabled{% endif %} aria-label="Move down" title="Move down">&#9660;</button>
        <span class="faq-admin-spacer"></span>
        <button type="submit">Save</button>
        <button type="submit" class="btn-danger"
                formaction="/admin/clowns/resources/{{ lk.link_id }}/delete" formnovalidate
                onclick="return confirm('Delete this link?')">Delete</button>
      </div>
    </form>
    {% endif %}
  </li>
{% endfor %}
</ul>
{% endif %}

{% if request.state.is_admin %}
<h2>Add a link</h2>
<form method="post" action="/admin/clowns/resources" class="faq-admin-fields faq-admin-new">
  <label>Label <input type="text" name="label" required></label>
  <label>URL <input type="url" name="url" placeholder="https://docs.google.com/..." required></label>
  <label>Description <input type="text" name="description"></label>
  <div class="faq-admin-actions"><button type="submit">Add to the bottom</button></div>
</form>
{% endif %}
{% endblock %}
```

Add to `app/static/style.css` (end of file):
```css
.krewe-links { padding-left: 1.1rem; }
.krewe-links > li { margin-bottom: var(--space-2); }
.krewe-link-desc { margin: 0.1rem 0 0; color: var(--color-muted); }
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/admin.py app/templates/admin/clowns_resources.html app/static/style.css tests/test_clowns_section.py
git commit -m "Add the clowns Resources links page + admin CRUD"
```

---

## Task 10: CSV import + export

**Files:**
- Modify: `app/routes/admin.py` — `CLOWN_CSV_COLUMNS`, `_clowns_import_page`, and four routes
- Create: `app/templates/admin/clowns_import.html`
- Modify: `tests/test_admin_routes.py` — add `/admin/clowns/import` to `test_member_is_bounced_from_admin_only_pages`
- Test: `tests/test_clowns_section.py`

**Interfaces:**
- Consumes: `_all_profiles`, `_update_clown_fields`, `_parse_years`, `_parse_bool`, `_csv_safe`, `_cognito`, `require_admin`.
- Produces:
  - `CLOWN_CSV_COLUMNS: list[str]` — the 11 recognised header names, `email` first.
  - `GET /admin/clowns/import` (admin) — upload form + summary.
  - `POST /admin/clowns/import` (admin) — form fields: `file` (UploadFile), `invite_missing` (checkbox `"1"`).
  - `GET /admin/clowns/import/template` (admin) — a CSV with just the header row.
  - `GET /admin/clowns/export.csv` (admin) — every profile in `CLOWN_CSV_COLUMNS` order.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_clowns_section.py`:
```python
import io


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
                 lieutenant_title="Float 1 Lieutenant", email="x@example.com", cognito_sub=None)
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
    assert p["is_lieutenant"] is True
    assert [int(y) for y in p["years_ridden"]] == [2021, 2022]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_clowns_section.py -k "import or export"`
Expected: FAIL — 404s.

- [ ] **Step 3: Implement**

`app/routes/admin.py`, Clowns section:
```python
CLOWN_CSV_COLUMNS = [
    "email", "display_name", "phone", "address", "bio",
    "emergency_contact_name", "emergency_contact_phone",
    "years_ridden", "is_lieutenant", "lieutenant_title", "active",
]
_CLOWN_CSV_TEXT_COLUMNS = (
    "display_name", "phone", "address", "bio",
    "emergency_contact_name", "emergency_contact_phone", "lieutenant_title",
)


def _clowns_import_page(
    request: Request, summary: dict | None = None,
    error: str | None = None, status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request, "admin/clowns_import.html",
        {"summary": summary, "error": error, "columns": CLOWN_CSV_COLUMNS},
        status_code=status_code,
    )


@router.get("/clowns/import")
def clowns_import_form(request: Request) -> Response:
    return _clowns_import_page(request)


@router.get("/clowns/import/template")
def clowns_import_template() -> Response:
    buf = io.StringIO()
    csv.writer(buf).writerow(CLOWN_CSV_COLUMNS)
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="clowns-template.csv"'},
    )


@router.get("/clowns/export.csv")
def clowns_export() -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CLOWN_CSV_COLUMNS)
    for p in _all_profiles():
        writer.writerow([
            _csv_safe(p.get("email")),
            _csv_safe(p.get("display_name")),
            _csv_safe(p.get("phone")),
            _csv_safe(p.get("address")),
            _csv_safe(p.get("bio")),
            _csv_safe(p.get("emergency_contact_name")),
            _csv_safe(p.get("emergency_contact_phone")),
            " ".join(str(int(y)) for y in (p.get("years_ridden") or [])),
            "yes" if p.get("is_lieutenant") else "no",
            _csv_safe(p.get("lieutenant_title")),
            "yes" if p.get("active", True) else "no",
        ])
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="clowns.csv"'},
    )


@router.post("/clowns/import")
def clowns_import(
    request: Request,
    file: UploadFile = File(...),
    invite_missing: str = Form(""),
) -> Response:
    raw = file.file.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    headers = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    if "email" not in headers:
        return _clowns_import_page(request, error="The CSV needs an 'email' column.", status_code=400)

    profiles = paginate(CLOWN_PROFILES().scan)
    by_sub = {p["cognito_sub"]: p for p in profiles if p.get("cognito_sub")}
    by_email = {
        (p.get("email") or "").strip().lower(): p
        for p in profiles if not p.get("cognito_sub") and p.get("email")
    }
    cognito_by_email: dict[str, str] = {}
    for u in _cognito().list_users(UserPoolId=settings.cognito_user_pool_id).get("Users", []):
        attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
        if attrs.get("email"):
            cognito_by_email[attrs["email"].strip().lower()] = u["Username"]

    do_invite = bool(invite_missing)
    created = updated = invited = 0
    skipped: list[str] = []

    for row_num, row in enumerate(reader, start=2):
        norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        email = norm.get("email", "")
        if not email:
            skipped.append(f"row {row_num}: missing email")
            continue
        key = email.lower()
        sub = cognito_by_email.get(key)
        profile = by_sub.get(sub) if sub else by_email.get(key)

        fields: dict = {}
        for col in _CLOWN_CSV_TEXT_COLUMNS:
            if norm.get(col):
                fields[col] = norm[col]
        if norm.get("years_ridden"):
            fields["years_ridden"] = _parse_years(norm["years_ridden"])
        if norm.get("is_lieutenant"):
            fields["is_lieutenant"] = _parse_bool(norm["is_lieutenant"])
        if norm.get("active"):
            fields["active"] = _parse_bool(norm["active"])

        if profile:
            _update_clown_fields(profile["clown_id"], fields)
            updated += 1
            continue

        if sub is None and do_invite:
            try:
                resp = _cognito().admin_create_user(
                    UserPoolId=settings.cognito_user_pool_id, Username=email,
                    UserAttributes=[
                        {"Name": "email", "Value": email},
                        {"Name": "email_verified", "Value": "true"},
                    ],
                )
                sub = resp["User"]["Username"]
                invited += 1
            except ClientError:
                skipped.append(f"row {row_num}: could not invite {email}")
                continue

        CLOWN_PROFILES().put_item(Item={
            "clown_id": f"clown_{uuid.uuid4().hex}",
            "cognito_sub": sub, "email": email,
            "display_name": fields.get("display_name"),
            "photo_url": None,
            "bio": fields.get("bio"),
            "phone": fields.get("phone"),
            "address": fields.get("address"),
            "emergency_contact_name": fields.get("emergency_contact_name"),
            "emergency_contact_phone": fields.get("emergency_contact_phone"),
            "years_ridden": fields.get("years_ridden", []),
            "is_lieutenant": fields.get("is_lieutenant", False),
            "lieutenant_title": fields.get("lieutenant_title"),
            "active": fields.get("active", True),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        created += 1

    return _clowns_import_page(request, summary={
        "created": created, "updated": updated, "invited": invited, "skipped": skipped,
    })
```

Create `app/templates/admin/clowns_import.html`:
```html
{% extends "admin/_base.html" %}
{% block content %}
<h1>Import clowns from a spreadsheet</h1>
{% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}

{% if summary %}
<div class="import-summary">
  <p>created {{ summary.created }} · updated {{ summary.updated }} · invited {{ summary.invited }}
     · skipped {{ summary.skipped | length }}</p>
  {% if summary.skipped %}
  <ul>{% for s in summary.skipped %}<li>{{ s }}</li>{% endfor %}</ul>
  {% endif %}
</div>
{% endif %}

<p>CSV columns (a header row is required; <code>email</code> is the only required value per row —
   any others are optional and only the columns present are written):
   <code>{{ columns | join(", ") }}</code>.
   <code>years_ridden</code> accepts a list like <code>2019 2021-2023</code>.
   <a href="/admin/clowns/import/template">Download a blank template</a> ·
   <a href="/admin/clowns/export.csv">Export current data</a>.</p>

<form method="post" action="/admin/clowns/import" enctype="multipart/form-data">
  <label>CSV file <input type="file" name="file" accept=".csv,text/csv" required></label>
  <label><input type="checkbox" name="invite_missing" value="1">
    Also send a Cognito login invite to emails that don't have an account yet</label>
  <button type="submit">Import</button>
</form>
{% endblock %}
```

In `tests/test_admin_routes.py`, `test_member_is_bounced_from_admin_only_pages` — add `"/admin/clowns/import"` to the tuple.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/admin.py app/templates/admin/clowns_import.html tests/test_clowns_section.py tests/test_admin_routes.py
git commit -m "Add clown CSV import and export"
```

---

## Task 11: Hub links to manage/import for admins + final wiring check

Small task: make sure the hub's admin links exist (they do from Task 4's template), the roster/lieutenants/directory/resources are reachable from the hub for everyone, and run a template-render smoke check for each new page. No new routes.

**Files:**
- Test: `tests/test_clowns_section.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_clowns_section.py`:
```python
def test_hub_shows_admin_links_only_to_admins(dynamodb_tables):
    m, mctx = _client(admin=False)
    try:
        body = m.get("/admin/clowns").text
        assert 'href="/admin/clowns/roster"' in body
        assert 'href="/admin/clowns/manage"' not in body
        assert 'href="/admin/clowns/import"' not in body
    finally:
        mctx.stop()
    a, actx = _client(sub="admin-2", admin=True)
    try:
        body = a.get("/admin/clowns").text
        assert 'href="/admin/clowns/manage"' in body
        assert 'href="/admin/clowns/import"' in body
    finally:
        actx.stop()


def test_every_clowns_view_page_renders_for_a_member(dynamodb_tables):
    m, mctx = _client(admin=False)
    try:
        for path in ("/admin/clowns", "/admin/clowns/roster", "/admin/clowns/lieutenants",
                     "/admin/clowns/directory", "/admin/clowns/resources", "/admin/clowns/profile"):
            assert m.get(path).status_code == 200, path
    finally:
        mctx.stop()
```

- [ ] **Step 2: Run to verify it fails or passes**

Run: `.venv/bin/python -m pytest -q tests/test_clowns_section.py -k "hub_shows_admin or every_clowns_view"`
Expected: PASS if Task 4's template already gated the admin links with `{% if request.state.is_admin %}` (it does). If the first test fails on the admin-link visibility, adjust `app/templates/admin/clowns_home.html` so the `manage`/`import` `<li>` is inside `{% if request.state.is_admin %}` (as written in Task 4).

- [ ] **Step 3: (only if a test failed) fix the hub template**

Ensure the admin `<li>` in `clowns_home.html` is wrapped exactly as in Task 4.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_clowns_section.py app/templates/admin/clowns_home.html
git commit -m "Cover clowns hub link visibility + page render smoke"
```

---

## Verification

1. **Full suite**

   Run: `.venv/bin/python -m pytest -q` — all green.

2. **Infra synth** (needs Node on PATH; the repo installs CDK in `infra/.venv`)

   ```bash
   export PATH="$HOME/.nvm/versions/node/<version>/bin:$PATH"
   cd infra
   .venv/bin/python -c "
   import aws_cdk as cdk
   from jesters_rodeo_stack import JestersRodeoStack
   a = cdk.App(context={'site_url':'https://jesters.rodeo','domain_name':'jesters.rodeo','hosted_zone_id':'Z0121493A7MWN9K1E9EF','cognito_domain_prefix':'jesters-rodeo-admin','ses_sender_email':'jtgraves@gmail.com'})
   s = JestersRodeoStack(a, 'JestersRodeoStack', env=cdk.Environment(account='111111111111', region='us-east-2'))
   t = a.synth().get_stack_by_name('JestersRodeoStack').template
   tables = [n for n,r in t['Resources'].items() if r['Type']=='AWS::DynamoDB::Table']
   assert any('ClownProfilesTable' in n for n in tables), tables
   assert any('KreweLinksTable' in n for n in tables), tables
   print('synth ok:', len(tables), 'tables')
   "
   ```
   Expects `synth ok: 10 tables` and no other resource changes vs. the last deploy (`cdk diff` for a full picture).

3. **Deploy**: `cd infra && cdk deploy -c ses_sender_email=jtgraves@gmail.com`. Two new tables, no data migration, no new IAM.

4. **Manual smoke**
   - Sign in as a non-admin clown → the "Clowns" nav link appears → the hub opens → a profile row is created (check `ClownProfiles` in the console) → "My profile" saves a name, a photo, and an emergency contact.
   - Sign in as an admin → "Manage clowns" sets a rider's `years_ridden` (try `2019 2021-2023`) and lieutenant status → the roster and lieutenants pages reflect it → add a historical rider with name + years and confirm they show on that past-year roster only.
   - Resources: add a Google-Docs link as admin, confirm a member sees it without the edit controls.
   - Import: upload a two-row CSV (`email,display_name,years_ridden`) with one known and one unknown email, invite box unchecked → summary shows `created 1 · updated 1`; the unknown email becomes a login-less profile on the right past-year roster.

## Notes for the executor

- `tests/test_clowns_section.py` builds its own `TestClient` via the `_client()` helper (Task 4) because most new tests need a specific `sub`/`email`/role rather than the fixed `admin_client`/`member_client` fixtures. `_client()` returns `(client, patch_context)`; always `ctx.stop()` in a `finally`.
- DynamoDB via the boto3 *resource* returns numbers as `Decimal`. When comparing years, coerce: `[int(y) for y in (p.get("years_ridden") or [])]`. The route code already does this in `_profile_years` and the CSV paths.
- Reserved DynamoDB words in this feature: `url` (aliased as `#u` in `update_krewe_link`). `name` is **not** used as a bare attribute here (`display_name` is the column). `years_ridden`, `active`, `bio`, `phone`, `address`, `label`, `description`, `email` are all safe as bare names, but `_update_clown_fields` aliases everything defensively anyway.
- New template files all `{% extends "admin/_base.html" %}` so the nav + `request.state.is_admin` are available.
