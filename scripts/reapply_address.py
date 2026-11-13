"""Re-apply an interface's address a few times.

A port that has no peer yet keeps its link trying to come up and flaps.  The
VRP sends its gratuitous ARP when the address is configured - so if the port
is up in that moment, the peer learns the address and the link settles.  The
port has to be up at the right moment, so the address is re-applied a few
times.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ensp_mcp import agentd  # noqa: E402

ip = sys.argv[1]
ifname = sys.argv[2]
addr = sys.argv[3]
mask = sys.argv[4]
rounds = int(sys.argv[5]) if len(sys.argv) > 5 else 6

last = ""
for i in range(rounds):
    try:
        with agentd.VrpConsole(ip, wake=False) as con:
            out = con.run([
                "system-view",
                f"interface {ifname}",
                "undo ip address",
                f"ip address {addr} {mask}",
                "quit",
                "return",
            ], wait=0.8, until_prompt=False, verify=False)
        last = out
    except Exception as exc:  # noqa: BLE001
        last = f"error: {exc}"
    time.sleep(1.5)

print(json.dumps({"ip": ip, "interface": ifname, "rounds": rounds,
                  "tail": last[-400:]}, ensure_ascii=False, indent=2))
