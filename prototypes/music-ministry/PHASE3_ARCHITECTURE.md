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