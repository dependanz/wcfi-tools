"""WCFI v5: offline fixed-master-headroom verification against existing v4 stress PCM.

Research-only dry seven-stem audio. Uses ONE master trim calibrated on the clean
v3 passage, held fixed across every v4 fault and policy. Streams float PCM only
to FFmpeg's null sink: no WAV files, host writes, monitoring or automatic gains.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import soundfile as sf

from . import mix_fault_authority_v4 as v4
from . import mix_objective_audit as v1

TARGET_LUFS = -24.0  # a TEST LEVEL; not automatically appropriate for church audio
EXPECTED_SCENARIOS = tuple(v4.SCENARIOS)
TRUE_PEAK_TOL_DB = 0.16  # FFmpeg R128 reports one decimal place
LUFS_TOL = 0.2


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def finite(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise ValueError('Expected finite, real metric')
    return float(x)


def validate_reports(trial_path, audit_path, v3_path, v4_path):
    trial_path = Path(trial_path).resolve(strict=True)
    audit_path = Path(audit_path).resolve(strict=True)
    v3_path = Path(v3_path).resolve(strict=True)
    v4_path = Path(v4_path).resolve(strict=True)
    trial, v3r, v4r = [json.loads(p.read_text(encoding='utf-8'))
                        for p in (trial_path, v3_path, v4_path)]
    if trial['protocol']['source_audit_sha256'] != digest(audit_path):
        raise ValueError('Trial and source audit mismatch')
    if v3r['protocol']['kind'] != 'seven_stem_offline_gain_candidate_explorer_v3':
        raise ValueError('Expected v3 candidate report')
    if v4r['protocol']['kind'] != 'seven_stem_fault_authority_audit_v4':
        raise ValueError('Expected v4 fault report')
    if v4r['protocol']['v3_sha256'] != digest(v3_path):
        raise ValueError('V4 is not derived from this precise v3 summary')
    if v4r['protocol']['trial_sha256'] != digest(trial_path):
        raise ValueError('V4 trial fingerprint mismatch')
    if v4r['protocol']['audit_sha256'] != digest(audit_path):
        raise ValueError('V4 source audit fingerprint mismatch')
    if (v3r['protocol']['report_sha256'] != digest(trial_path)
            or v3r['protocol']['source_audit_sha256'] != digest(audit_path)):
        raise ValueError('V3 provenance mismatch')
    if (v3r['protocol']['host_writes'] != 0 or
            v3r['protocol']['automatic_gain_decision_made'] is not False or
            v3r['protocol']['no_audio_output'] is not True):
        raise ValueError('Refuse v3 that authorized host or audio actions')
    if (v4r['protocol']['host_writes'] != 0 or
            v4r['protocol']['audio_files_written'] != 0 or
            v4r['protocol']['automatic_gain_decision_made'] is not False):
        raise ValueError('Refuse v4 that authorized host/audio output')
    if (finite(v4r['protocol']['hard_true_peak_ceiling_dbtp']) != -3.0 or
            finite(v4r['protocol']['exploratory_guardband_db']) != 0.5 or
            finite(v4r['protocol']['injected_fault_db']) != 10.0 or
            tuple(v4r['protocol']['sustained_fault_seconds']) != (20, 50)):
        raise ValueError('Changed v4 stress or peak contract')
    if tuple(v4r['scenarios']) != EXPECTED_SCENARIOS:
        raise ValueError('Unexpected v4 scenarios')
    if v4r.get('frontier') != v3r.get('raw_safe_pareto_frontier'):
        raise ValueError('V3 and v4 Pareto frontier mismatch')
    verified = v1._check_source_files(trial)
    frontier, grid = v4.verify_prior(v3r, trial, audit_path.read_bytes(), verified)
    if set(v4r['candidate_results']) != set(frontier):
        raise ValueError('V4 candidate set mismatch')
    for name in frontier:
        info = v4r['candidate_results'][name]
        prior = v3r['candidates'][name]
        if set(info['fault_scenarios']) != set(EXPECTED_SCENARIOS):
            raise ValueError('V4 scenario set mismatch')
        if prior['r128']['status'] != 'measured':
            raise ValueError('V3 clean loudness is not measured')
        if not math.isclose(finite(info['baseline_v3_true_peak_dbtp']),
                            finite(prior['r128']['true_peak_dbtp']), abs_tol=1e-9):
            raise ValueError('V4 baseline true peak differs from v3')
        for s in EXPECTED_SCENARIOS:
            x = info['fault_scenarios'][s]
            if x['status'] != 'measured' or x['no_audio_file_written'] is not True:
                raise ValueError('V4 stress is not a measured, file-free scan')
            finite(x['true_peak_dbtp']); finite(x['integrated_lufs'])
    return trial, v3r, v4r, frontier, grid


def trim_from_clean(clean_lufs, target=TARGET_LUFS):
    clean_lufs, target = finite(clean_lufs), finite(target)
    if not -35.0 <= target <= -16.0:
        raise ValueError('Test loudness target must be between -35 and -16 LUFS')
    cut = target - clean_lufs
    if not -24.0 <= cut <= 0.0:
        raise ValueError('Refuse upward master gain or extreme attenuation')
    return cut


def scalar_prediction(x, clean_trim, guardband=-3.5):
    # Multiplication by a positive scalar commutes with ideal true-peak
    # oversampling. R128's displayed values are quantized, so these are
    # *predictions* independently checked by subsequent FFmpeg scans.
    return {
        'predicted_true_peak_dbtp': finite(x['true_peak_dbtp']) + clean_trim,
        'predicted_integrated_lufs': finite(x['integrated_lufs']) + clean_trim,
        'predicted_headroom_to_guardband_db': guardband - (finite(x['true_peak_dbtp'])+clean_trim),
    }


def scan_trimmed(readers, gains, scenario, clean_trim, *, start, ffmpeg,
                 n_bins=1200):
    """Re-scan exact upstream hypothetical stereo sums with frozen master trim."""
    if scenario not in EXPECTED_SCENARIOS:
        raise ValueError('Unknown fixed fault scenario')
    if type(n_bins) is not int or not 1 <= n_bins <= 1200:
        raise ValueError('Invalid duration')
    if not -24 <= finite(clean_trim) <= 0:
        raise ValueError('Only bounded attenuation is allowed')
    for r in readers.values():
        r.seek(start)
    cmd = [str(ffmpeg), '-nostdin', '-hide_banner', '-nostats', '-threads', '1',
           '-filter_threads', '1', '-f', 'f32le', '-ar', str(v4.RATE),
           '-ac', '2', '-i', 'pipe:0', '-af', 'ebur128=peak=true', '-f', 'null', '-']
    mult = 10 ** (clean_trim / 20.0)
    max_peak = 0.0
    exceeds = 0
    first_exceed = None
    threshold = 10 ** ((v4.CEILING - v4.GUARDBAND)/20)
    with tempfile.TemporaryFile(mode='w+t', encoding='utf-8') as log:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=log)
        try:
            for i in range(n_bins):
                blocks = []
                for t in v1.TRACKS:
                    b = readers[t].read(v4.BIN, dtype='float32', always_2d=True)
                    if len(b) != v4.BIN:
                        raise ValueError('Unexpected short WAV source: '+t)
                    blocks.append(b)
                output = v4.mix_block(blocks, gains, scenario, i) * mult
                if not np.isfinite(output).all():
                    raise ValueError('Nonfinite trimmed sum')
                peak = float(np.max(np.abs(output)))
                max_peak = max(max_peak, peak)
                if peak > threshold:
                    exceeds += 1
                    if first_exceed is None: first_exceed = i/10
                p.stdin.write(output.astype('<f4').tobytes())
        except Exception:
            p.kill(); p.wait()
            raise
        finally:
            if p.stdin is not None and not p.stdin.closed: p.stdin.close()
        try: code = p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait()
            raise ValueError('FFmpeg fixed-trim scan timeout')
        log.seek(0)
        if code: raise ValueError('FFmpeg fixed-trim scan exited '+str(code))
        result = v4.parse_r128_summary(log.read())
    return { 'status':'measured', **result,
             'sample_peak_dbfs':20*math.log10(max(max_peak,1e-30)),
             'sample_peak_guardband_exceeding_100ms_bins':exceeds,
             'first_sample_peak_guardband_exceedance_seconds':first_exceed,
             'no_audio_file_written':True }


def run(report_path, audit_path, v3_path, v4_path, ffmpeg, output):
    output = Path(output)
    if output.exists():
        raise ValueError('Output folder must not already exist')
    trial, v3r, v4r, frontier, grid = validate_reports(report_path, audit_path, v3_path, v4_path)
    ffmpeg = Path(ffmpeg).resolve(strict=True)
    if not ffmpeg.is_file(): raise ValueError('FFmpeg executable is not a file')
    rate, _, _, end, sources = v1._source_windows(trial)
    start = trial['protocol']['evaluation_source_frames'][0]
    if rate != v4.RATE or end-start != 120*v4.RATE:
        raise ValueError('Wrong trial duration or sample rate')
    controls = {}
    for name in frontier:
        clean = v3r['candidates'][name]['r128']
        trim = trim_from_clean(clean['integrated_lufs'])
        details = {}
        for scenario in EXPECTED_SCENARIOS:
            prior = v4r['candidate_results'][name]['fault_scenarios'][scenario]
            details[scenario] = {
                'upstream_true_peak_dbtp': prior['true_peak_dbtp'],
                'upstream_integrated_lufs': prior['integrated_lufs'],
                **scalar_prediction(prior, trim)
            }
        controls[name] = {'clean_lufs':clean['integrated_lufs'],
                          'clean_true_peak_dbtp':clean['true_peak_dbtp'],
                          'frozen_master_trim_db':trim,
                          'predicted_clean_true_peak_dbtp':finite(clean['true_peak_dbtp'])+trim,
                          'scenarios':details}
    with ExitStack() as stack:
        readers={t:stack.enter_context(sf.SoundFile(sources[t]['path'],'r')) for t in v1.TRACKS}
        for name in frontier:
            for scenario in EXPECTED_SCENARIOS:
                expected=controls[name]['scenarios'][scenario]
                actual=scan_trimmed(readers,grid[name],scenario,
                       controls[name]['frozen_master_trim_db'],start=start,ffmpeg=ffmpeg)
                if abs(actual['true_peak_dbtp']-expected['predicted_true_peak_dbtp'])>TRUE_PEAK_TOL_DB:
                    raise ValueError(f'Unexpected true-peak mismatch: {name}/{scenario}')
                if abs(actual['integrated_lufs']-expected['predicted_integrated_lufs'])>LUFS_TOL:
                    raise ValueError(f'Unexpected loudness mismatch: {name}/{scenario}')
                expected.update(measured_fixed_trim=actual,
                    peak_guardband_pass=actual['true_peak_dbtp']<=v4.CEILING-v4.GUARDBAND,
                    peak_guardband_margin_db=(v4.CEILING-v4.GUARDBAND)-actual['true_peak_dbtp'])
    for t, meta in sources.items():
        st = Path(meta['path']).stat()
        if st.st_size != meta['size'] or st.st_mtime_ns != meta['mtime_ns']:
            raise ValueError('Source WAV metadata changed during scan: '+t)
    out = {
      'protocol':{'kind':'seven_stem_fixed_master_trim_fault_audit_v5',
                  'source':'same_frozen_2026_10_04_development_recording',
                  'target_clean_lufs':TARGET_LUFS,
                  'master_trim_calibrated_only_on_clean_v3_lufs':True,
                  'master_trim_frozen_across_fault_scenarios':True,
                  'true_peak_guardband_dbtp':v4.CEILING-v4.GUARDBAND,
                  'v3_sha256':digest(v3_path), 'v4_sha256':digest(v4_path),
                  'trial_sha256':digest(report_path),'audit_sha256':digest(audit_path),
                  'source_verified':True,'host_writes':0,'audio_files_written':0,
                  'live_eligible':False,'automatically_selected':False,
                  'perceptual_improvement_established':False},
      'candidates':controls,
      'all_tested_faults_meet_fixed_trim_guardband':all(
          x['peak_guardband_pass'] for c in controls.values() for x in c['scenarios'].values()),
      'automatic_selection':None,
      'limitations':[
        'Fixed master trim is a separate output-stage engineering control, not a musical balance prescription.',
        'A -24 LUFS clean-recording target is experimental, not a church PA level or published live streaming requirement.',
        'These are synthetic faults on previously examined development recordings, not held-out input uncertainty.',
        'The real Ableton master graph, external processing and device output remain unmeasured.',
        'This does not justify automatic KIT boosts or changes to backing vocal settings.',
        'Already-saturated source material cannot be repaired by downstream attenuation.',
        'No automatic host actions or audio files were produced.',
      ]}
    output.mkdir(parents=True, exist_ok=False)
    (output/'summary.json').write_text(json.dumps(out,indent=2,allow_nan=False),encoding='utf-8')
    return out


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',required=True,type=Path)
    p.add_argument('--audit',required=True,type=Path)
    p.add_argument('--v3-summary',required=True,type=Path)
    p.add_argument('--v4-summary',required=True,type=Path)
    p.add_argument('--ffmpeg',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    args=p.parse_args()
    try:
        r=run(args.report,args.audit,args.v3_summary,args.v4_summary,args.ffmpeg,args.output)
        print(json.dumps({'output':str(args.output.resolve()),
          'candidate_count':len(r['candidates']),
          'scenario_count':len(EXPECTED_SCENARIOS),
          'fixed_trim_pass':{name:all(x['peak_guardband_pass'] for x in v['scenarios'].values())
                             for name,v in r['candidates'].items()},
          'all_tested_faults_meet_fixed_trim_guardband':r['all_tested_faults_meet_fixed_trim_guardband'],
          'host_writes':0},indent=2))
    except (ValueError,KeyError,AssertionError,RuntimeError,OSError,TypeError) as exc:
        p.exit(1,'WCFI v5 fixed master headroom study refused: '+str(exc)+'\n')

if __name__=='__main__': main()
