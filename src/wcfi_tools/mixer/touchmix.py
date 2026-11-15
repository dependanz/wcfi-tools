"""Experimental *native* QSC TouchMix-30 Pro TCP protocol driver.

Reverse-engineered from a single authorized iPhone capture (2026-11-15).
No Mixing Station, vendor SDK, or iPhone dependency. Python standard library only.

Only one, opt-in, explicit test-channel write is permitted by this first driver.
This is not a qualified implementation for live service audio.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
import ipaddress
import math
import socket
import struct
import time
import xml.etree.ElementTree as ET
import zlib

DEFAULT_PORT = 58210
SOFTWARE_VERSION = 0x00030000
COMPAT_CLIENT_TYPE = "TM30Pro-iPhone"  # Captured wire client-type token, not an app dependency.
EXPECTED_CONFIG_VERSION = "3.0.12462"
MAX_COMPRESSED_BYTES = 3 * 1024 * 1024
MAX_XML_BYTES = 20 * 1024 * 1024

# Only values observed in a single authorized capture. These identifiers are NOT
# proven to be stable across connections, firmware revisions, or other mixers.
OBSERVED_INPUT_FADERS = {
    "In 1": (1227, 0x6EF904CB),
    "In 2": (1315, 0x6EF90523),
    "In 3": (1403, 0x6EF9057B),
}


class TouchMixError(RuntimeError):
    """Connection, framing, interpretation, or safety refusal."""


@dataclass(frozen=True)
class Frame:
    kind: int
    subtype: int
    payload: bytes

    def encode(self) -> bytes:
        length = 6 + len(self.payload)
        if length > 0xFFFF:
            raise TouchMixError("Protocol frame exceeds uint16 length")
        return struct.pack("<HHH", self.kind, self.subtype, length) + self.payload


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        data = sock.recv(length - len(chunks))
        if not data:
            raise TouchMixError(f"Peer closed connection after {len(chunks)}/{length} bytes")
        chunks.extend(data)
    return bytes(chunks)


def recv_frame(sock: socket.socket) -> Frame:
    kind, subtype, length = struct.unpack("<HHH", _recv_exact(sock, 6))
    if length < 6:
        raise TouchMixError(f"Invalid frame length {length}")
    return Frame(kind, subtype, _recv_exact(sock, length - 6))


def pack_greeting(name: str = "WCFI-tools", client_type: str = COMPAT_CLIENT_TYPE) -> Frame:
    if not name or not client_type:
        raise TouchMixError("Name and client type cannot be empty")
    n = name.encode("utf-8")
    c = client_type.encode("ascii")
    if len(n) > 255 or len(n) + len(c) > 1024:
        raise TouchMixError("Client identity too long")
    # Observed: uint16 protocol, uint32 software, uint32 name bytes,
    # uint16 total identity bytes, followed by two unseparated UTF-8 strings.
    data = struct.pack("<HIIH", 1, SOFTWARE_VERSION, len(n), len(n) + len(c)) + n + c
    return Frame(1, 3, data)


def parse_greeting(frame: Frame) -> dict[str, object]:
    if (frame.kind, frame.subtype) != (1, 3) or len(frame.payload) < 12:
        raise TouchMixError("Peer did not send the expected TouchMix greeting")
    version, software, name_length, total_length = struct.unpack_from("<HIIH", frame.payload)
    identity = frame.payload[12:]
    if name_length > total_length or len(identity) != total_length:
        raise TouchMixError("Invalid greeting identity lengths")
    try:
        name = identity[:name_length].decode("utf-8")
        device_type = identity[name_length:].decode("ascii")
    except UnicodeDecodeError as exc:
        raise TouchMixError("Invalid identity string encoding") from exc
    if device_type != "TM30Pro":
        raise TouchMixError(f"Refusing unexpected mixer type {device_type!r}")
    if version != 1:
        raise TouchMixError(f"Unsupported TouchMix protocol version {version}")
    return {"name": name, "device_type": device_type,
            "protocol_version": version, "software_version_token": software}


def _bounded_inflate(compressed: bytes) -> bytes:
    try:
        decompressor = zlib.decompressobj()
        plain = decompressor.decompress(compressed, MAX_XML_BYTES + 1)
        if len(plain) > MAX_XML_BYTES or decompressor.unconsumed_tail:
            raise TouchMixError("Configuration decompresses beyond permitted size")
        plain += decompressor.flush(MAX_XML_BYTES + 1 - len(plain))
        if len(plain) > MAX_XML_BYTES or not decompressor.eof or decompressor.unused_data:
            raise TouchMixError("Invalid, truncated, or oversized compressed configuration")
        return plain
    except zlib.error as exc:
        raise TouchMixError("Invalid compressed TouchMix configuration") from exc


class ConfigTransfer:
    """Collect only the captured named .dscomp file-transfer format in memory."""

    def __init__(self):
        self.active = False
        self.name: str | None = None
        self.content = bytearray()

    def add(self, frame: Frame) -> bytes | None:
        if (frame.kind, frame.subtype) != (3, 4):
            return None
        if len(frame.payload) < 6:
            raise TouchMixError("Short file-transfer payload")
        offset, n = struct.unpack_from("<IH", frame.payload)
        chunk = frame.payload[6:]
        if len(chunk) < n:
            raise TouchMixError("Truncated file-transfer chunk")
        chunk = chunk[:n]  # Completion frame can contain extra metadata.
        if offset == 0xFFFFFFFF:
            self.name = chunk.decode("utf-8", errors="replace")
            self.active = self.name == "NH4-config.xml.dscomp"
            self.content.clear()
            return None
        if offset == 0xFFFFFFFE:
            if not self.active:
                return None
            self.active = False
            return _bounded_inflate(bytes(self.content))
        if not self.active:
            return None
        if offset != len(self.content):
            raise TouchMixError(f"Out-of-order configuration chunk at offset {offset}")
        if len(self.content) + len(chunk) > MAX_COMPRESSED_BYTES:
            raise TouchMixError("Configuration transfer exceeds permitted size")
        self.content.extend(chunk)
        return None


@dataclass(frozen=True)
class Fader:
    channel: str
    channel_type: int
    parameter_index: int
    xml_db: float | None


def parse_faders(xml: bytes) -> tuple[dict[str, object], dict[str, Fader]]:
    # Reject DTD/entities; we don't need any of them in the captured schema.
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise TouchMixError("Refusing XML with DTD or entity declarations")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise TouchMixError("Invalid mixer configuration XML") from exc
    if root.tag != "Config":
        raise TouchMixError("Expected a <Config> XML root")
    info = {"config_version": root.get("Version"), "config_name": root.get("Name"),
            "channel_elements": len(root.findall("Channel"))}
    faders: dict[str, Fader] = {}
    for channel in root.findall("Channel"):
        control = next((e for e in channel.findall("Effect") if e.get("Name") == "Control"), None)
        if control is None:
            continue
        level = next((p for p in control.findall("Parameter") if
                      p.get("Type") == "41" and p.get("Instance") == "0"), None)
        if level is None:
            continue
        name = channel.get("Name")
        if not name or name in faders:
            raise TouchMixError("Missing or repeated mixer channel name")
        db = None
        if level.get("value") is not None:
            try:
                db = float(level.get("value"))
            except ValueError as exc:
                raise TouchMixError("Invalid mixer configuration fader value") from exc
        faders[name] = Fader(name, int(channel.get("Type", "-1")),
                             int(level.attrib["Index"]), db)
    return info, faders


def pack_parameter_write(identifier: int, normalized: float) -> Frame:
    if not 0 <= identifier <= 0xFFFFFFFF or not math.isfinite(normalized):
        raise TouchMixError("Invalid parameter identifier or non-finite fader value")
    if not 0 <= normalized <= 1:
        raise TouchMixError("Normalized fader value must be in 0..1")
    # Observed client kind/subtype 5/1: 1 pair, 8 payload bytes,
    # uint32 full identifier, float32 normalized-looking value.
    return Frame(5, 1, struct.pack("<IHIf", 1, 8, identifier, normalized))


def plan_input_fader(channel: str, normalized: float, config: dict[str, object],
                     faders: dict[str, Fader]) -> dict[str, object]:
    if config.get("config_version") != EXPECTED_CONFIG_VERSION:
        raise TouchMixError("Unverified firmware/configuration version; refusing writes")
    if not 0.35 <= normalized <= 0.75:
        raise TouchMixError("First hardware experiment limits normalized position to 0.35..0.75")
    if channel not in OBSERVED_INPUT_FADERS:
        raise TouchMixError("Only captured input faders In 1, In 2, In 3 are permitted")
    fader = faders.get(channel)
    expected_index, identifier = OBSERVED_INPUT_FADERS[channel]
    if fader is None or fader.channel_type != 1 or fader.parameter_index != expected_index:
        raise TouchMixError("Live configuration disagrees with captured fader index/type")
    # This intentionally does not convert configuration dB to UI normalized units.
    # The capture did not establish that transform or confirm readback semantics.
    return {"channel": channel, "parameter_index": fader.parameter_index,
            "observed_identifier": f"0x{identifier:08X}",
            "requested_normalized": normalized, "configuration_db_snapshot": fader.xml_db,
            "protocol_version": config["config_version"],
            "warning": "Unverified write identifier; no live readback or automatic rollback"}


def _validate_target(host: str, *, allow_loopback: bool = False) -> None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise TouchMixError("Use a literal private IPv4 address, not a DNS name") from exc
    rfc1918 = any(ip in network for network in (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    ))
    if ip.version != 4 or not (rfc1918 or (allow_loopback and ip.is_loopback)):
        raise TouchMixError("Only RFC1918 IPv4 mixers (or explicit loopback simulator) are allowed")


class TouchMixClient:
    def __init__(self, host: str, port: int = DEFAULT_PORT, *, timeout: float = 8,
                 name: str = "WCFI-tools", allow_loopback: bool = False):
        _validate_target(host, allow_loopback=allow_loopback)
        if not 1 <= port <= 65535 or not 0.25 <= timeout <= 60:
            raise TouchMixError("Invalid port or timeout")
        self.host, self.port, self.timeout, self.name = host, port, timeout, name
        self.sock: socket.socket | None = None
        self.greeting: dict[str, object] | None = None

    def __enter__(self) -> TouchMixClient:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        try:
            self.greeting = parse_greeting(recv_frame(self.sock))
        except Exception:
            self.sock.close()
            self.sock = None
            raise
        return self

    def __exit__(self, *_: object) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def identify(self) -> dict[str, object]:
        if self.greeting is None:
            raise TouchMixError("Not connected")
        return {**self.greeting, "host": self.host, "port": self.port}

    def retrieve_configuration(self, *, trace: Callable[[dict[str, object]], None] | None = None
                               ) -> tuple[dict[str, object], dict[str, Fader]]:
        """Identify this client and read a fresh configuration; never write parameters.

        The optional trace emits framing/transfer metadata, NOT mixer settings.
        In particular, diagnostic timeouts are distinct from write outcomes.
        """
        if self.sock is None:
            raise TouchMixError("Not connected")
        self.sock.sendall(pack_greeting(self.name).encode())
        transfer = ConfigTransfer()
        deadline = time.monotonic() + self.timeout
        started = time.monotonic()
        counts: Counter[str] = Counter()
        last_event: str | None = None

        def progress(stage: str) -> dict[str, object]:
            return {
                "stage": stage,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "frames_by_kind": dict(sorted(counts.items())),
                "config_transfer_started": transfer.name == "NH4-config.xml.dscomp",
                "config_compressed_bytes": len(transfer.content),
                "last_frame": last_event,
            }

        if trace is not None:
            trace(progress("client_greeting_sent"))
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TouchMixError(f"Configuration deadline reached: {progress('deadline')}")
            self.sock.settimeout(left)
            try:
                frame = recv_frame(self.sock)
            except (TimeoutError, socket.timeout) as exc:
                raise TouchMixError(f"Configuration timeout: {progress('socket_timeout')}") from exc
            except TouchMixError as exc:
                raise TouchMixError(f"Configuration connection ended: {progress('connection_ended')}: {exc}") from exc
            key = f"{frame.kind}/{frame.subtype}"
            counts[key] += 1
            last_event = key
            previous_bytes = len(transfer.content)
            result = transfer.add(frame)
            if trace is not None and (counts[key] == 1 or
                                      len(transfer.content) // 16384 > previous_bytes // 16384):
                trace(progress("frame_received"))
            if result is not None:
                if trace is not None:
                    trace(progress("configuration_complete"))
                return parse_faders(result)

    def send_test_fader(self, channel: str, value: float, config: dict[str, object],
                        faders: dict[str, Fader]) -> dict[str, object]:
        if self.sock is None:
            raise TouchMixError("Not connected")
        plan = plan_input_fader(channel, value, config, faders)
        _index, identifier = OBSERVED_INPUT_FADERS[channel]
        self.sock.sendall(pack_parameter_write(identifier, value).encode())
        # TCP sendall completion is NOT mixer acceptance or proof of movement.
        return {**plan, "sent": True, "verified": False,
                "result": "sent_unverified_check_physical_mixer"}
