# Native TouchMix-30 Pro driver (experimental)

**State:** offline protocol parsing validated against an authorized capture; live control **not yet verified**. Do not use to operate an active service, rehearsal, or connected speaker system.

This WCFI-tools driver speaks directly to the mixer over TCP. It does not require Mixing Station, an Apple device, an Android emulator, or a vendor control SDK. The iPhone was used once as a reference implementation to observe the private protocol.

## Evidence and limitations

- Bonjour advertises `_ds2inet._tcp.local.` with model `TM30Pro`; SRV supplies TCP endpoint.
- Six-byte, little-endian frame header: `uint16 kind, uint16 subtype, uint16 total_length`.
- Server greeting `1/3`; client greeting `1/3`. The observed compatibility client-type token is `TM30Pro-iPhone`.
- The mixer transfers `NH4-config.xml.dscomp` using `3/4` frames. It is a zlib-compressed XML configuration.
- Fader parameter indices map to `Channel/Effect[@Name="Control"]/Parameter[@Type="41"]`.
- The authorized iPhone emitted `5/1` parameter frames containing full 32-bit IDs and float32 normalized-looking values.
- **Unverified:** high 16 bits of parameter IDs, authorization/registration behavior, actual write acknowledgment, normalized-to-dB mapping, identifier stability across firmware/mixer/session. The captured high word `0x6EF9` is not a documented constant.

These are reverse-engineered observations from one authorized recording, not a vendor specification. No vendor app code or private capture is bundled.

## Installation and first read-only test

From the WCFI-tools checkout:

```powershell
python -m pip install -e ".[dev]"
pytest -q tests/test_touchmix.py
wcfi mixer touchmix identify --host 10.2.2.4
wcfi mixer touchmix inspect --host 10.2.2.4
```

The address above is only an example from the current local experiment. Inspect downloads XML **into RAM**, parses faders and indices, then disconnects. The printed XML dB values are a configuration snapshot, not proven real-time readback.

## Diagnostic: configuration timeout after an earlier successful read

The driver originally required one specific file name, `NH4-config.xml.dscomp`.
The mixer can send other named files, so current code accepts any conservatively
matched `*-config.xml.dscomp` candidate and still validates its XML/schema.
This is an unconfirmed explanation for the 2026-11-15 trace, not proof of
the cause. Trace now reports anonymized file-start/completion markers (hash
prefixes only, never raw file names).

When a connection times out during configuration retrieval, **no parameter
write has occurred**. Do not interpret a failed `--apply` invocation as a
fader movement, and do not repeatedly retry experimental writes.

Update the feature branch and use a read-only trace, preferably after closing
the official mobile app's remote session:

```powershell
git pull --ff-only
python -m pytest -q tests/test_touchmix.py
wcfi mixer touchmix inspect --host 10.2.2.4 --timeout 30 --trace 1>$null
```

The tracing output deliberately includes only message kind/subtype counts,
elapsed time, and compressed-transfer progress—not the actual private
configuration. It tells us whether the mixer returned a `2/2` acknowledgment,
started `3/4` configuration chunks, or stayed silent. This does not send
any fader command or guarantee that a future write is accepted.

## Read-only observation after the first hardware transmissions

Three independent test writes were transmitted on 2026-11-15, but did not
receive verified application-level acknowledgements. The pre-write XML
configuration snapshot for In 1 changed from approximately -18.537 to
-10.647 dB between attempts. **The operator confirmed this increase was a
manual fader adjustment, not caused by WCFI-tools.** The subsequent two
read-only snapshots both measured -10.64679 dB. Thus there is currently
**no verified physical fader movement caused by our driver**. Do not mark a
hardware-control checkpoint or infer write success from these observations.

The new observe command sends NO parameter-update frames. It downloads a
configuration snapshot, announces a pause, and downloads a second snapshot:

```powershell
wcfi mixer touchmix observe --host 10.2.2.4 --channel "In 1" --samples 2 --interval 8
```

For a physical control-to-CLI correlation test, use an *isolated* input and
move the actual console fader during the announced pause; compare its
positions with the CLI's snapshot delta. The results are configuration
snapshots, not proof of software-to-hardware write acceptance.

## Gated single-fader test

By default this is **dry-run**: a plan is generated but no parameter-write frame is transmitted.

```powershell
wcfi mixer touchmix fader --host 10.2.2.4 --channel "In 1" --value 0.55
```

Only when a physically isolated, unused input is confirmed safe, with no active service/rehearsal or connected audible route, run a **single** experimental write. Do not test with an audible input or Main.

```powershell
wcfi mixer touchmix fader --host 10.2.2.4 --channel "In 1" --value 0.55 `
    --apply --confirm-isolated --confirm-channel "In 1"
```

The command sends exactly one candidate `5/1` frame. It reports `sent_unverified_check_physical_mixer`. Successful TCP transmission does **not** prove the TouchMix applied it. Check the mixer physically or in an authorized app. Do not rapidly resend if there is no movement.

Safety fences: only `In 1`, `In 2`, `In 3` whose indices match the authorized capture; firmware config `3.0.12462`; value range `0.35..0.75`; explicit isolated-channel and channel-name confirmations; private IPv4 destination required; Main/FX/mute/scene writes disabled. The command does **not** know the current normalized value, revert an earlier value, or convert dB.

## Offline simulator

The synthetic simulator implements only the greeting, configuration-transfer, and one-parameter-write subset. No DSP, meters, scene management, or mixer networking is simulated. It listens on `127.0.0.1` only and serves one connection.

Terminal A:

```powershell
wcfi mixer touchmix simulate --port 58211
```

Terminal B (each example requires a fresh simulator):

```powershell
wcfi mixer touchmix inspect --host 127.0.0.1 --port 58211 --simulator
wcfi mixer touchmix fader --host 127.0.0.1 --port 58211 --simulator `
    --channel "In 1" --value 0.55 --apply --confirm-isolated --confirm-channel "In 1"
```

The simulator prints any write it observed, but its acceptance is not evidence that the physical mixer accepts the same message.


## Outstanding control-session question

The authorized iPhone capture contains more than a greeting and file transfer:
the client emitted a `8/3` frame (18 bytes) and many `6/1` request frames,
with `7/1` responses from the mixer. The current independent client does
not reproduce those interactions. Their exact semantics, and whether they
are required for write authorization, are **unknown**. The original
`send_test_fader` method sent one `5/1` frame and immediately closed
its session. The current experimental command now holds the same connection
open for up to 2 seconds by default, passively counting any server frames
using `--observe-replies-seconds` (0 to 10). It transmits no additional
commands and still cannot identify a write acknowledgment. A TCP `sendall`
return only establishes local transmission.

Before another real-mixer write, we should inspect these packets from the
authorized capture and add passive post-write response observation to the
independent driver. Do not blindly replay unknown session frames or assume
`0x6EF9` is a permanent parameter-ID namespace.

## Privacy and next steps

Never commit actual PCAP/PCAPNG captures, .dscomp files, complete settings XML, or real device identifiers. Store captures outside the repository, e.g. `C:\dev\_captures\wcfi-tools\touchmix`. Tests use synthetic identity/configuration data.

1. Establish a live client greeting and read-only XML transfer.
2. Verify or refute identifier stability with a second authorized session.
3. Try a single physically isolated input fader; check its position on the console.
4. Capture readback/ack behavior and calibrate fader scale.
5. Add Bonjour discovery and broaden simulator coverage.
6. **Only checkpoint / merge after a real fader movement is confirmed.**
