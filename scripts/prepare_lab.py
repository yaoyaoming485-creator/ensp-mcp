"""Shut down the ports that carry no link, one session per command, with retries.

An unlinked port keeps its link trying to come up and announces it on the
console for ever; that noise lands in the middle of the command being typed
and the router then reads half of it.  Sending every command in its own
session and checking the result makes the shutdown stick.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ensp_mcp import agentd  # noqa: E402
from ensp_mcp import lab as labmod  # noqa: E402

root = Path(sys.argv[1])
attempts = int(sys.argv[2]) if len(sys.argv) > 2 else 4
lab = labmod.Lab.load(root)
rows = []

for dev in lab.devices:
    unused = lab.unused_port_names(dev)
    shut, failed = [], []
    for name in unused:
        ok = False
        for attempt in range(attempts):
            try:
                with agentd.VrpConsole(dev.ip, wake=False) as con:
                    con.run([f"interface {name}", "shutdown", "quit"],
                            wait=1.0, until_prompt=False, verify=False)
                    time.sleep(1.0)
                    out = con.run([f"display interface {name}"],
                                  wait=1.5, until_prompt=False, verify=False)
            except Exception as exc:  # noqa: BLE001
                out = f"error: {exc}"
            if "Administratively DOWN" in out or "administratively down" in out:
                ok = True
                break
        (shut if ok else failed).append(name)
    rows.append({"device": dev.name, "ip": dev.ip,
                 "shut": shut, "still_up": failed})

print(json.dumps(rows, ensure_ascii=False, indent=2))
