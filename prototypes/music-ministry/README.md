# WCFI Music Ministry — simple menu preview

A one-screen tool center following the original sketch: WCFI identity, Music Ministry title, an upcoming-line-up preview above Line-up Maker, Song Catalogue and Resources. A configured private snapshot selects the featured service. All line-ups opens announced plans and personal drafts. Without a configured snapshot, the original synthetic demonstration remains available. Long content scrolls within panels; the main page does not scroll. No canvas or Three.js imports run.

## Brand and navigation

The blue/white theme follows [the public WCFI site](https://www.wcfi.org/), inspected on 2026-10-17. Its main blue is `#094773`; lighter blues and navy surfaces adapt it for the tool UI. Headings use the local sans-serif stack. The official white logo is served locally as `dist/wcfi-logo.png`, downloaded unchanged from [the public site asset](https://static.wixstatic.com/media/c70496_846b491fc4914711851c58f37f06250e~mv2.png). CSS framing excludes the source image's transparent right margin, as the church site's own logo treatment does.

The return arrow and logo form one accessible link to `https://wcfi.org/` in the same tab. The back buttons within tools and Escape still return to the ministry menu. No church homepage files are changed, and no runtime logo fetch goes to Wix.

## Open and run

- [Private preview](https://danzielaptop.tail4c8306.ts.net/dashboards/wcfi-music-ministry/#home)
- [Dashies menu](https://danzielaptop.tail4c8306.ts.net/)
- Local: <http://127.0.0.1:8816/>

From the wcfi-tools repository in PowerShell:

```powershell
powershell -NoProfile -File .\prototypes\music-ministry\start.ps1
```

The independent Python process listens only on `127.0.0.1:8816`. It serves an explicit static-file allowlist plus one fixed ministry-data endpoint, never a directory or arbitrary file selector. Logs, process metadata, private input and verification receipts are under `$env:USERPROFILE\.wcfi\music-ministry-preview`, outside Git. The helper reuses a healthy running instance. No startup task is installed: rerun it after reboot. Keep the laptop awake and Tailscale connected on both devices.

Dashies ID `wcfi-music-ministry` remains an external proxy to `http://127.0.0.1:8816/`, under the existing owner gateway. No additional Tailscale Serve handler, access grant or native dashboard conversion is needed. A server.py change requires restarting only the verified WCFI process; changes to served assets appear on browser refresh. Check process.json against the actual process command before stopping it. Dashies manages the catalog entry separately.

## What works

- **Upcoming line-up:** explicitly featured source plan, leader/practice preview, song/key preview and unresolved-information notice. Songs opens by default with only the main ordered set; separate closing reprises are hidden. Team and Details have separate accessible tabs. One compact notice remains visible, unknown keys use an explained question mark, and inferred keys remain labelled. The missing-key notice says Keys not set; individual availability notes are hidden in the mockup. Assignment provenance remains available and the private snapshot retains the underlying evidence. Details shows practice and opening Scripture, with missing values labelled Not supplied in pastel red, and Copy to maker. Location/start/soundcheck placeholders, clarification lists, review/slide states, source-analysis notes, conflict narratives and the duplicate closing explanation are omitted from Details. The private snapshot retains their source evidence. Unannounced dates are retained as private source context but never rendered as placeholder line-ups.
- **Line-up Maker:** name/date, song selection, reordering/removal, known or unknown performance keys, notes and browser-local save/edit. Copying an announced plan creates a separate personal working copy; saving cannot overwrite its source or replace the featured service. Existing synthetic saves remain available.
- **Song Catalogue:** title search, category filter, service-specific arrangements and supplied recording references. Metadata-only songs do not have invented lyrics/chords or transposition controls. Original demo charts retain their existing transposition functionality when opened.
- **Resources:** reviewed-document links, rehearsal events and resources, preparation workflow, unresolved questions and evidence boundaries from the configured snapshot.
- **Header clock:** device-local 12-hour time with seconds, refreshed every second while visible and immediately when returning to the page. No network request or live-region announcement is used.
- **Navigation:** tap a menu choice directly; on keyboard use Up/Down, Home/End and Enter. Escape leaves chart focus first, then returns to the main menu. Native Tab/Enter remains available. Tool highlights follow hover or visible keyboard focus and clear when the pointer or focus leaves; there is no persistent selected-card highlight. Short enter/back fades respect system reduced motion.


The catalogue is not connected to CCLI. Supplied resource links open only on user selection; there are no embedded external players, automatic source fetches or analytics. This is not a packaged `wcfi` command or Wix deployment. Private source plans are a read-only snapshot, not live Messenger synchronization or a shared editing database.

## Private snapshot contract

The default data file is `$env:USERPROFILE\.wcfi\music-ministry-preview\private\ministry.json`. The server reads it only through `/ministry-data.json`, returns `Cache-Control: no-store`, allows no cross-origin reads and rejects cross-site browser requests and non-loopback Host headers. Remote access must remain behind the existing owner-only Dashies gateway. Trusted processes on this computer can access the loopback service. The raw handoff is never served. Keep real data, screenshots and source links outside Git, devbrain and shared diagnostics.

The JSON contains `schemaVersion: 1`, `featuredLineupId`, review cutoff/label/timezone, `songs`, `lineups`, `events`, `resources`, `unannounced` dates and evidence/pending notes. Song IDs and line-up IDs are unique stable lowercase identifiers. Songs contain titles/categories and optional reference links. A line-up contains its date/leader, practice summary, facts with evidence states, ordered songs with nullable performance keys and arrangement sequences, closing reprise, team assignment/availability, conflicts and source history. Resource links are HTTPS only. Unknown keys are `null`; never use C as a placeholder. Source keys are service-specific, not canonical song keys. An inferred closing key must retain its inference label.

The private snapshot stays in browser memory, separate from saved personal drafts. Source plans have no write endpoint. Absence of a data file returns `available: false` for the synthetic demo; invalid/unavailable data produces a visible loading error instead of silently claiming a valid plan. For isolated synthetic server tests, use `server.py --data-file <fixture-path> --port <loopback-port>`. Source tests must not use real ministry data.

The v2 browser key is `wcfi-music-hub-v2`. The previous `wcfi-music-preview-v1` remains untouched; its valid set is copied into the first v2 draft and display preferences are retained. Dashies applies its per-app browser storage namespace. No network requests to external asset providers occur at runtime.

## Files and checks

`hub.mjs` owns navigation and drafts; `ministry.mjs` validates and renders private snapshot data; `lineups.mjs` validates browser saves, including unknown metadata-song keys; `chords.mjs` transposes the supplied demo charts. `server.py` owns the fixed private endpoint. Historical `scene.mjs`, vendored Three.js, `app.mjs` and `styles.css` remain unused.

```powershell
node --test .\prototypes\music-ministry\tests\chords.test.mjs .\prototypes\music-ministry\tests\lineups.test.mjs .\prototypes\music-ministry\tests\ministry.test.mjs
.\.venv\Scripts\python.exe -m pytest .\prototypes\music-ministry\tests\test_server.py -q
```

V11 adds the local-time header clock and shorter key notice, with private verification receipts under runtime v11. It retains server health version 3. V10 simplified Details to practical values without audit-style metadata. Separate closing-song entries are hidden from Songs; private source evidence is unchanged. V10 verification receipts live under runtime v10. Source-plan tabs support Left/Right, Home/End, native focus and reloadable hash links; an ordinary line-up link always starts on Songs. Closing-song links retain their distinct key evidence. V9 qualification covers all three panels at eight sizes including 927×1145, source/draft separation, exact arrangement sequences, unknown/inferred keys, roster conflicts, maker save/reload, keyboard and emulated touch. Private backups and receipts live under the runtime v9 directory. V8 privacy/hosting receipts remain applicable because no server, access or proxy configuration changed. Physical iOS Safari still needs the user's trial.

## Third-party code

`dist/vendor/three.module.min.js` and `three.core.min.js` come from the npm `three@0.180.0` package, downloaded with lifecycle scripts disabled. The upstream MIT license is retained in `dist/vendor/LICENSE`. Source: <https://github.com/mrdoob/three.js/tree/r180>. These historical assets are unused by the current app. Active runtime imports use local relative paths for proxy compatibility.

Announced line-up titles are derived from the date and song leader for cards, detail headings, browser titles and new personal copies. The list omits repeated date/leader labels. Original source titles and closing-song evidence stay in the private snapshot; legacy displayTitle values are ignored.

Screen layout fixes the document to the dynamic viewport; navigation stays in place while content panels scroll internally. Print layout is unchanged. Physical iOS Safari toolbar/keyboard and rubber-band behavior still require device acceptance.

V15: Latest line-ups uses a single column without the snapshot footer or subtitle. Details omits personal-copy controls. The home preview renders the full main song set (internal overflow for unusually long sets). Back preserves a whitelisted home/list origin through tabs, reloads and song excursions using the hash query. Direct links default to the list.

V16 adds browser-local song authoring, inline bracketed chords, chart preview and per-line-up section orders/performance keys. Morning Mercy is the single featured original UX demo. Separate workshop storage loads before line-up validation; no private snapshot changes or shared writes. Saved arrangements block deletion/renaming of referenced sections until their flows are updated. Slide-maker view expands repeated sections. This is not a Wix authentication implementation.

V17 revises authoring into Edit/Preview with collapsible sections, persistent Save/Cancel and visible validation summary plus section/line errors. Bracketed invalid chords and suspicious unbracketed H–Z chord-like tokens (such as Hm7) are blocked. Ordinary unbracketed words remain lyrics. Invalid saves preserve stored data. In-app Cancel/Back and page unload guard unsaved changes.

V18: editing updates section names/counts and validation inline while typing. Drafts (including invalid/incomplete sections) autosave separately per song, resume after refresh or Close, and can be reopened from the catalogue. Save song validates before replacing the usable chart and clears its draft. Duplicate section names are case-insensitive and remain invalid; repeated performance sections belong in the line-up flow.

V19: valid song edits automatically update the local chart; invalid edits remain recoverable drafts. No Save song button. All invalid sections retain an outline and Needs attention badge when collapsed. Status distinguishes saved chart from draft-only changes, and leaving an invalid editor requires acknowledgement.

Song-view simplification: catalogue viewing/editing and transposition remain; Add to line-up and Slide-maker view are removed. Add songs through Line-up Maker. Existing per-line-up arrangements remain available in service context.

Song export: Song Catalogue → Export all songs downloads the full saved visible catalogue regardless of search filters. Edit song → Export song downloads the current validated song; fix errors before exporting. Files use versioned wcfi-song-catalogue JSON with song details, section order, lyrics/chords and sequence. No import, uploads, line-up arrangements or invalid drafts are included. Export from the browser/device where the songs were authored.
