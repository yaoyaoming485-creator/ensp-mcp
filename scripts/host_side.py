"""Host-side driver for an eNSP guest device.

This is the piece that replaces the eNSP client when you want to bring a
device up yourself and then talk to its VRP console.

It runs three things at once:

  1. a **named-pipe server** for the device's COM2 UART, so VirtualBox
     connects to us and we own the pipe instance (see
     evidence/agentd-protocol.md: the roles are inverted for robustness, not
     because a VBox-created pipe refuses read-write);
  2. a **media server** on 192.168.56.1, which the guest dials back to;
  3. a **control client** on guest TCP 56789, answering the guest's boot
     frame with code 1 so the VRP processes start.

Usage:

    python host_side.py --pipe '\\\\.\\pipe\\MYR2-COM2' --guest-ip 192.168.56.101

Keep it running for as long as the device should stay up: the guest powers
itself off when the control connection goes away.
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading
import time

try:
    from ensp_mcp.agentd import (AGENTD_CONTROL_PORT, CONTROL_CODES,
                                  DEFAULT_DEVICE_ID, DEFAULT_MEDIA_PORT,
                                  MediaServer, SerialPipeServer,
                                  build_handshake_reply)
except ImportError:  # running straight from a checkout
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
    from ensp_mcp.agentd import (AGENTD_CONTROL_PORT, CONTROL_CODES,
                                  DEFAULT_DEVICE_ID, DEFAULT_MEDIA_PORT,
                                  MediaServer, SerialPipeServer,
                                  build_handshake_reply)

import ctypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.ReadFile.restype = ctypes.c_int
k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                         ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
k32.WriteFile.restype = ctypes.c_int
k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                          ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _read(handle, want: int, timeout: float) -> bytes:
    buf = ctypes.create_string_buffer(4096)
    cnt = ctypes.c_uint32(0)
    out = b""
    deadline = time.time() + timeout
    while len(out) < want and time.time() < deadline:
        if not k32.ReadFile(handle, buf, want - len(out), ctypes.byref(cnt), None):
            time.sleep(0.05)
            continue
        if cnt.value:
            out += buf.raw[:cnt.value]
    return out


def _write(handle, data: bytes) -> None:
    buf = ctypes.create_string_buffer(data, len(data))
    cnt = ctypes.c_uint32(0)
    k32.WriteFile(handle, buf, len(data), ctypes.byref(cnt), None)


def serial_worker(pipe_path: str, guest_ip: str, netmask: str,
                  media_port: int, device_id: bytes) -> None:
    server = SerialPipeServer(pipe_path)
    while True:
        handle = server.wait_client(timeout=300.0)
        log(f"pipe: VirtualBox connected to {pipe_path}")
        try:
            challenge = _read(handle, 4, timeout=180.0)
            if len(challenge) < 4:
                log(f"pipe: short challenge {challenge!r}")
                continue
            log(f"pipe: challenge {challenge.hex(' ')}")
            reply = build_handshake_reply(challenge, guest_ip, netmask,
                                          media_port, device_id)
            _write(handle, reply)
            log(f"pipe: sent A+B (media_port={media_port} ip={guest_ip})")
            while True:
                chunk = _read(handle, 4096, timeout=1.0)
                if chunk:
                    printable = sum(1 for b in chunk if 32 <= b < 127 or b in (9, 10, 13))
                    if printable > len(chunk) * 0.5:
                        log(f"pipe RX| {chunk.decode('ascii', 'replace')!r}")
                    else:
                        log(f"pipe RX HEX| {chunk[:48].hex(' ')}")
        except Exception as exc:  # noqa: BLE001
            log(f"pipe: {type(exc).__name__}: {exc}")
        finally:
            server.close()


def control_worker(guest_ip: str) -> None:
    deadline = time.time() + 600
    sock = None
    while time.time() < deadline:
        try:
            sock = socket.create_connection((guest_ip, AGENTD_CONTROL_PORT), timeout=3)
            log(f"control: connected {guest_ip}:{AGENTD_CONTROL_PORT}")
            break
        except OSError:
            time.sleep(1.0)
    if sock is None:
        log("control: could not connect")
        return
    sock.settimeout(240)
    buf = b""
    try:
        while True:
            while len(buf) < 44:
                chunk = sock.recv(44 - len(buf))
                if not chunk:
                    log("control: peer closed")
                    return
                buf += chunk
            frame, buf = buf[:44], buf[44:]
            code = struct.unpack(">I", frame[0:4])[0]
            log(f"control: <- {CONTROL_CODES.get(code, code)}")
            if code == 0:
                sock.sendall(struct.pack(">I", 1) + frame[4:41] + b"\x00" * 3)
                log("control: -> 1 (go)")
    except OSError as exc:
        log(f"control: {exc}")


def media_worker(host_ip: str, media_port: int) -> None:
    server = MediaServer(host_ip, media_port)
    try:
        server.start()
    except Exception as exc:  # noqa: BLE001
        log(f"media: {exc}")
        return
    log(f"media: listening on {host_ip}:{media_port}")
    try:
        conn, peer = server.accept(timeout=300.0)
    except Exception as exc:  # noqa: BLE001
        log(f"media: {exc}")
        return
    log(f"media: guest connected from {peer}")
    buf = b""
    frames = 0
    conn.settimeout(2.0)
    while True:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while len(buf) >= 244:
            frame, buf = buf[:244], buf[244:]
            frames += 1
            payload = frame[:240]
            text = payload.decode("ascii", "replace")
            printable = sum(1 for c in text if 32 <= ord(c) < 127 or c in "\r\n\t")
            if printable > len(text) * 0.5:
                log(f"media[{frames}] TEXT| {text!r}")
            else:
                log(f"media[{frames}] HEX | {payload[:64].hex(' ')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipe", required=True,
                    help=r"named pipe VirtualBox should connect to, e.g. \\.\pipe\MYR2-COM2")
    ap.add_argument("--guest-ip", default="192.168.56.101")
    ap.add_argument("--netmask", default="255.255.255.0")
    ap.add_argument("--media-port", type=int, default=DEFAULT_MEDIA_PORT)
    ap.add_argument("--host-ip", default="192.168.56.1")
    ap.add_argument("--device-id", default=DEFAULT_DEVICE_ID.decode("latin-1"))
    args = ap.parse_args(argv)

    device_id = args.device_id.encode("latin-1")
    if len(device_id) != 37:
        print(f"error: device id must be 37 bytes, got {len(device_id)}", file=sys.stderr)
        return 2

    threads = [
        threading.Thread(target=serial_worker,
                         args=(args.pipe, args.guest_ip, args.netmask,
                               args.media_port, device_id), daemon=True),
        threading.Thread(target=media_worker,
                         args=(args.host_ip, args.media_port), daemon=True),
        threading.Thread(target=control_worker, args=(args.guest_ip,), daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
