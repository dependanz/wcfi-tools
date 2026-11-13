# WCFI Toolkit

```bash
pip install wcfi
```

A modular toolkit built for **Word Christian Fellowship International**, with tools that other churches and AV teams may eventually adopt.

## What can the toolkit do?

- [x] **Meeting Minutes** — Transcribe recordings and draft meeting minutes.
- [x] **Soundcheck** — Analyze rehearsal tracks and prepare calibrated Ableton Live set copies.
- [x] **Monitor Control** — View and control connected displays and application windows on Windows.
- [x] **Feedback Detection** — Detect persistent tones and feedback-like growth.
- [x] **Ableton Live Tools** — Inspect and explicitly control tracks, faders, and devices.
- [x] **Audio Emulator** — Simulate multichannel audio inputs and measure DAW outputs.
- [x] **Audio Research Lab** — Evaluate recordings, mix proposals, and experimental audio models.
- [ ] **Speaker Attribution** — Identify participants in recorded meetings.
- [ ] **Mixing Assistant** — Offer validated, reviewable mixing recommendations.
- [ ] **Live Automixing** — Develop and qualify safe automated mixing for real services.

Checked items have an initial implementation; they are **not necessarily production-ready**.

## Documentation

[Soundcheck](docs/soundcheck.md) · [Monitor Control](docs/monitor-control.md) · [Feedback Detection](docs/audio-detection.md) · [Ableton Bridge](docs/ableton-bridge.md) · [Audio Emulator](docs/audio-emulator.md) · [Rehearsal Analysis](docs/role-analysis.md) · [Home Shadow](docs/home-shadow.md) · [Mix Evaluation](docs/full-mix-benchmark.md)

## Development

Currently, install from source (Python 3.11+):

```bash
git clone https://github.com/dependanz/wcfi-tools.git
cd wcfi-tools
git switch checkpoint/audio-emulator-monitor-control
python -m pip install -e ".[dev]"
wcfi --help
```

Optional features require additional dependencies and, in some cases, native components. See the relevant documentation. The current Python distribution is named `wcfi-tools`; the shorter `wcfi` PyPI package is a future goal.

## License

[MIT](LICENSE).
