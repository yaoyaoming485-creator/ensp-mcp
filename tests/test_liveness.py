"""Liveness tests: the supervisor must never probe the guest's control port.

The guest's agentd calls ``accept()`` exactly once in its whole life -
``0x0804b309`` in ``startup`` is the only accept call site in the binary -
and then blocks in ``recv(44)`` on that one socket (``0x0804adf2``).  The
kernel's accept queue for port 56789 is 128 deep (``listen(fd, 128)`` at
``0x0804b157``) and nothing ever drains it.

Two things follow:

* a *gracefully* closed probe connection stays in that queue forever (the
  peer sent a FIN, so the kernel parks it in CLOSE_WAIT waiting for an
  application that will never accept it) - a probe per second therefore
  fills the queue after a bit more than two minutes, the guest stops
  completing handshakes, and the probes start failing for real;
* the liveness of a guest has to be read from the control connection the
  host already holds, not from a fresh connect.

Run with:  uv run python tests/test_liveness.py
"""
from __future__ import annotations

import inspect
import socket
import sys
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ensp_mcp import agentd, host, supervise  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class EchoResetServer:
    """A TCP server that records how the peer closed the connection."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.close_kind = "none"
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _peer = self.sock.accept()
        except OSError:
            return
        try:
            conn.recv(64)
        except ConnectionResetError:
            self.close_kind = "reset"
            return
        except OSError:
            self.close_kind = "reset"
            return
        # a clean FIN: recv returns b"" instead of raising
        self.close_kind = "fin"
        try:
            conn.close()
        except OSError:
            pass

    def stop(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


print("=" * 72)
print("1. a probe leaves no trace in the guest's accept queue")
print("=" * 72)

server = EchoResetServer()
try:
    answered = agentd.probe_endpoint(f"127.0.0.1:{server.port}", timeout=2.0)
    check("the probe sees the service", answered)
    server._thread.join(timeout=3.0)
    check("the peer got a reset, not a clean close",
          server.close_kind == "reset",
          f"close_kind={server.close_kind!r}")
finally:
    server.stop()

# The mechanism: SO_LINGER with a zero timeout makes close() send a RST.
probe = socket.socket()
probe.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
linger = probe.getsockopt(socket.SOL_SOCKET, socket.SO_LINGER, 8)
check("a zero linger turns close() into a reset",
      linger[0] == 1 and linger[1] == 0,
      f"l_onoff={linger[0]} l_linger={linger[1]}")
probe.close()

print()
print("=" * 72)
print("2. the supervisor reads liveness from the control connection")
print("=" * 72)

src = inspect.getsource(supervise)
check("the supervisor never probes the control port",
      "probe_endpoint" not in src,
      "supervise.py must not open a second connection to the guest")
check("the supervisor watches the control connection",
      "control_up" in src)

host_src = inspect.getsource(host)
check("the driver tracks the control connection",
      "control_up" in host_src)

# The event itself: it is set while the connection is open and cleared when
# the device is reset for another attempt.
opts = host.HostOptions(guest_ip="192.168.56.2", pipe_path=r"\\.\pipe\enspctl-test",
                         media_port=20002)
dev = host.HostDevice(opts=opts)
check("a fresh device has no control connection", not dev.control_up.is_set())
dev.control_up.set()
check("an open control connection is visible", dev.control_up.is_set())
dev.reset()
check("resetting the device clears it", not dev.control_up.is_set())

print()
print("=" * 72)
if failures:
    verdict = f"{len(failures)} FAILURE(S): " + ", ".join(failures)
else:
    verdict = "ALL PASS"
print(f"RESULT: {verdict}")
sys.exit(1 if failures else 0)
