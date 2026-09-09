# Clowns section — members-only krewe area

## Context

The site serves one float — the **Reaux-de-Eaux clowns** — within the Knights
of Babylon krewe. The float throws the annual Jester's Reaux-de-Eaux second
line parade on Knights of Babylon's behalf and donates a portion of the
proceeds to a local charity.

This adds a **members-only section** for the clowns (both plain members and
admins — everyone with a login). It holds the current rider roster, prior-year
rosters, float-lieutenant bios, clown photos and contact info, and a page of
links to the Google Docs/Sheets the float keeps operational data in.

Scope is deliberately narrow: this is one float, not a whole krewe. No
captain, officers, committees, or dues tracking — only float lieutenants.

## Decisions already made (do not re-litigate)

- **Members only.** Every page here requires a signed-in clown. There is no
  public view.
- **Everyone who needs to see it has a Cognito login.** But *historical*
  riders may not — the roster has changed over the years. So clown profiles
  are **not** keyed by the Cognito `sub`; a profile can exist with no login
  and be linked to an account later if that person rejoins.
- **Self-service editing.** A clown edits their own basic fields (photo, bio,
  phone, address, emergency contact). An admin owns the "official" fields
  (which years they rode, lieutenant status/title, active flag) and can edit
  anyone.
- **A profile is created automatically** on a clown's first visit to the
  section (and linked to a pre-seeded profile by email if one exists).
- **Bulk CSV upload** for seeding/maintaining profile data, including prior
  years' rosters (to be provided later).
- **Section name: "Clowns."** The existing Cognito login/role management page
  moves from `/admin/clowns` to `/admin/clown_mgmt` (nav label stays "Clown
  Management", still admin-only) to free the `/admin/clowns` namespace.
- Photos reuse the existing `event-images` S3 bucket. It is public-read, so a
  headshot URL is reachable by anyone who has the link even though the section
  is login-gated. Acceptable for a float.
- CSV only (no `.xlsx` — that would need a new dependency).

## Why the routes live under `/admin/`

`set_session_cookie` scopes the session cookie to `path="/admin"` (that plus
`SameSite=Lax` is the CSRF defence). A top-level `/clowns` route would not
receive the cookie. So the section lives at `/admin/clowns/...`; the nav just
labels it "Clowns". The URL prefix is cosmetic.

## Route move: Clown Management → `/admin/clown_mgmt`

Rename in `app/routes/admin.py` and update every reference:

| Now | Becomes |
| --- | --- |
| `GET /admin/clowns` (`list_clowns`) | `GET /admin/clown_mgmt` |
| `POST /admin/clowns` (`create_clown`) | `POST /admin/clown_mgmt` |
| `POST /admin/clowns/{username}/promote` | `POST /admin/clown_mgmt/{username}/promote` |
| `POST /admin/clowns/{username}/demote` | `POST /admin/clown_mgmt/{username}/demote` |
| `POST /admin/clowns/{username}/delete` | `POST /admin/clown_mgmt/{username}/delete` |

- Template `app/templates/admin/clowns.html` → `admin/clown_mgmt.html`; update
  its form `action`s and the in-handler redirect targets.
- Nav (`admin/_base.html`): the admin-only link now points at
  `/admin/clown_mgmt`; a new **un-gated** `Clowns` link points at
  `/admin/clowns`.
- Tests: repoint every `test_*clown*` URL in `tests/test_admin_routes.py`, the
  `/admin/clowns` entry in `test_member_is_bounced_from_admin_only_pages`, and
  the nav-link assertions in `test_admin_nav_shows_clowns_link` /
  `test_member_nav_hides_admin_links` (now: `Clowns` visible to members,
  `Clown Management` / `/admin/clown_mgmt` hidden from members).

## Data model (`app/models.py`)

```python
class ClownProfile(BaseModel):
    clown_id: str                       # generated, e.g. "clown_<uuid4hex>"
    cognito_sub: str | None = None      # link to a login; None for a historical-only rider
    email: str | None = None            # used to auto-link a login on first visit / CSV match
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
    active: bool = True                  # False = departed; kept for history
    created_at: str


class KreweLink(BaseModel):
    link_id: str                        # "lnk_<uuid4hex>"
    label: str
    url: str
    description: str | None = None
    sort_order: int = 0
    created_at: str
```

- `len(years_ridden)` is the tenure count; the list is the history.
- `active` drives the directory and "current riders" views; it does **not**
  hide someone from a past-year roster their `years_ridden` includes.

## Infrastructure (`infra/jesters_rodeo_stack.py`)

Two new tables, same shape as `FaqEntriesTable` (PAY_PER_REQUEST, RETAIN, no
GSI):

- `ClownProfilesTable` — partition key `clown_id`
- `KreweLinksTable` — partition key `link_id`

Wire through: add to `_create_tables()` return dict; add
`CLOWN_PROFILES_TABLE` / `KREWE_LINKS_TABLE` to `common_env`; the existing
`for table in tables.values(): table.grant_read_write_data(app_lambda)` loop
covers read/write. **No new IAM** — the CSV import's "invite" path calls
`cognito-idp:AdminCreateUser` / `ListUsers`, both already granted for Clown
Management.

Wire the two table names through `app/config.py` (`clown_profiles_table`,
`krewe_links_table`), `app/db.py` (`CLOWN_PROFILES()`, `KREWE_LINKS()`), and
`tests/conftest.py` (env defaults + `create_table` in `dynamodb_tables`).

## Auth

Reuse the existing `member_router` (dep `require_member`) / `router` (dep
`require_admin`) split in `app/routes/admin.py`:

- **`member_router`**: the hub, roster, roster-by-year, lieutenants,
  directory, resources (GET), and My Profile (GET + POST).
- **`router`** (admin only): manage, resources add/edit/delete/reorder (POST),
  CSV import (GET + POST), CSV export.

`require_member` already returns the full Cognito claims dict, which includes
`sub` and `email` (the app requests the `email` scope).

## First-visit profile resolution

A helper — `_my_profile(claims) -> dict` — called at the top of the hub and
My Profile views (and anywhere the caller needs the current clown's profile):

1. Scan `ClownProfiles` for `cognito_sub == claims["sub"]`. If found, return
   it. (Small table; a scan is fine. No GSI.)
2. Else scan for a profile with **no** `cognito_sub` and `email ==
   claims["email"]` (case-insensitive). If found, `update_item` to set
   `cognito_sub`, then return it. This is how a pre-seeded or returning
   rider's history attaches to their new account.
3. Else create a fresh profile: `clown_id = f"clown_{uuid4().hex}"`,
   `cognito_sub = claims["sub"]`, `email = claims["email"]`, `active = True`,
   `years_ridden = []`, `created_at = now`. Put and return.

## Pages

All templates extend `admin/_base.html`. Routes are under `/admin/clowns`.

### `GET /admin/clowns` — hub (member)
Calls `_my_profile`. Links to Roster, Lieutenants, Directory, Resources, and
"My profile". If the caller's profile is sparse (no `display_name` or
`photo_url`), show a one-line nudge to fill it in.

### `GET /admin/clowns/roster?year=<Y>` — roster (member)
- Default `year` = the max `year` across `EVENTS()` scan; if there are no
  events, `date.today().year`.
- Riders = all `ClownProfile` where `year in years_ridden`, sorted by
  `display_name` (fallback `email`).
- Render a photo grid: photo (or a fleur-de-lis placeholder), name, and a
  tenure badge — `"{ordinal(len(years_ridden))} year"`, plus a milestone note
  (`🏅 5-year rider`, `10-year`, `15-year`, `20-year`) when the count is a
  multiple of 5.
- A `<select>` of every year present in any profile's `years_ridden` (desc)
  re-loads the page with `?year=`.

### `GET /admin/clowns/lieutenants` — bios (member)
Cards (photo, `display_name`, `lieutenant_title`, `bio`) for profiles where
`is_lieutenant`, sorted by name. `bio` renders with `white-space: pre-line`.

### `GET /admin/clowns/directory` — contact info (member)
Table of `active` profiles: name, phone (via the `phone` filter), email
(mailto), emergency contact name + phone. Scrolls inside `.table-scroll`.

### `GET/POST /admin/clowns/profile` — My Profile (member)
Form pre-filled from `_my_profile(claims)`. Editable: `display_name`, photo
upload (reuse `_maybe_upload_image(file, "clowns", "Clown photo")` + the
`admin/_image_size_guard.html` include), `bio`, `phone`, `address`,
`emergency_contact_name`, `emergency_contact_phone`. POST updates **only that
profile** (looked up again by `sub`, never trusting a form-supplied id).
`years_ridden` / `is_lieutenant` / `active` are **not** on this form.

### `GET/POST /admin/clowns/manage` — admin
- Lists **all** profiles (active first, then by name).
- Per profile, an inline form: `years_ridden` (text input, see parsing
  below), `is_lieutenant` checkbox + `lieutenant_title`, `active` checkbox,
  and a "linked account" control — shows `email` / linked-or-not, with a
  button to **unlink** (`cognito_sub = None`) or a small field to link by
  entering an email that matches a Cognito user.
- `POST /admin/clowns/manage/{clown_id}` applies those fields.
- A "＋ Add a historical rider" form (name + years) creates a login-less
  profile directly, for one-off backfills without a spreadsheet.

### `GET /admin/clowns/resources` — Resources (member; admin controls inline)
One page, one GET on `member_router`. It renders the `KreweLink` list ordered
by `sort_order` then `label` — each `label` is an external link
(`target="_blank" rel="noopener"`) with `description` beneath. When
`request.state.is_admin` is true it also shows management controls, mirroring
the FAQ admin: an add form, and per row an inline edit form + ▲/▼ reorder
(renumber `sort_order` to `0..n`) + delete.

Mutation routes, all on `router` (admin only):
`POST /admin/clowns/resources` (add),
`POST /admin/clowns/resources/{link_id}` (update),
`POST /admin/clowns/resources/{link_id}/move`,
`POST /admin/clowns/resources/{link_id}/delete`.
URL validation: must start `http://` or `https://` (as the charity /
beneficiary website fields do).

### `GET/POST /admin/clowns/import` — CSV upload (admin)
- GET renders an upload form + a "Download CSV template" link + a link to the
  export (round-trip).
- Recognized columns (header row required; names case-insensitive, trimmed):
  `email` (**required per row**), `display_name`, `phone`, `address`, `bio`,
  `emergency_contact_name`, `emergency_contact_phone`, `years_ridden`,
  `is_lieutenant`, `lieutenant_title`, `active`. Unknown columns are ignored.
- Parse with `csv.DictReader`. Reject a file with no `email` header (400 with
  a message).
- Build a `{lower(email): sub}` map once from `cognito-idp:list_users`.
- Per row:
  - Blank `email` → skip, record `(row_number, "missing email")`.
  - Find the target profile: by `cognito_sub` (if the email is in the Cognito
    map) else by unlinked `email` match.
  - **Update** an existing profile: write only columns that are *present in
    the file*. A blank cell for a text column leaves the stored value
    untouched (lets a partial sheet update just `years_ridden`). For
    `years_ridden`, `is_lieutenant`, `active`, a present-but-blank cell is
    also treated as "leave as-is".
  - **Create** a new profile when none matches:
    - email in Cognito map → link `cognito_sub`.
    - email not in Cognito, "invite unknown emails" checkbox on →
      `admin_create_user(email)`, link the returned `Username` (the sub),
      count as *invited*.
    - else → login-less profile (`cognito_sub = None`). This is the historical
      seeding path.
- `years_ridden` parsing (shared with the manage form): split on any
  non-digit-non-hyphen run; expand `A-B` to the inclusive range; `int()` each;
  dedupe; sort. `"2018-2021, 2023"` → `[2018, 2019, 2020, 2021, 2023]`.
- `is_lieutenant` / `active`: truthy = `1`, `true`, `yes`, `y` (case-
  insensitive); anything else falsy.
- Render a result summary: `created N`, `updated N`, `invited N`, and a list
  of skipped rows with reasons. Nothing is committed transactionally — each
  row is written as processed; a mid-file error is reported and processing
  continues.

### `GET /admin/clowns/export.csv` — admin
Dumps every profile in the import column order (using `_csv_safe` for the
free-text fields, `years_ridden` space-joined). Lets an admin pull current
data into Sheets, edit, and re-upload.

## Nav (`app/templates/admin/_base.html`)

- Add, **not** gated by `{% if admin %}`: `<a href="/admin/clowns">Clowns</a>`
  (place it near the top, before the admin-only block).
- The existing admin-only link becomes
  `<a href="/admin/clown_mgmt">Clown Management</a>`.

## Edge cases

- **Clown with a login but no profile** — transient; `_my_profile` creates one
  on their next visit. Views that list "all clowns with logins" are not
  needed here (the roster is profile-driven).
- **Two profiles matching one email** (a login-less seed + a later auto-
  create) — avoided: `_my_profile` step 2 links the existing unlinked profile
  instead of creating a second. The CSV importer follows the same match
  order. If a duplicate is somehow created, `manage` lets an admin see both
  and delete one.
- **Departed rider** — admin sets `active = False`. They stay in
  `years_ridden` history and past rosters; they drop out of the directory and
  the current-year roster (unless the current year is still in their list).
- **Returning rider** — either the email auto-link fires on their first login,
  or an admin links the historical profile to the new account on `manage`.
- **Duplicate year in `years_ridden`** — parser dedupes.
- **A clown editing someone else's profile** — impossible: the profile POST
  re-derives the target from the session `sub`, ignoring any id in the form.

## Testing

`tests/test_admin_routes.py` (+ a new `tests/test_clowns_section.py` if it
gets large):

- **Route move**: every existing `clown_mgmt` test repointed; member bounced
  from `/admin/clown_mgmt`; nav shows `Clowns` to a member and hides
  `Clown Management`.
- **Model**: `ClownProfile` / `KreweLink` defaults.
- **First visit**: no profile → one created, linked to `sub`; a login-less
  profile with a matching email → linked, not duplicated.
- **Roster**: `?year` filters by `years_ridden`; default year = latest event
  year; tenure/milestone badge text.
- **Lieutenants / directory**: only `is_lieutenant` / only `active`.
- **My Profile**: a member edits their own fields; a second member's POST
  touches only their own row; `years_ridden` is not writable here.
- **Manage** (admin): set years/lieutenant/active; link + unlink an account;
  add a historical rider; a member is bounced.
- **Resources**: member sees the list; admin add/edit/reorder/delete; bad URL
  rejected; member bounced from the POSTs.
- **CSV import**: create a login-less profile; update an existing profile
  (only present columns; blank cell leaves value); invite path creates the
  Cognito user (patch `_cognito`) and links; blank-email row skipped; bad
  `years_ridden` reported; `A-B` range expands; result summary counts.
- **CSV export**: round-trips (export output re-imports cleanly).

## Verification

1. `.venv/bin/python -m pytest -q` green.
2. `cdk synth` (from `infra/`, node on PATH) shows the two new tables and no
   unrelated resource changes.
3. `cdk deploy`. Then: sign in as a non-admin clown → "Clowns" nav link
   appears, opens the hub, a profile is auto-created, My Profile saves a photo
   and bio. Sign in as an admin → `manage` sets someone's years and lieutenant
   status; the roster and lieutenants pages reflect it; a small CSV upload
   (a couple of current riders + one historical `email,years_ridden` row with
   an unknown email and the invite box unchecked) produces a login-less
   profile that shows on the right past-year roster.

## Suggested phasing (for the implementation plan)

1. Route move (`/admin/clowns` → `/admin/clown_mgmt`), two new tables + all
   config/db/conftest wiring, the two models.
2. `_my_profile`, the hub, My Profile edit, `manage`.
3. Roster / roster-by-year / lieutenants / directory + the `Clowns` nav link.
4. Resources (member view + admin CRUD/reorder).
5. CSV import + export.
