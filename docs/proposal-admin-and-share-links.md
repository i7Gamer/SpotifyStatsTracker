# Proposal: Admin role + read-only share links

Status: **proposal — not implemented**. Two related features, shippable independently, in this order:

1. **Admin role** — small, fixes an existing exposure (every logged-in user currently sees every other user's email on `/overview`).
2. **Read-only share links** — public tokenized URLs for a user's Wrapped page, no login required for viewers.

---

## Part A: Admin role

### Motivation

- `/overview`'s per-user table shows **all users' emails**, sync status, and play counts to any logged-in user. On a shared instance that's cross-user PII exposure.
- Future admin-only surfaces need a place to hang: backup status, user management, instance settings.

### Data model

Add to `users` (migration `1.17.0 → 1.18.0`, `migrate1_17_0.py`):

```sql
ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0;
```

Follow the existing `addUserPasswordHashColumnIfMissing()` pattern in `Database/repository.py` (guarded, idempotent), plus the matching `CREATE TABLE` line in `Database/db.py`'s `SCHEMA` for fresh installs.

### Who becomes admin

Two mechanisms, both idempotent:

1. **Migration bootstrap**: the migration promotes the earliest-created user (`MIN(created_at)`) — on a self-hosted instance that's whoever set the server up. Only when no admin exists yet, so re-runs are no-ops.
2. **`ADMIN_EMAIL` env var override**: synced at every app startup (in `SpotifyDashboardApp.__init__`, after migrations). If set and that email exists, that user is promoted. This is both the explicit-configuration path and the recovery path if the migration guessed wrong.

Deliberately **no in-app grant/revoke UI in v1**: an admin-management UI is its own privilege-escalation surface (CSRF target, session-hijack target), and a single-admin model covers the actual deployments. Revisit only if someone asks for multi-admin.

### Enforcement

- New `Repository.isAdmin(username)` + memoized `g.isAdmin` in a context processor (mirrors `_injectShareStatus`).
- `/overview`: the aggregate stats stay public (current, documented intent); the **per-user table renders only for admins**. Non-admin logged-in users see the same page an anonymous visitor sees, plus their own row at most.
- Optional (cheap): an "Admin" chip next to the username on `/profile`.

### Tests

- Migration test (`test_migrate1_17_0.py`, mirroring `test_migrate1_15_0.py`'s harness): column added, earliest user promoted, idempotent, no-op when an admin already exists.
- Bootstrap test: `ADMIN_EMAIL` promotes at startup; unknown email is a logged no-op.
- Route tests: non-admin gets no user table (and **no emails anywhere in the response body**), admin gets it; anonymous unchanged.

### Estimated size

~1 day including tests. No UI beyond hiding a table section.

---

## Part B: Read-only share links (Wrapped)

### Motivation

Wrapped pages are built to be shown around. Today the only way is screenshots or handing over credentials. The share system (mutual accepted shares) covers *comparison between accounts*; this covers *broadcast*: "here's my year, look."

### Scope decision: Wrapped only in v1

**Compare links are explicitly out of scope.** A Compare page exposes a *second* user's listening data; publishing it needs both parties' consent, which needs consent UX on top of the share system. That's a product decision to make separately — don't let it stall Wrapped links.

### Data model

New table (same or next migration):

```sql
CREATE TABLE IF NOT EXISTS share_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL UNIQUE,      -- secrets.token_urlsafe(32), ~256 bits
    username    TEXT NOT NULL REFERENCES users(username),
    kind        TEXT NOT NULL CHECK (kind IN ('wrapped')),
    year        INTEGER NOT NULL,          -- pinned at creation; a link is "my 2025", not "my latest"
    created_at  REAL NOT NULL,
    expires_at  REAL                       -- NULL = never
);
CREATE INDEX IF NOT EXISTS idx_share_links_username ON share_links(username);
```

- **Revocation = row deletion**, matching `user_shares`' convention (no soft-delete state to reason about).
- Tokens stored **plaintext**: they gate listening stats only, and the same database already holds far more sensitive material (encrypted cookies). Hashing tokens (SHA-256 at lookup) is a cheap upgrade if we ever want defense-in-depth; note it in the code, don't build it first.

### Routes

| Route | Auth | Purpose |
|---|---|---|
| `POST /wrapped/share-links` | session + CSRF | Create a link for `(kind='wrapped', year)`. Rate-limited (`_RateLimiter` bucket `"share_link"`) so a compromised session can't mint thousands. Redirects back with the URL flashed once and listed on `/profile`. |
| `GET /shared/<token>` | **none** | Public read-only Wrapped page. 404 on unknown/expired token (same response for both — don't reveal which). |
| `GET /shared/<token>/img/tracks/<filename>`, `.../artists/<filename>` | token | Token-scoped image serving (see below). |
| `POST /profile/share-links/<int:link_id>` with `action=revoke` | session + CSRF | Ownership-checked delete, mirroring `profileShareAction`'s pattern exactly. |

### Rendering without a session — the real work

`wrappedPage` currently assumes a session end-to-end. Refactor, don't fork:

1. **Extract data assembly**: pull the cache-read → JSON-parse → `_embed*TextElements` pipeline out of `wrappedPage` into `_buildWrappedContext(db, username, year, groupBy, limit)`. `wrappedPage` and the public route both call it. The mock-detection branch stays in `wrappedPage` (it exists for unit tests only).
2. **`g.db` for timezone**: the embed helpers read `g.db.tz`; the public route sets `g.db` the same way `get_current_user_or_redirect` does. The owner's timezone is the right one to render in (it's *their* year).
3. **Context processors**: `_injectShareStatus` already no-ops safely without a session username — verify with a test, don't assume.
4. **Template**: render `wrapped.html` with a `publicView=True` flag that
   - swaps the base to a stripped public layout (no nav/topbar/search — the current `layout.html` builds nav from session state),
   - suppresses detail links (`_wrapped_list.html` already supports `suppressDetailLinks` from the Compare work),
   - keeps the year fixed (no year badges) and hides the AJAX filter controls in v1 — a static page is the 80% win; interactive public filters mean exposing the AJAX endpoint publicly too, which can come later.
5. **Images**: `/img/<username>/...` is session-gated (`_authorized_image_username`), so covers would 404 for anonymous viewers. Add token-scoped image routes (table above) that validate the token and then serve from the same shared directories; templates take an `imageBase` variable (default `/img/<username>`, public `/shared/<token>/img`).

### Abuse and privacy considerations

- **Token guessing**: 256-bit random tokens make brute force moot, but rate-limit `/shared/<token>` misses per IP anyway (reuse `_RateLimiter`) so the instance doesn't serve as a fast 404 oracle.
- **Anonymous recalculation cost**: a cache-miss on the public route may trigger `recalculateWrappedForYear`. That path is already per-(user, year) lock-guarded with a staleness re-check, so repeated anonymous hits are bounded — verify with a test rather than adding a second guard.
- **Search engines**: `X-Robots-Tag: noindex` on all `/shared/*` responses.
- **PII**: the public page shows the username and listening stats. Never the email. Grep the rendered template context in a test to enforce this.
- **Expiry**: creation UI offers never / 7 days / 30 days (`SHARE_LINK_EXPIRY_CHOICES`). Expired links 404 identically to unknown ones; a periodic cleanup is unnecessary (rows are tiny), delete lazily on lookup.

### UI touchpoints

- Wrapped page (owner view): "Create share link" button next to the year badges → POST → flash the URL.
- Profile page: "Share links" section listing active links (kind, year, created, expiry) with copy + revoke buttons — sits naturally under the existing "Data Sharing" section.

### Tests

- Repo: create/lookup/revoke/expiry semantics; ownership checks on revoke.
- Routes: public page renders the owner's cached wrapped data with no session; unknown/expired/revoked tokens 404 identically; no email in the response; images 404 without a token and serve with one; rate-limit behavior on misses; CSRF on create/revoke.
- Refactor safety: `wrappedPage` behavior unchanged after the `_buildWrappedContext` extraction (the existing wrapped test suite is the guard).

### Estimated size

2–3 days. The `wrappedPage` extraction (step 1) is the risky part — do it as its own commit with the existing tests green before adding any public route.

---

## Suggested implementation order

1. `migrate1_17_0`: `is_admin` column + `share_links` table (one migration, both features' schema).
2. Admin bootstrap + `/overview` gating (ship it — standalone value).
3. `_buildWrappedContext` extraction, no behavior change.
4. Public route + token image routes + public template variant.
5. Creation/revocation UI + rate limiting + noindex.
