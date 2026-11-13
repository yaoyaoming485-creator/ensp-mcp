"""Turn on IKE debugging on one device and watch it while traffic flows."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ensp_mcp import agentd  # noqa: E402

watch_ip = sys.argv[1]
trigger_ip = sys.argv[2]
trigger_cmd = sys.argv[3]
seconds = float(sys.argv[4]) if len(sys.argv) > 4 else 25.0


def trigger() -> None:
    time.sleep(5)
    try:
        with agentd.VrpConsole(trigger_ip) as con:
            con.run([trigger_cmd], wait=3.0, until_prompt=False, verify=False)
    except Exception as exc:  # noqa: BLE001
        print(f"trigger failed: {type(exc).__name__}: {exc}")


threading.Thread(target=trigger, daemon=True).start()

with agentd.VrpConsole(watch_ip) as con:
    for cmd in ("system-view", "debug ike all", "return", "terminal debugging"):
        con.send(cmd + "\r\n")
        time.sleep(1.0)
        con._drain(0.8)
    end = time.time() + seconds
    while time.time() < end:
        out = con._drain(1.0)
        if out.strip():
            print(out)
