"""Capture the host's traffic while IPsec traffic flows, and list the flows.

The relay carries the guests' ethernet frames as UDP payloads, so a capture
on the host shows the frames the guests put on the wire - including the UDP
ports of the IKE exchange.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ensp_mcp import agentd, capture  # noqa: E402

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"D:\work\ensp-guest\ipsec-capture.pcapng")
trigger_ip = sys.argv[2] if len(sys.argv) > 2 else "192.168.56.101"
trigger_cmd = sys.argv[3] if len(sys.argv) > 3 else "ping -c 2 10.1.1.1"
seconds = float(sys.argv[4]) if len(sys.argv) > 4 else 15.0


def trigger() -> None:
    time.sleep(seconds * 0.4)
    try:
        with agentd.VrpConsole(trigger_ip) as con:
            con.run([trigger_cmd], wait=3.0, until_prompt=False, verify=False)
    except Exception as exc:  # noqa: BLE001
        print(f"trigger failed: {type(exc).__name__}: {exc}")


threading.Thread(target=trigger, daemon=True).start()

try:
    path, notes = capture.elevated_capture(seconds, out, filters=[])
    print("captured:", path)
    for note in notes:
        print("  ", note)
except Exception as exc:  # noqa: BLE001
    print(f"capture failed: {type(exc).__name__}: {exc}")
    raise SystemExit(1)

summary = capture.summarize(path)
print(capture.render_table(summary))
