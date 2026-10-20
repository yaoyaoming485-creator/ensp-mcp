"""Rewrite a guest's ``resource.cfg`` so VRP's interface sockets point at an
address the guest actually owns.

Why this is needed
------------------
Each eNSP AR guest ships a ``/dev/upload/resource.cfg`` that pins the VRP
interface sockets to a fixed address, e.g.

    interface[0] = socket[local:192.168.1.3:10000 dest:192.168.1.104:30000 ...]

The guest gets its IP from the host during the configuration handshake, so if
the pinned address does not match the IP the host handed over, VRP cannot bind
its sockets and dies with:

    bind: Cannot assign requested address
    ERROR :Start port 0 fail,return -1
    VPORT_Hal_PortEnable failed! u32Ret = -1

Rewriting the file to use the address the guest really has fixes that.

Usage
-----
    python patch_resource.py <image.vdi-or-raw> --guest-ip 192.168.56.101 \\
        --host-ip 192.168.56.1 [--netmask 255.255.255.0] [--dry-run]

The image is a VirtualBox disk; it is not modified in place here.  Mount it
first (or convert to raw and losetup it) and point the script at the mounted
partition instead:

    python patch_resource.py --path /mnt/r1p1/dev/upload/resource.cfg ...
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def rewrite(text: str, guest_ip: str, host_ip: str, netmask: str) -> tuple[str, int]:
    """Point every socket at the guest's real address.

    ``dest:`` becomes the host's address (the media/console side), ``local:``
    becomes the guest's own address.
    """
    old_local = re.findall(r"local:(\d+\.\d+\.\d+\.\d+):", text)
    old_dest = re.findall(r"dest:(\d+\.\d+\.\d+\.\d+):", text)
    if not old_local and not old_dest:
        return text, 0

    out = text
    for ip in set(old_local):
        out = out.replace(f"local:{ip}:", f"local:{guest_ip}:")
    for ip in set(old_dest):
        out = out.replace(f"dest:{ip}:", f"dest:{host_ip}:")
    return out, len(set(old_local)) + len(set(old_dest))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?", help="image file (informational)")
    ap.add_argument("--path", default="",
                    help="path to resource.cfg inside a mounted image")
    ap.add_argument("--guest-ip", default="192.168.56.101")
    ap.add_argument("--host-ip", default="192.168.56.1")
    ap.add_argument("--netmask", default="255.255.255.0")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    target = Path(args.path) if args.path else None
    if target is None or not target.exists():
        print("error: pass --path <resource.cfg> (mount the image first)",
              file=sys.stderr)
        return 2

    original = target.read_text(encoding="utf-8", errors="replace")
    new, changed = rewrite(original, args.guest_ip, args.host_ip, args.netmask)
    if changed == 0:
        print("no socket entries found - nothing to do")
        return 0

    if args.dry_run:
        print(f"would rewrite {changed} address(es):")
        for line in new.splitlines():
            if "socket[" in line:
                print("  " + line.strip()[:160])
        return 0

    backup = target.with_suffix(target.suffix + ".orig")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8", errors="replace")
    target.write_text(new, encoding="utf-8", errors="replace")
    print(f"rewrote {changed} address(es) in {target} (backup: {backup.name})")
    for line in new.splitlines():
        if "socket[" in line:
            print("  " + line.strip()[:160])
    return 0


if __name__ == "__main__":
    sys.exit(main())
