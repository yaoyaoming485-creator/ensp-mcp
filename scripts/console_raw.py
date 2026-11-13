"""Talk to a guest's VRP console and print everything it sends back."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ensp_mcp import agentd  # noqa: E402

ip = sys.argv[1]
cmds = sys.argv[2:]

with agentd.VrpConsole(ip) as con:
    for cmd in cmds:
        con.send(cmd + "\r\n")
        time.sleep(1.2)
        out = con._drain(1.2)
        print(f"--- after {cmd!r} ---")
        print(out)
