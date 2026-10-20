"""Host-side driver for one eNSP guest device.

This is the part that replaces the eNSP client. It runs three things for a
single device:

  1. a **named-pipe server** for the device's COM2 UART, so VirtualBox
     connects to us;
  2. a **media server** on the host-only adapter, which the guest dials back
     to;
  3. a **control client** on guest TCP 56789, answering the guest's boot
     frame with code 1 so the VRP processes start.

The protocol (recovered from the guest's ``agentd`` binary) is documented in
``evidence/agentd-protocol.md``.

The device must be configured with a COM2 pipe in *client* mode, i.e. its
``.vbox`` has::

    <Port slot="1" ... server="false" path="\\\\.\\pipe\\NAME" hostMode="HostPipe"/>

because a pipe that VirtualBox creates itself only grants the host read
access.
"""
from __future__ import annotations

import ctypes
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field

from .agentd import (AGENTD_CONTROL_PORT, CONTROL_CODES, MediaServer,
                     SerialPipeServer, build_handshake_reply)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.ReadFile.restype = ctypes.c_int
k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                         ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
k32.WriteFile.restype = ctypes.c_int
k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                          ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]


@dataclass
class HostOptions:
    guest_ip: str
    pipe_path: str
    media_port: int
    host_ip: str = "192.168.56.1"
    netmask: str = "255.255.255.0"
    device_id: bytes = b"4CA76BCE-449E-4dc0-B3E3-E86C85B97001\x00"
    verbose: bool = True


@dataclass
class HostDevice:
    """Drives one guest. ``start()`` returns once the device reports DEV2."""

    opts: HostOptions
    log_prefix: str = ""
    _threads: list[threading.Thread] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    events: list[str] = field(default_factory=list)
    ready: threading.Event = field(default_factory=threading.Event)
    failed: str = ""

    # ------------------------------------------------------------------ --
    def log(self, msg: str) -> None:
        if self.opts.verbose:
            print(f"[{time.strftime('%H:%M:%S')}] {self.log_prefix}{msg}", flush=True)

    # ------------------------------------------------------------------ --
    def _read(self, handle, want: int, timeout: float) -> bytes:
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

    def _write(self, handle, data: bytes) -> None:
        buf = ctypes.create_string_buffer(data, len(data))
        cnt = ctypes.c_uint32(0)
        k32.WriteFile(handle, buf, len(data), ctypes.byref(cnt), None)

    # ------------------------------------------------------------------ --
    def _serial_worker(self) -> None:
        server = SerialPipeServer(self.opts.pipe_path)
        while not self._stop.is_set():
            try:
                handle = server.wait_client(timeout=300.0)
            except Exception as exc:  # noqa: BLE001
                self.log(f"pipe: {exc}")
                return
            self.log("pipe: VirtualBox connected")
            try:
                challenge = self._read(handle, 4, timeout=180.0)
                if len(challenge) < 4:
                    self.log(f"pipe: short challenge {challenge!r}")
                    continue
                self.log(f"pipe: challenge {challenge.hex(' ')}")
                reply = build_handshake_reply(
                    challenge, self.opts.guest_ip, self.opts.netmask,
                    self.opts.media_port, self.opts.device_id)
                self._write(handle, reply)
                self.log(f"pipe: sent A+B (media_port={self.opts.media_port} "
                         f"ip={self.opts.guest_ip})")
                while not self._stop.is_set():
                    chunk = self._read(handle, 4096, timeout=1.0)
                    if chunk:
                        printable = sum(1 for b in chunk if 32 <= b < 127 or b in (9, 10, 13))
                        if printable > len(chunk) * 0.5:
                            self.log(f"pipe RX| {chunk.decode('ascii', 'replace')!r}")
                        else:
                            self.log(f"pipe RX HEX| {chunk[:48].hex(' ')}")
            except Exception as exc:  # noqa: BLE001
                self.log(f"pipe: {type(exc).__name__}: {exc}")
            finally:
                server.close()

    def _media_worker(self) -> None:
        server = MediaServer(self.opts.host_ip, self.opts.media_port)
        try:
            server.start()
        except Exception as exc:  # noqa: BLE001
            self.log(f"media: {exc}")
            return
        self.log(f"media: listening on {self.opts.host_ip}:{self.opts.media_port}")
        try:
            conn, peer = server.accept(timeout=300.0)
        except Exception as exc:  # noqa: BLE001
            self.log(f"media: {exc}")
            return
        self.log(f"media: guest connected from {peer}")
        conn.settimeout(2.0)
        buf = b""
        frames = 0
        while not self._stop.is_set():
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
                    self.log(f"media[{frames}] TEXT| {text!r}")
                else:
                    self.log(f"media[{frames}] HEX | {payload[:64].hex(' ')}")

    def _control_worker(self) -> None:
        deadline = time.time() + 600
        sock = None
        while time.time() < deadline and not self._stop.is_set():
            try:
                sock = socket.create_connection(
                    (self.opts.guest_ip, AGENTD_CONTROL_PORT), timeout=3)
                self.log(f"control: connected {self.opts.guest_ip}:{AGENTD_CONTROL_PORT}")
                break
            except OSError:
                time.sleep(1.0)
        if sock is None:
            self.failed = "the guest never answered on the agentd control port"
            self.ready.set()
            return
        sock.settimeout(240)
        buf = b""
        try:
            while not self._stop.is_set():
                while len(buf) < 44:
                    chunk = sock.recv(44 - len(buf))
                    if not chunk:
                        self.log("control: peer closed")
                        self.ready.set()
                        return
                    buf += chunk
                frame, buf = buf[:44], buf[44:]
                code = struct.unpack(">I", frame[0:4])[0]
                name = CONTROL_CODES.get(code, f"code {code}")
                self.events.append(name)
                self.log(f"control: <- {name}")
                if code == 0:
                    sock.sendall(struct.pack(">I", 1) + frame[4:41] + b"\x00" * 3)
                    self.log("control: -> 1 (go)")
                if code in (8, 9):
                    self.ready.set()
        except OSError as exc:
            self.failed = str(exc)
            self.ready.set()

    # ------------------------------------------------------------------ --
    def start(self, wait_ready: bool = True, ready_timeout: float = 240.0) -> "HostDevice":
        for fn in (self._serial_worker, self._media_worker, self._control_worker):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        if wait_ready and not self.ready.wait(ready_timeout):
            self.failed = self.failed or "timed out waiting for the device to start"
        return self

    def stop(self) -> None:
        self._stop.set()

    @property
    def started(self) -> bool:
        return "DEV2 started" in self.events


def drive_many(options: list[HostOptions], prefixes: list[str] | None = None,
               wait_ready: bool = True) -> list[HostDevice]:
    """Drive several devices at once."""
    devices: list[HostDevice] = []
    for i, opt in enumerate(options):
        prefix = (prefixes[i] if prefixes and i < len(prefixes) else "")
        devices.append(HostDevice(opts=opt, log_prefix=prefix).start(wait_ready=False))
    if wait_ready:
        for dev in devices:
            dev.ready.wait(240)
    return devices
