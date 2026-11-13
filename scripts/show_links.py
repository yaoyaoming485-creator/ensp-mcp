"""Re-pair the links of a lab without touching the images.

The guest's own ``resource.cfg`` maps an *interface index* to the host port
the interface talks to; that mapping does not change when two links are
re-paired.  So re-pairing only means editing the lab's link table and
restarting the supervisor.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = root / "lab.json"
data = json.loads(manifest.read_text(encoding="utf-8"))

if "--write" in sys.argv:
    new_links = json.loads(sys.stdin.read().lstrip("﻿"))
    data["links"] = new_links
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print("links rewritten:")
    for link in data["links"]:
        print("  ", json.dumps(link, ensure_ascii=False))
else:
    for link in data.get("links", []):
        print(json.dumps(link, ensure_ascii=False))
