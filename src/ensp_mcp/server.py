"""ensp-mcp: an MCP server for Huawei eNSP.

Four capabilities, all reverse-engineered from an installed eNSP 1.3.00.100:

1. **Topology authoring** -- build, read and validate ``.topo`` files offline.
2. **Real VRP console** -- run commands on the router itself and get exact
   text back. The guest's VRP process serves a plain-text console on TCP
   40000; this is the same channel the eNSP client uses.  **This is the only
   way device state is read.**
3. **Guest screen (diagnostic only)** -- look at the VirtualBox virtual screen
   (``screenshotpng`` + ``keyboardputstring``) of the *guest*.  For an eNSP AR
   device this screen shows the Tiny Core Linux guest, *not* the router CLI.
   It exists to answer "did the guest boot?", and it is never scraped to
   produce text: reading a device means reading the text console, and if the
   text console cannot be reached the call fails instead of degrading into
   pixels.
4. **Lifecycle and capture** -- start and stop device machines, and take a
   packet capture whose IP headers are read back out of it.

EVERY TOOL ANSWERS WITH A MACHINE-READABLE SHAPE
-------------------------------------------------
A failing call returns JSON with ``ok: false``, an ``error_code``, the
``error`` text, a ``hint`` that says what to do about it and, where one
exists, a structured ``action``.  The codes are listed per tool in the
registry (:mod:`ensp_mcp.registry`), which the ``ensp_tool_registry`` tool
hands out and which the MCP metadata of every tool carries as ``meta``.

The preconditions of a tool are declared the same way: ``meta.requires``
lists every precondition with the probe that checks it and the error code
that is raised when it does not hold.

Tool names are prefixed ``ensp_`` so they are easy to allow-list.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer, Image

from . import agentd, capture, console, models, occupancy, registry, topo, vbox

mcp = MCPServer("ensp")

DEFAULT_TOPO_DIR = Path(os.environ.get("ENSP_TOPO_DIR", Path.cwd()))

#: virtual machines this server refuses to start or stop on its own
DEFAULT_PROTECTED = ["KVM-Host"]


def _resolve_out(path: str | None, spec: dict) -> Path:
    if path:
        p = Path(path)
        return p if p.is_absolute() else (DEFAULT_TOPO_DIR / p)
    stem = str(spec.get("name") or "topology")
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in stem).strip("-") or "topology"
    return DEFAULT_TOPO_DIR / f"{safe}.topo"


def _fail(code: str, message: str, hint: str = "", action: Any = None,
          **extra: Any) -> str:
    """The one shape every failure takes.

    ``error_code`` is the stable, machine-readable part; ``error`` is the
    sentence a human reads; ``hint`` says what to do about it; ``action`` is
    the structured thing to do, when there is one.
    """
    payload: dict[str, Any] = {"ok": False, "error_code": code, "error": message}
    if hint:
        payload["hint"] = hint
    if action is not None:
        payload["action"] = action
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _console_answers(guest_ip: str) -> bool:
    """Does the console say *anything* when it is spoken to?

    Used to tell "the eNSP client has the console" apart from "the eNSP
    client has an idle socket".  The socket is closed normally here: a reset
    would be visible to the guest as a broken session.
    """
    host, port = agentd.split_endpoint(guest_ip, agentd.VRP_CONSOLE_PORT)
    s = socket.socket()
    s.settimeout(2.0)
    try:
        s.connect((host, port))
        s.sendall(b"\r\n")
        s.settimeout(1.5)
        try:
            return bool(s.recv(256))
        except socket.timeout:
            return False
        except OSError:
            return False
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# topology tools
# --------------------------------------------------------------------------

@mcp.tool(meta=registry.meta_for("ensp_list_models"))
def ensp_list_models(category: str = "") -> str:
    """List the eNSP device models this server can generate.

    Returns each model's category, its interfaces **in index order** (the index
    is what a link's a_port/b_port refers to), and whether it needs an external
    device image that the eNSP installer does not ship.

    Args:
        category: optional filter (router, switch, firewall, wireless, endpoint).
    """
    rows = models.describe_all()
    if category:
        want = category.strip().lower()
        rows = [r for r in rows if r["category"] == want]
    return json.dumps({"count": len(rows), "models": rows}, ensure_ascii=False, indent=2)


@mcp.tool(meta=registry.meta_for("ensp_create_topology"))
def ensp_create_topology(spec: dict, out_path: str = "", version: str = "", encoding: str = "utf-8") -> str:
    """Create an eNSP ``.topo`` file from a spec.

    ``spec`` shape::

        {
          "name": "ospf-lab",
          "devices": [{"name": "R1", "model": "AR2220"},
                      {"name": "R2", "model": "AR2220", "x": 450, "y": 250}],
          "links":   [{"a": "R1", "b": "R2", "a_port": 1, "b_port": 0, "type": "Copper"}],
          "labels":  [{"text": "Area0", "x": 190, "y": 160}],
          "boxes":   [{"x": 100, "y": 100, "w": 600, "h": 300, "color": 16776960}]
        }

    ``x``/``y`` are optional (grid auto-layout). For a link you may give either
    ``a_port``/``b_port`` as an interface index, or ``a_if``/``b_if`` as a name
    such as ``"GE0/0/1"``.

    The file is written as **UTF-8 without a BOM** even though it declares
    ``encoding="UNICODE"`` -- that is what eNSP itself produces. Writing UTF-16
    makes eNSP load the file as zero devices, with no error shown.

    Args:
        spec: the topology specification.
        out_path: destination; defaults to <name>.topo in the working directory.
        version: ``<topo version="...">`` value; defaults to the installed eNSP.
        encoding: utf-8 (default and correct), utf-8-sig, or utf-16.
    """
    try:
        path = topo.write(spec, _resolve_out(out_path, spec), version=version or None, encoding=encoding)
    except topo.TopoError as exc:
        return _fail("topo_invalid", str(exc),
                     hint="Fix the spec and write the topology again.")
    except OSError as exc:
        return _fail("topo_write_failed", f"{type(exc).__name__}: {exc}",
                     hint="Check that the destination directory exists and is writable.")
    report = topo.validate(spec)
    with open(path, "rb") as fh:
        head = fh.read(2)
    return json.dumps(
        {
            "ok": True,
            "path": str(path),
            "bytes": path.stat().st_size,
            "version": topo.resolve_version(version or None),
            "encoding": encoding,
            "has_bom": head in (b"\xff\xfe", b"\xfe\xff", b"\xef\xbb"),
            "devices": report.get("device_count"),
            "links": report.get("link_count"),
            "warnings": report.get("warnings", []),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(meta=registry.meta_for("ensp_read_topology"))
def ensp_read_topology(path: str) -> str:
    """Parse an existing ``.topo`` file into JSON (devices, links, labels, boxes).

    Args:
        path: path to the .topo file.
    """
    try:
        data = topo.read(path)
    except FileNotFoundError as exc:
        return _fail("topo_not_found", f"{type(exc).__name__}: {exc}",
                     hint="Check the path; the file does not exist.")
    except Exception as exc:  # noqa: BLE001
        return _fail("topo_unreadable", f"{type(exc).__name__}: {exc}",
                     hint="The file exists but is not a topology this reader understands.")
    data["ok"] = True
    data["path"] = str(path)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool(meta=registry.meta_for("ensp_validate_topology"))
def ensp_validate_topology(spec: dict) -> str:
    """Validate a topology spec without writing a file.

    Reports problems that would make eNSP reject or misread the topology, and
    warns about devices whose images are not installed.

    Args:
        spec: the same specification accepted by ensp_create_topology.
    """
    return json.dumps(topo.validate(spec), ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# runtime tools
# --------------------------------------------------------------------------

@mcp.tool(meta=registry.meta_for("ensp_status"))
def ensp_status() -> str:
    """Report whether eNSP and VirtualBox are present, and what is running.

    Use this first to find out whether devices are up before touching a console.

    It also reports which consoles are occupied and by whom, and whether this
    server is elevated -- the two things that decide whether the next call
    will work.
    """
    out: dict[str, Any] = {}
    try:
        out["vboxmanage"] = vbox.vboxmanage_path()
    except vbox.VBoxError as exc:
        out["vboxmanage"] = None
        out["error"] = str(exc)
    out["ensp_client"] = vbox.find_ensp_client()
    out["ensp_version"] = vbox.find_ensp_version()
    ensp_procs = vbox.ensp_processes()
    out["ensp_running"] = bool(ensp_procs)
    out["ensp_processes"] = ensp_procs
    out["vbox_processes"] = sorted({p["name"] for p in vbox.vbox_processes()})
    out["elevated"] = capture.is_elevated()
    out["pktmon_available"] = capture.pktmon_available()
    try:
        vms = vbox.list_vms()
        out["vm_total"] = len(vms)
        out["devices_running"] = [
            {"vm": v.name, "state": v.state} for v in vms if v.running
        ]
        managed = [v.name for v in vms if vbox.is_ensp_managed(v)]
        out["ensp_managed_machines"] = managed
        out["ensp_managed_count"] = len(managed)
    except vbox.VBoxError as exc:
        out["vm_total"] = None
        out["error_vms"] = str(exc)
    try:
        out["console_occupancy"] = occupancy.summarize()
    except Exception as exc:  # noqa: BLE001
        out["console_occupancy"] = {"error": f"{type(exc).__name__}: {exc}"}
    out["protected_machines"] = list(DEFAULT_PROTECTED)
    return json.dumps(out, ensure_ascii=False, indent=2)


@mcp.tool(meta=registry.meta_for("ensp_list_devices"))
def ensp_list_devices(running_only: bool = True) -> str:
    """List device VMs and how to reach each one's console.

    ``serial_pipe`` is the COM2 host pipe. It is **not** the console: it
    carries the guest agent's configuration handshake (a 4-byte challenge and
    the 28+27 byte reply), which is why it looks like a serial console and is
    not one. The router's own CLI is a plain-text service on the guest
    (TCP 40000) -- use ``ensp_vrp_command`` for that, and ``ensp_vrp_list_devices``
    to find the guest addresses.

    Args:
        running_only: only include VMs that are currently running.
    """
    try:
        vms = vbox.list_vms()
    except vbox.VBoxError as exc:
        return _fail("vbox_not_found", str(exc),
                     hint="Install VirtualBox or set VBOXMANAGE to VBoxManage.exe.")
    if running_only:
        vms = [v for v in vms if v.running]
    rows = []
    for v in vms:
        try:
            ch = vbox.console_channels(v.name)
        except vbox.VBoxError as exc:
            ch = {"vm": v.name, "error": str(exc)}
        ch["uuid"] = v.uuid
        ch["protected"] = v.name in DEFAULT_PROTECTED
        ch["managed_by"] = "ensp" if vbox.is_ensp_managed(v) else "local"
        rows.append(ch)
    return json.dumps({"ok": True, "count": len(rows), "devices": rows},
                      ensure_ascii=False, indent=2)


@mcp.tool(meta=registry.meta_for("ensp_open_topology"))
def ensp_open_topology(path: str = "", wait_seconds: float = 0.0) -> str:
    """Launch the eNSP GUI, optionally opening a topology file.

    Devices are not started by this call; in eNSP you still select the devices
    and use the start button. It opens the file so the topology is loaded.

    Args:
        path: path to a .topo file (empty string just launches eNSP).
        wait_seconds: seconds to wait after launching before returning.
    """
    if path:
        p = Path(path)
        if not p.is_file():
            return _fail("topo_not_found", f"topology file does not exist: {p}",
                         hint="Check the path; ensp_read_topology can list what is there.")
    try:
        info = vbox.launch_ensp(path or None, wait=wait_seconds)
    except vbox.VBoxError as exc:
        return _fail("ensp_launch_failed", str(exc),
                     hint="Is eNSP installed? ensp_status reports the client path.")
    except Exception as exc:  # noqa: BLE001
        # Anything the launcher throws used to escape the tool and reach the
        # client as a bare "Error executing tool ensp_open_topology", with the
        # reason nowhere to be seen.
        return _fail("ensp_launch_failed", f"{type(exc).__name__}: {exc}",
                     hint="The eNSP client could not be started; the details are "
                          "in the error text.")
    info["ok"] = True
    info["note"] = ("Devices are not started by this call; start them in eNSP, "
                    "or with ensp_start_devices.")
    return json.dumps(info, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# device lifecycle
# --------------------------------------------------------------------------

def _resolve_vm(name: str, vms: list[vbox.VmInfo]) -> tuple[vbox.VmInfo | None, str]:
    """Find the VirtualBox machine behind a device name.

    eNSP names the clone it makes for a device after the device, but a name
    that only *contains* the device name is also accepted -- and only when it
    is unambiguous, because guessing the wrong machine is worse than failing.
    """
    for v in vms:
        if v.name == name:
            return v, "exact"
    for v in vms:
        if v.name.lower() == name.lower():
            return v, "case-insensitive"
    partial = [v for v in vms if name.lower() in v.name.lower()]
    if len(partial) == 1:
        return partial[0], "partial"
    return None, "not found"


def _topology_device_names(topo_path: str) -> list[str]:
    """The device names of a topology file, or an empty list."""
    if not topo_path:
        return []
    try:
        data = topo.read(topo_path)
    except Exception:  # noqa: BLE001
        return []
    return [str(d.get("name", "")) for d in data.get("devices", [])]


def _unresolved_device(name: str, topo_path: str, vms: list[vbox.VmInfo],
                       stopping: bool) -> tuple[str, str, dict]:
    """Why a device name did not resolve to a machine, and what to do about it.

    A device of the eNSP GUI is the one case where the name is *right* and
    the lookup still fails: the eNSP client names its clones after the
    device's ssid (``AR_Clone_<guid>``), not after the device, and it is the
    client that gives the guest its address.  Saying "no such machine" for
    that is technically true and practically useless.
    """
    managed = [v for v in vms if vbox.is_ensp_managed(v)]
    in_topology = name in _topology_device_names(topo_path)
    client_running = bool(vbox.ensp_processes())
    if not (in_topology or (managed and client_running)):
        return ("device_not_found",
                f"no VirtualBox machine named {name!r}",
                {"kind": "list_machines"})

    if in_topology:
        reason = (f"{name!r} is a device of the eNSP GUI, and the eNSP client "
                  "owns its machine")
    else:
        reason = (f"{name!r} is most likely a device of the eNSP GUI: the "
                  "eNSP client owns its machines and is running")
    if stopping:
        hint = ("Stop it in the eNSP GUI, or name the machine itself - the "
                "eNSP client calls its clones AR_Clone_<ssid>, and "
                "ensp_list_devices lists them. To stop a lab of this project, "
                "use ensp_stop_devices(lab_root=...).")
        action = {"kind": "use_ensp_gui"}
    else:
        hint = ("Start it in the eNSP GUI. The client also gives the guest its "
                "address: the guest's agentd gets its IP from the client's "
                "serial handshake, so a machine started behind the client's "
                "back boots but never becomes reachable. To drive a device "
                "from this server, use a lab of this project instead - "
                "ensp_start_devices(lab_root=...) brings a device up with a "
                "working console that the eNSP client does not hold.")
        action = {"kind": "use_lab_mode",
                  "detail": "ensp_start_devices(lab_root=...)"}
    return ("device_needs_ensp_client", reason, action)


def _lab_needs_preparation(lab) -> bool:
    """Do the lab's machines still have to be made?

    Cloning a machine and rewriting its image takes minutes, and a tool call
    may not block for that long, so the work is done in the background.
    """
    for d in lab.devices:
        if not lab.vm_exists(d):
            return True
        disk = lab.device_disk(d)
        if disk is None:
            return True
        if lab.needs_patch(d, disk, dest_port=True,
                           port_map=lab.interface_ports(d)):
            return True
    return False


def _spawn_lab_job(lab_root: str, topo_path: str = "") -> int:
    """Run ``enspctl lab up`` detached, and return its pid.

    The job makes the machines, rewrites their images and starts the
    supervisor; ``ensp_lab_status`` reports how far it got.
    """
    import subprocess
    import sys

    args = [sys.executable, "-m", "ensp_mcp.cli", "lab", "up", "--root", str(lab_root)]
    if topo_path:
        args += ["--topo", str(topo_path)]
    root = Path(lab_root)
    root.mkdir(parents=True, exist_ok=True)
    log = open(root / "lab-up.log", "a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=flags)
    return proc.pid


@mcp.tool(meta=registry.meta_for("ensp_prepare_lab"))
def ensp_prepare_lab(lab_root: str) -> str:
    """Quiet a lab: shut down the ports that no link uses.

    A port with no peer keeps its link trying to come up, and the device
    announces it on the console over and over.  That noise lands in the
    middle of the command that is being typed, and the router then reads
    half of it and answers *"Unrecognized command found at '^' position"* -
    which looks like the MCP mistyped the command.

    This shuts those ports down, and reports what it did per device.

    Args:
        lab_root: a lab directory of this project.
    """
    from . import lab as labmod
    try:
        lab = labmod.Lab.load(lab_root)
    except Exception as exc:  # noqa: BLE001
        return _fail("lab_not_found", f"{type(exc).__name__}: {exc}",
                     hint="The directory has to be a lab this project created.")
    rows = []
    for dev in lab.devices:
        unused = lab.unused_port_names(dev)
        if not unused:
            rows.append({"device": dev.name, "ip": dev.ip, "shut": [],
                         "note": "every port carries a link"})
            continue
        commands = ["system-view"]
        for name in unused:
            commands += [f"interface {name}", "shutdown", "quit"]
        commands += ["display interface brief", "return"]
        try:
            with agentd.VrpConsole(dev.ip, wake=False) as con:
                # no echo verification: a console that is announcing a flapping
                # port splits the command, and the check would fail on a
                # command that did run
                out = con.run(commands, wait=1.0, until_prompt=False,
                              verify=False)
            still_up = [ln.strip() for ln in out.splitlines()
                        if " has turned into UP state" in ln]
            rows.append({"device": dev.name, "ip": dev.ip, "shut": unused,
                         "note": ("the console is still announcing a port"
                                  if still_up else None)})
        except Exception as exc:  # noqa: BLE001
            rows.append({"device": dev.name, "ip": dev.ip, "shut": [],
                         "error": f"{type(exc).__name__}: {exc}"})
    return json.dumps({
        "ok": all("error" not in r for r in rows),
        "lab": lab.name,
        "root": str(lab.root),
        "devices": rows,
    }, ensure_ascii=False, indent=2)


@mcp.tool(meta=registry.meta_for("ensp_lab_status"))
def ensp_lab_status(lab_root: str) -> str:
    """The state of a lab: the supervisor, and every device in it.

    Use it after ``ensp_start_devices``: the machines have to be cloned and
    their images rewritten, which takes minutes, so that work runs in the
    background and this is how the caller watches it.

    Args:
        lab_root: a lab directory of this project.
    """
    from . import lab as labmod
    from . import supervise
    try:
        lab = labmod.Lab.load(lab_root)
    except Exception as exc:  # noqa: BLE001
        return _fail("lab_not_found", f"{type(exc).__name__}: {exc}",
                     hint="The directory has to be a lab this project created.")
    state = lab.status()
    log = Path(lab_root) / "lab-up.log"
    tail = []
    if log.is_file():
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
    return json.dumps({
        "ok": True,
        "lab": lab.name,
        "root": str(lab.root),
        "devices": state.get("devices", []),
        "supervisor": state.get("supervisor", {}),
        "lab_up_log": tail,
    }, ensure_ascii=False, indent=2)


@mcp.tool(meta=registry.meta_for("ensp_start_devices"))
def ensp_start_devices(devices: list[str] = [], lab_root: str = "",
                       wait: float = 0.0,
                       protected: list[str] = DEFAULT_PROTECTED,
                       override_protected: bool = False,
                       topo_path: str = "", like_ensp: bool = False) -> str:
    """Start device virtual machines.

    Two ways to do it, and they are not the same thing:

    * ``lab_root`` -- the project's own lab. The supervisor is started, which
      is what brings the devices up **with an address and a working VRP
      console**, because the guest only answers after the host has done its
      serial handshake with it. The eNSP client is not involved, so it holds
      no console session either: this is the mode in which this server can
      actually drive a device.  With ``topo_path`` as well, a lab is created
      from that topology first - its device names, its ssids and its links
      all come from the file - so **one call starts a whole topology**.
    * ``devices`` -- plain VirtualBox machines, started headless. This is
      enough for a machine that already has its addressing. A device of the
      eNSP GUI does **not** work this way: the eNSP client gives the guest
      its address, so a machine started behind the client's back boots but
      never becomes reachable -- and the client names its clones after the
      device's ssid, so the device's own name does not even find it. That
      case is reported as ``device_needs_ensp_client``.

    Args:
        devices: the device (or machine) names to start.
        lab_root: a lab directory of this project; starts its supervisor.
        wait: seconds to wait for the devices to answer.
        protected: machine names this server refuses to touch.
        override_protected: start them anyway.
        topo_path: the topology the devices come from.  With ``lab_root`` it
            is the lab that is created; without it, it lets the call tell
            "this is an eNSP device" from "this name is a typo".
        like_ensp: give the devices the identity the eNSP client would - the
            first address is .2, the machine is called ``AR_Clone_<ssid>`` and
            the serial pipe is the client's own.  The devices are then the
            ones the rest of the lab already expects, and the eNSP client is
            not needed at all.
    """
    if not devices and not lab_root:
        return _fail("no_devices_given", "neither devices nor lab_root was given",
                     hint="Name the devices to start, or point at a lab directory.")

    if lab_root:
        from . import lab as labmod
        from . import supervise
        root = Path(lab_root)
        created = None
        if topo_path and not (root / "lab.json").is_file():
            # the topology is the source of the devices: its names, its ssids
            # and its links all come from the file
            try:
                created = labmod.Lab.create(root.name or "lab", root, topo_path,
                                            like_ensp=like_ensp)
            except Exception as exc:  # noqa: BLE001
                return _fail("lab_create_failed", f"{type(exc).__name__}: {exc}",
                             hint="The topology could not be turned into a lab; "
                                  "check the path and the file.")
        try:
            lab = labmod.Lab.load(lab_root)
        except Exception as exc:  # noqa: BLE001
            return _fail("lab_not_found", f"{type(exc).__name__}: {exc}",
                         hint="The directory has to be a lab this project created.")
        problems = []
        patched = None
        busy = lab.busy_ports()
        if busy:
            return _fail(
                "lab_ports_in_use",
                "the host ports this lab needs are already held: "
                + ", ".join(str(p) for p in sorted(busy)[:12]),
                hint="Two labs cannot run at the same time - the interface "
                     "sockets live on fixed host ports. Stop the other lab "
                     "(enspctl lab down --root <other>) and start this one again.",
                action={"kind": "stop_other_lab"},
                lab=lab.name, root=str(lab.root))
        if _lab_needs_preparation(lab):
            # cloning a machine and rewriting its image takes minutes, and a
            # tool call may not block for that long: the work runs in the
            # background and ensp_lab_status reports how far it got
            pid = _spawn_lab_job(lab_root, topo_path)
            return json.dumps({
                "ok": True,
                "mode": "lab",
                "state": "preparing",
                "lab": lab.name,
                "root": str(lab.root),
                "pid": pid,
                "created_from_topology": str(created.topo_path) if created else None,
                "planned_devices": [
                    {"device": d.name, "vm": d.vm, "ip": d.ip,
                     "model": d.model, "ssid": d.device_id.rstrip("\x00")}
                    for d in lab.devices
                ],
                "note": "the machines are being cloned and their images "
                        "rewritten; poll ensp_lab_status(lab_root=...) until "
                        "the devices answer",
                "problems": problems,
            }, ensure_ascii=False, indent=2)
        try:
            lab.create_vms()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"creating the machines failed: {exc}")
        # the guest's resource.cfg has to point at the lab's addresses, and at
        # the model's board, before the machines are started
        try:
            total, failed = lab.patch_devices(with_board=True)
            patched = f"{total} socket line(s) patched"
            if failed:
                problems.append("could not patch: " + ", ".join(failed))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"patching the images failed: {exc}")
        try:
            supervise.spawn(lab_root)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"starting the supervisor failed: {exc}")
        ready, rows = supervise.wait_until_up(
            Path(lab_root), deadline_seconds=max(30.0, wait or 300.0))
        return json.dumps({
            "ok": bool(ready) or not problems,
            "mode": "lab",
            "lab": lab.name,
            "root": str(lab.root),
            "ready": ready,
            "created_from_topology": str(created.topo_path) if created else None,
            "patched": patched,
            "devices": rows,
            "problems": problems,
            "log": str(supervise.log_file(lab.root)),
        }, ensure_ascii=False, indent=2)

    try:
        vms = vbox.list_vms()
    except vbox.VBoxError as exc:
        return _fail("vbox_not_found", str(exc),
                     hint="Install VirtualBox or set VBOXMANAGE to VBoxManage.exe.")
    rows = []
    failed = False
    for name in devices:
        vm, how = _resolve_vm(name, vms)
        if vm is None:
            code, reason, action = _unresolved_device(name, topo_path, vms,
                                                       stopping=False)
            failed = True
            rows.append({"device": name, "ok": False,
                         "error_code": code, "error": reason,
                         "action": action,
                         "known_machines": [v.name for v in vms][:40]})
            continue
        if vm.name in (protected or []) and not override_protected:
            failed = True
            rows.append({"device": name, "vm": vm.name, "ok": False,
                         "error_code": "vm_protected",
                         "error": f"{vm.name} is on the protected list",
                         "hint": "Pass override_protected=true if it is yours to start."})
            continue
        if vm.running:
            rows.append({"device": name, "vm": vm.name, "ok": True,
                         "state": "running", "resolution": how,
                         "note": "already running"})
            continue
        res = vbox.run_vboxmanage(["startvm", vm.name, "--type", "headless"])
        if res.returncode != 0:
            failed = True
            rows.append({"device": name, "vm": vm.name, "ok": False,
                         "error_code": "device_start_failed",
                         "error": (res.stderr or res.stdout).strip()})
            continue
        rows.append({"device": name, "vm": vm.name, "ok": True,
                     "state": "starting", "resolution": how})
    return json.dumps({
        "ok": not failed,
        "mode": "vbox",
        "devices": rows,
        "note": ("A machine started on its own does not get an address: a "
                 "device of the eNSP GUI needs the eNSP client, and a device "
                 "of this project's lab needs its supervisor (pass lab_root)."),
    }, ensure_ascii=False, indent=2)


@mcp.tool(meta=registry.meta_for("ensp_stop_devices"))
def ensp_stop_devices(devices: list[str] = [], lab_root: str = "",
                      force: bool = False,
                      protected: list[str] = DEFAULT_PROTECTED,
                      override_protected: bool = False,
                      topo_path: str = "") -> str:
    """Stop device virtual machines.

    The machines are asked to power off gracefully; ``force`` pulls the plug
    instead. With ``lab_root`` the lab's supervisor is stopped first, so the
    guests are not left waiting for a host that is gone.

    A device of the eNSP GUI is owned by the eNSP client: its machine is
    named after the device's ssid (``AR_Clone_<guid>``), so the device's own
    name does not find it, and the client is what started it. That case is
    reported as ``device_needs_ensp_client``.

    Args:
        devices: the device (or machine) names to stop.
        lab_root: a lab directory of this project; stops its supervisor.
        force: power off instead of asking nicely.
        protected: machine names this server refuses to touch.
        override_protected: stop them anyway.
        topo_path: optional topology the device names come from; it lets the
            call tell "this is an eNSP device" from "this name is a typo".
    """
    if not devices and not lab_root:
        return _fail("no_devices_given", "neither devices nor lab_root was given",
                     hint="Name the devices to stop, or point at a lab directory.")

    if lab_root:
        from . import lab as labmod
        from . import supervise
        try:
            lab = labmod.Lab.load(lab_root)
        except Exception as exc:  # noqa: BLE001
            return _fail("lab_not_found", f"{type(exc).__name__}: {exc}",
                         hint="The directory has to be a lab this project created.")
        problems = []
        try:
            supervise.stop(lab_root)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"stopping the supervisor failed: {exc}")
        try:
            lab.stop_vms()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"stopping the machines failed: {exc}")
        return json.dumps({
            "ok": not problems,
            "mode": "lab",
            "lab": lab.name,
            "root": str(lab.root),
            "devices": lab.status().get("devices", []),
            "problems": problems,
        }, ensure_ascii=False, indent=2)

    try:
        vms = vbox.list_vms()
    except vbox.VBoxError as exc:
        return _fail("vbox_not_found", str(exc),
                     hint="Install VirtualBox or set VBOXMANAGE to VBoxManage.exe.")
    rows = []
    failed = False
    for name in devices:
        vm, how = _resolve_vm(name, vms)
        if vm is None:
            code, reason, action = _unresolved_device(name, topo_path, vms,
                                                       stopping=True)
            failed = True
            rows.append({"device": name, "ok": False,
                         "error_code": code, "error": reason,
                         "action": action,
                         "known_machines": [v.name for v in vms][:40]})
            continue
        if vm.name in (protected or []) and not override_protected:
            failed = True
            rows.append({"device": name, "vm": vm.name, "ok": False,
                         "error_code": "vm_protected",
                         "error": f"{vm.name} is on the protected list",
                         "hint": "Pass override_protected=true if it is yours to stop."})
            continue
        if not vm.running:
            rows.append({"device": name, "vm": vm.name, "ok": True,
                         "state": "not running", "resolution": how})
            continue
        action = "poweroff" if force else "acpipowerbutton"
        res = vbox.run_vboxmanage(["controlvm", vm.name, action])
        if res.returncode != 0:
            failed = True
            rows.append({"device": name, "vm": vm.name, "ok": False,
                         "error_code": "device_stop_failed",
                         "error": (res.stderr or res.stdout).strip()})
            continue
        rows.append({"device": name, "vm": vm.name, "ok": True,
                     "state": "stopping", "action": action, "resolution": how})
    return json.dumps({"ok": not failed, "mode": "vbox", "devices": rows},
                      ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------

@mcp.tool(meta=registry.meta_for("ensp_capture"))
def ensp_capture(duration: float = 10.0, out_path: str = "", backend: str = "auto",
                 filters: list[str] = [], elevate: bool = False,
                 interface: str = "", analyze: bool = True) -> str:
    """Capture packets and read the IP headers back out of them.

    The result is the table a QoS report needs: source, destination,
    protocol and the **DSCP** of every flow that was seen. That is what the
    wire carried -- a router's own counters cannot say how a packet looked
    on the link.

    The Windows backend is ``pktmon``, which talks to the PktMon kernel
    driver and therefore needs an **elevated process**. This server usually
    runs unelevated, so the call says so instead of capturing nothing;
    ``elevate=true`` retries the capture in a helper that is started through
    the shell's ``runas`` verb.

    Args:
        duration: how many seconds to capture.
        out_path: where to write the capture file.
        backend: auto (pktmon, then raw), pktmon or raw.
        filters: pktmon filter expressions, e.g. ["-p 40000"] or ["-i 10.12.0.1"].
        elevate: allow an elevated helper to take the capture.
        interface: the adapter to capture on (raw backend).
        analyze: read the IP headers out of the capture.
    """
    if backend not in ("auto", "pktmon", "raw"):
        return _fail("capture_backend_unknown", f"unknown backend {backend!r}",
                     hint="Use auto, pktmon or raw.")
    try:
        res = capture.capture(duration=duration, out_path=out_path or "",
                              backend=backend, filters=list(filters or []),
                              elevate=elevate, interface=interface)
    except capture.CaptureError as exc:
        return _fail("capture_failed", str(exc),
                     hint="Check the backend and the destination path.")
    except Exception as exc:  # noqa: BLE001
        return _fail("capture_failed", f"{type(exc).__name__}: {exc}",
                     hint="Check the backend and the destination path.")
    if not res.get("ok"):
        detail = res.get("error", "")
        needs_admin = "elevated" in detail or "拒绝访问" in detail
        return _fail(
            "capture_requires_elevation" if needs_admin else "capture_failed",
            detail,
            hint=("The PktMon driver only talks to an elevated process. Retry "
                  "with elevate=true, or run the server elevated."
                  if needs_admin else
                  "Neither backend could take the capture; the attempts are "
                  "listed with the reason."),
            attempts=res.get("attempts", []))
    out: dict[str, Any] = {
        "ok": True,
        "backend": res["backend"],
        "file": res["file"],
        "duration": duration,
        "attempts": res.get("attempts", []),
    }
    if res.get("warnings"):
        out["warnings"] = res["warnings"]
    if analyze:
        try:
            summary = capture.summarize(res["file"])
            out["summary"] = summary
            out["table"] = capture.render_table(summary)
        except Exception as exc:  # noqa: BLE001
            out["analysis_error"] = f"{type(exc).__name__}: {exc}"
    return json.dumps(out, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# console tools
# --------------------------------------------------------------------------

@mcp.tool(structured_output=False, meta=registry.meta_for("ensp_console_screen"))
def ensp_console_screen(vm: str, save_path: str = "") -> list[Any]:
    """Capture a running device's guest screen (diagnostic only).

    **Do not use this tool to read device state.**  It returns a 720x400 PNG
    bitmap of the guest's Tiny Core Linux console, not the router CLI, and
    turning that bitmap into characters would need OCR.  This project reads
    device state as plain text only, over the guest's TCP 40000, with
    ``ensp_vrp_command``.

    Use this tool to answer "did the guest boot / did it die", i.e. whether the
    login prompt is on screen.

    Args:
        vm: the device name, which is also the VirtualBox VM name (e.g. "R1").
        save_path: optional path to also keep the PNG on disk.
    """
    try:
        path = console.capture(vm, save_path or None)
    except (vbox.VBoxError, console.ConsoleError) as exc:
        return [f"failed to capture {vm}: {exc}"]
    size = path.stat().st_size
    return [
        f"console screen of {vm} ({size} bytes): {path} "
        f"(guest screen, not the router CLI - use ensp_vrp_command for state)",
        Image(path=str(path)),
    ]


@mcp.tool(meta=registry.meta_for("ensp_console_type"))
def ensp_console_type(vm: str, text: str, enter: bool = True) -> str:
    """Type text into a running device's console, optionally pressing Enter.

    Keystrokes go through the VM's virtual keyboard, so the guest must have the
    console focused -- for eNSP devices that is the default.

    Args:
        vm: device / VM name (e.g. "R1").
        text: literal text to type. Use ASCII; the guest keyboard is US layout.
        enter: press Enter after typing.
    """
    try:
        console.send_text(vm, text, enter=enter)
    except (vbox.VBoxError, console.ConsoleError) as exc:
        return _fail("device_not_running", f"failed to type into {vm}: {exc}",
                     hint="Is the machine running? ensp_list_devices says so.")
    return f"typed {len(text)} chars into {vm}" + (" + Enter" if enter else "")


@mcp.tool(meta=registry.meta_for("ensp_console_key"))
def ensp_console_key(vm: str, key: str, times: int = 1) -> str:
    """Send a named key to a device console.

    Useful keys: enter, esc, tab, backspace, up, down, left, right, space,
    delete, ctrl-c, ctrl-z, ctrl-a, ctrl-d, ctrl-p, ctrl-u, ctrl-w, y, n, q,
    F1..F10.

    Args:
        vm: device / VM name.
        key: key name from the list above.
        times: how many times to press it.
    """
    try:
        console.send_key(vm, key, times=times)
    except (vbox.VBoxError, console.ConsoleError) as exc:
        return _fail("device_not_running", f"failed to send {key} to {vm}: {exc}",
                     hint="Is the machine running? ensp_list_devices says so.")
    return f"sent {key} x{times} to {vm}"


@mcp.tool(meta=registry.meta_for("ensp_console_send_lines"))
def ensp_console_send_lines(vm: str, lines: list[str], settle: float = 0.45) -> str:
    """Send a block of console commands, one per line.

    Blank lines and lines starting with ``#`` are skipped but still add a pause,
    which paces long VRP configuration blocks. Pair this with
    ensp_console_screen to check the result.

    Args:
        vm: device / VM name.
        lines: the commands, in order.
        settle: seconds to wait after each line.
    """
    try:
        sent = console.send_lines(vm, lines, settle=settle)
    except (vbox.VBoxError, console.ConsoleError) as exc:
        return _fail("device_not_running", f"failed while sending to {vm}: {exc}",
                     hint="Is the machine running? ensp_list_devices says so.")
    return f"sent {sent} of {len(lines)} lines to {vm} (settle={settle}s); capture the screen to verify"


# --------------------------------------------------------------------------
# real VRP console tools
#
# The tools above drive the VirtualBox virtual screen, which for an eNSP AR
# device shows the Tiny Core Linux guest -- not the router CLI.  The router's
# own console is a plain-text TCP service on the guest (port 40000); these
# tools talk to it directly and therefore return exact text.
# --------------------------------------------------------------------------

@mcp.tool(meta=registry.meta_for("ensp_vrp_list_devices"))
def ensp_vrp_list_devices(prefix: str = "192.168.56.") -> str:
    """List eNSP guest devices reachable on the host-only subnet.

    A guest is a device whose ``agentd`` answers on TCP 56789; the VRP console
    itself listens on TCP 40000 of the same guest.

    Every row also says **who holds the console** right now. That is the
    question that decides whether a command can be run at all: the console
    serves one client at a time, and the eNSP client holds a session for as
    long as eNSP runs.

    Args:
        prefix: the host-only subnet prefix, e.g. "192.168.56.".
    """
    try:
        guests = agentd.guest_endpoints(prefix)
    except OSError as exc:
        return _fail("no_guests_reachable", f"{type(exc).__name__}: {exc}",
                     hint="The host-only adapter may be down.")
    occ = occupancy.occupancy_many(guests)
    rows = []
    for ip in guests:
        host, port = agentd.split_endpoint(ip, agentd.VRP_CONSOLE_PORT)
        # a NAT guest is addressed through the loopback: the console of
        # device n is 40000 + n and its control channel 56789 + n
        if port == agentd.VRP_CONSOLE_PORT:
            control = f"{host}:{agentd.AGENTD_CONTROL_PORT}"
        else:
            index = port - agentd.VRP_CONSOLE_PORT
            control = f"{host}:{agentd.AGENTD_CONTROL_PORT + index}"
        row = {
            "guest_ip": ip,
            "agentd_control": agentd.probe_endpoint(control, timeout=0.4),
            "vrp_console": agentd.probe_endpoint(ip, timeout=0.4),
        }
        row["console"] = occupancy.compact(occ.get(ip, {}))
        rows.append(row)
    return json.dumps({"ok": True, "count": len(rows), "devices": rows},
                      ensure_ascii=False, indent=2)


@mcp.tool(meta=registry.meta_for("ensp_vrp_command"))
def ensp_vrp_command(guest_ip: str, commands: list[str], wait: float = 1.5) -> str:
    """Run VRP commands on a device and return the exact text output.

    This is the real router CLI: the guest's VRP process serves a plain-text
    console on TCP 40000, the same channel the eNSP client itself uses. Output
    comes back verbatim -- no screenshots and no OCR.

    **This is the only supported way to read device state.**  The screen of an
    eNSP device is a bitmap of the guest's Linux console; it is never scraped
    and never used as a fallback.  If the console cannot be reached, or the
    device does not execute a command, the call reports ``ok: false`` instead
    of returning a picture or a banner.

    The console accepts one client at a time, so all commands run over a single
    short-lived session.  The session waits out the ``Press any key to get
    started`` banner a freshly booted device shows, and verifies that every
    command was echoed back before it reports success.

    When the eNSP client is holding the console, the call does not wait for a
    timeout it cannot win: it reports ``error_code: console_held_by_ensp_client``
    together with the process that holds it and what to do about it.

    Args:
        guest_ip: the device's address on the host-only subnet, e.g. "192.168.56.2".
        commands: the VRP commands to run, in order.
        wait: seconds to wait for each command's output.
    """
    if not commands:
        return _fail("no_commands_given", "no commands given",
                     hint="Pass at least one VRP command.")

    # Who has the console?  Answering this before the call is made is the
    # difference between a diagnosis and a timeout.
    occ = occupancy.occupancy(guest_ip)
    if occ.get("held_by_ensp_client") and not _console_answers(guest_ip):
        return _fail("console_held_by_ensp_client", occ.get("reason", ""),
                     hint=occ.get("hint", ""), action=occ.get("action"),
                     occupancy=occ)

    try:
        with agentd.VrpConsole(guest_ip) as con:
            output = con.run(commands, wait=wait)
            banner = con.banner
    except agentd.VrpConsoleError as exc:
        message = str(exc)
        occ = occupancy.occupancy(guest_ip)
        if occ.get("held_by_ensp_client"):
            return _fail("console_held_by_ensp_client", occ.get("reason", ""),
                         hint=occ.get("hint", ""), action=occ.get("action"),
                         occupancy=occ, detail=message)
        if "cannot reach VRP console" in message:
            return _fail("device_unreachable", message,
                         hint=("The device did not answer on its console port. "
                               "Start it (ensp_start_devices) and give the guest "
                               "a minute to boot."),
                         occupancy=occ)
        return _fail("console_no_echo", message,
                     hint=("The console accepted the connection but did not echo "
                           "the command, so the device is not accepting input. "
                           "It may still be booting, or another client may hold "
                           "the session."),
                     occupancy=occ)
    return json.dumps({
        "ok": True,
        "guest_ip": guest_ip,
        "commands": commands,
        "banner": banner.strip(),
        "output": output,
    }, ensure_ascii=False, indent=2)


@mcp.tool(meta=registry.meta_for("ensp_vrp_agent_handshake"))
def ensp_vrp_agent_handshake(guest_ip: str, timeout: float = 10.0) -> str:
    """Run the guest agentd control handshake and report what the device says.

    The guest listens on TCP 56789 and sends a 44-byte "boot" frame; answering
    with code 1 releases its start state and it then reports prepare / DEV0 /
    DEV1 / DEV2. Useful to confirm a device is actually up.

    The control socket serves one client at a time, so a device that the eNSP
    client is already driving will not answer; the returned error says so.

    Args:
        guest_ip: the device's address on the host-only subnet.
        timeout: seconds to wait for the guest's frames.
    """
    try:
        result = agentd.control_handshake(guest_ip, timeout=timeout)
    except agentd.AgentdError as exc:
        message = str(exc)
        if "may already hold" in message or "timed out" in message:
            return _fail("agentd_busy", message,
                         hint=("The eNSP client holds this device's agentd "
                               "control session; the socket serves one client "
                               "at a time."))
        return _fail("device_unreachable", message,
                     hint="Is the device up? ensp_vrp_list_devices lists them.")
    result["ok"] = True
    return json.dumps(result, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# the registry itself
# --------------------------------------------------------------------------

@mcp.tool(meta=registry.meta_for("ensp_tool_registry"))
def ensp_tool_registry(name: str = "") -> str:
    """Every tool, its preconditions and the errors it can raise.

    The same data is in the MCP metadata of every tool (``meta.requires``,
    ``meta.error_codes``); this tool hands it over in one piece, so an agent
    can check a precondition before it calls.

    Args:
        name: only describe this tool.
    """
    if name:
        spec = registry.get(name)
        if spec is None:
            return _fail("unknown_tool", f"no tool named {name!r}",
                         hint="Call it without a name to get the whole list.")
        return json.dumps(spec.as_dict(), ensure_ascii=False, indent=2)
    return json.dumps(registry.as_dict(), ensure_ascii=False, indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
