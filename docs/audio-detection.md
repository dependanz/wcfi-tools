# Audio detection

`wcfi audio detect howler` is an experimental passive listener for sustained
narrow tones and feedback-like growth. It works with a local microphone or a
saved recording, using the same streaming detector in both modes.

## Try it

Install into the environment that runs `wcfi`:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[audio-detect]"
wcfi audio detect devices
wcfi audio detect howler --seconds 15
```

With no `--seconds`, microphone analysis runs until **Ctrl+C**. It prints the
selected device, input level, candidate frequencies, and a summary. The command
opens input only: there is no monitoring playback, automatic filtering, or mixer
control. No audio is uploaded or saved.

Choose an input by the index or name printed by `devices`:

```powershell
wcfi audio detect howler --device 7 --seconds 30
wcfi audio detect howler --device "Microphone Array" --channel 1
```

Device indexes may change after hardware is connected or disconnected. A device
name may appear under several Windows audio APIs; an explicit index removes that
ambiguity. The device's default sample rate is used unless `--sample-rate` is set.

## Recordings and reports

```powershell
wcfi audio detect howler "rehearsal.wav"
wcfi audio detect howler "iphone-recording.m4a" --output howler-report.json
wcfi audio detect howler "rehearsal.wav" --seconds 30 --json
```

WAV/FLAC and formats supported by libsndfile are read directly. Other formats,
including iPhone M4A/MP4 recordings, require **FFmpeg on PATH** and are temporarily
decoded locally. The temporary audio is removed when the input closes. An iPhone
recording must first be copied to the computer; this command does not yet connect
to a phone's live microphone over the network.

`--channel` selects one channel (default 1); channels are never averaged, so
opposite-phase stereo cannot cancel a tone. File analysis runs as fast as it can,
and reported times refer to the input audio. `--seconds` caps the amount analyzed.

`--json` emits JSON Lines: session metadata, detection start/update/end events,
live input levels, capture-gap notices, and a final summary. `--output` writes
that summary to a **new** JSON file. It contains measurements and the first 1,000
completed events, never audio. The total count and omitted count remain available;
JSON Lines continues to emit every event. Existing reports are not overwritten.
Ctrl+C during listening finishes active events and produces a partial summary.

## Interpreting detections

- **TONAL CANDIDATE**: a narrow, sufficiently prominent tone persists. A musical
  note, whistle, mechanical tone, or steady feedback can all produce this label.
- **FEEDBACK-LIKE**: a persistent tone also shows growth consistent with feedback.
  This is evidence, not proof of an unstable feedback loop.
- **No sustained candidates**: no events met the configured thresholds in the
  received audio. This does not establish that the room or equipment is fault-free.

The first implementation uses spectral prominence and temporal tracking. It is
not a trained classifier, a reproduction of NINOS²-T, or a ground-loop diagnostic.
The numeric score is a heuristic, not a calibrated probability. Growing musical
tones can trigger it, while steady or heavily processed feedback may remain a
tonal candidate. There is no automatic suppression.

Tune the persistence and frequency range:

```powershell
wcfi audio detect howler --min-duration 0.8 --threshold-db 20
wcfi audio detect howler --min-frequency 200 --max-frequency 8000
```

Lower `--threshold-db` is more sensitive; longer `--min-duration` rejects shorter
tones but delays reports. The default range starts at 100 Hz; a dedicated mains
hum classifier is not included in this first command. The upper limit is bounded
by the input's Nyquist frequency.

## Microphone limitations

A laptop or phone microphone is sufficient for useful experiments when the tone
is present in its captured signal. Automatic gain, noise suppression, echo
cancellation, clipping, placement, and background music can change the evidence.
PortAudio capture does not guarantee that driver or OS processing is disabled;
turn off voice enhancements in the recording-device settings where available.
Input levels are digital **dBFS**, not calibrated sound-pressure measurements.

Capture overflows reset persistence tracking and are reported as gaps. Times
count received samples and exclude unknown missing intervals. A disconnected or
stalled input fails with an error instead of being treated as silence.

Validation includes synthetic detector controls, file/CLI checks, mocked input
lifecycle tests, and a five-second Windows WASAPI laptop-microphone capture check.
Real-world accuracy across microphones, rooms, and musical material has not been
established.
