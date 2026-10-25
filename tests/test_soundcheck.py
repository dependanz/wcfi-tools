"""Audio behavior and non-destructive set-copy contracts; no hardware/cloud needed."""

from __future__ import annotations

import gzip
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from wcfi_tools.cli import app
from wcfi_tools.soundcheck.ableton import (
    EQ_NAME,
    GAIN_NAME,
    GLOBAL_IDS,
    apply_plan,
    devices,
    inspect_set,
    read_set,
    value,
)
from wcfi_tools.soundcheck.analysis import analyze_audio
from wcfi_tools.soundcheck.pipeline import calibrate, find_recording, render_report
from wcfi_tools.soundcheck.profiles import file_hash, load_profile, write_json

np = pytest.importorskip("numpy")
sf = pytest.importorskip("soundfile")
pytest.importorskip("scipy")


@pytest.fixture
def tone(tmp_path):
    t = np.arange(12 * 24000) / 24000
    audio = .04 * np.sin(2 * np.pi * 440 * t)
    path = tmp_path / "RED.wav"
    sf.write(path, audio, 24000, subtype="FLOAT")
    return path


@pytest.fixture
def live_set(tmp_path):
    """Minimal schema fixture; actual installed Live 11 set is checked separately."""
    root = ET.Element("Ableton", Creator="Ableton Live 11.3.35")
    live = ET.SubElement(root, "LiveSet")
    tracks = ET.SubElement(live, "Tracks")
    counter = iter(range(100, 10000))

    def leaf(parent, path, item):
        for tag in path.split("/"):
            found = parent.find(tag)
            parent = found if found is not None else ET.SubElement(parent, tag)
        parent.set("Value", str(item))

    def parameter(parent, name, item):
        p = ET.SubElement(parent, name)
        ET.SubElement(p, "Manual", Value=str(item))
        ET.SubElement(p, "AutomationTarget", Id=str(next(counter)))

    def track(tag, tid, name):
        tr = ET.SubElement(tracks, tag, Id=str(tid))
        leaf(tr, "Name/EffectiveName", name)
        leaf(tr, "DeviceChain/AudioInputRouting/Target", f"AudioIn/External/M{tid}")
        leaf(tr, "DeviceChain/AudioInputRouting/LowerDisplayString", str(tid + 1))
        leaf(tr, "DeviceChain/AudioOutputRouting/Target", "AudioOut/GroupTrack")
        leaf(tr, "DeviceChain/Mixer/Volume/Manual", ".8")
        chain = ET.SubElement(ET.SubElement(tr.find("DeviceChain"), "DeviceChain"), "Devices")
        return tr, chain

    for tid, name in ((1, "RED"), (2, "EGUITAR")):
        tr, chain = track("AudioTrack", tid, name)
        utility = ET.SubElement(chain, "StereoGain", Id="0")
        for key, item in {"UserName": "", "LegacyMode": "false", "MidSideBalanceOn": "false",
                          "BassMonoAudition": "false"}.items():
            leaf(utility, key, item)
        for key, item in {"On": "true", "Mute": "false", "Gain": 10, "LegacyGain": 0,
                          "PhaseInvertL": "false", "PhaseInvertR": "false", "ChannelMode": 1,
                          "StereoWidth": 1, "MidSideBalance": 1, "Mono": "false",
                          "BassMono": "false", "Balance": 0, "DcFilter": "false"}.items():
            parameter(utility, key, item)
        ET.SubElement(utility, "Pointee", Id=str(next(counter)))
        plugin = ET.SubElement(chain, "PluginDevice", Id="1")
        leaf(plugin, "OpaqueState", "DO NOT CHANGE THIS THIRD PARTY PLUGIN")
        leaf(tr, "DeviceChain/Clips/AudioClip/SampleRef/FileRef/RelativePath", "Samples/raw.wav")
    _, group_chain = track("GroupTrack", 3, "VOCALS")
    eq = ET.SubElement(group_chain, "Eq8", Id="0")
    for key, item in {"UserName": "", "Mode": 1, "AuditionOnOff": "true"}.items():
        leaf(eq, key, item)
    for key, item in {"On": "true", "GlobalGain": 3, "Scale": .5, "AdaptiveQ": "true"}.items():
        parameter(eq, key, item)
    ET.SubElement(eq, "Pointee", Id=str(next(counter)))
    for i in range(8):
        band = ET.SubElement(eq, f"Bands.{i}")
        for side in ("ParameterA", "ParameterB"):
            p = ET.SubElement(band, side)
            for key, item in {"IsOn": "true", "Mode": 3, "Freq": 1000,
                              "Gain": 8, "Q": 4}.items():
                parameter(p, key, item)
    master = ET.SubElement(live, "MasterTrack")
    leaf(master, "AudioOutputRouting/Target", "AudioOut/External/S11")
    leaf(master, "UntouchedMasterProcessing", "original")
    ET.SubElement(live, "NextPointeeId", Value=str(next(counter)))
    path = tmp_path / "Service.als"
    path.write_bytes(gzip.compress(ET.tostring(root)))
    return path


def make_profile(live_set, tone):
    profile = inspect_set(live_set)
    profile["tracks"][0]["file"] = str(tone)
    profile["tracks"][1]["enabled"] = False
    path = live_set.parent / "profile.json"
    write_json(path, profile)
    return path


def test_silence_is_blocked(tone):
    sf.write(tone, np.zeros(12 * 24000), 24000)
    result = analyze_audio(tone, "vocal")
    assert result["status"] == "blocked"
    assert result["gain_db"] is None
    assert result["eq"] == []


def test_clipping_is_blocked(tone):
    sf.write(tone, np.tile([1., -1.], 6 * 24000), 24000, subtype="FLOAT")
    result = analyze_audio(tone, "vocal")
    assert result["status"] == "blocked"
    assert any("full scale" in r for r in result["reasons"])


def test_antiphase_stereo_is_not_mistaken_for_silence(tone):
    audio, sr = sf.read(tone)
    sf.write(tone, np.column_stack([audio, -audio]), sr, subtype="FLOAT")
    result = analyze_audio(tone, "keys")
    assert result["status"] == "blocked"
    assert result["metrics"]["active_seconds"] >= 8
    assert result["metrics"]["stereo_correlation"] == -1


def test_gain_responds_to_changed_input_and_respects_peak_margin(tone):
    first = analyze_audio(tone, "vocal")
    audio, sr = sf.read(tone)
    sf.write(tone, audio * 2, sr, subtype="FLOAT")
    second = analyze_audio(tone, "vocal")
    assert first["status"] == second["status"] == "ready"
    assert first["gain_db"] - second["gain_db"] == pytest.approx(6.02, abs=.02)
    assert second["metrics"]["estimated_sample_peak_after_gain_dbfs"] <= -14.99
    assert len(first["eq"]) == 1  # pure note is insufficient evidence for broad EQ


def test_quiet_passages_do_not_drive_gain(tone):
    first = analyze_audio(tone, "vocal")
    audio, sr = sf.read(tone)
    sf.write(tone, np.concatenate([np.zeros(len(audio)), audio]), sr, subtype="FLOAT")
    second = analyze_audio(tone, "vocal")
    assert second["gain_db"] == pytest.approx(first["gain_db"], abs=.02)


def test_excessive_boost_is_blocked(tone):
    audio, sr = sf.read(tone)
    sf.write(tone, audio * .1, sr, subtype="FLOAT")
    result = analyze_audio(tone, "vocal")
    assert result["status"] == "blocked"
    assert any("boost" in r for r in result["reasons"])


def test_broad_eq_is_bounded_and_cut_only(tone):
    from scipy.signal import butter, sosfilt

    rng = np.random.default_rng(42)
    noise = rng.normal(size=12 * 24000)
    mud = sosfilt(butter(2, [150, 400], fs=24000, btype="bandpass", output="sos"), noise)
    audio = noise * .024 + mud * .18
    sf.write(tone, audio, 24000, subtype="FLOAT")
    result = analyze_audio(tone, "vocal")
    assert result["status"] == "ready"
    bells = [b for b in result["eq"] if b["kind"] == "bell"]
    assert bells
    assert len(bells) <= 2
    assert all(-3 <= b["gain_db"] < 0 and b["q"] <= 1 for b in bells)


def test_copy_preserves_routing_effects_master_and_original(live_set, tone):
    profile = make_profile(live_set, tone)
    original_hash = file_hash(live_set)
    before = read_set(live_set)
    plan = calibrate(profile, live_set.parent / "run")
    output = live_set.with_name("Calibrated.als")
    assert apply_plan(plan, output) == ["RED"]
    after = read_set(output)
    assert file_hash(live_set) == original_hash
    for path in ("./LiveSet/MasterTrack", "./LiveSet/Tracks/GroupTrack",
                 "./LiveSet/Tracks/AudioTrack[@Id='2']",
                 "./LiveSet/Tracks/AudioTrack[@Id='1']/DeviceChain/AudioInputRouting",
                 "./LiveSet/Tracks/AudioTrack[@Id='1']/DeviceChain/AudioOutputRouting",
                 "./LiveSet/Tracks/AudioTrack[@Id='1']/DeviceChain/Mixer",
                 ".//PluginDevice", ".//Clips"):
        assert ET.tostring(before.find(path)) == ET.tostring(after.find(path))
    chain = devices(after.find("./LiveSet/Tracks/AudioTrack[@Id='1']"))
    assert value(chain[0], "UserName") == GAIN_NAME
    assert value(chain[1], "UserName") == EQ_NAME
    assert float(value(chain[0], "Gain/Manual")) == pytest.approx(10 ** (plan["tracks"][0]["gain_db"] / 20))
    assert value(chain[1], "Bands.0/ParameterA/Mode/Manual") == "1"
    assert value(chain[1], "Bands.0/ParameterB/IsOn/Manual") == "false"
    ids = [e.get("Id") for e in after.iter() if e.tag in GLOBAL_IDS]
    assert len(ids) == len(set(ids))
    assert int(after.find("./LiveSet/NextPointeeId").get("Value")) > max(map(int, ids))


def test_repeat_calibration_does_not_stack_gain_or_devices(live_set, tone):
    profile = make_profile(live_set, tone)
    plan = calibrate(profile, live_set.parent / "run1")
    first = live_set.with_name("First.als")
    apply_plan(plan, first)
    config = json.loads(profile.read_text())
    config["source_set"] = str(first)
    profile.write_text(json.dumps(config))
    second_plan = calibrate(profile, live_set.parent / "run2")
    second = live_set.with_name("Second.als")
    apply_plan(second_plan, second)
    a = devices(read_set(first).find("./LiveSet/Tracks/AudioTrack[@Id='1']"))
    b = devices(read_set(second).find("./LiveSet/Tracks/AudioTrack[@Id='1']"))
    assert len(a) == len(b) == 3
    assert ET.tostring(a) == ET.tostring(b)


def test_inserting_utility_uses_neutral_stereo_settings(live_set, tone):
    root = read_set(live_set)
    chain = devices(root.find("./LiveSet/Tracks/AudioTrack[@Id='1']"))
    chain.remove(chain[0])
    live_set.write_bytes(gzip.compress(ET.tostring(root)))
    plan = calibrate(make_profile(live_set, tone), live_set.parent / "run")
    output = live_set.with_name("Inserted.als")
    apply_plan(plan, output)
    root = read_set(output)
    chain = devices(root.find("./LiveSet/Tracks/AudioTrack[@Id='1']"))
    assert value(chain[0], "StereoWidth/Manual") == "1"
    assert value(chain[0], "BassMono/Manual") == "false"
    assert chain[2].tag == "PluginDevice"
    ids = [e.get("Id") for e in root.iter() if e.tag in GLOBAL_IDS]
    assert len(ids) == len(set(ids))


def test_wrong_recording_channel_count_is_blocked(live_set, tone):
    audio, sr = sf.read(tone)
    sf.write(tone, np.column_stack([audio, audio]), sr, subtype="FLOAT")
    plan = calibrate(make_profile(live_set, tone), live_set.parent / "run")
    assert plan["tracks"][0]["status"] == "blocked"
    assert any("channel count" in r for r in plan["tracks"][0]["reasons"])


@pytest.mark.parametrize("changed", ["set", "audio"])
def test_changed_inputs_invalidate_plan(live_set, tone, changed):
    profile = make_profile(live_set, tone)
    plan = calibrate(profile, live_set.parent / "run")
    path = live_set if changed == "set" else tone
    with path.open("ab") as stream:
        stream.write(b"changed")
    output = live_set.with_name("New.als")
    with pytest.raises(ValueError, match="changed"):
        apply_plan(plan, output)
    assert not output.exists()


def test_never_overwrites_or_moves_set_away_from_samples(live_set, tone, tmp_path):
    plan = calibrate(make_profile(live_set, tone), tmp_path / "run")
    with pytest.raises(ValueError, match="overwritten"):
        apply_plan(plan, live_set)
    with pytest.raises(ValueError, match="beside"):
        apply_plan(plan, tmp_path / "elsewhere" / "new.als")
    output = tmp_path / "existing.als"
    output.write_bytes(b"important")
    with pytest.raises(ValueError):
        apply_plan(plan, output)
    assert output.read_bytes() == b"important"


def test_automated_input_gain_is_rejected(live_set, tone):
    root = read_set(live_set)
    target = root.find(".//StereoGain/Gain/AutomationTarget").get("Id")
    ET.SubElement(root.find("LiveSet"), "PointeeId", Value=target)
    live_set.write_bytes(gzip.compress(ET.tostring(root)))
    plan = calibrate(make_profile(live_set, tone), live_set.parent / "run")
    with pytest.raises(ValueError, match="automation"):
        apply_plan(plan, live_set.with_name("New.als"))


def test_missing_audio_is_reported_without_changing_track(live_set, tone):
    profile = make_profile(live_set, tone)
    tone.unlink()
    plan = calibrate(profile, live_set.parent / "run")
    assert plan["tracks"][0]["status"] == "blocked"
    with pytest.raises(ValueError, match="No channels"):
        apply_plan(plan, live_set.with_name("New.als"))


def test_recording_binding_requires_date_and_unambiguous_take(tone):
    today = datetime.now(UTC).astimezone().date()
    assert find_recording(tone.parent, "RED", today, False) == tone
    other = tone.with_name("RED 0002 [rehearsal].wav")
    other.write_bytes(tone.read_bytes())
    with pytest.raises(ValueError, match="takes"):
        find_recording(tone.parent, "RED", today, False)
    assert find_recording(tone.parent, "RED", today, True) == other
    with pytest.raises(ValueError, match="No recording"):
        find_recording(tone.parent, "RE", today, True)
    with pytest.raises(ValueError):
        find_recording(tone.parent, "RED", today.replace(year=today.year - 1), True)


def test_profile_rejects_duplicate_ids_nonfinite_limits_and_postfx(live_set, tone):
    path = make_profile(live_set, tone)
    original = json.loads(path.read_text())
    for key, val in (("max_boost_db", float("nan")), ("capture_kind", "post_fx"),
                     ("tracks", [original["tracks"][0]] * 2)):
        bad = {**original, key: val}
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_profile(path)


def test_html_escapes_track_names():
    report = render_report({"source_set": "<script>x</script>", "created_at": "today", "tracks": []})
    assert "<script>" not in report
    assert "&lt;script&gt;" in report


def test_cli_soundcheck_has_no_api_setup_requirement(live_set, tone, tmp_path):
    runner = CliRunner()
    profile = tmp_path / "cli.json"
    result = runner.invoke(app, ["soundcheck", "init", str(live_set), "--profile", str(profile)])
    assert result.exit_code == 0, result.output
    config = json.loads(profile.read_text())
    config["tracks"][0]["file"] = str(tone)
    config["tracks"][1]["enabled"] = False
    profile.write_text(json.dumps(config))
    output = tmp_path / "cli-run"
    copy = tmp_path / "Ready.als"
    result = runner.invoke(app, ["soundcheck", "calibrate", str(profile), "--output", str(output),
                                 "--write-set", str(copy)])
    assert result.exit_code == 0, result.output
    assert copy.exists() and (output / "report.html").exists()
    result = runner.invoke(app, ["soundcheck", "calibrate", str(profile), "--output", str(output)])
    assert result.exit_code == 1  # immutable run directories
