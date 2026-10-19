"""ensp-mcp: an MCP server for Huawei eNSP.

Two capabilities, both reverse-engineered from an installed eNSP 1.3.00.100:

1. **Topology authoring** -- build, read and validate ``.topo`` files offline.
2. **Device console** -- read and drive any running device's console through the
   VirtualBox virtual screen (``screenshotpng`` + ``keyboardputstring``).

Tool names are prefixed ``ensp_`` so they are easy to allow-list.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer, Image

from . import console, models, topo, vbox

mcp = MCPServer("ensp")

DEFAULT_TOPO_DIR = Path(os.environ.get("ENSP_TOPO_DIR", Path.cwd()))


def _resolve_out(path: str | None, spec: dict) -> Path:
    if path:
        p = Path(path)
        return p if p.is_absolute() else (DEFAULT_TOPO_DIR / p)
    stem = str(spec.get("name") or "topology")
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in stem).strip("-") or "topology"
    return DEFAULT_TOPO_DIR / f"{safe}.topo"


# --------------------------------------------------------------------------
# topology tools
# --------------------------------------------------------------------------

@mcp.tool()
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


@mcp.tool()
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
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
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


@mcp.tool()
def ensp_read_topology(path: str) -> str:
    """Parse an existing ``.topo`` file into JSON (devices, links, labels, boxes).

    Args:
        path: path to the .topo file.
    """
    try:
        data = topo.read(path)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    data["ok"] = True
    data["path"] = str(path)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
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

@mcp.tool()
def ensp_status() -> str:
    """Report whether eNSP and VirtualBox are present, and what is running.

    Use this first to find out whether devices are up before touching a console.
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
    try:
        vms = vbox.list_vms()
        out["vm_total"] = len(vms)
        out["devices_running"] = [
            {"vm": v.name, "state": v.state} for v in vms if v.running
        ]
    except vbox.VBoxError as exc:
        out["vm_total"] = None
        out["error_vms"] = str(exc)
    return json.dumps(out, ensure_ascii=False, indent=2)


@mcp.tool()
def ensp_list_devices(running_only: bool = True) -> str:
    """List device VMs and how to reach each one's console.

    ``serial_pipe`` is reported for completeness but is **not** the console: it
    carries a fixed 4-byte heartbeat frame once per second. The working console
    is the virtual screen -- use ensp_console_screen / ensp_console_type.

    Args:
        running_only: only include VMs that are currently running.
    """
    try:
        vms = vbox.list_vms()
    except vbox.VBoxError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    if running_only:
        vms = [v for v in vms if v.running]
    rows = []
    for v in vms:
        try:
            ch = vbox.console_channels(v.name)
        except vbox.VBoxError as exc:
            ch = {"vm": v.name, "error": str(exc)}
        ch["uuid"] = v.uuid
        rows.append(ch)
    return json.dumps({"ok": True, "count": len(rows), "devices": rows}, ensure_ascii=False, indent=2)


@mcp.tool()
def ensp_open_topology(path: str, wait_seconds: float = 0.0) -> str:
    """Launch the eNSP GUI, optionally opening a topology file.

    Devices are not started by this call; in eNSP you still select the devices
    and use the start button. It opens the file so the topology is loaded.

    Args:
        path: path to a .topo file (empty string just launches eNSP).
        wait_seconds: seconds to wait after launching before returning.
    """
    try:
        info = vbox.launch_ensp(path or None, wait=wait_seconds)
    except vbox.VBoxError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    info["ok"] = True
    return json.dumps(info, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# console tools
# --------------------------------------------------------------------------

@mcp.tool(structured_output=False)
def ensp_console_screen(vm: str, save_path: str = "") -> list[Any]:
    """Capture a running device's console screen.

    The console is a 720x400 VGA text mode, so it comes back as an image. Read
    the attached PNG to see the device output.

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
        f"console screen of {vm} ({size} bytes): {path}",
        Image(path=str(path)),
    ]


@mcp.tool()
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
        return f"failed to type into {vm}: {exc}"
    return f"typed {len(text)} chars into {vm}" + (" + Enter" if enter else "")


@mcp.tool()
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
        return f"failed to send {key} to {vm}: {exc}"
    return f"sent {key} x{times} to {vm}"


@mcp.tool()
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
        return f"failed while sending to {vm}: {exc}"
    return f"sent {sent} of {len(lines)} lines to {vm} (settle={settle}s); capture the screen to verify"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
