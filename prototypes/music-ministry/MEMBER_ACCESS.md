# WCFI Music Ministry — member access contract (Phase 2)

Status: **product decision recorded; implementation pending**. This document defines future app authorization, not an implemented sign-in system. It is scoped to `prototypes/music-ministry` and does **not** authorize changes to the live Wix site, public domains, Wix CMS, Wix Members configuration, or production hosting before Phase 6.

## Confirmed product decision

**Member sign-in is required to use the entire Music Ministry application.** The song catalogue, chord/lyric sheets, lineups, lineup maker, resources, search responses, exports, and any future shared editing APIs must not be accessible anonymously.

An unauthenticated visitor may see only a minimal sign-in / access-denied entry screen and non-sensitive static assets. A link to that screen from `wcfi.org` is **Phase 6 work**, not part of this milestone. No publicly browsable song catalogue or lineup page is planned.

Signing in proves identity; it must not, by itself, confer permission to edit or publish. Eligibility and authorization must be checked on the server for every request.

## Default authorization policy (proposed for implementation)

Roles are *application roles*, independent of the eventual identity provider:

| Principal / app role | Browse approved song catalogue and available charts | View published ministry lineups | Edit songs / import charts | Create / edit draft lineups | Publish lineups / manage memberships |
| --- | --- | --- | --- | --- | --- |
| Anonymous, expired session, pending/revoked account | No | No | No | No | No |
| Approved member (`member`) | Yes | Yes | No | No | No |
| Song editor (`song_editor`) | Yes | Yes | Yes | No | No |
| Worship leader (`worship_leader`) | Yes | Yes | No | Yes (authorized drafts) | No |
| Administrator (`admin`) | Yes | Yes | Yes | Yes | Yes |

Roles may be combined (for example, a worship leader who also edits songs); admin has all application permissions. Membership must be active/approved. Exactly **which church accounts receive approved app membership** and who assigns those roles will be determined when the identity provider is selected. The policy above does not assume every account on the public Wix website automatically gains app access.

Songs in the shared library can be **draft**, **approved**, or **archived**. A member sees approved songs; editors see their own drafts and approved songs; admins can review and archive. Draft lineup owners and authorized leaders may edit them. Unpublished lineups and notes are not shown to ordinary members. Members see only published lineups they are permitted to view.

Private information about team members (contact details, individual availability, attendance, internal feedback, conflicts, and source evidence) requires a separate field-level policy. Default: members do **not** receive private contact and administrative notes. An assigned musician may see their own assignment; worship leaders/admins may view scheduling information needed for their responsibilities. Do not use one all-inclusive ministry snapshot as a response for every role.

## Authentication and server enforcement

- In Phase 3, select an identity/session provider and define how it recognizes **approved members**. Candidates may include Wix Members integration or independent authentication; no provider is selected in this document.
- All song, lineup, draft, arrangement, import, export, and resource **data APIs** require a validated session and server-side authorization. Reject unauthenticated calls with 401 and unauthorized calls with 403 (or 404 for intentionally concealed resource existence). Block anonymous access even when a URL is guessed directly.
- Do not confuse hiding navigation buttons or browser routes with authorization. Never trust `role`, `userId`, owner ID, or publication state supplied by the browser to grant privileges.
- Use server-generated stable IDs and revision/version checks for mutation, to prevent silent overwrite by concurrent editors. Record actor, time, and action for mutations and publication.
- Restrict imports by schema, size, section count, and content validation; use server-verified target IDs and authorization. Existing `wcfi-song-catalogue` version 1 export is a *candidate input contract*, not automatically trusted.
- Private authenticated API responses: `Cache-Control: no-store`; verify same-site/session protections, CSRF for cookie-authenticated mutations, rate limiting, logout/session expiry, and revocation. Never store session secrets in the song JSON or publicly served JavaScript. No credential tokens in `localStorage`.
- Ensure song charts and private plan data are **not embedded in a public static build**. Merely protecting `/ministry-data.json` is insufficient if static assets or generated exports reveal real ministry content.
- Review publishing/licensing rights for third-party lyrics and charts before any wider distribution; an authenticated member portal is not automatically exempt from copyright requirements.

## Compatibility with V19 preview

The current V19 app is a **private, owner-only loopback/Dashies preview**, not a member-authenticated service. It reads a separate private snapshot via a read-only endpoint and saves personally authored songs/lineups in browser storage. Its existing private-preview route and snapshot contract must not be mistaken for the Phase 3 security model.

Until shared migration is proven:
1. Keep the existing V19 preview working and do **not** replace or clear browser-local workshop data, drafts, or lineups.
2. Produce an explicit verified backup/export of browser-authored songs and a separate backup of lineup/draft/arrangement state before migration. Current song export excludes lineups and invalid drafts.
3. Develop shared data/authenticated APIs using **synthetic fixtures only**; do not commit real song charts, personal names, private ministry snapshots, login tokens, or screenshots containing personal data.
4. For isolated testing, use separate ports and data stores. **No Wix site, CMS, domain, public URL, or Wix Members changes before Phase 6.**

## Phase 3 acceptance gates

1. Anonymous and unapproved accounts cannot list, retrieve, search, export, or mutate any ministry data, even by direct endpoint calls.
2. Approved members can read approved catalogue entries and published lineups, but cannot create/edit/publish data or access another user's private drafts.
3. Song editors can modify authorized song drafts without gaining publication or membership management privileges.
4. Worship leaders can manage authorized draft lineups and arrangements, but cannot publish unless explicitly granted an additional permission.
5. Administrators can approve/revoke app membership and publish lineups; revocation terminates future access. Unauthorized mutations are rejected and audited.
6. Private team contact and scheduling data are segregated from ordinary member responses, including errors, exports, caches, and logs.
7. Two authorized devices see a consistent shared catalogue; concurrent edits are version checked and recoverable.
8. Browser-local V19 data survives failed/aborted migration; export/import round trips preserve chart content, ordered sections, stable IDs, keys, and arrangement relationships.
9. Tests exercise both client flows and direct API access; physical iOS Safari acceptance remains required.
10. Before production, complete a security and permissions review plus a review of licensing for charts/lyrics.

## Explicitly out of scope

No live Wix modifications; no public app launch; no identity provider connection; no signing up real users; no copying of private data; no code change that removes the current loopback/owner-only gate; and no claim that member sign-in is already implemented.

Related implementation baseline: `prototypes/music-ministry/README.md` (V19), `dist/hub.mjs`, `dist/ministry.mjs`, `dist/lineups.mjs`, and `server.py`.
