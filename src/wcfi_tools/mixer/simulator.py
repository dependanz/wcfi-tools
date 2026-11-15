"""Tiny loopback-only TouchMix wire simulator for protocol tests and CLI rehearsals.

This implements only greeting, configuration transfer, and captured 5/1 writes.
It deliberately does not claim to simulate DSP, metering, permissions, or all
TouchMix protocol behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import socket
import struct
import xml.etree.ElementTree as ET
import zlib

from .touchmix import Frame, OBSERVED_INPUT_FADERS, recv_frame


def synthetic_config() -> bytes:
    root = ET.Element("Config", Version="3.0.12462", DeviceType="3", Name="TM30")
    for n, (channel, (index, _identifier)) in enumerate(OBSERVED_INPUT_FADERS.items()):
        ch = ET.SubElement(root, "Channel", Type="1", Instance=str(n), Name=channel)
        eff = ET.SubElement(ch, "Effect", Type="26", Instance="0", Name="Control")
        ET.SubElement(eff, "Parameter", Type="41", Instance="0", value="-12.0", Index=str(index))
    return ET.tostring(root, encoding="utf-8")


def _send_config(sock: socket.socket, xml: bytes) -> None:
    name = b"NH4-config.xml.dscomp"
    sock.sendall(Frame(3, 4, struct.pack("<IH", 0xFFFFFFFF, len(name)) + name).encode())
    compressed = zlib.compress(xml)
    for offset in range(0, len(compressed), 1012):
        part = compressed[offset:offset + 1012]
        sock.sendall(Frame(3, 4, struct.pack("<IH", offset, len(part)) + part).encode())
    sock.sendall(Frame(3, 4, struct.pack("<IH", 0xFFFFFFFE, len(name)) + name).encode())


@dataclass
class TouchMixSimulator:
    host: str = "127.0.0.1"
    port: int = 58211
    received_writes: list[tuple[int, float]] = field(default_factory=list)
    greeting_received: bool = False

    def serve_once(self, *, ready=None) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("Simulator only binds 127.0.0.1")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, self.port))
            listener.listen(1)
            self.port = listener.getsockname()[1]
            if ready is not None:
                ready.set()
            conn, _peer = listener.accept()
            with conn:
                conn.settimeout(5)
                n = b"DemoMixer123"
                model = b"TM30Pro"
                greeting = struct.pack("<HIIH", 1, 0x00030000, len(n), len(n) + len(model)) + n + model
                conn.sendall(Frame(1, 3, greeting).encode())
                try:
                    client_hello = recv_frame(conn)
                except (OSError, EOFError, RuntimeError):
                    return  # identify-only clients close without a handshake.
                if (client_hello.kind, client_hello.subtype) != (1, 3):
                    return
                self.greeting_received = True
                conn.sendall(Frame(2, 2, b"\x1b\x00\x01\x00\x00\x00").encode())
                _send_config(conn, synthetic_config())
                while True:
                    try:
                        frame = recv_frame(conn)
                    except (OSError, EOFError, RuntimeError):
                        return
                    if (frame.kind, frame.subtype) != (5, 1):
                        continue
                    if len(frame.payload) == 14:
                        count, body_length, identifier, value = struct.unpack("<IHIf", frame.payload)
                        if count == 1 and body_length == 8:
                            self.received_writes.append((identifier, value))
