"""Configuration boundary tests: fake host only, never connects to Ableton."""
from copy import deepcopy
import math

import pytest

from wcfi_tools.audio.control_node import PARAMETERS, RED_PARAMETERS, METERS, ControlNodeError, bind, compare_telemetry, configure_one


class Host:
    def __init__(self):
        self.controls = []
        defaults = (0., 0., -24., 12., 12., -60.)
        for (setting, (name, low, high)), value in zip(PARAMETERS.items(), defaults):
            self.controls.append({"id": setting, "name": name,
                "min": 0., "max": 1., "value": (value-low)/(high-low),
                "display": str(value), "is_enabled": True, "automation_state": 0})
        self.snapshot = {"session_id": "session", "tracks": [{"id": "lapel", "name": "LAPEL",
            "devices": [{"id": "pre", "name": "WCFI Analysis Node"},
                        {"id": "node", "name": "WCFI Control Node", "parameters": self.controls},
                        {"id": "post", "name": "WCFI Analysis Node"}]}]}
        self.calls = []
        self.race = None
        self.after_write = None
        self.lost_reply = False

    def control(self, setting):
        return next(item for item in self.controls if item["id"] == setting)

    def call(self, operation, args=None, *, request_id=None):
        assert operation in ("inspect_session", "set_parameter")
        self.calls.append((operation, deepcopy(args), request_id))
        if operation == "inspect_session":
            return deepcopy(self.snapshot)
        assert request_id and args["session_id"] == "session"
        assert args["normalized"] is True
        if self.race:
            self.race(self)
        control = self.control(args["parameter_id"])
        if abs(control["value"] - args["expected_value"]) > 1e-6:
            raise RuntimeError("host CAS conflict")
        raw = control["min"] + args["value"] * (control["max"] - control["min"])
        receipt = {"before": deepcopy(control), "dry_run": args["dry_run"], "requested_value": raw}
        if args["dry_run"]:
            return receipt
        control["value"] = raw
        receipt.update(after=deepcopy(control), verified=True)
        if self.after_write:
            self.after_write(self)
        if self.lost_reply:
            raise TimeoutError("write applied; reply lost")
        return receipt

    @property
    def writes(self):
        return [call for call in self.calls if call[0] == "set_parameter"]


def setup():
    host = Host()
    return host, bind(host.snapshot, "LAPEL")


def red_setup():
    host = Host()
    track = host.snapshot["tracks"][0]
    track["name"] = "RED"
    track["devices"][1]["name"] = "WCFI Control Node RED"
    for (setting, (name, low, high)), value in zip(RED_PARAMETERS.items(), (0., 0., -26., 6., 6., -55.)):
        host.control(setting).update(name=name, value=(value-low)/(high-low), display=str(value))
    return host, bind(host.snapshot, "RED", "WCFI Control Node RED")


def test_red_identity_uses_bounded_ranges_and_manual_defaults():
    host, owned = red_setup()
    assert owned["profile"] == "red-single-mic"
    assert owned["parameters"]["max-boost"]["plain_value"] == 6.
    assert owned["parameters"]["target-rms"]["plain_value"] == -26.
    result = configure_one(host, owned, "trim-db", 3.)
    assert host.writes[0][1]["value"] == .75
    assert result["next_binding"]["parameters"]["trim-db"]["plain_value"] == 0.


@pytest.mark.parametrize("setting,value", [("trim-db", 6.01), ("trim-db", -6.01),
    ("max-boost", 12.), ("max-cut", 7.)])
def test_red_limits_rejected_before_host_access(setting, value):
    host, owned = red_setup()
    with pytest.raises(ControlNodeError):
        configure_one(host, owned, setting, value, apply=True, journal=lambda _: None)
    assert host.calls == []


def test_red_binding_profile_change_does_not_reinterpret_parameters():
    host, owned = red_setup()
    host.snapshot["tracks"][0]["devices"][1]["name"] = "WCFI Control Node"
    with pytest.raises(ControlNodeError):
        configure_one(host, owned, "trim-db", 3.)
    assert host.writes == []


def test_legacy_binding_without_profile_keeps_legacy_range():
    host, owned = setup()
    owned.pop("profile")
    configure_one(host, owned, "trim-db", 18.)
    assert host.writes[0][1]["value"] == .875


def test_red_telemetry_distinguishes_holding_from_legacy_active():
    host, owned = red_setup()
    values = (-30., -20., -24., -14., 6., 6., 0., 4., 0., 1.)
    for (key, (name, low, high)), value in zip(METERS.items(), values):
        if key == "state_code":
            high = 4.
        host.controls.append({"id": key, "name": name, "min": 0., "max": 1.,
                              "value": (value-low)/(high-low)})
    telemetry = bind(host.snapshot, "RED", "WCFI Control Node RED")["telemetry"]
    assert telemetry["ready"] and telemetry["state"] == "holding"
    assert telemetry["values"]["state_code"] == 4


def test_red_binding_preserves_existing_processing_between_analysis_taps():
    host, _ = red_setup()
    devices = host.snapshot["tracks"][0]["devices"]
    devices.insert(1, {"id": "utility", "name": "Utility"})
    devices.insert(2, {"id": "eq", "name": "Pro-Q 4"})
    owned = bind(host.snapshot, "RED", "WCFI Control Node RED")
    assert owned["analysis_node_ids"] == ["pre", "post"]
    assert owned["device_order"] == ["pre", "utility", "eq", "node", "post"]
    devices[1], devices[2] = devices[2], devices[1]
    with pytest.raises(ControlNodeError, match="device_order"):
        configure_one(host, owned, "auto", 1.)
    assert host.writes == []


def test_red_ambiguous_analysis_bracket_rejected():
    host, _ = red_setup()
    host.snapshot["tracks"][0]["devices"].insert(1, {"id": "extra", "name": "WCFI Analysis Node"})
    with pytest.raises(ControlNodeError, match="exactly one"):
        bind(host.snapshot, "RED", "WCFI Control Node RED")


def test_renamed_legacy_plugin_cannot_be_mistaken_for_bounded_red_plugin():
    host = Host()
    host.snapshot["tracks"][0]["devices"][1]["name"] = "WCFI Control Node RED"
    with pytest.raises(ControlNodeError, match="Calibrate & Hold"):
        bind(host.snapshot, "LAPEL", "WCFI Control Node RED")
    assert host.calls == []


def test_default_dry_run_never_changes_host_or_owned_binding():
    host, owned = setup()
    before, saved = deepcopy(host.snapshot), deepcopy(owned)
    result = configure_one(host, owned, "trim-db", 18.)
    assert result["event"] == "dry_run"
    assert host.snapshot == before and owned == saved
    assert result["next_binding"] == owned
    assert host.writes[0][1]["dry_run"] is True
    assert host.writes[0][1]["value"] == .875


def test_apply_journals_before_write_and_returns_new_binding():
    host, owned = setup()
    saved, events = deepcopy(owned), []
    def journal(event):
        if event["event"] == "write_pending":
            assert host.writes == []
        events.append(event)
    result = configure_one(host, owned, "trim-db", 18., apply=True, journal=journal)
    assert [event["event"] for event in events] == ["write_pending", "confirmed"]
    assert owned == saved
    assert result["next_binding"]["parameters"]["trim-db"]["plain_value"] == 18.
    assert [call[0] for call in host.calls] == ["inspect_session", "set_parameter", "inspect_session"]
    with pytest.raises(ControlNodeError, match="Manual override"):
        configure_one(host, owned, "auto", 1.)
    configure_one(host, result["next_binding"], "auto", 1.)
    assert len(host.writes) == 2


def test_host_native_range_conversion_uses_normalized_input():
    host = Host()
    host.control("trim-db").update(min=-24., max=24., value=0.)
    result = configure_one(host, bind(host.snapshot, "LAPEL"), "trim-db", 18.)
    assert result["receipt"]["requested_value"] == 18.
    assert host.writes[0][1]["value"] == .875
    assert host.writes[0][1]["expected_value"] == 0.


@pytest.mark.parametrize("setting,value", [("trim-db",25.), ("auto",.5), ("target-rms",-40.),
    ("max-cut",-1.), ("activity-threshold",math.nan), ("max-boost",math.inf), ("auto",True)])
def test_invalid_values_rejected_before_host_call(setting, value):
    host, owned = setup()
    with pytest.raises(ControlNodeError):
        configure_one(host, owned, setting, value)
    assert host.calls == []


def test_apply_requires_journal_and_failed_journal_prevents_write():
    host, owned = setup()
    with pytest.raises(ControlNodeError, match="journal"):
        configure_one(host, owned, "auto", 1., apply=True)
    def unavailable(event):
        raise OSError("disk full")
    with pytest.raises(OSError):
        configure_one(host, owned, "auto", 1., apply=True, journal=unavailable)
    assert host.writes == []


@pytest.mark.parametrize("change", ["manual", "automated", "disabled", "reordered", "session"])
def test_stale_ownership_stops_before_any_write(change):
    host, owned = setup()
    if change == "manual":
        host.control("max-cut")["value"] = .25
    elif change == "automated":
        host.control("trim-db")["automation_state"] = 1
    elif change == "disabled":
        host.control("trim-db")["is_enabled"] = False
    elif change == "reordered":
        host.snapshot["tracks"][0]["devices"].reverse()
    else:
        host.snapshot["session_id"] = "new-session"
    with pytest.raises(ControlNodeError):
        configure_one(host, owned, "trim-db", 18., apply=True, journal=lambda event: None)
    assert host.writes == []


def test_manual_target_change_between_inspect_and_write_hits_original_cas():
    host, owned = setup()
    host.race = lambda h: h.control("trim-db").update(value=.6)
    with pytest.raises(RuntimeError, match="CAS conflict"):
        configure_one(host, owned, "trim-db", 18., apply=True, journal=lambda event: None)
    assert len(host.writes) == 1
    assert host.writes[0][1]["expected_value"] == .5
    assert host.control("trim-db")["value"] == .6


def test_other_parameter_race_is_detected_on_readback_without_rollback():
    host, owned = setup()
    host.after_write = lambda h: h.control("auto").update(value=1.)
    events = []
    with pytest.raises(ControlNodeError, match="Manual override"):
        configure_one(host, owned, "trim-db", 18., apply=True, journal=events.append)
    assert host.control("trim-db")["value"] == .875
    assert host.control("auto")["value"] == 1.
    assert len(host.writes) == 1
    assert events[-1]["write_may_have_applied"] is True


def test_uncertain_write_never_retries_or_restores():
    host, owned = setup()
    host.lost_reply, events = True, []
    with pytest.raises(TimeoutError):
        configure_one(host, owned, "trim-db", 18., apply=True, journal=events.append)
    assert len(host.writes) == 1
    assert host.control("trim-db")["value"] == .875
    assert events[-1]["event"] == "stop_inspect_before_further_commands"


def test_placement_and_duplicate_names_are_rejected():
    host = Host()
    host.snapshot["tracks"][0]["devices"][0]["name"] = "Something else"
    with pytest.raises(ControlNodeError, match="neighbors"):
        bind(host.snapshot, "LAPEL")
    host = Host()
    host.snapshot["tracks"].append(deepcopy(host.snapshot["tracks"][0]))
    with pytest.raises(ControlNodeError, match="Expected one track"):
        bind(host.snapshot, "LAPEL")


def test_cli_apply_requires_receipt_before_connecting(monkeypatch, tmp_path):
    import json
    from typer.testing import CliRunner
    from wcfi_tools.commands.control_node import app
    from wcfi_tools.ableton import client
    host, owned = setup()
    binding = tmp_path / "owned.json"
    binding.write_text(json.dumps(owned), encoding="utf-8")
    def forbidden(**kwargs):
        pytest.fail("Should not connect without a journal path")
    monkeypatch.setattr(client, "connect", forbidden)
    result = CliRunner().invoke(app, ["set", str(binding), "--setting", "trim-db",
                                     "--value", "18", "--apply"])
    assert result.exit_code == 1
    assert "--apply requires" in result.stdout


def test_cli_defaults_to_dry_run(monkeypatch, tmp_path):
    import json
    from typer.testing import CliRunner
    from wcfi_tools.commands.control_node import app
    from wcfi_tools.ableton import client
    host, owned = setup()
    binding = tmp_path / "owned.json"
    binding.write_text(json.dumps(owned), encoding="utf-8")
    monkeypatch.setattr(client, "connect", lambda **kwargs: host)
    result = CliRunner().invoke(app, ["set", str(binding), "--setting", "trim-db", "--value", "18"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["event"] == "dry_run"
    assert host.control("trim-db")["value"] == .5


def test_cli_apply_writes_receipt_and_refuses_receipt_overwrite(monkeypatch, tmp_path):
    import json
    from typer.testing import CliRunner
    from wcfi_tools.commands.control_node import app
    from wcfi_tools.ableton import client
    host, owned = setup()
    binding, receipt = tmp_path / "owned.json", tmp_path / "receipt.jsonl"
    binding.write_text(json.dumps(owned), encoding="utf-8")
    monkeypatch.setattr(client, "connect", lambda **kwargs: host)
    args = ["set", str(binding), "--setting", "trim-db", "--value", "18",
            "--apply", "--receipt", str(receipt)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.stdout
    events = [json.loads(line) for line in receipt.read_text(encoding="utf-8").splitlines()]
    assert [item["event"] for item in events] == ["write_pending", "confirmed"]
    saved = receipt.read_bytes()
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert receipt.read_bytes() == saved and len(host.writes) == 1


def metered_host():
    host = Host()
    values = (-40.,-30.,-37.,-27.,0.,3.,0.,0.,0.,100.)
    for (key,(name,low,high)), value in zip(METERS.items(), values):
        host.controls.append({"id":"meter-"+key,"name":name,"min":0.,"max":1.,
                              "value":(value-low)/(high-low),"display":str(value)})
    return host


def test_meter_snapshot_has_readable_units_and_does_not_claim_freshness():
    host = metered_host()
    result = bind(host.snapshot,'LAPEL')['telemetry']
    assert result['ready'] and result['state']=='manual'
    assert result['delivery']=='unverified_single_snapshot'
    assert result['values']['input_rms_dbfs']==pytest.approx(-40.)
    assert result['values']['applied_gain_db']==pytest.approx(3.)


def test_moving_readonly_meters_do_not_invalidate_parameter_ownership():
    host = metered_host()
    owned = bind(host.snapshot,'LAPEL')
    host.control('meter-sequence')['value'] = 101/65535
    result = configure_one(host,owned,'trim-db',0.)
    assert result['event']=='dry_run' and len(host.writes)==1


def test_meter_delivery_handles_wrap_and_distinguishes_no_change():
    host = metered_host()
    host.control('meter-sequence')['value'] = 1.
    first = bind(host.snapshot,'LAPEL')
    host.control('meter-sequence')['value'] = 1/65535
    second = bind(host.snapshot,'LAPEL')
    assert compare_telemetry(first,second)['delivery']=='advanced_between_inspections'
    assert compare_telemetry(second,second)['delivery']=='not_confirmed_between_inspections'
    second['device_id']='different'
    with pytest.raises(ControlNodeError): compare_telemetry(first,second)


def test_waiting_sentinel_and_old_plugin_do_not_appear_ready():
    host = metered_host()
    host.control('meter-input_rms_dbfs')['value']=0.
    assert bind(host.snapshot,'LAPEL')['telemetry']['ready'] is False
    assert bind(Host().snapshot,'LAPEL')['telemetry']['available'] is False
