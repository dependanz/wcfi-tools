# Offline gain-controller prerequisite

Run from the existing wcfi-tools checkout:

```powershell
.\.venv\Scripts\python.exe -m wcfi_tools.cli audio gain-shadow run --output _work/offline-gain-controller-2026-10-24-01/run-01
```

The command executes the fixed Revision 2 engineering suite once. The output
directory must be new. Exit status 0 means all gates passed, 2 means a gate or
recorded run failed, and 1 means setup failed. Failures are retained; there is no
automatic retry. It changes no real fader and reads no audio. There is no host,
driver, Analysis Node, transport, desktop or network connection.

Only the existing October 11 09:49:38 feature JSON is loaded, and only its six raw
RMS arrays at indices [240,480) enter decisions. No post-FX, output, performer or
bleed labels are invented. January recordings remain reserved and untouched.

The pure controller consumes two-second estimates from a causal eight-second
adapter. Its total virtual AC GUITAR offset is bounded to [-3,0] dB; proposals
are 0.5 dB increments separated by at least ten seconds and three new eligible
estimates. Gain is computed once from the total offset. Fast evaluations clear
pending evidence without advancing the evaluation clock; missing evaluations
discard stale evidence. Positive proposals only undo its own attenuation.
Revision changes suspend until explicit rearm with a new revision and anchor.

AC/RED drift uses the identical shared raw-support frames. Each independent
context source uses its own fixed raw support (at least four frames), with at
least two contexts required. All supported contexts must agree; sources cannot
disappear when attenuated. Inferred attribution disagreement and declared
invalidity require a fresh eight-second history; insufficient raw support is
reported separately as an unavailable opportunity. These are energy masks, not
semantic activity detection.

One-second frame t represents [t-1,t); even endpoints are scheduled decisions.
Missing frames are explicit invalid placeholders, including missing even
endpoints. The synthetic fault clock labels frame endpoints. Positive injected
gain starts at frame-start onset and is removed after ninety seconds. Rearm is
an explicit simulated operator event: it accepts a new baseline using only the
last eight observed level differences, never the private perturbation label.

The run freezes code/config/input hashes, fixture hashes, opportunity masks and
the private scorer key before evaluation. Seventy-seven cases are run separately
on fully supported synthetic scalars and the frozen October observations. Each
has three identical replays and exact full-log comparison at all 120 prefixes,
also with corrupted future anchor values, gaps and shuffled future labels.
The controller never receives the key or future opportunity totals.

Artifacts include `freeze.json`, `opportunities.json`, public scalar fixtures,
`private-scorer-key.json`, one canonical JSONL decision log per case/family,
separate processing timings, and `results.json`. Every scheduled log retains its
raw estimate, source interval, support, veto, revision, action, incremental delta,
total virtual offset and effective gain. Processing and sample clocks are distinct.

The CPU worker is constrained to one idle-priority logical CPU, one numerical
thread, 512 MiB peak memory, sixteen MiB post-warmup growth and five minutes.
No GPU or model is involved. A runtime capability guard rejects attempted audio,
reserved-recording, host-import, process-start or socket operations.

Passing this suite establishes bounded arithmetic on a known same-time oracle
anchor, not a professional mix, natural false-alarm performance or a live policy.
The live balance target, trustworthy activity/bleed interpretation, actual host
clock and output path remain unvalidated. Stop for supervisory review before
host integration or any reserved-recording qualification.
