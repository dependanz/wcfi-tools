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

## Privacy and next steps

Never commit actual PCAP/PCAPNG captures, .dscomp files, complete settings XML, or real device identifiers. Store captures outside the repository, e.g. `C:\dev\_captures\wcfi-tools\touchmix`. Tests use synthetic identity/configuration data.

1. Establish a live client greeting and read-only XML transfer.
2. Verify or refute identifier stability with a second authorized session.
3. Try a single physically isolated input fader; check its position on the console.
4. Capture readback/ack behavior and calibrate fader scale.
5. Add Bonjour discovery and broaden simulator coverage.
6. **Only checkpoint / merge after a real fader movement is confirmed.**
