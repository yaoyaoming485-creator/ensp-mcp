"""Text-only console tests.

Two things are checked here:

1. The VRP console is driven as **plain text** and the client can tell a
   command that really ran from one that was swallowed by the boot banner.
   The "press any key" race is reproduced with a fake device.
2. Reading device state never goes through a screenshot.  The screen code
   (VirtualBox PNG of the guest's Linux console) is diagnostic only, it is
   never a fallback for the text console.

Run with:  uv run python tests/test_console_text.py
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ensp_mcp import agentd, console, vbox  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeVrpDevice:
    """A VRP console that behaves like the real one, badly and well.

    ``swallow_first`` reproduces the boot banner of a freshly started device:
    the first line it receives is consumed as the "press any key" wake-up
    and never reaches the CLI.  ``swallow_next`` models a lost wake-up key -
    the first *command* is eaten and only the one after it runs.
    ``echo=False`` models a console that accepts input but never executes
    anything.  ``wrap`` echoes the command over two lines, the way VRP does
    when the command is longer than the screen.
    """

    def __init__(self, swallow_first: bool = False, echo: bool = True,
                 swallow_next: bool = False, wrap: bool = False):
        self.swallow_first = swallow_first
        self.echo = echo
        self.swallow_next = swallow_next
        self.wrap = wrap
        self.executed: list[str] = []
        self.received: list[str] = []
        self.pending = "Press any key to get started\r\n" if swallow_first else "<R1>\r\n"
        self.at_banner = swallow_first

    # --- socket-ish surface used by VrpConsole ---------------------------
    def settimeout(self, _t) -> None:
        pass

    def close(self) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        line = data.decode("utf-8", "replace")
        self.received.append(line)
        if self.at_banner:
            # any line dismisses the banner, but it is consumed, not executed
            self.at_banner = False
            self.pending += "<R1>\r\n"
            return
        cmd = line.strip()
        if not cmd:
            return
        if self.swallow_next:
            # the wake-up key was lost, so this command is the one that gets
            # eaten by the banner
            self.swallow_next = False
            self.pending += "<R1>\r\n"
            return
        if self.echo:
            self.executed.append(cmd)
            if self.wrap and len(cmd) > 4:
                self.pending += f"{cmd[:4]}\r\n{cmd[4:]}\r\n"
            else:
                self.pending += f"{cmd}\r\n"
            self.pending += "Huawei Versatile Routing Platform Software\r\n<R1>\r\n"
        else:
            self.pending += "<R1>\r\n"

    def recv(self, _n: int) -> bytes:
        if not self.pending:
            return b""
        out, self.pending = self.pending, ""
        return out.encode("utf-8")


def make_console(device: FakeVrpDevice) -> agentd.VrpConsole:
    con = agentd.VrpConsole("192.168.56.101")
    con.sock = device  # type: ignore[assignment]
    con.min_prompt_wait = 0.3
    return con


print("=" * 72)
print("1. the boot banner must not swallow a command")
print("=" * 72)

device = FakeVrpDevice(swallow_first=True)
con = make_console(device)
out = con.run(["display version"])
check("the command was executed", device.executed == ["display version"],
      f"executed={device.executed}")
check("the output contains the VRP banner text",
      "Huawei Versatile Routing Platform Software" in out, out.replace("\r", " ")[:120])
check("the output does not leak the boot banner as the result",
      "Press any key to get started" not in out.split("=== display version", 1)[-1])

print()
print("=" * 72)
print("2. a command that does not run must fail loudly")
print("=" * 72)

device = FakeVrpDevice(swallow_first=False, echo=False)
con = make_console(device)
try:
    con.run(["display version"])
    check("a swallowed command raises", False, "no exception was raised")
except agentd.VrpConsoleError as exc:
    check("a swallowed command raises", True, str(exc)[:110])
    check("the error names the command", "display version" in str(exc))

print()
print("=" * 72)
print("2b. a lost wake-up key must not run the command twice")
print("=" * 72)

device = FakeVrpDevice(swallow_next=True)
con = make_console(device)
out = con.run(["display version"])
check("the command ran exactly once", device.executed == ["display version"],
      f"executed={device.executed}")
check("the output holds the real result",
      "Huawei Versatile Routing Platform Software" in out)

print()
print("=" * 72)
print("2c. a wrapped echo is not a failure")
print("=" * 72)

device = FakeVrpDevice(wrap=True)
con = make_console(device)
out = con.run(["display version"])
check("the command ran exactly once", device.executed == ["display version"],
      f"executed={device.executed}")
check("the wrapped echo was accepted",
      "Huawei Versatile Routing Platform Software" in out)

print()
print("=" * 72)
print("3. state is read as text, never from a screenshot")
print("=" * 72)

check("the screen module has no screenshot-to-text bridge",
      not hasattr(console, "read_screen_text"))

# Any attempt to take a screenshot while driving the text console must blow up:
# the text path must never fall back to pixels.
original_take = vbox.take_screenshot


def explode(*_a, **_kw):
    raise AssertionError("the text console tried to take a screenshot")


vbox.take_screenshot = explode  # type: ignore[assignment]
try:
    device = FakeVrpDevice(swallow_first=True)
    con = make_console(device)
    out = con.run(["display version"])
    check("the text console runs without touching the screen code",
          "Huawei Versatile Routing Platform Software" in out)
except AssertionError as exc:
    check("the text console runs without touching the screen code", False, str(exc))
finally:
    vbox.take_screenshot = original_take  # type: ignore[assignment]

# The agentd module (the protocol core) must not import the screen code at all.
import inspect  # noqa: E402

agentd_src = inspect.getsource(agentd)
check("the protocol core does not use the screen code",
      "take_screenshot" not in agentd_src and "console.capture" not in agentd_src)

print()
print("=" * 72)
if failures:
    verdict = f"{len(failures)} FAILURE(S): " + ", ".join(failures)
else:
    verdict = "ALL PASS"
print(f"RESULT: {verdict}")
print("=" * 72)
sys.exit(1 if failures else 0)
