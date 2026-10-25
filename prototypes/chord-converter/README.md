# WCFI chord sheet converter

Independent, buildless browser tool. Run `./start.ps1` to serve on loopback port 8818. The owner-only Dashies proxy entry is `wcfi-chord-converter`; no new Tailscale Serve handler is needed. Logs/process metadata live at `~/.wcfi/chord-converter`, outside Git.

Convert chord-only lines plus the following lyric line to bracketed inline chords, or convert inline chords back. Position uses expanded monospaced columns (4/8-space tabs), with optional word-start alignment. Preserve section headers, blank lines, slash bass and extensions including B7(b9). Unrecognised lines remain unchanged. One-letter chord/lyric ambiguity and chords beyond line end produce review notes. Reverse conversion inserts spacing when chord names would overlap. Input stays in browser memory only; no upload, tracking, persistence or external dependencies. Example lyrics are original synthetic text. Browser clipboard requires HTTPS; fallback selects output for native Copy.

Run `node --test tests/converter.test.mjs`. Check phone/tablet UX on physical Safari separately. No change to per-line-up arrangements or source ministry data.

Broken web copy mode joins lyric fragments with their existing edge spaces, converts chord-only rows in sequence, and keeps blank-line passage boundaries and headings. It preserves midword chord changes and adjacent/trailing chords. Original wrapping and missing word spaces cannot be inferred reliably; a concise review note accompanies output. Column controls are disabled in this mode. Tests use synthetic text only.
