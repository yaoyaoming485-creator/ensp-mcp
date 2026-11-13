"""Unwrap the relay's UDP payloads and look at the guests' frames inside.

The host's capture shows the relay's transport: every guest frame travels
inside a UDP datagram to the relay's port for that interface.  This unwraps
those payloads and reports the inner IP/UDP headers, so the IKE exchange can
be read directly.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ensp_mcp import capture  # noqa: E402

path = Path(sys.argv[1])
path = Path(sys.argv[1])
only = {int(p) for p in sys.argv[2:]} or None

seen: dict[tuple, int] = {}
for link_type, buf in capture.iter_packets(path.read_bytes()):
    ip = capture.ipv4_header(buf, link_type)
    if not ip or ip.get("proto") != 17:
        continue
    off = ip["header_offset"] + ip["ihl"]
    if off + 8 > len(buf):
        continue
    sport, dport, ulen = struct.unpack_from("!HHH", buf, off)
    payload = buf[off + 8: off + 8 + max(0, ulen - 8)]
    if len(payload) <= 4:
        continue                      # the four-byte VPORT handshake
    inner = capture.ipv4_header(payload, 1)     # LINKTYPE_ETHERNET
    if not inner:
        continue
    ioff = inner["header_offset"] + inner["ihl"]
    ports = ""
    if inner.get("proto") == 17 and ioff + 8 <= len(payload):
        sp, dp, _ = struct.unpack_from("!HHH", payload, ioff)
        ports = f"{sp}->{dp}"
        if only and (sp not in only and dp not in only):
            continue
    elif only:
        continue
    key = (inner["src"], inner["dst"], inner.get("proto"), ports)
    seen[key] = seen.get(key, 0) + 1

print(f"{'inner src':<16} {'inner dst':<16} {'proto':<5} {'count':>6}  ports")
for (src, dst, proto, ports), count in sorted(seen.items(), key=lambda kv: -kv[1]):
    print(f"{src:<16} {dst:<16} {str(proto):<5} {count:>6}  {ports}")
