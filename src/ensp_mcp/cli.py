"""enspctl: drive Huawei eNSP from the command line.

Sub-commands::

    topo    create | read | validate          work with .topo files
    lab     create | up | down | status | destroy   manage a lab
    dev     list | exec | shell              talk to a device
    run     <device> <command>...             shorthand for dev exec
    status                                   the eNSP / VirtualBox environment
    models  [category]                       the device model catalogue
    open    [topo]                           launch the eNSP application
    handshake <guest-ip>                     run the agentd handshake by hand

A *lab* is a directory that owns a topology plus one VirtualBox machine per
device, and can be brought up without the eNSP application. See ``lab.py``.

Device state is read as text over the guest's VRP console; the guest screen
is never scraped. The screen tools of the MCP server stay diagnostic only.

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

from . import agentd, lab as labmod, models, topo, vbox


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
                            start_ip=args.start_ip, media_port=args.media_port,
                            nic_mode=args.nic)
    print(f"lab {lab.name!r} created in {lab.root}")
    for d in lab.devices:
        print(f"  {d.name:<10} model={d.model:<10} vm={d.vm:<24} ip={d.ip:<16} pipe={d.pipe}")
    if args.with_vms:
        print("creating VirtualBox machines ...")
        lab.create_vms()
    return 0


def cmd_lab_patch(args) -> int:
    """Rewrite each device's resource.cfg so VRP can bind its sockets."""
    from . import board as boardmod
    from . import rescfg
    lab = labmod.Lab.load(args.root)
    print("patching resource.cfg ...")
    total = 0
    failed: list[str] = []
    for d in lab.devices:
        vdi = lab.device_disk(d)
        if vdi is None:
            print(f"  {d.name}: no disk found, skipped")
            failed.append(d.name)
            continue
        ports = lab.interface_ports(d)
        # The board is the part that tells the guest which ports it has.  It
        # is only written when it is asked for: without it the guest keeps
        # the board the image shipped with (the AR201's nine ports), which is
        # what a lab that only needs the interfaces to come up can live with.
        brd = boardmod.board_for(d.model) if getattr(args, "board", False) else None
        if brd is None and not getattr(args, "board", False):
            # A lab that was patched with a board keeps it.  "lab up" calls
            # this without a board to move the addresses, and that rewrites
            # the image from the pristine base - which would drop the board
            # again and put the guest back on the image's own ports.
            if lab.marker_board_state(d) is not None:
                brd = boardmod.board_for(d.model)
        if getattr(args, "board", False) and brd is None:
            print(f"  {d.name}: no board known for {d.model}, keeping the image's")
        # The device type is the one number that really decides the board:
        # the guest's VRP rewrites the interface list itself, from the board
        # its device type selects (the board descriptions live in the guest).
        devtype = getattr(args, "device_type", None)
        if not lab.needs_patch(d, vdi, dest_port=True, port_map=ports,
                               board=brd, device_type=devtype):
            print(f"  {d.name}: already patched -> local {d.ip}, "
                  f"dest {lab.guest_host_ip} ({vdi.name})")
            continue
        try:
            n, new_disk = rescfg.rewrite_vdi(vdi, d.ip, lab.guest_host_ip,
                                              vm_name=d.vm,
                                              media_port=d.media_port,
                                              port_map=ports,
                                              board=brd,
                                              device_type=devtype)
        except rescfg.ResourceCfgError as exc:
            print(f"  {d.name}: FAILED {exc}")
            failed.append(d.name)
            continue
        total += n
        lab.mark_patched(d, new_disk, dest_port=True, port_map=ports, board=brd,
                         device_type=devtype)
        extra = ""
        if brd is not None:
            extra = f", board {d.model} ({brd.port_count} ports, device_type {brd.device_type})"
        print(f"  {d.name}: {n} socket line(s) -> local {d.ip}, "
              f"dest {lab.guest_host_ip}{extra}")
    print(f"patched {total} socket line(s)")
    if failed:
        # A guest whose resource.cfg still points at the old addresses cannot
        # bind its sockets: VRP dies with "VPORT_Hal_PortEnable failed!".
        # That has to be an error, not a warning.
        print("error: could not patch " + ", ".join(failed)
              + " - without it the guest cannot bind its sockets", file=sys.stderr)
        return 1
    return 0


def cmd_lab_up(args) -> int:
    lab = labmod.Lab.load(args.root)

    from . import supervise
    if args.foreground:
        if args.with_vms:
            lab.create_vms()
        if args.patch:
            cmd_lab_patch(argparse.Namespace(root=args.root))
        return supervise.run_foreground(Path(args.root), verbose=not args.quiet)

    # The guests power themselves off when the control socket closes, so the
    # drivers must outlive this command: they run in a detached supervisor.
    # The old supervisor has to go *first*: while it is alive it holds the
    # named pipes and the disk images, and patching a locked disk fails with
    # VBOX_E_INVALID_OBJECT_STATE.
    print("stopping any previous supervisor ...")
    supervise.stop(args.root)
    if args.with_vms:
        lab.create_vms()
    if args.patch:
        # The image can only be rewritten while the machine is off - a
        # running VM holds its disk and the clone fails with
        # VBOX_E_INVALID_OBJECT_STATE.
        if any(lab.needs_patch(d, lab.device_disk(d), dest_port=True,
                               port_map=lab.interface_ports(d))
               for d in lab.devices):
            print("stopping the machines so their images can be rewritten ...")
            lab.stop_vms()
        rc = cmd_lab_patch(argparse.Namespace(root=args.root))
        if rc != 0:
            print("aborting: the guest images could not be patched", file=sys.stderr)
            return rc
    print("starting the lab supervisor ...")
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
    # no lab: list every eNSP guest that can be reached
    guests = agentd.guest_endpoints()
    rows = []
    for ip in guests:
        host, port = agentd.split_endpoint(ip, agentd.VRP_CONSOLE_PORT)
        if port == agentd.VRP_CONSOLE_PORT:
            control = f"{host}:{agentd.AGENTD_CONTROL_PORT}"
        else:
            control = f"{host}:{agentd.AGENTD_CONTROL_PORT + (port - agentd.VRP_CONSOLE_PORT)}"
        rows.append({
            "guest_ip": ip,
            "agentd_control": agentd.probe_endpoint(control, timeout=0.4),
            "vrp_console": agentd.probe_endpoint(ip, timeout=0.4),
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
    """The address of the VRP console, as the host reaches it.

    The result is ``"host"`` or ``"host:port"``: behind NAT the host reaches
    the console through a forwarded port on the loopback.
    """
    if args.guest_ip:
        return args.guest_ip
    lab = _lab_or_none(args)
    if lab is not None:
        return lab.console_endpoint(lab.device(args.device))
    # fall back to the only guest that answers
    guests = agentd.guest_ip_from_host_only()
    if len(guests) == 1:
        return guests[0]
    if not guests:
        raise labmod.LabError("no eNSP guest found on the host-only subnet")
    raise labmod.LabError(
        f"several guests are reachable ({', '.join(guests)}) - name one with --guest-ip")


def _config_lines(path: str) -> list[str]:
    """Read a configuration file into a list of VRP commands.

    Blank lines and lines starting with ``#`` are skipped, so a config file
    can carry the comments that explain it.
    """
    out: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def cmd_dev_exec(args) -> int:
    commands = list(args.commands)
    if getattr(args, "file", ""):
        try:
            commands = _config_lines(args.file)
        except OSError as exc:
            return _die(f"cannot read {args.file}: {exc}")
    if not commands:
        return _die("no commands given")
    try:
        guest = _resolve_guest(args)
    except labmod.LabError as exc:
        return _die(str(exc))
    try:
        with agentd.VrpConsole(guest) as con:
            out = con.run(commands, wait=args.wait)
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
            try:
                sys.stdout.write(con.exec_one(line, wait=15.0))
            except agentd.VrpConsoleError as exc:
                print(f"error: {exc}", file=sys.stderr)
            sys.stdout.flush()
    return 0


def cmd_run(args) -> int:
    args.guest_ip = args.guest_ip or None
    return cmd_dev_exec(args)


# -------------------------------------------------------- environment ------

def cmd_status(args) -> int:
    """Report whether eNSP and VirtualBox are present, and what is running."""
    out: dict = {}
    try:
        out["vboxmanage"] = vbox.vboxmanage_path()
    except vbox.VBoxError as exc:
        out["vboxmanage"] = None
        out["error"] = str(exc)
    out["ensp_client"] = vbox.find_ensp_client()
    out["ensp_version"] = vbox.find_ensp_version()
    procs = vbox.ensp_processes()
    out["ensp_running"] = bool(procs)
    out["ensp_processes"] = procs
    out["vbox_processes"] = sorted({p["name"] for p in vbox.vbox_processes()})
    try:
        vms = vbox.list_vms()
        out["vm_total"] = len(vms)
        out["devices_running"] = [
            {"vm": v.name, "state": v.state} for v in vms if v.running
        ]
    except vbox.VBoxError as exc:
        out["vm_total"] = None
        out["error_vms"] = str(exc)
    if args.json:
        _print_json(out)
        return 0
    print(f"VBoxManage      : {out['vboxmanage']}")
    print(f"eNSP client    : {out['ensp_client']}")
    print(f"eNSP version   : {out['ensp_version']}")
    print(f"eNSP running   : {out['ensp_running']}")
    for p in out["ensp_processes"]:
        print(f"  - {p['name']} (pid {p.get('pid')})")
    print(f"VirtualBox     : {', '.join(out['vbox_processes']) or '-'}")
    print(f"machines       : {out['vm_total']}")
    for d in out["devices_running"]:
        print(f"  - {d['vm']} ({d['state']})")
    return 0


def cmd_models(args) -> int:
    """List the device models this tool can generate."""
    rows = models.describe_all()
    if args.category:
        want = args.category.strip().lower()
        rows = [r for r in rows if r["category"] == want]
    if args.json:
        _print_json({"count": len(rows), "models": rows})
        return 0
    print(f"{'model':<12} {'category':<10} {'ports':<6} image")
    for r in rows:
        need = "external" if r.get("needs_device_image") else "-"
        print(f"{r['model']:<12} {r['category']:<10} "
              f"{len(r.get('ports', [])):<6} {need}")
    return 0


def cmd_open(args) -> int:
    """Launch the eNSP application, optionally with a topology."""
    try:
        info = vbox.launch_ensp(args.path or None, wait=args.wait)
    except vbox.VBoxError as exc:
        return _die(str(exc))
    info["ok"] = True
    if args.json:
        _print_json(info)
        return 0
    print(f"eNSP started (pid {info.get('pid')}): {info.get('exe')}")
    if info.get("path"):
        print(f"opened: {info['path']}")
    print("devices are not started by this call - select them and press start")
    return 0


def cmd_handshake(args) -> int:
    """Run the guest agentd control handshake by hand."""
    try:
        result = agentd.control_handshake(args.guest_ip, timeout=args.timeout)
    except agentd.AgentdError as exc:
        return _die(str(exc))
    result["ok"] = True
    _print_json(result)
    return 0


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
    lc.add_argument("--nic", choices=["hostonly", "nat"], default="hostonly",
                    help="how the guests are attached to the host: a host-only "
                         "NIC (default) or VirtualBox's NAT")
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
    lu.add_argument("--no-patch", dest="patch", action="store_false",
                    help="skip the resource.cfg rewrite (the images are already "
                         "patched, and rewriting them is expensive)")
    lu.set_defaults(func=cmd_lab_up)

    lp = l.add_parser("patch", help="rewrite each device's resource.cfg")
    lp.add_argument("--root", required=True)
    lp.add_argument("--board", action="store_true",
                    help="also write the model's board (device_type and the "
                         "port list) into resource.cfg, so the guest presents "
                         "the model's ports instead of the ones the image "
                         "shipped with")
    lp.add_argument("--device-type", type=int, default=None,
                    help="write this device_type into resource.cfg: it is the "
                         "number that decides which board the guest comes up "
                         "with, because the guest's VRP rewrites the port list "
                         "itself")
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
    de.add_argument("--file", default="",
                    help="read the commands from a file (one per line)")
    de.set_defaults(func=cmd_dev_exec)

    ds = d.add_parser("shell", help="interactive VRP session")
    ds.add_argument("device", nargs="?", default="")
    ds.add_argument("--root", default="")
    ds.add_argument("--guest-ip", default="")
    ds.set_defaults(func=cmd_dev_shell)

    # ---- run (shorthand) -----------------------------------------------
    r = sub.add_parser("run", help="run VRP commands on a device (shorthand)")
    r.add_argument("device")
    r.add_argument("commands", nargs="*")
    r.add_argument("--root", default="")
    r.add_argument("--guest-ip", default="")
    r.add_argument("--wait", type=float, default=1.5)
    r.add_argument("--raw", action="store_true")
    r.add_argument("--file", default="",
                   help="read the commands from a file (one per line)")
    r.set_defaults(func=cmd_run)

    # ---- environment ----------------------------------------------------
    st_ = sub.add_parser("status", help="the eNSP / VirtualBox environment")
    st_.add_argument("--json", action="store_true")
    st_.set_defaults(func=cmd_status)

    mo = sub.add_parser("models", help="the device model catalogue")
    mo.add_argument("category", nargs="?", default="",
                    help="router | switch | firewall | wireless | endpoint")
    mo.add_argument("--json", action="store_true")
    mo.set_defaults(func=cmd_models)

    op = sub.add_parser("open", help="launch the eNSP application")
    op.add_argument("path", nargs="?", default="", help="topology file to open")
    op.add_argument("--wait", type=float, default=0.0)
    op.add_argument("--json", action="store_true")
    op.set_defaults(func=cmd_open)

    hs = sub.add_parser("handshake",
                         help="run the agentd control handshake by hand")
    hs.add_argument("guest_ip", help="the device's address, e.g. 192.168.56.2")
    hs.add_argument("--timeout", type=float, default=10.0)
    hs.set_defaults(func=cmd_handshake)

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
