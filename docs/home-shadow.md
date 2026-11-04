# Home shadow of the recorded set

`wcfi audio shadow-home` observes the Analysis Nodes of the set that is open in Ableton Live
and runs three existing policies in shadow, logging what each one would have done. It is meant
for a rehearsal recording played back at home, where nothing is broadcast. It binds nodes by
label and position for the Live process that is running now (node ids change with every
process), waits for you to press Play, collects for a fixed number of seconds, and runs:

- `v2`: `PublicationClockGainV2` on one-second window groups from the post taps of RED,
  AC GUITAR, KIT, BASS, KEYS and EGUITAR, fed as the October 25 live shadow fed it;
- `coordinator`: `MixCoordinator` on 100 ms PCM bins from the dry pre taps of its seven tracks,
  after a calibration window of `--calibration-seconds` computed by
  `mix_coordinator_trial.calibration`;
- `red`: `ActivityCalibration` on RED's pre tap 100 ms RMS.

The host is inspected read-only once per second. A transport change is expected; any other
change to the set (a fader, a device, a parameter, routing) stops the run, because the
observations would no longer describe the mix that was bound. The stop names the paths that
moved (for example `tracks[0].volume.value`), as seen at the inspection that caught them.

The bound nodes must belong to the Live process the bridge inspects: the snapshot carries the
bridge descriptor's pid as `host_pid`, and binding fails when the nodes publish another pid
(a second Live, or an orphaned one whose node segments are still held open).

## What it never does

- It writes nothing to Live: no fader, parameter, device, routing or Control Node change. The
  command calls only the bridge's `connect` and the `inspect_session` read; its source names no
  write operation, a test keeps it that way, and every host call the run makes is counted by
  operation name so `writes` in the summary is measured, not assumed.
- It never starts, stops or records the transport. You press Play; you press Stop.
- It saves no PCM. Output holds one-second and 100 ms levels, policy inputs and decisions, and
  host inspections; no audio file is written and no audio leaves the laptop.
- It applies no proposal. Every decision in the output is shadow only, and the existing
  qualification of each policy is unchanged (`live_eligible` stays false).
- It touches no reserved recordings and no emulator. The source is the open set as Live plays it.

## Prepare the set

1. Open `NEWGDN-AutoMixTest-v2` in Live 11.3.35 with the WCFI_Bridge control surface selected.
   Each music track needs the chain `[Analysis Node (pre, position 0), Control Node, Analysis
   Node (post, position 1)]`; Master and the returns keep their node pairs.
2. Record the starting faders, for example with `wcfi audio ableton inspect --output <new
   file>`. The policies reason about changes from a known balance, so the faders must be where
   they were when that balance was chosen.
3. Note each Control Node's Auto state. A legacy Auto-enabled Control Node changes gain inside
   the chain; the shadow does not alter it, but the post taps then include its movement, and
   the pre taps do not. Write the state down next to the run's output path.
4. Check the binding before playing: `wcfi audio shadow-home bind` must list every role with
   one active node from one Live pid. A missing, duplicated or stale label is reported and
   nothing runs.
5. Stop the transport at the start of the recorded arrangement. Do not touch the mixer while
   the shadow runs.

## Commands

```powershell
wcfi audio shadow-home bind                       # post taps of the six V2 roles, as JSON
wcfi audio shadow-home bind --stage pre --roles "RED,AC GUITAR,KIT"
$env:OPENBLAS_NUM_THREADS = '1'; $env:OMP_NUM_THREADS = '1'; $env:MKL_NUM_THREADS = '1'
wcfi audio shadow-home run --output _work\home-shadow-2026-10-26\run-01 --seconds 600
```

Set the three thread variables in the shell before `run`: every earlier evidence run used one
numerical thread, and without them the BLAS library under numpy reserves about 1 GB of commit at
import. The memory ceiling (256 MiB by default) bounds the process's peak working set, the
resident footprint; peak commit and the thread variables are recorded in `summary.json` under
`resources` as evidence, not enforced.

`bind` reads the node inventory and prints the binding record: stage, roles, each role's
`node_id`, `pid`, `epoch`, `label`, `position`, `channels` and `sample_rate`, the binding's
`source_map_revision`, `host_pid` and `epoch`. `ACGUITAR` is the node label for the role
`AC GUITAR`.

`run` options: `--output` (new directory; an existing path is refused), `--seconds` (default
600, at most 2400), `--no-wait-for-play` (collect immediately instead of waiting for the
transport), `--coordinator-stage pre|post` (default `pre`), `--calibration-seconds` (default 30,
shorter than `--seconds` when the coordinator runs), `--policies` (default
`v2,coordinator,red`), `--wait-minutes` (how long to wait for Play before giving up; default
30, at most 720) and `--instance` (bridge instance prefix when several are active).

The run as a whole is bounded by `max_seconds`, counted from the start of the command and
covering the wait for Play as well as the collection. The command sets it to `--seconds` plus
`--wait-minutes` of allowance (default 30 minutes, so `--seconds 2400` can complete); a run whose
wait outlasts the allowance ends with `max_seconds_reached`. For an unattended arm, for example
when nobody is at the laptop yet, raise it: `--wait-minutes 480` keeps one run waiting for eight
hours while inspecting the host once a second and writing nothing.

Start `run`, then press Play in Live. The collection clock starts when the transport is
playing and every bound node is fresh on two consecutive reads (the second read also settles
the coordinator grid, below). While the host plays but a bound node stays inactive, stale or
unreadable, a `waiting_for_fresh_nodes` event names it at most once per second, and after
`fresh_wait_seconds` (30 s) the run ends with `nodes_not_fresh` and the node in the detail.
Pressing Stop ends the run after a five-second grace; that counts as a finished window. With
`--no-wait-for-play` the grace is armed only once Play has been seen, so a stopped transport
collects for the whole window. Ctrl-C ends the run with reason `interrupted` and exit 1; what
was written stays. The command prints the outcome, the reason, the V2 group and proposal
counts, the coordinator bin and gap counts, RED's lock state and the output path, then exits:

- `0`: the window finished (`collection_seconds_elapsed`) or the transport stopped;
- `1`: nothing was collected: setup failed (no bridge, bad option, existing output, missing
  bridge DLL, a role missing, duplicated, stale, spanning two Live processes or belonging to a
  pid other than the inspected Live's), or the run stopped before collection began
  (`inspection_failed` on the first inspection, `nodes_not_fresh`, `max_seconds_reached` while
  waiting, a host change before Play, `interrupted`);
- `2`: collection began but ended early, and the reason line says why: `host_state_changed`
  (the host changed outside the transport; the detail names the paths), `inspection_failed`,
  `node_identity_changed`, `memory_limit_exceeded` (peak working set above the ceiling) or
  `max_seconds_reached`.

Early output is kept; nothing is retried.

## The coordinator grid

The coordinator's 100 ms bins need one sample grid across its seven tracks, whose nodes count
frames independently. The grid is the plugin-reported continuous host sample
(`host_sample`, flagged in `flags & 1`), which the Analysis Node publishes whether or not the
transport plays. It is trusted only after a lockstep check: across two consecutive fresh
reads, every coordinator node's `host_sample + block_size` must have advanced by exactly what
its `written_frame` did (the same rule the monitor's `Stream.append` applies before calling a
host clock valid). When any node fails it, the grid falls back to each node's own counter from
the collection start (`approximate_publication_clock`). The choice is reported as
`collection.grid` and `coordinator.grid` (`alignment`, `anchor`, `lockstep_verified`) in
`summary.json`; it is a property of the coordinator's input, not a qualification of the run.

## Output files

Every file is JSON or JSON Lines; none holds samples.

- `started.json`: start time and the configuration, with a `declared` object (`pcm_saved`
  false, `host_mutations` 0, `emulator_used` false, `live_eligible` false,
  `musical_quality_validated` false) stating up front what the run is built not to do.
- `host-before.json` and `host-after.json`: the first and last `inspect_session` snapshots with
  their state fingerprints; `host-after.json` carries `diff.changed_paths`, the dotted paths
  that differ outside the transport.
- `binding.json` (and `binding-rebind-N.json`, N from 1, if a node identity changed before
  Play): the binding record of each policy (`v2`, `coordinator`, `red`) with its roles, nodes,
  `source_map_revision`, `host_pid` and `epoch`, plus `inspected_pid`, the pid of the Live the
  bridge inspects; `node-baseline.json` holds the zero-frame metadata read of every bound node.
- `bridge-inspections.jsonl`: one record per inspection with the transport, the fingerprint,
  `state_changed` and, when it changed, `changed_paths` (at most 20) against the frozen first
  state.
- `levels.jsonl`: the one-second window rows of every bound node, pre and post.
- `window-groups.jsonl`, `input-status.jsonl`, `v2-decisions.jsonl`, `v2-proposals.jsonl`:
  the six-role groups fed to V2, its input status per group, every decision, and the nonzero
  proposals (`shadow_only` true, `applied` false).
- `coordinator-calibration.json` and `coordinator-bins.jsonl`: the calibration plan and
  settings (or the calibration error) with the grid, the measured `host_writes` at the time
  and `declared.live_eligible` false, then every 100 ms bin with its phase, per-track levels,
  current and target corrections and coordinator events.
- `red-calibration.jsonl`: ActivityCalibration's observation of each RED pre bin.
- `gaps.jsonl` and `events.jsonl`: dropped frames and incomplete bins; read failures with their
  native code, grid discontinuities, rebinds, nodes waited for and collection start.
- `summary.json`.

## Reading summary.json

- `outcome` (`completed`, `stopped` or `failed`), `reason` and `detail` say how the run ended;
  `stop_reason` repeats `reason`. For `host_state_changed` the detail names the changed paths
  as the inspection that caught the change saw them, and `host_after.diff` lists what differed
  at the final snapshot, which may be more.
- `host_pid` and `bindings.<policy>.source_map_revision` (with its `epoch`) identify the Live
  process and the nodes that were bound; `inspected_pid` is the pid of the Live the bridge
  inspected, and the two are equal on every run that bound. A later run on a restarted Live
  has a different pid and revisions.
- `collection` holds the collection start and end, when Play was observed, `seconds_requested`,
  `max_seconds` and the coordinator `grid`.
- V2: `complete_groups` is the number of six-role one-second groups fed, `proposed_moves` the
  number of nonzero shadow proposals, `controller_evaluations` the number of decisions, and
  `decision_reason_counts` and `input_reason_counts` explain holds. A high hold count with few
  proposals is the expected shape on a steady mix.
- Coordinator: `coordinator_bins` is the number of 100 ms bins processed after calibration;
  `coordinator` holds `calibrated`, `calibration_error`, `gap_notifications`, `event_types`,
  the `grid`, the `settings` from calibration and `final_correction_db` per track, all
  unapplied. `gap_notifications` counts the incomplete bins the coordinator was told of, in
  grid order; a dropped block on a node it does not consume (a post tap) never reaches it.
  `gaps` and `gap_kinds` count dropped frames and incomplete bins across all sources.
- RED: `red_lock` is `{ready, ready_bin_index, gain_db}` and `red` adds the bins observed, the
  median level and the last reason.
- Measured: `host_calls` counts every host operation the run made by name, `inspections` the
  `inspect_session` reads among them (the failed first read of a `inspection_failed` run and
  the final read included), and `writes` and `host_mutations` the calls that were anything
  else, 0 on every run. `connect()` also reads `status` once per bridge descriptor before the
  run starts; that read is not in these counts.
- Declared: `declared` holds `pcm_saved`, `emulator_used`, `live_eligible` and
  `musical_quality_validated`, all false by design rather than by measurement.
  `host_sample_alignment` is the run's qualification statement, false on every run: no window
  fed to V2 is host-sample aligned, whatever grid the coordinator used.
- `sha256` lists the digests of the modules named in `HASHED_MODULES`: `session_nodes.py`,
  `home_shadow.py`, `experimental_live_input.py`, `experimental_audio_gain.py` (the V1 base
  V2 evaluates with), `experimental_audio_gain_v2.py`, `mix_coordinator.py`,
  `mix_coordinator_trial.py` (the calibration) and `activity_calibration.py`.

A completed run is evidence about these policies on this recording at these faders. It says
nothing about live behavior on a mixed service, and it qualifies nothing for application.
