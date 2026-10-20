"""Decode the 244-byte frames the guest pushes to the media server.

`agentd`'s send_ssid() forwards every SysV message-queue item of type 7 as a
244-byte frame:

    [240 bytes payload][37-byte ssid at offset 178]

The payload comes from the VRP process, so this is where the router's
console/telemetry stream arrives on the host.
"""
from __future__ import annotations

import socket
import struct
import sys
import time

HOST = "192.168.56.1"
PORT = 20002
GUEST = "192.168.56.101"
SSID_PREFIX = b"4CA76BCE"


def describe(frame: bytes) -> str:
    payload = frame[:240]
    ssid = frame[178:215]
    text = payload.decode("ascii", "replace")
    printable = sum(1 for c in text if 32 <= ord(c) < 127 or c in "\r\n\t")
    head = f"ssid={'OK' if ssid.startswith(SSID_PREFIX) else ssid[:12]!r} "
    if printable > len(text) * 0.4:
        return head + f"TEXT {text!r}"
    return head + f"HEX {payload[:48].hex(' ')}"


def main() -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, PORT))
    except OSError as exc:
        print(f"bind failed: {exc}")
        return 1
    srv.listen(4)
    srv.settimeout(30)
    print(f"media server listening on {HOST}:{PORT}")
    try:
        conn, peer = srv.accept()
    except socket.timeout:
        print("no guest connected")
        return 1
    print(f"guest connected from {peer}")
    conn.settimeout(5.0)
    buf = b""
    frames = 0
    end = time.time() + 60
    while time.time() < end:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            continue
        except OSError as exc:
            print(f"read error: {exc}")
            break
        if not chunk:
            break
        buf += chunk
        while len(buf) >= 244:
            frame, buf = buf[:244], buf[244:]
            frames += 1
            print(f"[{frames}] {describe(frame)}")
    print(f"total frames: {frames}")
    conn.close()
    srv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
