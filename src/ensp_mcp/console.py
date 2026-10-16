"""Console session helpers on top of the VirtualBox screen primitives.

The device console is a VGA text screen, so there is no byte stream to read:
output comes back as an image of the screen and input goes in as keystrokes.
This module wraps both so callers can think in terms of "send this command" and
"show me the screen".
"""

from __future__ import annotations

import time
from pathlib import Path

from . import vbox

# Set-1 make/break scancodes. Extended keys carry a 0xE0 prefix.
_KEYS: dict[str, list[str]] = {
    "enter": ["1c", "9c"],
    "return": ["1c", "9c"],
    "esc": ["01", "81"],
    "escape": ["01", "81"],
    "tab": ["0f", "8f"],
    "backspace": ["0e", "8e"],
    "space": ["39", "b9"],
    "delete": ["e0", "53", "e0", "d3"],
    "insert": ["e0", "52", "e0", "d2"],
    "up": ["e0", "48", "e0", "c8"],
    "down": ["e0", "50", "e0", "d0"],
    "left": ["e0", "4b", "e0", "cb"],
    "right": ["e0", "4d", "e0", "cd"],
    "home": ["e0", "47", "e0", "c7"],
    "end": ["e0", "4f", "e0", "cf"],
    "pageup": ["e0", "49", "e0", "c9"],
    "pagedown": ["e0", "51", "e0", "d1"],
    "ctrl-c": ["1d", "2e", "ae", "9d"],
    "ctrl-z": ["1d", "2c", "ac", "9d"],
    "ctrl-a": ["1d", "1e", "9e", "9d"],
    "ctrl-d": ["1d", "20", "a0", "9d"],
    "ctrl-p": ["1d", "19", "99", "9d"],
    "ctrl-u": ["1d", "16", "96", "9d"],
    "ctrl-w": ["1d", "11", "91", "9d"],
    "ctrl-y": ["1d", "15", "95", "9d"],
    "shift-tab": ["0f", "8f"],
    "y": ["15", "95"],
    "n": ["31", "b1"],
    "q": ["10", "90"],
    "F1": ["3b", "bb"],
    "F2": ["3c", "bc"],
    "F3": ["3d", "bd"],
    "F4": ["3e", "be"],
    "F5": ["3f", "bf"],
    "F6": ["40", "c0"],
    "F7": ["41", "c1"],
    "F8": ["42", "c2"],
    "F9": ["43", "c3"],
    "F10": ["44", "c4"],
}

KEY_NAMES = tuple(sorted(_KEYS))


class ConsoleError(RuntimeError):
    pass


def send_key(vm_name: str, key: str, times: int = 1, gap: float = 0.08) -> None:
    """Send a named key (see :data:`KEY_NAMES`)."""
    codes = _KEYS.get(key) or _KEYS.get(key.lower())
    if codes is None:
        raise ConsoleError(f"unknown key {key!r}; known keys: {', '.join(KEY_NAMES)}")
    for _ in range(max(1, times)):
        vbox.keyboard_put_scancode(vm_name, *codes)
        time.sleep(gap)


def send_text(vm_name: str, text: str, enter: bool = True, per_char: float = 0.0) -> None:
    """Type text, optionally pressing Enter afterwards."""
    if per_char > 0:
        for ch in text:
            vbox.keyboard_put_string(vm_name, ch)
            time.sleep(per_char)
    else:
        vbox.keyboard_put_string(vm_name, text)
    if enter:
        time.sleep(0.12)
        send_key(vm_name, "enter")


def send_lines(vm_name: str, lines: list[str], settle: float = 0.45) -> int:
    """Type a block of commands, one per line, waiting for each to be accepted.

    Empty strings and ``#``-prefixed comments are skipped but still cost one
    settle delay, which is handy for pacing long VRP configuration blocks.
    """
    sent = 0
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line.strip() and not line.lstrip().startswith("#"):
            typed = line
            # VRP's console echoes typed characters; very long lines can outrun
            # the emulated UART, so chunk them.
            if len(typed) > 200:
                for i in range(0, len(typed), 200):
                    vbox.keyboard_put_string(vm_name, typed[i : i + 200])
                    time.sleep(0.2)
            else:
                vbox.keyboard_put_string(vm_name, typed)
            time.sleep(0.12)
            send_key(vm_name, "enter")
            sent += 1
        time.sleep(settle)
    return sent


def capture(vm_name: str, out_path: str | Path | None = None) -> Path:
    """Capture the device screen, defaulting to a temp file."""
    if out_path is None:
        import tempfile

        out_path = Path(tempfile.gettempdir()) / f"ensp-console-{vm_name}.png"
    return vbox.take_screenshot(vm_name, out_path)


def read_screen_text(vm_name: str) -> dict:
    """Best-effort *text* view of the screen.

    There is no text channel: the screen is a bitmap of an 80x25 VGA text mode.
    Recovering characters needs OCR, which this server does not do.  Callers that
    need text should read the image content returned by the console tool.
    """
    path = capture(vm_name)
    return {
        "vm": vm_name,
        "image_path": str(path),
        "text": None,
        "note": (
            "The eNSP console is a VGA text screen (720x400, 80x25 cells), not a byte "
            "stream, so it is returned as an image. Read the attached image."
        ),
    }
