"""enspctl: drive Huawei eNSP from the command line.

Sub-commands::

    topo    create | read | validate          work with .topo files
    lab     create | up | down | status | destroy   manage a lab
    dev     list | exec | shell              talk to a device
    run     <device> <command>...             shorthand for dev exec

A *lab* is a directory that owns a topology plus one VirtualBox machine per
device, and can be brought up without the eNSP application. See ``lab.py``.

Examples::

    enspctl topo create --spec spec.json -o lab.topo
    enspctl lab create demo --topo lab.topo --root ./demo
    enspctl lab up   --root ./demo
    enspctl dev list --root ./demo
    enspctl run R1 "display version" --root ./demo
    enspctl lab down --root ./demo
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import agentd, lab as labmod, topo, vbox


def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _die(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------- topo ----

def cmd_topo_create(args) -> int:
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else Path.cwd() / f"{spec.get('name', 'topology')}.topo"
    path = topo.write(spec, out, version=args.version or None, encoding=args.encoding)
    print(f"wrote {path} ({path.stat().st_size} bytes, {args.encoding})")
    parsed = topo.read(path)
    print(f"  {len(parsed.get('devices', []))} devices, "
          f"{len(parsed.get('links', []))} links, loads_in_ensp={parsed.get('loads_in_ensp')}")
    return 0


def cmd_topo_read(args) -> int:
    _print_json(topo.read(args.path))
    return 0


def cmd_topo_validate(args) -> int:
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    result = topo.validate(spec)
    _print_json(result)
    return 0 if result.get("ok", not result.get("problems")) else 1


# ----------------------------------------------------------------- lab ----

def cmd_lab_create(args) -> int:
    lab = labmod.Lab.create(args.name, args.root, args.topo,
                            subnet=args.subnet, host_ip=args.host_ip,
                            start_ip=args.start_ip, media_port=args.media_port)
    print(f"lab {lab.name!r} created in {lab.root}")
    for d in lab.devices:
        print(f"  {d.name:<10} model={d.model:<10} vm={d.vm:<24} ip={d.ip:<16} pipe={d.pipe}")
    if args.with_vms:
        print("creating VirtualBox machines ...")
        lab.create_vms()
    return 0


def cmd_lab_patch(args) -> int:
    """Rewrite each device's resource.cfg so VRP can bind its sockets."""
    from . import rescfg
    lab = labmod.Lab.load(args.root)
    print("patching resource.cfg ...")
    total = 0
    for d in lab.devices:
        vdi = lab.device_disk(d)
        if vdi is None:
            print(f"  {d.name}: no disk found, skipped")
            continue
        try:
            n = rescfg.rewrite_vdi(vdi, d.ip, lab.host_ip, vm_name=d.vm)
            total += n
            print(f"  {d.name}: {n} socket line(s) -> local {d.ip}, dest {lab.host_ip}")
        except rescfg.ResourceCfgError as exc:
            print(f"  {d.name}: FAILED {exc}")
    print(f"patched {total} socket line(s)")
    return 0


def cmd_lab_up(args) -> int:
    lab = labmod.Lab.load(args.root)
    if args.with_vms:
        lab.create_vms()
    if args.patch:
        cmd_lab_patch(argparse.Namespace(root=args.root))

    from . import supervise
    if args.foreground:
        return supervise.run_foreground(Path(args.root), verbose=not args.quiet)

    # The guests power themselves off when the control socket closes, so the
    # drivers must outlive this command: they run in a detached supervisor.
    print("starting the lab supervisor ...")
    supervise.stop(args.root)          # a stale one would hold the pipes
    supervise.spawn(args.root, verbose=not args.quiet)
    print("waiting for the devices ...")
    ready, _ = supervise.wait_until_up(Path(args.root), deadline_seconds=300)
    lab.print_status()
    if not ready:
        print("\nthe lab did not come up; see " + str(supervise.log_file(Path(args.root))),
              file=sys.stderr)
        return 1
    print("\nlab is up. Stop it with:  enspctl lab down --root " + str(args.root))
    return 0


def cmd_lab_down(args) -> int:
    from . import supervise
    lab = labmod.Lab.load(args.root)
    print("stopping the supervisor ...")
    supervise.stop(args.root)
    print("stopping machines ...")
    lab.stop_vms()
    return 0


def cmd_lab_status(args) -> int:
    lab = labmod.Lab.load(args.root)
    if args.json:
        _print_json(lab.status())
    else:
        lab.print_status()
    return 0


def cmd_lab_destroy(args) -> int:
    lab = labmod.Lab.load(args.root)
    print("stopping and removing machines ...")
    lab.stop_vms()
    lab.destroy_vms()
    print(f"lab {lab.name!r} destroyed (files left in {lab.root})")
    return 0


# ----------------------------------------------------------------- dev ----

def _lab_or_none(args):
    if not getattr(args, "root", None):
        return None
    try:
        return labmod.Lab.load(args.root)
    except labmod.LabError:
        return None


def cmd_dev_list(args) -> int:
    if args.root:
        lab = labmod.Lab.load(args.root)
        if args.json:
            _print_json(lab.status())
        else:
            lab.print_status()
        return 0
    # no lab: list every eNSP guest on the host-only subnet
    guests = agentd.guest_ip_from_host_only()
    rows = []
    for ip in guests:
        ports = agentd.probe_guest_ports(ip)
        rows.append({
            "guest_ip": ip,
            "agentd_control": ports.get(agentd.AGENTD_CONTROL_PORT, False),
            "vrp_console": ports.get(agentd.VRP_CONSOLE_PORT, False),
        })
    if args.json:
        _print_json({"count": len(rows), "devices": rows})
    else:
        print(f"{'guest_ip':<18} agentd  vrp")
        for r in rows:
            print(f"{r['guest_ip']:<18} {'yes' if r['agentd_control'] else '-':<7} "
                  f"{'yes' if r['vrp_console'] else '-'}")
    return 0


def _resolve_guest(args) -> str:
    if args.guest_ip:
        return args.guest_ip
    lab = _lab_or_none(args)
    if lab is not None:
        return lab.device(args.device).ip
    # fall back to the only guest that answers
    guests = agentd.guest_ip_from_host_only()
    if len(guests) == 1:
        return guests[0]
    if not guests:
        raise labmod.LabError("no eNSP guest found on the host-only subnet")
    raise labmod.LabError(
        f"several guests are reachable ({', '.join(guests)}) - name one with --guest-ip")


def cmd_dev_exec(args) -> int:
    try:
        guest = _resolve_guest(args)
    except labmod.LabError as exc:
        return _die(str(exc))
    try:
        with agentd.VrpConsole(guest) as con:
            out = con.run(args.commands, wait=args.wait)
    except agentd.VrpConsoleError as exc:
        return _die(str(exc))
    if args.raw:
        sys.stdout.write(out)
    else:
        print(out.rstrip())
    return 0


def cmd_dev_shell(args) -> int:
    try:
        guest = _resolve_guest(args)
    except labmod.LabError as exc:
        return _die(str(exc))
    try:
        con = agentd.VrpConsole(guest).connect()
    except agentd.VrpConsoleError as exc:
        return _die(str(exc))
    with con:
        if con.banner.strip():
            print(con.banner.rstrip())
        print("(type 'quit' to leave)")
        while True:
            try:
                line = input(f"{args.device or guest}> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if line.lower() in ("quit", "exit"):
                break
            if not line:
                continue
            con.send(line + "\r\n")
            sys.stdout.write(con._drain_until_prompt(15.0))
            sys.stdout.flush()
    return 0


def cmd_run(args) -> int:
    args.guest_ip = args.guest_ip or None
    return cmd_dev_exec(args)


# --------------------------------------------------------------- main ----

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="enspctl",
        description="Drive Huawei eNSP from the command line.",
    )
    p.add_argument("--version", action="version", version="enspctl 1.0.0")
    sub = p.add_subparsers(dest="group", required=True)

    # ---- topo ----------------------------------------------------------
    t = sub.add_parser("topo", help="work with .topo files").add_subparsers(
        dest="cmd", required=True)

    tc = t.add_parser("create", help="build a .topo from a JSON spec")
    tc.add_argument("--spec", required=True, help="JSON spec file")
    tc.add_argument("-o", "--out", default="")
    tc.add_argument("--version", default="")
    tc.add_argument("--encoding", default="utf-8", choices=["utf-8", "utf-8-sig", "utf-16"])
    tc.set_defaults(func=cmd_topo_create)

    tr = t.add_parser("read", help="parse a .topo into JSON")
    tr.add_argument("path")
    tr.set_defaults(func=cmd_topo_read)

    tv = t.add_parser("validate", help="check a JSON spec")
    tv.add_argument("--spec", required=True)
    tv.set_defaults(func=cmd_topo_validate)

    # ---- lab -----------------------------------------------------------
    l = sub.add_parser("lab", help="manage a lab").add_subparsers(
        dest="cmd", required=True)

    lc = l.add_parser("create", help="create a lab from a .topo")
    lc.add_argument("name")
    lc.add_argument("--topo", required=True, help="topology file")
    lc.add_argument("--root", default="", help="lab directory (default ./<name>)")
    lc.add_argument("--subnet", default=labmod.DEFAULT_SUBNET)
    lc.add_argument("--host-ip", default=labmod.DEFAULT_HOST_IP)
    lc.add_argument("--start-ip", type=int, default=labmod.DEFAULT_BASE_IP)
    lc.add_argument("--media-port", type=int, default=labmod.DEFAULT_MEDIA_PORT)
    lc.add_argument("--with-vms", action="store_true",
                    help="also clone the VirtualBox machines")
    lc.set_defaults(func=cmd_lab_create)

    lu = l.add_parser("up", help="start the lab")
    lu.add_argument("--root", required=True)
    lu.add_argument("--with-vms", action="store_true", default=True)
    lu.add_argument("--foreground", action="store_true",
                    help="run the drivers in this terminal instead of a supervisor")
    lu.add_argument("--quiet", action="store_true")
    lu.add_argument("--patch", action="store_true", default=True,
                    help="rewrite each device's resource.cfg first (default)")
    lu.set_defaults(func=cmd_lab_up)

    lp = l.add_parser("patch", help="rewrite each device's resource.cfg")
    lp.add_argument("--root", required=True)
    lp.set_defaults(func=cmd_lab_patch)

    ld = l.add_parser("down", help="stop the lab's machines")
    ld.add_argument("--root", required=True)
    ld.set_defaults(func=cmd_lab_down)

    ls_ = l.add_parser("status", help="show the lab's devices")
    ls_.add_argument("--root", required=True)
    ls_.add_argument("--json", action="store_true")
    ls_.set_defaults(func=cmd_lab_status)

    lx = l.add_parser("destroy", help="stop and remove the lab's machines")
    lx.add_argument("--root", required=True)
    lx.set_defaults(func=cmd_lab_destroy)

    # ---- dev -----------------------------------------------------------
    d = sub.add_parser("dev", help="talk to devices").add_subparsers(
        dest="cmd", required=True)

    dl = d.add_parser("list", help="list reachable devices")
    dl.add_argument("--root", default="")
    dl.add_argument("--json", action="store_true")
    dl.set_defaults(func=cmd_dev_list)

    de = d.add_parser("exec", help="run VRP commands on a device")
    de.add_argument("device")
    de.add_argument("commands", nargs="+")
    de.add_argument("--root", default="")
    de.add_argument("--guest-ip", default="")
    de.add_argument("--wait", type=float, default=1.5)
    de.add_argument("--raw", action="store_true", help="print without headers")
    de.set_defaults(func=cmd_dev_exec)

    ds = d.add_parser("shell", help="interactive VRP session")
    ds.add_argument("device", nargs="?", default="")
    ds.add_argument("--root", default="")
    ds.add_argument("--guest-ip", default="")
    ds.set_defaults(func=cmd_dev_shell)

    # ---- run (shorthand) -----------------------------------------------
    r = sub.add_parser("run", help="run VRP commands on a device (shorthand)")
    r.add_argument("device")
    r.add_argument("commands", nargs="+")
    r.add_argument("--root", default="")
    r.add_argument("--guest-ip", default="")
    r.add_argument("--wait", type=float, default=1.5)
    r.add_argument("--raw", action="store_true")
    r.set_defaults(func=cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "root", "") == "" and hasattr(args, "root"):
        args.root = ""
    try:
        return args.func(args)
    except labmod.LabError as exc:
        return _die(str(exc))
    except vbox.VBoxError as exc:
        return _die(str(exc))
    except agentd.AgentdError as exc:
        return _die(str(exc))
    except agentd.VrpConsoleError as exc:
        return _die(str(exc))
    except FileNotFoundError as exc:
        return _die(str(exc))
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
