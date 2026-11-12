# Music Ministry Phase 3 — isolated shared-data prototype

Status: **development prototype only**. Production member login, shared APIs, and deployment are **not implemented**. This deliberately changes no Wix site, Wix CMS, member configuration, public domain, or the existing V19 preview. It introduces no credentials and no real ministry content.

## Architecture decision in progress

- **Identity:** Preferred integration candidate is Wix Members via Wix Headless OAuth 2 authorization-code + PKCE (including Wix-managed Google social login). Authentication is not yet configured; no HTTP callback, token verifier, or session exchange exists in this branch. An approved Wix site member must also have an approved *application* membership. Google login alone never grants app access.
- **App:** Preserve the existing songbook and lineup web prototype and its current private read-only snapshot until migration is verified. The first backend milestone is a standalone SQLite **domain prototype** with no socket listener and no login UI. It is not used by V19.
- **Storage candidates:** (A) Wix CMS with Wix backend methods and member OAuth; (B) independently hosted Postgres with a Wix identity adapter and application-level role checks. The local SQLite prototype does not decide the eventual database. Wix CMS's basic ANYONE/SITE_MEMBER/AUTHOR/ADMIN permissions alone are not sufficient for all app roles; sensitive operations would require explicit backend checks.
- **Existing-site integration:** Wix documents adding an independently hosted frontend to an existing Wix site. Setting up an OAuth headless client and redirect URI is a change to that Wix site's settings and therefore deferred until Phase 6. A wholly separate disposable Wix test project could be considered only if explicitly authorized. No Wix APIs are called by this prototype.
- **Alternatives:** Planning Center Services and WorshipTools Planning are candidate off-the-shelf worship-planning systems. Proclaim Music remains a candidate song library; Faithlife Equip's former church-management offering was sunset in 2023. These should be evaluated before production migration and should not silently replace the existing WCFI prototype.

## First implemented slice: `shared_store.py`

- SQLite records for approved app members and app roles; song drafts/approved songs; lineup drafts/published lineups; minimal audit events.
- Domain methods require a **server-verified subject**. This is **not an authentication implementation**. No browser-supplied user/role data may be passed as trusted input. First admin provisioning is intentionally left to a future secure bootstrap mechanism; tests seed a synthetic admin directly into a temporary database.
- Approved members can see approved songs and published lineups. Song editors see their own draft songs; leaders see their own draft lineups; admins have broad access. Only admins can approve songs and publish lineups. A lineup cannot reference unapproved songs.
- Song and lineup edits use optimistic revision numbers. Approved songs and published lineups are immutable in this slice; approval-safe revision proposals are a future task.
- Export-compatible version-1 song imports preserve ordered sections and inline chord lines. Imports are atomic and fail on ID collisions; no data is overwritten. Existing browser data is unchanged.
- Test fixtures are synthetic. No live JSON catalogues, private user names, copyrighted lyrics or tokens are included.

## Local isolated tests

From repository root:

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s .\prototypes\music-ministry\tests -p test_shared_store.py -v
```

No server starts, no network calls occur, no Wix settings change, and databases are temporary files created under test-managed directories.

## Production gaps before any member deployment

1. Select app backend strategy: Wix CMS and custom Wix backend, or independent Postgres/API + Wix Headless. Independently benchmark costs, permissions, backup/export, and portability.
2. Implement actual Wix OAuth callback/session validation and an authenticated server boundary; add authentication/authorization tests over real HTTP boundaries with simulated identities, not fake production headers.
3. Implement secure first-admin enrollment, onboarding, approval, account-linking, logout, expiry/revocation, and CSRF protection. Protect all endpoints, including search, exports, and file downloads.
4. Add revision proposals for approved charts and published lineups, preserving approved versions until a new one is reviewed.
5. Handle rich per-service arrangements, team availability, private notes, and nested field visibility without disclosing them to regular members.
6. Add browser backup/export for the current V19 local song workshop **and** lineup/draft/arrangement state before changing browser storage. The V19 song-only export omits drafts and lineup arrangements.
7. Build end-to-end tests across multiple sessions and a real shared persistence service, including concurrent edits and interrupted migration. Add manual iOS Safari checks.
8. Review distribution permissions for copyrighted chart/lyric content. Member-only access does not itself grant content licenses.
9. Do not configure or publish to the real Wix website until Phase 6 and explicit authorization.

## Public documentation consulted (November 2026)

- https://dev.wix.com/docs/go-headless/self-managed-headless/get-started/add-a-frontend-to-an-existing-wix-site
- https://dev.wix.com/docs/go-headless/authentication/members/about-member-login
- https://dev.wix.com/docs/go-headless/authentication/members/identity-providers/sign-in-with-google-or-facebook-rest
- https://support.wix.com/en/article/site-members-managing-signup-login-and-security-settings-for-your-site-members
- https://dev.wix.com/docs/api-reference/business-solutions/cms/collection-management/data-collections/data-collection-object
- https://www.planningcenter.com/services
- https://www.worshiptools.com/en-us/planning
- https://support.proclaim.logos.com/hc/en-us/articles/19864373315341-What-is-Proclaim-Music
- https://www.logos.com/grow/faithlife-strategic-direction-moving-forward/
## V19 migration compatibility preflight (read-only)

The `phase3_migration_plan.py` module inspects versioned `wcfi-music-local-backup` v1 envelopes and reports only aggregate counts and categorical blockers; song lyrics, chart lines, notes and titles are not included in reports. It does not write to SQLite, modify browser storage, start an HTTP server or contact Wix. It deliberately **never declares automatic migration ready**.

Compatibility gaps are explicit: browser-local lineups may have no service date, may refer to demo or private-snapshot-only song IDs, and may contain service-specific arrangements not yet modeled in the shared store. Invalid/incomplete song editor drafts, the current unsaved lineup draft, and legacy browser state require separate handling. Demo lineups are excluded. The planner validates authorable songs against the existing shared-store schema and flags incomplete or incompatible records instead of inventing missing keys, dates, identities, or permissions.

To run synthetic tests from the repository root:

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s .\prototypes\music-ministry\tests -p test_phase3_migration_plan.py -v
```

Never upload real browser backups to a public Git repository, share them in CI logs, or automatically import them. If an owner chooses to inspect a personal backup locally, run the module only on that machine and keep outputs metadata-only. A future migration must map approved app memberships, preserve all arrangements and drafts, import transactionally, and provide independent round-trip verification before deleting or replacing local work.

## Atomic private draft staging (synthetic development only)

V19 allows saved personal lineups with an empty date. The shared domain now accepts `date: ""` for a **draft**; publication calls strict date validation and refuses an undated lineup. The read-only migration planner counts compatible undated lineups as *draft-staging candidates* and separately reports a `lineups:publication_date_required` hold. No date is invented.

The isolated module `phase3_migration_stage.py` provides two explicit interfaces:

- `prepare_v19_draft_stage(envelope)` validates a **limited subset** of complete V19 backups and returns only SHA-256 fingerprint and aggregate staging counts; it does not write.
- `stage_v19_drafts(store, verified_admin, envelope, reviewed_sha256=..., confirmed=True)` performs a **single SQLite transaction**, requiring an approved application administrator, an exact matching reviewed digest, and explicit confirmation. Source songs and lineups are stored as *private drafts only*, never auto-approved or auto-published. On identifier conflicts, all inserts are rolled back, including earlier songs.

This experimental stager is deliberately fail-closed: it supports only backups with exactly the current workshop and hub records, no unfinished editor drafts, no saved arrangements, no non-empty personal lineup draft, no legacy browser state, and no unresolved references. Unsupported data must be preserved in the original downloaded backup and handled by a later dedicated migration. It does not import browser-specific preferences, extra authoring fields, or archived local state, and therefore **is not a lossless production migration**. Do not delete existing browser records or treat this staging operation as final cutover.

The trusted administrator subject **must** come from a verified server-side identity adapter; it cannot come from a browser header or form. No public HTTP route, sign-in flow, test user enrollment, credentials or Wix connection has been added. Existing V19 previews, localStorage, and the actual Wix website remain unchanged.

Run only the synthetic tests on the development worktree:

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s .\prototypes\music-ministry\tests -p test_phase3_migration_stage.py -v
```

The migration planner continues to mark automatic production import unavailable because Wix identity/authorization mapping, rich arrangements, legacy state and verified cutover remain incomplete. Never submit real song content or raw local backups to CI or GitHub.

## Bundled demo lineup compatibility (Phase 3)

The read-only preflight and synthetic draft stager now agree on the V19 **bundled demo fixture** (`id = "demo-sunday"`, `demo = true` with a recognizable lineup shape). It is not a user-authored ministry service and must **not** become a published or draft shared ministry lineup. The planner reports `demo_lineups_excluded` and counts all other lineups; the staging preview and result also report this number. Unknown or malformed demo rows are **not silently skipped**; they require review. All records remain intact in the original downloaded browser backup.

The fix resolves a mismatch where the planner excluded the bundled demo but the stager rejected any backup containing it. No source data is deleted or edited. This is still a development-only, opt-in, admin-gated staging component. It does not preserve every browser preference or provide complete production cutover, and no real owner data or current browser session is staged by this branch.

## Disposable SQLite staging rehearsal — Phase 3

The local-only phase3_migration_rehearsal.py performs a narrowly bounded, **temporary** trial of the atomic V19 draft stager. It does **not** create Wix members, connect to an existing shared database, run the HTTP API, alter the original backup, or change browser storage.

1. Read the supplied wcfi-music-local-backup JSON locally, enforcing an 8 MiB limit and rejecting duplicate JSON fields.
2. Run the existing staging eligibility check and compute a canonical SHA-256 fingerprint.
3. Create SQLite inside a temporary directory, seeding synthetic-only admin/member identities **in that temporary database**. No real login, verified church member or production authorization is involved.
4. Stage there, then verify each **normalized** song and lineup by reading it back. Check ownership, revision, draft status, audit count, no publication and no visibility to the synthetic member.
5. Close and remove temporary SQLite, re-hash the original file and print only metadata/digests.

Run with a private development backup from a checked-out worktree using Python 3.11+:

~~~powershell
& .\.venv\Scripts\python.exe -B .\prototypes\music-ministry\phase3_migration_rehearsal.py --backup "C:\path\to\wcfi-local-backup.json"
~~~

The utility offers **no option to target an existing database**. It temporarily writes normalized private content to a local disposable *unencrypted* SQLite file; run only on a machine you trust. It does not print charts or notes. Keep the original backup private.

**This is not a lossless production migration.** Domain validators project source data onto a narrower backend schema. Browser preferences and supplementary per-record fields are not transferred to normalized records. The original downloaded backup remains authoritative. Durable archival or mapping, real identity and access control, and production rollback require separate implementation and testing. The WCFI Wix website remains untouched until Phase 6.
