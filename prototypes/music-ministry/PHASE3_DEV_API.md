# Phase 3 — isolated HTTP API contract (no Wix or V19 changes)

**Status:** synthetic test harness only. This is not a production server, an authentication provider, a deployed web app, or a source of real ministry data. It modifies neither `wcfi.org` nor the existing V19 preview.

## Purpose and trust boundary

`phase3_dev_api.py` exposes a test-only loopback HTTP boundary over `shared_store.py` for two independent clients. `make_loopback_server()` binds only `127.0.0.1`, with an ephemeral port by default. There is deliberately **no public CLI entrypoint, login route, signup route, or HTTP session-issuance route**.

The injected `SyntheticSessionAuthority` creates random opaque cookie values and separate CSRF tokens **only inside test code**. The HTTP handler looks those up in an in-process session table; it does not accept client-supplied `userId`, role, email, or verified-subject headers. A session is never sufficient to retrieve data: `shared_store.py` still checks that the matching application member is currently approved and has the required role for every action. Revoking a membership denies the next request, even if the cookie itself is unexpired. No session token is stored in the database, and no token appears in a source file or URL.

The synthetic authority is intentionally unsuitable for production: it has no Wix signature/token exchange, Wix member identity validation, session persistence across restarts, account onboarding, logout route, MFA, origin-aware reverse proxy, production cookie issuance, or secure-transport requirement. **Never deploy it or connect it to real private data.** A future Wix Headless identity adapter must implement the equivalent verified server-side principal lookup without a browser-controlled identity bypass.

## HTTP contract (test-only)

| Method | Path | Purpose |
| --- | --- | --- |
| GET / HEAD | `/healthz` | Non-sensitive health response only |
| GET | `/api/v1/songs` | Approved songs plus own editor drafts (admin sees all) |
| POST | `/api/v1/songs` | Create a draft song with server-generated ID |
| PATCH | `/api/v1/songs/{id}` | Version-checked editor draft update (`song`, `expectedRevision`) |
| POST | `/api/v1/songs/{id}/approve` | Administrator approval |
| POST | `/api/v1/catalogue/import` | Atomic v1 catalogue import into drafts (editor+) |
| GET | `/api/v1/lineups` | Published lineups plus own leader drafts; strips notes and private fields |
| POST | `/api/v1/lineups` | Create a draft lineup with server-generated ID |
| PATCH | `/api/v1/lineups/{id}` | Version-checked owner draft update (`lineup`, `expectedRevision`) |
| POST | `/api/v1/lineups/{id}/publish` | Administrator publication |

Cookie: `wcfi_dev_session` (synthetic test fixture only). Mutations also require `X-WCFI-CSRF` matching a separate fixture-generated token; a cross-site Origin or Sec-Fetch-Site is denied. The local Host must match the server's loopback address and port. No permissive CORS or OPTIONS support is provided. JSON bodies are restricted to 512 KiB, and errors return non-sensitive codes. HTTP status: 401 unknown, expired or unapproved principal; 403 insufficient role, CSRF or forbidden Origin; 409 conflicting revision/identifier; 422 invalid record; 404 unknown route. Responses use `Cache-Control: no-store`. No real user data, song content, lineups, private ministry snapshots, or secrets are used in tests.

## Automated verification

From a clean checkout with Python 3.11 or newer, in repository root:

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s .\prototypes\music-ministry\tests -p test_phase3_dev_api.py -v
```

CI runs the isolated HTTP tests on Python 3.11 and 3.12. Scenarios include anonymous denial, unapproved membership, forged identity headers, CSRF and hostile Origin denial, draft visibility and admin publication, catalogue import, two independent editor sessions observing the same data, stale-edit conflict, session expiry, database-level revocation, response data minimization, and strict loopback binding.

## Next steps before wiring the V19 UI or evaluating a Wix test project

1. Implement a real verified Wix identity adapter **only in an isolated test project**, with a secure authorization-code/PKCE callback flow, anti-CSRF state, correct token validation and issuer/audience binding. Do **not** plug in a Google ID token directly as a Music Ministry role.
2. Implement production-grade sessions (secure HttpOnly SameSite cookies, HTTPS, session renewal/expiry/logout, device revocation, CSRF, abuse limits and audit retention).
3. Add a staged browser backup/export for V19 song drafts, saved lineups, arrangements, and preferences before attempting any migration. Existing song-only JSON export is not enough.
4. Provide field-specific scheduling permissions and approved-chart revision workflows. Current HTTP lineup list intentionally omits `notes` even for leaders until that policy is implemented; it is not a full editor transport.
5. Prove two real browser clients can sync safely through a properly authenticated development API with automated end-to-end and physical iOS Safari tests.
6. Only after Phase 6 approval modify the real Wix site's CMS, member configuration, OAuth client, pages, domain or public deployment.