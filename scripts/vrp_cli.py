"""Command-line client for an eNSP device's VRP console.

The console is a plain-text TCP service on the guest (port 40000) - the same
channel the eNSP client uses. Output is exact text; no screenshots involved.

    python vrp_cli.py --guest 192.168.56.101 display version
    python vrp_cli.py --guest 192.168.56.101 --interactive
    python vrp_cli.py --list
"""
from __future__ import annotations

import argparse
import sys
import time

try:
    from ensp_mcp.agentd import VrpConsole, VrpConsoleError, guest_ip_from_host_only
except ImportError:  # running straight from a checkout
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
    from ensp_mcp.agentd import VrpConsole, VrpConsoleError, guest_ip_from_host_only


def interactive(guest: str) -> int:
    with VrpConsole(guest) as con:
        if con.banner.strip():
            print(con.banner.rstrip())
        print("(type 'quit' to exit)")
        while True:
            try:
                line = input("router> ").strip()
            except EOFError:
                break
            if line.lower() in ("quit", "exit"):
                break
            if not line:
                continue
            con.send(line + "\r\n")
            print(con._drain_until_prompt(10.0), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("commands", nargs="*", help="VRP commands to run")
    ap.add_argument("--guest", default="192.168.56.101")
    ap.add_argument("--wait", type=float, default=2.0)
    ap.add_argument("--interactive", action="store_true")
    ap.add_argument("--list", action="store_true",
                    help="list reachable devices and exit")
    args = ap.parse_args(argv)

    if args.list:
        for ip in guest_ip_from_host_only():
            print(ip)
        return 0

    if args.interactive:
        return interactive(args.guest)

    if not args.commands:
        ap.print_help()
        return 2

    try:
        with VrpConsole(args.guest) as con:
            print(con.run(args.commands, wait=args.wait), end="")
    except VrpConsoleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
