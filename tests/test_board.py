"""The guest's board description, and the file the guest reads it from.

The guest's VRP reads ``/dev/upload/resource.cfg`` at start-up: the
``device_type = <n>`` line picks the board, the ``interface[i] = ...`` lines
describe the ports, and ``slotinfo[i] = <n>`` describes the slots.  eNSP's
own client writes that file from the model, so a guest whose file was not
rewritten runs the board the image shipped with - the AR201's nine ports,
where only the ninth is routable.

These tests check that the file we write is one the guest can read.

Run with:  uv run python tests/test_board.py
"""
from __future__ import annotations

import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ensp_mcp import board  # noqa: E402
from ensp_mcp import rescfg  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        failures.append(label)


#: the file the image ships with (the AR201's board)
STOCK = """#####################################################
##  Read Me AR
#####################################################

{Begin}

{Total Resource}
total_core_num = 4
cap_total_size = 0x10000000
lctl_total_size = 0x20000000

device_type = 33

interface[0] = socket[local:192.168.1.3:10000 dest:192.168.1.104:30000 role:client protocol:udp][port_phy card_id:0 panel_port:0 port_name:Ethernet cpu_port:0 encap_flag:0 engine_type:3 port_type:0 mod_id:0 port_id:0 phy_id:1]
interface[1] = socket[local:192.168.1.3:10001 dest:192.168.1.104:30001 role:client protocol:udp][port_phy card_id:0 panel_port:0 port_name:Ethernet cpu_port:1 encap_flag:0 engine_type:3 port_type:0 mod_id:0 port_id:1 phy_id:1]

{CAP Resource}
cap_core_mask = 0xE
cap_mem_size = 0x10000000
"""

print("=" * 72)
print("1. the board table")
print("=" * 72)

ar201 = board.board_for("AR201")
ar1220 = board.board_for("AR1220")
check("the AR201's board is known", ar201 is not None)
check("the AR201 has nine ports", ar201 is not None and ar201.port_count == 9,
      f"ports={ar201.port_count if ar201 else 0}")
check("the AR201's device type is 33", ar201 is not None and ar201.device_type == 33)
check("the AR1220's board is known", ar1220 is not None)
check("the AR1220 has ten ports (GE x2 + FE x8)",
      ar1220 is not None and ar1220.port_count == 10,
      f"ports={ar1220.port_count if ar1220 else 0}")
check("an unknown model has no board", board.board_for("NE40E") is None)

print()
print("=" * 72)
print("2. the interface lines the guest can read")
print("=" * 72)

lines = board.interface_lines(ar1220, "192.168.56.101", "192.168.56.1",
                               {i: 30000 + i for i in range(10)})
check("one line per port", len(lines) == 10, f"lines={len(lines)}")
check("the ports are numbered in panel order",
      all(re.search(rf"interface\[{i}\] ", ln) for i, ln in enumerate(lines)))
check("cpu_port follows the port number",
      all(f"cpu_port:{i} " in ln for i, ln in enumerate(lines)))
check("the port name of a group is repeated",
      sum("port_name:GE " in ln for ln in lines) == 2
      and sum("port_name:Ethernet " in ln for ln in lines) == 8)
check("port_id counts inside the group",
      "port_id:0 " in lines[0] and "port_id:1 " in lines[1]
      and "port_id:0 " in lines[2])
check("the local port follows the port number",
      "local:192.168.56.101:10000 " in lines[0]
      and "local:192.168.56.101:10009 " in lines[9])
check("the dest port is the host's port for that interface",
      "dest:192.168.56.1:30000 " in lines[0]
      and "dest:192.168.56.1:30009 " in lines[9])

# the guest parses these lines with sscanf, so the field order has to be the
# one the eNSP client writes
fmt = ("interface[%d] = socket[local:%s:%d dest:%s:%d role:client protocol:udp]"
       "[port_phy card_id:0 panel_port:0 port_name:%s cpu_port:%d encap_flag:%d"
       " engine_type:%d port_type:%d mod_id:%d port_id:%d phy_id:1]")
check("the line layout is the one the client writes",
      fmt % (1, "192.168.56.101", 10001, "192.168.56.1", 30001, "GE", 1,
             5, 1, 0, 1, 1) == lines[1],
      lines[1])

print()
print("=" * 72)
print("3. the file the guest reads")
print("=" * 72)

# the patch script has to produce a file the guest can parse
patched = STOCK
for ln in lines:
    pass
# emulate the patch: replace the interface block and the device type
out_lines = []
first = None
for line in patched.splitlines():
    if re.match(r"\s*interface\[\d+\]\s*=", line):
        if first is None:
            first = len(out_lines)
        continue
    out_lines.append(line)
out_lines[first:first] = lines
text = "\n".join(out_lines) + "\n"
text = re.sub(r"(?m)^(\s*device_type\s*=\s*)\d+", r"\g<1>33", text)

check("the file has one interface line per port",
      len(re.findall(r"(?m)^interface\[\d+\] = ", text)) == 10,
      f"lines={len(re.findall(r'interface[' + chr(92) + 'd+' + chr(93), text))}")
check("the device type is still there", "device_type = 33" in text)
check("the other sections survived", "{CAP Resource}" in text and "{Begin}" in text)
check("the guest's parser can read the port line",
      re.search(r"port_phy card_id:0 panel_port:0 port_name:GE cpu_port:\d+ "
                r"encap_flag:5 engine_type:1 port_type:0 mod_id:1 port_id:\d+ "
                r"phy_id:1", text) is not None)

print()
print("=" * 72)
print("4. the board travels with the patch marker")
print("=" * 72)

from ensp_mcp import lab as labmod  # noqa: E402

state = labmod._board_state(ar1220)
check("the marker remembers the board", state is not None and state["device_type"] == 33)
check("the marker remembers the groups",
      state is not None and len(state["groups"]) == 2
      and state["groups"][0][0] == "GE")
check("no board means no board in the marker",
      labmod._board_state(None) is None)

print()
print("=" * 72)
print("5. the patch script that really runs")
print("=" * 72)

import json  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

# the python that the WSL patch script runs - it is the code that touches the
# guest's file, so it is the code that has to be right
script = rescfg._LOOP_SCRIPT
start = script.index("<<'PYEOF'") + len("<<'PYEOF'")
end = script.index("\nPYEOF", start)
body = script[start:end]

with tempfile.TemporaryDirectory() as tmp:
    cfg = Path(tmp) / "resource.cfg"
    cfg.write_text(STOCK, encoding="utf-8", newline="")
    ports = {str(i): 30000 + i for i in range(10)}
    proc = subprocess.run(
        [sys.executable, "-c", body, str(cfg), "192.168.56.101", "192.168.56.1",
         json.dumps(ports), rescfg.board_json(ar1220)],
        capture_output=True, text=True, timeout=60)
    check("the patch script runs", proc.returncode == 0,
          (proc.stderr or proc.stdout).strip()[:160])
    text = cfg.read_text(encoding="utf-8")

check("the patched file has ten interface lines",
      len(re.findall(r"(?m)^interface\[\d+\] = ", text)) == 10,
      "lines=%d" % len(re.findall(r"^interface\[\d+\] = ", text, re.M)))
check("the patched file names the ports of the board",
      text.count("port_name:GE ") == 2 and text.count("port_name:Ethernet ") == 8)
check("the patched file has the device type", "device_type = 33" in text)
check("the patched file still has the other sections",
      "{CAP Resource}" in text and "{Total Resource}" in text)
check("the guest's own parser would accept every line",
      all(re.search(
          r"interface\[\d+\] = socket\[local:\d+\.\d+\.\d+\.\d+:\d+ "
          r"dest:\d+\.\d+\.\d+\.\d+:\d+ role:client protocol:udp\]\[port_phy "
          r"card_id:0 panel_port:0 port_name:\w+ cpu_port:\d+ encap_flag:\d+ "
          r"engine_type:\d+ port_type:\d+ mod_id:\d+ port_id:\d+ phy_id:\d+\]",
          ln) for ln in text.splitlines() if ln.startswith("interface[")),
      "one line does not match the guest's sscanf format")

print()
print("=" * 72)
if failures:
    verdict = f"{len(failures)} FAILURE(S): " + ", ".join(failures)
else:
    verdict = "ALL PASS"
print(f"RESULT: {verdict}")
sys.exit(1 if failures else 0)
