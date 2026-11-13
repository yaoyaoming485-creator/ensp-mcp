"""Summarise the relay's traffic census for the interesting interfaces."""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

log = Path(sys.argv[1])
wanted = sys.argv[2:] or ["site1", "internet", "site2"]
pat = re.compile(r"\] (?P<dev>\S+): udp relay: iface (?P<iface>\d+): (?P<rest>.*)")
kind = re.compile(r"(?P<n>\d+) x (?P<label>.+)")

data: dict[tuple[str, str], dict] = defaultdict(dict)
for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
    m = pat.search(line)
    if not m:
        continue
    dev, iface, rest = m.group("dev"), m.group("iface"), m.group("rest")
    if dev not in wanted:
        continue
    k = kind.match(rest.strip())
    if k:
        data[(dev, iface)][k.group("label").strip()] = int(k.group("n"))

for (dev, iface), labels in sorted(data.items()):
    if not labels:
        continue
    total = sum(labels.values())
    print(f"{dev:9} iface {iface:>2}: {total:>5} frame(s)  "
          + ", ".join(f"{n}x {lab}" for lab, n in sorted(labels.items(), key=lambda kv: -kv[1])))
