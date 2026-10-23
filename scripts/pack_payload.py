"""The guest's payload format: a 4096-byte XOR key, and nothing else.

The eNSP guest keeps the device software in ``/dev/upload/dev_base_0``,
``dev_base_1`` and ``dev_base_2``.  Those files are **not** compressed and
**not** packed in any complicated way: the guest's ``agentd`` decrypts them
with a plain XOR against a 4096-byte key that lives in agentd's ``.data``
section (``decompress()`` in agentd is a misnomer - it XORs every 4096-byte
block with the key, restarting the key at the beginning of each block, and
the tail of the file - ``size % 4096`` bytes - byte by byte with the same
key).

XOR is its own inverse, so the same key both encrypts and decrypts.

    python scripts/pack_payload.py key agentd                 # print the key
    python scripts/pack_payload.py decrypt dev_base_2 dev2     # plain ELF
    python scripts/pack_payload.py encrypt dev2 dev_base_2     # back again

The key is derived from the guest's own agentd, so nothing vendor-specific
is copied into this repository: only the SHA-256 of agentd is recorded, in
evidence/agentd-protocol.md.
"""
from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from pathlib import Path

KEY_LENGTH = 4096
#: virtual address of the key inside agentd (.data section)
KEY_VADDR = 0x08050220


def _sections(data: bytes) -> list[tuple[str, int, int, int, int]]:
    """(name, vaddr, vsize, rawptr, rawsize) of every section of an ELF."""
    e_shoff = struct.unpack_from("<I", data, 0x20)[0]
    e_shentsize = struct.unpack_from("<H", data, 0x2E)[0]
    e_shnum = struct.unpack_from("<H", data, 0x30)[0]
    out = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        name = data[off:off + 16].rstrip(b"\0").decode("latin-1")
        sh_type, _flags, sh_addr, sh_offset, sh_size = struct.unpack_from(
            "<IIIII", data, off + 4)
        out.append((name, sh_addr, sh_size, sh_offset, sh_size))
    return out


def key_from_agentd(path: Path, vaddr: int = KEY_VADDR) -> bytes:
    """The 4096-byte XOR key, taken from the guest's agentd."""
    data = path.read_bytes()
    for name, sh_addr, sh_size, sh_offset, _raw in _sections(data):
        if sh_addr <= vaddr < sh_addr + sh_size:
            off = sh_offset + (vaddr - sh_addr)
            return data[off:off + KEY_LENGTH]
    raise SystemExit(f"0x{vaddr:x} is not inside any section of {path}")


def xor(data: bytes, key: bytes) -> bytes:
    """XOR every byte with the key, restarting the key at each 4096-byte block.

    That is exactly what the guest does: the file is processed in blocks of
    ``KEY_LENGTH`` bytes and the key index restarts at every block, so the
    tail of the file (the last, partial block) is keyed from offset 0 again.
    """
    out = bytearray(len(data))
    for base in range(0, len(data), KEY_LENGTH):
        block = data[base:base + KEY_LENGTH]
        for i, b in enumerate(block):
            out[base + i] = b ^ key[i]
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("key", help="print the key taken from an agentd binary")
    p.add_argument("agentd")

    for name in ("decrypt", "encrypt"):
        p = sub.add_parser(name, help=f"{name} a payload file")
        p.add_argument("infile")
        p.add_argument("outfile")
        p.add_argument("--agentd", default="", help="agentd binary to take the key from")
        p.add_argument("--key-file", default="", help="a file holding the 4096-byte key")

    args = ap.parse_args()

    if args.cmd == "key":
        key = key_from_agentd(Path(args.agentd))
        print(f"key sha256: {hashlib.sha256(key).hexdigest()}")
        print(f"key head  : {key[:32].hex()}")
        return 0

    if args.key_file:
        key = Path(args.key_file).read_bytes()
    elif args.agentd:
        key = key_from_agentd(Path(args.agentd))
    else:
        raise SystemExit("give --agentd or --key-file")
    if len(key) < KEY_LENGTH:
        raise SystemExit(f"the key must be {KEY_LENGTH} bytes, got {len(key)}")

    data = Path(args.infile).read_bytes()
    out = xor(data, key)
    Path(args.outfile).write_bytes(out)
    print(f"{args.infile} ({len(data)} bytes) -> {args.outfile}")
    print(f"head: {out[:16].hex()} {out[:8]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
