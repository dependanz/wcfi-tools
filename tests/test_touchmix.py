"""Offline protocol tests; never contacts an actual network mixer."""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from wcfi_tools.mixer.simulator import TouchMixSimulator, synthetic_config
from wcfi_tools.mixer.touchmix import (
    ConfigTransfer, Frame, OBSERVED_INPUT_FADERS, TouchMixClient, TouchMixError,
    pack_greeting, pack_parameter_write, parse_faders, parse_greeting, plan_input_fader,
    recv_frame,
)

SERVER_GREETING = bytes.fromhex(
    "01 00 03 00 25 00 01 00 00 00 03 00 0c 00 00 00 13 00 "
    "44 65 6d 6f 4d 69 78 65 72 31 32 33 54 4d 33 30 50 72 6f"
)
CAPTURED_FADER_WRITE = bytes.fromhex(
    "05 00 01 00 14 00 01 00 00 00 08 00 cb 04 f9 6e 00 00 11 3f"
)


def test_exact_mixer_greeting():
    msg = Frame(1, 3, SERVER_GREETING[6:])
    assert msg.encode() == SERVER_GREETING
    assert parse_greeting(msg) == {
        "name": "DemoMixer123", "device_type": "TM30Pro",
        "protocol_version": 1, "software_version_token": 196608,
    }


def test_capture_exact_fader_packet():
    assert pack_parameter_write(0x6EF904CB, 0.56640625).encode() == CAPTURED_FADER_WRITE


def test_client_hello_matches_captured_layout():
    frame = pack_greeting("Tester")
    expected = bytes.fromhex(
        "01 00 03 00 26 00 01 00 00 00 03 00 06 00 00 00 14 00 "
        "54 65 73 74 65 72 54 4d 33 30 50 72 6f 2d 69 50 68 6f 6e 65"
    )
    assert frame.encode() == expected


def test_simulated_config_maps_only_three_faders():
    info, faders = parse_faders(synthetic_config())
    assert info["config_version"] == "3.0.12462"
    assert set(faders) == {"In 1", "In 2", "In 3"}
    assert faders["In 1"].parameter_index == 1227


def test_rejects_unidentified_and_main_faders():
    info, faders = parse_faders(synthetic_config())
    for channel in ("Main", "FX 3", "In 9"):
        with pytest.raises(TouchMixError):
            plan_input_fader(channel, 0.55, info, faders)
    for value in (-0.1, 0.1, 0.9, float("nan")):
        with pytest.raises(TouchMixError):
            plan_input_fader("In 1", value, info, faders)
    with pytest.raises(TouchMixError):
        plan_input_fader("In 1", 0.55, {**info, "config_version": "4.0"}, faders)
    with pytest.raises(TouchMixError):
        plan_input_fader("In 1", 0.55, info,
                         {**faders, "In 1": faders["In 2"]})


def test_config_transfer_rejects_out_of_order_chunks():
    trans = ConfigTransfer()
    n = b"NH4-config.xml.dscomp"
    trans.add(Frame(3, 4, struct.pack("<IH", 0xFFFFFFFF, len(n)) + n))
    with pytest.raises(TouchMixError):
        trans.add(Frame(3, 4, struct.pack("<IH", 10, 4) + b"aaaa"))


def test_config_rejects_dtd():
    with pytest.raises(TouchMixError):
        parse_faders(b'<!DOCTYPE Config [<!ENTITY x "y">]><Config>&x;</Config>')


def test_frame_reader_handles_tcp_fragmentation():
    left, right = socket.socketpair()
    try:
        left.settimeout(2)
        def send():
            for chunk in (SERVER_GREETING[:3], SERVER_GREETING[3:8], SERVER_GREETING[8:]):
                right.sendall(chunk)
                time.sleep(0.001)
        t = threading.Thread(target=send)
        t.start()
        assert recv_frame(left).encode() == SERVER_GREETING
        t.join(timeout=1)
    finally:
        left.close()
        right.close()


def test_simulator_handshake_and_fader_write():
    sim = TouchMixSimulator(port=0)
    ready = threading.Event()
    thread = threading.Thread(target=sim.serve_once, kwargs={"ready": ready}, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    with TouchMixClient("127.0.0.1", sim.port, allow_loopback=True) as client:
        assert client.identify()["name"] == "DemoMixer123"
        info, faders = client.retrieve_configuration()
        assert sim.greeting_received
        plan = plan_input_fader("In 1", 0.55, info, faders)
        assert plan["observed_identifier"] == "0x6EF904CB"
        result = client.send_test_fader("In 1", 0.55, info, faders)
        assert result["sent"] and not result["verified"]
    thread.join(timeout=2)
    assert len(sim.received_writes) == 1
    identifier, value = sim.received_writes[0]
    assert identifier == OBSERVED_INPUT_FADERS["In 1"][1]
    assert abs(value - 0.55) < 1e-6


def test_rejects_public_mixer_address():
    for host in ("8.8.8.8", "google.com", "::1", "0.0.0.0", "169.254.1.1"):
        with pytest.raises(TouchMixError):
            TouchMixClient(host)


def test_cli_help_offline():
    from typer.testing import CliRunner
    from wcfi_tools.commands.mixer import app
    result = CliRunner().invoke(app, ["touchmix", "--help"])
    assert result.exit_code == 0
    assert "identify" in result.output
    assert "inspect" in result.output
    assert "fader" in result.output
    assert "simulate" in result.output


def test_cli_does_not_transmit_without_confirmations():
    from typer.testing import CliRunner
    from wcfi_tools.commands.mixer import app
    result = CliRunner().invoke(app, ["touchmix", "fader", "--channel", "In 1",
                                        "--value", "0.55", "--host", "192.168.1.2", "--apply"])
    assert result.exit_code == 1
    assert "--confirm-isolated" in result.output


def test_trace_metadata_has_no_configuration_contents():
    sim = TouchMixSimulator(port=0)
    ready = threading.Event()
    thread = threading.Thread(target=sim.serve_once, kwargs={"ready": ready}, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    events = []
    with TouchMixClient("127.0.0.1", sim.port, allow_loopback=True) as client:
        info, _ = client.retrieve_configuration(trace=events.append)
        assert info["config_version"] == "3.0.12462"
    thread.join(timeout=2)
    assert events[0]["stage"] == "client_greeting_sent"
    assert events[-1]["stage"] == "configuration_complete"
    assert events[-1]["frames_by_kind"]["3/4"] >= 3
    assert "xml" not in str(events).lower()


def test_timeout_reports_frame_stage_without_sending_parameter_write():
    ready = threading.Event()
    port_holder = []
    def silent_peer():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port_holder.append(listener.getsockname()[1])
            ready.set()
            conn, _ = listener.accept()
            with conn:
                conn.sendall(SERVER_GREETING)
                try:
                    recv_frame(conn)
                except OSError:
                    return
                time.sleep(0.5)
    thread = threading.Thread(target=silent_peer, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    with TouchMixClient("127.0.0.1", port_holder[0], timeout=0.3,
                        allow_loopback=True) as client:
        with pytest.raises(TouchMixError, match="Configuration timeout:.*frames_by_kind"):
            client.retrieve_configuration()
    thread.join(timeout=2)


def test_config_filename_can_have_variable_prefix():
    import zlib
    raw = synthetic_config()
    compressed = zlib.compress(raw)
    xfer = ConfigTransfer()
    other = b"User.settings.dqx.dscomp"
    xfer.add(Frame(3, 4, struct.pack("<IH", 0xFFFFFFFF, len(other)) + other))
    xfer.add(Frame(3, 4, struct.pack("<IH", 0, 3) + b"abc"))
    xfer.add(Frame(3, 4, struct.pack("<IH", 0xFFFFFFFE, 0)))
    assert not xfer.seen_config
    name = b"NH5-config.xml.dscomp"
    xfer.add(Frame(3, 4, struct.pack("<IH", 0xFFFFFFFF, len(name)) + name))
    assert xfer.seen_config and xfer.active
    assert "NH5" not in str(xfer.last_marker)
    xfer.add(Frame(3, 4, struct.pack("<IH", 0, len(compressed)) + compressed))
    decoded = xfer.add(Frame(3, 4, struct.pack("<IH", 0xFFFFFFFE, 0)))
    assert decoded == raw
    assert xfer.marker_count == 4
    assert xfer.last_marker["config_candidate"] is True


def test_observe_is_read_only_and_reports_delta(monkeypatch):
    import json
    from typer.testing import CliRunner
    from wcfi_tools.commands import mixer as cmd
    from wcfi_tools.mixer.touchmix import Fader

    current = iter((-18.53717803955078, -10.64678955078125))
    writes = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def retrieve_configuration(self):
            level = next(current)
            return ({"config_version": "3.0.12462"},
                    {"In 1": Fader("In 1", 1, 1227, level)})

        def send_test_fader(self, *args):
            writes.append(args)
            raise AssertionError("Observe must never send a write")

    monkeypatch.setattr(cmd, "TouchMixClient", FakeClient)
    monkeypatch.setattr(cmd.time, "sleep", lambda _delay: None)
    result = CliRunner().invoke(cmd.app, ["touchmix", "observe", "--host", "10.2.2.4",
                                             "--channel", "In 1", "--samples", "2", "--interval", "0"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["read_only"] is True
    assert data["changed_by_at_least_0_01_db"] is True
    assert abs(data["delta_db_first_to_last"] - 7.890388) < 1e-4
    assert not writes


def test_observe_after_one_write_does_not_resend_or_claim_ack():
    sim = TouchMixSimulator(port=0)
    ready = threading.Event()
    thread = threading.Thread(target=sim.serve_once, kwargs={"ready": ready}, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    with TouchMixClient("127.0.0.1", sim.port, allow_loopback=True) as client:
        info, faders = client.retrieve_configuration()
        result = client.send_test_fader("In 1", 0.55, info, faders,
                                        observe_replies_seconds=0.05)
        assert result["sent"] and not result["verified"]
        assert result["post_write_frames_by_kind"] == {}
        assert result["reply_observation_seconds"] == 0.05
    thread.join(timeout=2)
    assert len(sim.received_writes) == 1
