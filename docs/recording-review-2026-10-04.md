# October 4 rehearsal: observations and tool requirements

## Evidence and scope

Reviewed the morning Arrangement in Ableton Live 11 using the running application.
Saved the current state as `WCFI-2026-10-04-Inspection.als` beside the original set before
changing monitoring. The original set was not overwritten. No EQ, compressor, fader,
send or output-routing settings were changed for this review.

The long matched take uses recording filename timestamp `2026-10-04 094134`.
Ableton displays 48 kHz, 24-bit recording data. The arrangement has a substantially
larger RED vocal waveform than YELLOW/GREEN in the louder passage; waveform size alone
does not establish loudness, vocal role, or the quality of the processed mix.

People were playing the instruments live during inspection. Initial peak-hold readings
and playback before every external input was switched to Monitor Off are **not valid
measurements of the recorded mix**. Selecting all tracks from a vocal track selected
that group's siblings, leaving instrument monitoring unchanged. Each instrument and
laptop input was subsequently set to Off, and the Master fell silent after playback
stopped and effect tails decayed. AMBIENT had no clip in the inspected arrangement region.

The review is visual and numerical; the computer-control connection provides no audible
feed to the assistant. Claims about harshness, intelligibility, masking, musical balance,
or whether a microphone contains the intended performer require an audible review.

## Verified processing observations

User-confirmed source roles: RED is always the lead vocal; LAPEL is normally the preacher.
The local band-rehearsal profile now excludes LAPEL until a verified spoken segment is
available. RED's role note is recorded, but the present input-gain algorithm still uses
the generic vocal target; it does not yet implement a lead-versus-backing balance target.
Whether AC GUITAR was used in this recording remains unconfirmed.

- KEYS has two enabled Utility stages set to +8 dB, both with 169% width, with EQ devices
  between them. This is not a calculation of the complete chain's net gain or stereo image.
- GREEN has an enabled +25 dB Utility before a 10:1 compressor, with a 1 ms attack,
  30 ms release, -14.8 dB threshold, +8 dB output and 100% wet processing.
- RED has a +10 dB input Utility, EQ/de-essing, a 3.82:1 compressor with a -33 dB
  threshold and +10 dB output blended at 46% wet, then saturation and a parallel rack.
  During isolated recorded playback near arrangement bar 465 (approximately 17:41),
  the compressor momentarily displayed -17.1 dB gain reduction. This is a snapshot,
  not a maximum across the rehearsal; the dry/wet blend means it is not the entire
  vocal output's attenuation.
- The Master includes multiband dynamics at 29% amount, a Glue Compressor at 19% wet
  with +6.67 dB makeup, a subsequent +6.78 dB Utility, and a Limiter set to -0.30 dB
  ceiling with -1.70 dB input gain. A limiter ceiling alone does not prove true-peak
  headroom or adequate loudness.

These settings are review candidates, not proof that a particular effect sounds bad.
Earlier group peak holds may include the live jamming and must not be attributed to
this recording.

## Consequences for the tool

1. **Verify the measurement source.** A virtual soundcheck must explicitly isolate the
   recorded inputs, check for residual signal while stopped, and restore the prior
   monitoring state afterward. Do not assume a group-level selection includes all tracks.
2. **Separate raw input calibration from finished-mix validation.** The existing
   `calibrate` command uses raw pre-effects recordings. The new `inspect-mix` command
   measures a Master export with all the existing processing, without normalizing it.
   Neither result substitutes for the other, and neither measures TouchMix/ATEM output.
3. **Require roles and evidence of performance.** Lead/backing assignments, an unused
   lapel or acoustic input, and bleed cannot be inferred reliably from level alone.
   A quiet channel should not receive automatic boosts just because its RMS is low.
4. **Evaluate the complete chain before accepting a baseline.** Multiple gain stages,
   existing EQ, compression, stereo processing and bus sums can counteract an input
   calibration. Audition the result and retain a repeatable before/after Master render.
   The present tool does not automatically retune those existing effects.

## Reproducible numerical check

`wcfi soundcheck inspect-mix` writes integrated loudness, loudness range, sample/true peak,
minute summaries of short-term loudness, an HTML report, and the underlying FFmpeg log.
The report records the input hash and rejects a file that changes during measurement.
It is a measurement report, not a plan accepted by `apply`.

The baseline export was requested from Ableton's Master, normalization off, stereo,
48 kHz and 32-bit. Render start is 9.1.1, length 884.2.1 at the displayed 105 BPM.
Report times are relative to this export, not the beginning of the Arrangement timeline.

The completed export is 2,021.936 seconds (33:41.936), stereo 48 kHz, 32-bit float.
The measured baseline is:

| Measurement | Result |
| --- | --- |
| Integrated loudness | -10.3 LUFS |
| Loudness range | 7.3 LU |
| Sample peak | -0.3 dBFS |
| Estimated true peak | +0.7 dBTP |
| Loudest momentary reading | -5.7 LUFS, window ending at file time 23:28.4 |

The true-peak estimate exceeds full scale even though sample peaks respect the
limiter ceiling. Output headroom therefore needs review before accepting a calibrated
mix. These are digital Master-export measurements, not measurements of the analog
TouchMix/ATEM path or the published livestream. They do not prove audible distortion.

The last five minutes have short-term medians around -65 LUFS. Rehearsal calibration
must use a verified active performance segment rather than assuming the end of a take
contains representative playing.

Local evidence is in `C:\Users\Danze\Documents\ChatGPT\wcfi\soundcheck\2026-10-04-mix-review`
(`report.html`, `mix.json`, and `ffmpeg.log`). The source SHA-256 is
`e611d41a6099d2de8f0d49bbd7f1cda21a1c18f40cbc80185cfd0c83814d45ce`.

After inspection, the preserved inspection copy was reopened without saving temporary
changes. Playback is stopped and input monitoring is restored to Auto. The copy's
monitoring values, manual parameter values, and input/output routing across all 25
tracks match the saved pre-inspection snapshot. The original set remains untouched.
