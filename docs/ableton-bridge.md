# Ableton bridge

`wcfi audio ableton` controls the open Live Set through a small Python Remote
Script running inside Live. No mouse/keyboard automation, Max for Live, MIDI
loopback driver, internet connection, API key or emulator is required. The
emulator and Analysis Nodes remain independent audio connections.

## Install and activate

```powershell
wcfi audio ableton install
```

The installer reads Live's saved User Library location and copies the standalone
`WCFI_Bridge` package into `User Library/Remote Scripts/`. You can select a
different existing library with `--user-library "C:\path\User Library"`.
It only updates its own managed package; it does not modify preferences or
restart Live. The reload-capable bootstrap needs one activation restart when
upgrading an older bridge. Save your work and restart Live once after installing
that bootstrap. Toggling the Control Surface to None and back can retain cached
Python helper modules; it did so on Live 11.3.35. Once the current bootstrap is
active, use the controlled code reload below for supported bundle updates.

In **Live Preferences > Link/Tempo/MIDI**, choose **WCFI_Bridge** in an unused
**Control Surface** slot. Set its **Input** and **Output** to **None**. If the
script is not listed, save your work and restart Live when convenient. Never
press Play to activate the bridge. This is the one-time manual setup; normal
operations work with Live in the background.

Ableton documents this installation mechanism and Python 3 support in Live 11
and later in [Installing third-party remote scripts](https://help.ableton.com/hc/en-us/articles/209072009-Installing-third-party-remote-scripts).
The embedded package uses only the standard library and Python 3.7 syntax.
The CLI itself uses this project's Python 3.11+ environment.

```powershell
wcfi audio ableton status
wcfi audio ableton inspect --output session-inspection.json
```

`status` reports the connected version, supported operations and whether that
running version exposes the required device APIs. Revision 5's context probe
identified a lossy insertion-mode getter on the tested host: it returned a Python
boolean rather than the full insertion-mode enum. Revision 6 avoids writing
insertion mode when either affected track exposes that boolean read type. Utility
insertion and guarded removal were verified on a return track in Live 11.3.35.
New insertions and context probes
require revision 6 or later. Browser discovery,
context inspection and receipt cleanup remain available on an older bridge that
exposes those operations. Upgrading the installed files alone does not upgrade
the running script: reload a staged bundle, then check `bridge_revision`,
`operations`, and the active bundle ID in `status`. An older bridge without the
reload bootstrap still requires the one activation restart described above.
It does not claim a successful Live
integration until the script has been activated. A new inspection output file
is required, so a previous snapshot cannot be silently overwritten.

## Reload installed bridge code

```powershell
# Read-only: inspect runtime, installed bundle and unresolved-operation state.
wcfi audio ableton status

# Check whether the staged bundle can be loaded; no runtime changes.
wcfi audio ableton reload --dry-run

# Reload the already installed bundle through the active bootstrap.
wcfi audio ableton reload

# Stage the project's current bundle, then perform the same controlled reload.
wcfi audio ableton reload --install
```

Reload requires stopped Play and Record and a valid managed bundle. It swaps a
complete, versioned bridge runtime on Live's own timer thread rather than
reloading individual Python modules into a partly old object graph. The new
runtime publishes a new session, port and authentication token. The old
runtime remains available if constructing the replacement fails. This changes
bridge code; it does not create, remove or reorder audio devices, change their
parameters, select a track, control transport, or operate your desktop.

`status.reload` reports `supported`, `bootstrap_version`, `bootstrap_current`,
`active_bundle_id`, `installed_bundle_id`, and `last_reload`. The bundle IDs are
SHA-256 content identities. A bootstrap that is missing or itself out of date
returns `bootstrap_update_required`; save the set and restart Live once after
installing it. Do not treat a newly staged bundle as evidence that it is running.
`--install` cannot be combined with `--dry-run`, because installation changes
files. Install separately before previewing a reload. `--user-library` is
available with `--install`; `--state-dir` selects the local discovery directory.

Unresolved operations must be inspected and reconciled before reloading.
Outstanding insertion receipts are also protected by default: a fresh runtime
cannot retain their old host-object identities. If you deliberately accept
losing those rollback records, use `reload --discard-receipts`. That option
discards bridge bookkeeping only; it does not delete the inserted host devices
or resolve an uncertain device operation. Preserve any experiment journal you
still need before taking that step.

Finish or restore any active `ReversibleTrial` before reloading. The bridge
can cancel queued RPCs, but it cannot detect an external experiment paused
between requests. A reload invalidates that experiment's session and object IDs;
its existing journal will then refuse automatic restoration across sessions.

The client sends exactly one reload request with a unique `reload_id`, even if
the connection closes before the acknowledgement arrives. It polls fresh local
descriptors from the **same Live process**, requires a matching reload receipt,
old session and expected bundle, and verifies the new session's active bundle.
It does not mistake another bridge, unrelated restart, or stale descriptor for
completion. After verified success the Python client adopts the new descriptor;
previous track, parameter, browser and receipt IDs still expire, so inspect again
before further edits. Status remains read-only throughout.

Revision 7's reload implementation passed 291 bridge tests on October 23, 2026
(two filesystem symlink tests skipped because Windows creation privileges were
unavailable). The tests include the real installed package and authenticated
loopback server against a fake Live host: successful replacement, lost replies,
failed imports/initialization/publication, queued-edit cancellation, unchanged
session state, receipt protection, repeated reloads and lifecycle cleanup.
After the one-time activation restart, revision 7 also passed a reload dry-run
and two consecutive reloads in Live 11.3.35. Live had reopened a basic seven-track
set (Master 0 dB to outputs 1/2, with no Master devices), rather than the prior
church rehearsal set; the host result applies to that open set. Each reload
returned a correlated completion receipt and a fresh session ID from the same
Live process. Both preserved every inspected audio-state field (track/device
order, parameters, faders, sends and routing), with Play and Record stopped.
The installed content bundle stayed the same for these host trials; loading
changed code and rejecting broken candidates were exercised by the automated
tests. End-to-end reload calls took 2.86 and 2.77 seconds, including control RPCs,
Live timer scheduling and reconnect verification. Those timings do not measure
DSP latency or prove uninterrupted PCM audio. No desktop controls were used.

After reopening the church set, a further reload dry-run and actual reload
also passed in its 19-track layout. The inspected audio state was unchanged,
including Master at -6.0 dB routed to outputs 23/24, and both transport flags
remained false. The reload helper completed in 2.90 seconds, including RPC and
reconnection checks. Fresh session-local IDs were obtained before continuing
the separate Pour Analysis Node comparison.

An explicit failed receipt reports `reload_failed` and the previous runtime
remains active. If verification times out, `reload_outcome_unknown` includes the
request ID; inspect `status.reload.last_reload` before deciding what to do next.
The command does not resend the mutation or assume that a missing reply means
nothing happened. `--timeout` controls the verification deadline (20 seconds by
default). No restart or reload is required for ordinary fader/parameter commands.

## Faders

```powershell
# Preview an ABSOLUTE Master fader setting without applying it.
wcfi audio ableton fader Master --db=-6 --dry-run

# Apply that setting, then report the actual value and display from Live.
wcfi audio ableton fader Master --db=-6

# Reduce RED by 1 dB relative to its current fader position.
wcfi audio ableton fader RED --db=-1 --relative
```

Track, group, return and Master faders use Live's own `str_for_value` conversion;
the bridge does not assume that the host's internal value is linear in dB or
amplitude. A relative change from a fader at silence is rejected. This controls
software faders, not the physical analogue trims of the console.

## Plug-in and native-device parameters

```powershell
wcfi audio ableton parameters RED "Pro-Q 4"
wcfi audio ableton set-parameter RED "Pro-Q 4" "Device On" --value=0 --dry-run
```

Inspect first. The parameter list gives **host values**, minimum, maximum,
displayed units, quantization and automation state. An EQ's internal value might
be normalized rather than Hz or dB; do not pass a displayed frequency as a host
value unless the reported range confirms that interpretation. `--normalized`
maps 0..1 across the host's reported range; it does not change parameter units.
Quantized parameters require integer host values (normalized values are rounded
to a valid step).

Only parameters exposed by Live can be controlled. Some third-party plug-ins
need their parameters added using Live's Configure panel before they appear.
The insertion command below initially supports the Audio Effects browser, not
arbitrary VST loading or editing opaque VST state. Disabled or
automated parameters are rejected, including an unknown automation state. The
bridge never calls `re_enable_automation` or silently overrides automation.

## EQ Eight processing mode

EQ Eight exposes its Stereo/L/R/M/S mode as a native device property, separately
from its filter parameters. Revision 8 adds a narrow operation for this property:

```powershell
wcfi audio ableton eq8-state KEYS "EQ Eight"
wcfi audio ableton set-eq8-mode KEYS "EQ Eight" --mode 2 --dry-run
wcfi audio ableton set-eq8-mode KEYS "EQ Eight" --mode 2
```

Mode `0` is Stereo, `1` is Left/Right, and `2` is Mid/Side. The client reads the
current mode before sending one compare-and-set request. Writes require stopped
Play and Record, a matching bridge session and EQ Eight device, readable native
state, and no unresolved insertion. Boolean or otherwise ambiguous three-way
mode values are rejected. This is not an arbitrary attribute setter.

`eq8-state` reports `global_mode`, `edit_mode`, and `oversample`; EQ Eight device
entries in `inspect` include these properties under `native_state`. Unavailable
properties remain visible as inspection errors and prevent mode writes. The
operation reads back its result, and `status.eq8_last_mutation` retains the last
write outcome, including failures after a change may have applied. An uncertain
write is not retried automatically. Inspect the actual state before recovery.

Record the original mode before an experiment and restore it explicitly before
`remove-inserted-device`: guarded removal checks both parameters and native
state against its insertion receipt. `ReversibleTrial` journals faders and
parameters; it does **not** yet journal native EQ Eight mode changes.

The revision 8 bridge suite passed 331 tests, with two Windows privilege-dependent
filesystem tests skipped. A read-only probe in Live 11.3.35 confirmed an integer
Stereo mode (`0`), the first editing bank (`false`), and oversampling off (`false`) on a
newly inserted EQ Eight. This confirms native-property inspection, not the audio
effect of an M/S edit. The property definitions are documented in the
[EQ Eight Live Object Model](https://docs.cycling74.com/apiref/lom/eq8device/).

## Move existing devices

```powershell
# Preview moving an existing device to final index 2 (third position).
wcfi audio ableton move-device RED "Pro-Q 4" --index 2 --dry-run

# Same-chain move; destination defaults to the device's current chain.
wcfi audio ableton move-device RED "Pro-Q 4" --index 2

# Another track, or a rack chain identified in the inspection.
wcfi audio ableton move-device RED "Pro-Q 4" --to-track KIT --index 1 --dry-run
wcfi audio ableton move-device RED "Pro-Q 4" --to-chain chain_ID --index 0 --dry-run
```

Indexes are zero-based positions in the final destination chain. The operation
uses Live's `move_device`, checks current source/destination order, then scans
again to report the actual destination and index. For forward moves within one
chain it translates final positions to Live 11's pre-removal insertion index;
the response includes `host_insertion_index` for diagnostics. Live can choose a nearby
valid position; `verified: false` and CLI exit code 2 indicate that the requested
position was not achieved. The device may have moved: inspect before proceeding.
Movement can affect the sound and rebuild processing; it is not guaranteed to
be silent during a broadcast. A rack cannot be moved into its own descendant.

Names are exact and case-insensitive. Duplicate names are rejected; use the
opaque IDs in the snapshot to disambiguate tracks, devices or parameters. IDs
are valid only for this running bridge and existing objects, not a saved preset.

## Insert an Audio Effect

```powershell
# Discover Live's actual Audio Effects browser, without loading or previewing.
wcfi audio ableton browse-devices

# Descend using an exact folder ID returned above; follow next_offset to page.
wcfi audio ableton browse-devices --parent browser_FOLDER_ID --offset 0 --limit 100

# Use a returned loadable item ID. Index 1 inserts after the first device.
# First inspect and check the host context; unresolved results need reconciliation.
wcfi audio ableton insertion-context Master
wcfi audio ableton check-insertion-context Master --dry-run
wcfi audio ableton check-insertion-context Master
wcfi audio ableton insert-device Master browser_ITEM_ID --index 1 --dry-run
wcfi audio ableton insert-device Master browser_ITEM_ID --index 1

# The successful insertion returns an insertion_id for guarded rollback.
wcfi audio ableton remove-inserted-device insertion_RECEIPT_ID --dry-run
wcfi audio ableton remove-inserted-device insertion_RECEIPT_ID
```

The IDs above are placeholders; copy IDs from the current browser/operation
response. There is no name-search fallback, guessed URI, filename loader or
arbitrary plug-in path. Browser entries include their name, path, URI and
loadability so callers can inspect the exact item. Browse folders explicitly;
`--offset` and `--limit` page within one folder. Browser IDs expire with the
running bridge and may become stale when the browser library changes.
Live's native `is_folder` flag can be false on a category that still has child
items. It is not a reliable leaf test: pass the category's ID as `--parent` to
inspect its children.

`insertion-context TRACK` is read-only and remains available while an unresolved
operation blocks mutations. `check-insertion-context TRACK` checks the target's
current device order, requires stopped Play and Record, temporarily changes Live's
internal track/device selection and, where the getter exposes the full enum or
integer mode, insertion mode, then restores them. It returns
a structured trace and verification flags. It never loads, moves or deletes a
device, changes a fader, controls transport or uses the desktop. `--dry-run`
validates the probe without making those temporary context changes. A failed
restoration blocks further mutations. The probe isolates the context-restoration
problem before another device load; passing it alone does not verify loading or
sound quality.

This first insertion implementation supports native Audio Effects in top-level audio
track, return and Master device chains. Nested rack-chain insertion and other
browser roots are not exposed. Indexes are zero-based final positions, including
the current chain length to append. The command checks the inspected chain order
before loading and verifies the created device and its final position afterward.
It rejects insertion or rollback while Play or Record is running. Stopped
transport does not mean live monitoring is silent: insertion can change the
audible signal or cause the host to rebuild its processing graph.

When either affected track's insertion-mode getter returns a boolean, revision 6
does not write either track's mode. Instead, it selects an explicit top-level
non-rack device as the loading anchor and verifies the selected device and absence
of a selected rack chain before loading. A nonempty target containing only racks
is unsupported in this branch; there is no nested-anchor fallback. An empty target
is allowed when the selection context is not nested. The new effect's actual
top-level placement is verified and moved to the requested index as needed.
For hosts exposing enum/integer modes, the explicit mode setup and restoration
remain available.

Live's browser loader uses Live's selected track and device insertion mode. The
bridge temporarily sets the required host context for loading and restores it
afterward, leaving mode untouched on the boolean-getter branch described above.
An active Hot-Swap operation is rejected instead of replacing an
existing device. It does not focus Live, open a window, simulate input or control
the transport. Selection can briefly change inside Live. The response reports
`selection_restored`, `selected_device_restored` for the originally selected
track, and `target_device_selection_restored` for the insertion target.

An empty target naturally selects its first newly inserted device. Live exposes
`selected_device` as read-only, so there is no supported restoration to an empty
selection while that device exists. The target's device-selection flag can
therefore be false while the original visible selection on another track was
fully restored. If the empty target was itself the originally selected track,
`selection_restored` and `selected_device_restored` report false and
`selection_note` explains the first-device selection. Read these fields separately
from successful device placement; they do not claim unchanged host selection in
that case. A load is one undo step, including positioning the new device.
`--dry-run` validates the request without calling the loader; it does not prove
that the device will load successfully on this host.

An unchanged boolean readback proves only that the exposed value stayed the same.
It cannot establish restoration of an original three-valued enum hidden by that
getter. On this branch, `insertion_mode_written: false` records that the bridge avoided
mode writes; it is not a claim that the full hidden mode was measured or restored.

Keep the returned `insertion_id` receipt if the change is a trial. Rollback is
restricted to the exact device that this running bridge created, with the
recorded device-chain order and inserted-device state unchanged. It cannot delete
an arbitrary pre-existing device by name or ID. A concurrent chain edit, stale receipt or restarted bridge
requires inspection and manual reconciliation instead of automatic deletion.
An insertion receipt is not a persistent preset or a crash-recovery journal.

As with movement, `verified: false` produces CLI exit code 2, and an error or
lost response can mean the host already changed. Inspect before another write;
the client never retries loading automatically. An ambiguous or delayed load
must not be interpreted as a clean failure and retried with a new request.

An unresolved insertion blocks further insertions, fader edits, parameter edits
and device moves; `status` reports `insertion_outcome_unresolved`. When Live puts
one otherwise verified new device in a different position, the response is
unverified but can retain a safely removable receipt. Only rollback of that
specific receipt is allowed, and verified rollback clears the block if placement
was the sole problem.

When exactly one created device and its chain are verified but restoring host
selection fails, only that device's guarded receipt removal is permitted.
Successful removal cleans up the created device but leaves the general mutation
block in place: selection still requires reconciliation and a bridge restart.
Unknown load outcomes and deletion errors offer no safe-removal exception; all
mutations remain blocked. Inspect and reconcile the actual set, then restart the
bridge before further mutations. Restarting clears the in-memory block and
receipts; it does not undo or prove recovery of the host's changes.

## Architecture and extension

```
CLI / Python client -> authenticated loopback RPC -> main-thread queue -> Live
Analysis Nodes     -> passive PCM bridge         -> measurements / listening
```

The public Python client is `wcfi_tools.ableton.client`. Its `connect`,
`set_fader`, `set_parameter`, `move_device`, `browse_devices`, `insert_device`
and `remove_inserted_device` helpers can also back a desktop backend.
`inspect_insertion_context` and `probe_insertion_context` expose the context
diagnostics. Callers
identify targets explicitly instead of relying on the current selection or
visible window; insertion temporarily adjusts Live selection as described above.

New operations belong in `LiveAdapter.operations`, with validation, an explicit
handler and readback. There is no arbitrary Python execution, generic attribute
setter or unrestricted Live method invocation endpoint. In particular, no Play,
Record, clip launch, transport setters, save, routing changes or generic device
deletion are exposed. Removal is limited to the insertion receipt operation.

The network workers never access Live objects. A Control Surface timer drains a
bounded queue on Live's main thread. Pending work expires after five seconds and
cannot execute later. If work has already started when a connection times out,
its outcome is uncertain: inspect before any further write. Requests have IDs
and recent results are deduplicated; the client never automatically retries a
write. Each mutation checks the inspected value or chain order and is wrapped
in one Live undo step. This is not a multi-command transaction or an audio-rate
automation mechanism.

The listener binds only to 127.0.0.1, requires a per-instance token, rejects
browser origins/foreign Host headers and bounds request size, worker count and
queue size. Discovery files live under the user's local `wcfi/ableton-bridge`
data directory, contain credentials and must not be shared. The CLI bypasses
proxies and rejects redirects. Multiple active bridges require `--instance`.

## Verification

The automated tests use a fake Live object graph behind the actual HTTP server
and client. They check main-thread writes, nonlinear fader conversion, readback,
nested racks, move mismatches, stale IDs, concurrent edits, automation rejection,
request expiry/deduplication, authentication and standalone installation. They
do not prove host-version compatibility or a particular VST's parameter access.
The insertion and rollback checks also cover exact browser selection,
placement readback, selection restoration and blocking writes after an uncertain
outcome in a fake host. Actual host validation is separate from that coverage.

On October 23, 2026, revision 6 passed the context probe and two actual Utility
insertion/removal trials on B-VOCALVERB in Live 11.3.35: index 0 before the existing
reverb and index 1 after it. Placement and guarded removal were verified,
including removal dry-runs and a repeated removal returning an unchanged result.
Every inspected session field except its inspection timestamp was restored,
including track/device parameters, faders, routing and selection flags. Play and
Record remained stopped; no desktop control or audio I/O was used. This validates
that native Utility operation on the tested return, not every native device,
third-party VST or host version.

Earlier host tests exposed a lossy mode getter: `builtins.bool` returned `True`,
writing mode `2` changed it to `False`, and writing the captured `True` did not
restore it. This is an observed read-type limitation in this environment, not a
confirmed general Ableton defect. The successful revision 6 trials used a verified
top-level loading anchor with `insertion_mode_written: false`; the exposed boolean
was preserved, while the full hidden enum remains unmeasurable through that getter.
The two insertion helpers took 3.48 and 3.61 seconds, including three RPCs each.
These are measured control-operation times, not DSP latency or a performance
guarantee for other sessions.

After activation, first run `status` and `inspect`, check that the expected set
and devices appear, then run a `--dry-run`. Verify a reversible change and its
restoration on a rehearsal session before relying on write operations live.
Live's read-only `is_playing` value is included in inspection; the bridge has no
operation to start playback.

Live 11.3.35 was checked against an open rehearsal set on this machine: session
inspection, an unused return fader, Valhalla's Mix parameter and movement of a
bypassed Pro-Q device. The first movement round-trip exposed the pre-removal
index convention; readback detected the mismatch and the original order was
restored. The bridge now translates that index. Pro-Q in that set exposed only
Device On, while Valhalla exposed its effect parameters.

After restarting Live, revision 2 passed same-chain forward/backward moves,
including moving to the last position, and moving the bypassed EQ to an unused
return and back. The fader was restored to its exact original host value;
Valhalla's Mix and RED's device order were restored, and Master stayed at
-6.0 dB with output routing 23/24. Playback and recording stayed stopped.
Five status requests in the background took a median 1.03 seconds each on this
machine. Treat this as measured control latency, not a real-time DSP path or
a guarantee for another machine or foreground/background state.

For restoring a fader from an inspection without losing precision, the low-level
`set_fader` operation also accepts `db: null` and `raw_value: <saved volume.value>`,
with the same `track_id`, `session_id` and `expected_value` checks. This is a host
fader value, not dB; it is mutually exclusive with a dB or relative change.

## Reversible rehearsal trials

`wcfi_tools.ableton.transaction.ReversibleTrial` journals a finite experiment
before each write and restores exact host parameter values in reverse order:

```python
from pathlib import Path
from wcfi_tools.ableton.client import connect
from wcfi_tools.ableton.transaction import ReversibleTrial

trial = ReversibleTrial(connect(), Path("trial-journal.json"))
try:
    trial.set_fader("KIT", 6.0)  # Absolute software fader setting.
    # Capture and evaluate the actual Analysis Nodes here.
finally:
    trial.restore()
```

The caller must provide a new journal path and already-stopped Play and Record.
The helper never controls transport, opens a window, or starts a source. It
dry-runs each edit, verifies readback, and refuses to overwrite a concurrently
changed control. A lost write response is reconciled by inspection before
rollback; it is not blindly retried. Callers must still guard unrelated routing,
devices, emulator settings, audio continuity, and the outcome of restoration.
A journal preserves recovery evidence after a process crash; it is not an
automatic crash-recovery service.

`capture_pairs(..., ready_event=threading.Event())` can signal a coordinator when
all node readers are ready. The coordinator can then start an emulator loop;
the capture reader itself still has no source or transport controls. With Live
stopped, some hosts do not expose an advancing shared sample clock. In that
case use verified contiguous PCM alignment rather than assuming the independent
node counters refer to the same samples. The pure-array helpers in
`wcfi_tools.audio.trial_verification` check channel order, one common gain,
full-window and short-window residuals, and ambiguous matches.

A track's after-effects node precedes its fader. A balance edit must therefore
be verified at the summed Master nodes. The Master after-effects node also
precedes its fader; capture the emulator's final output pair to verify that
last stage. Parameter readback alone does not prove an audio change, and a
successful waveform check does not establish professional mix quality.
