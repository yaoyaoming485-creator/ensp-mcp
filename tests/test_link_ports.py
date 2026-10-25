"""The link mapping: panel port vs. guest port.

A topology names the *panel* ports of a device ("GE0/0/0").  The device
image has its own ports - the VPORT indexes of the guest's ``resource.cfg``
- and the eNSP client makes the two the same number by writing the guest's
port list from the model, in panel order.  When the guest's board does not
match the model (a stock AR image has nine ports, the AR1220 has ten
panel ports), the link has to say which guest port a panel port is really
wired to.  ``guestSrc``/``guestTar`` in the ``.topo`` carry that.

Run with:  uv run python tests/test_link_ports.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ensp_mcp import topo  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        failures.append(label)


SPEC = {
    "name": "t",
    "devices": [
        {"id": "11111111-1111-1111-1111-111111111111", "name": "R1",
         "model": "AR1220"},
        {"id": "22222222-2222-2222-2222-222222222222", "name": "R2",
         "model": "AR1220"},
    ],
    "links": [
        {"a": "R1", "b": "R2", "a_port": 0, "b_port": 0, "a_if": "GE0/0/0",
         "b_if": "GE0/0/0", "type": "Copper"},
    ],
}

print("=" * 72)
print("1. a plain topology round-trips")
print("=" * 72)

path = Path(__file__).with_name("tmp-link-ports.topo")
topo.write(SPEC, path)
read_back = topo.read(path)
link = read_back["links"][0]
check("the panel port survives", link["a_port"] == 0 and link["b_port"] == 0,
      f"a_port={link['a_port']} b_port={link['b_port']}")
check("no guest port is invented", link["a_guest_port"] is None,
      f"a_guest_port={link['a_guest_port']!r}")
text = path.read_text(encoding="utf-8")
check("the .topo stays loadable by eNSP (no extra attributes)",
      "guestSrc" not in text and "guestTar" not in text)

print()
print("=" * 72)
print("2. a guest port override is carried through")
print("=" * 72)

SPEC2 = {
    **SPEC,
    "links": [{**SPEC["links"][0], "a_guest_port": 8, "b_guest_port": 8}],
}
path2 = Path(__file__).with_name("tmp-link-ports2.topo")
topo.write(SPEC2, path2)
back2 = topo.read(path2)
check("the guest port survives", back2["links"][0]["a_guest_port"] == 8,
      f"a_guest_port={back2['links'][0]['a_guest_port']!r}")
check("the peer's guest port survives", back2["links"][0]["b_guest_port"] == 8)
check("the .topo says so", 'guestSrc="8"' in path2.read_text(encoding="utf-8"))
check("the panel port is still there", back2["links"][0]["a_port"] == 0)

for p in (path, path2):
    try:
        p.unlink()
    except OSError:
        pass

print()
print("=" * 72)
if failures:
    verdict = f"{len(failures)} FAILURE(S): " + ", ".join(failures)
else:
    verdict = "ALL PASS"
print(f"RESULT: {verdict}")
sys.exit(1 if failures else 0)
