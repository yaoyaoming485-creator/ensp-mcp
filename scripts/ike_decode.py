"""Decode the IKE (ISAKMP) messages inside a capture.

The capture holds the guests' frames inside the relay's UDP payloads; this
unwraps them and decodes the SA payload of every IKE message, so the two
ends' proposals can be compared directly.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ensp_mcp import capture  # noqa: E402

path = Path(sys.argv[1])

AUTH = {1: "HMAC-MD5", 2: "HMAC-SHA", 3: "DES", 4: "3DES", 5: "AES-CBC",
        6: "AES-CTR", 7: "AES-CBC-128", 8: "AES-CTR-128", 9: "AES-CBC-192",
        10: "AES-CBC-256"}
HASH = {1: "MD5", 2: "SHA1", 3: "Tiger", 4: "SHA2-256"}
DH = {1: "MODP-768", 2: "MODP-1024", 5: "MODP-1536"}
TRANSFORM = {1: "AES-CBC", 2: "AES-CTR", 3: "3DES-CBC", 4: "DES-CBC", 5: "AES-128"}


def ike_messages(payload: bytes):
    """Yield (exchange, sa_proposals) for every ISAKMP message in ``payload``."""
    pos = 0
    while pos + 28 <= len(payload):
        if payload[pos + 17] >> 4 != 0x1:    # not an ISAKMP v1 header
            break
        next_payload = payload[pos + 16]
        version = payload[pos + 17]
        exchange = payload[pos + 18]
        flags = payload[pos + 19]
        length = struct.unpack_from("!I", payload, pos + 24)[0]
        if length < 28 or pos + length > len(payload):
            break
        msg = payload[pos:pos + length]
        props = []
        if next_payload == 33:               # SA
            plen = msg[28]                   # length in 32-bit words, minus one
            props = parse_sa(msg[32:28 + (plen + 1) * 4])
        yield exchange, flags, props
        pos += length


def parse_sa(sa: bytes):
    """The proposals of an SA payload."""
    out = []
    i = 0
    while i + 8 <= len(sa):
        length, pnum, proto = struct.unpack_from("!HBB", sa, i)
        if length < 8:
            break
        spi_size = sa[i + 4]
        spi = sa[i + 8:i + 8 + spi_size]
        body = sa[i + 8 + spi_size:i + length]
        out.append({"num": pnum, "proto": proto, "spi": spi.hex(),
                    "transforms": parse_transforms(body)})
        i += length
    return out


def parse_transforms(body: bytes):
    """The transforms of a proposal."""
    out = []
    i = 0
    while i + 8 <= len(body):
        length, tnum, trid = struct.unpack_from("!HBB", body, i)
        if length < 8:
            break
        attrs = body[i + 8:i + length]
        auth = hash_ = dh = enc = None
        j = 0
        while j + 4 <= len(attrs):
            af, _v, alen = struct.unpack_from("!HHH", attrs, j)
            val = attrs[j + 4:j + 4 + alen]
            if af == 1:
                enc = val.decode(errors="replace")
            elif af == 2:
                hash_ = val.decode(errors="replace")
            elif af == 3:
                auth = val.decode(errors="replace")
            elif af == 4:
                dh = val.decode(errors="replace")
            j += 4 + alen
        out.append({"num": tnum, "id": trid,
                    "enc": enc, "hash": hash_, "auth": auth, "dh": dh})
        i += length
    return out


count = 0
for link_type, buf in capture.iter_packets(path.read_bytes()):
    ip = capture.ipv4_header(buf, link_type)
    if not ip or ip.get("proto") != 17:
        continue
    off = ip["header_offset"] + ip["ihl"]
    if off + 8 > len(buf):
        continue
    _sp, _dp, ulen = struct.unpack_from("!HHH", buf, off)
    payload = buf[off + 8: off + 8 + max(0, ulen - 8)]
    if len(payload) <= 4:
        continue
    inner = capture.ipv4_header(payload, 1)
    if not inner or inner.get("proto") != 17:
        continue
    ioff = inner["header_offset"] + inner["ihl"]
    if ioff + 8 > len(payload):
        continue
    sp, dp, ulen2 = struct.unpack_from("!HHH", payload, ioff)
    if sp != 500 and dp != 500:
        continue
    ikep = payload[ioff + 8: ioff + 8 + max(0, ulen2 - 8)]
    for exchange, flags, props in ike_messages(ikep):
        names = {2: "Identity", 4: "Certificate", 5: "CertificateReq",
                 33: "SA", 34: "Nonce", 35: "Notification", 36: "Delete",
                 37: "VendorID"}
        print(f"IKE {inner['src']}:{sp} -> {inner['dst']}:{dp}  "
              f"exchange={exchange} flags=0x{flags:02x} "
              f"payload={names.get(33,'?')}")
        for p in props:
            print(f"   proposal #{p['num']} proto={p['proto']} spi={p['spi']}")
            for t in p["transforms"]:
                print(f"      transform #{t['num']} id={t['id']} "
                      f"enc={t['enc']} hash={t['hash']} auth={t['auth']} dh={t['dh']}")
        count += 1
if not count:
    print("no IKE messages found")
