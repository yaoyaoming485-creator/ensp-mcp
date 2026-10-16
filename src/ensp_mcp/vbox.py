"""VirtualBox and eNSP discovery plus the low-level device console primitives.

Reverse-engineered facts this module encodes
-------------------------------------------
* every eNSP device is a VirtualBox VM whose **name equals the device name**
  used in the topology (``R1``, ``LSW1``, ...);
* the device console is the VM's **virtual screen**, a 720x400 VGA text mode
  (80x25).  eNSP itself shows it through VRDP;
* therefore the console is both readable and writable from the command line:

  - read  -- ``VBoxManage controlvm <vm> screenshotpng <file>``
  - write -- ``VBoxManage controlvm <vm> keyboardputstring`` /
    ``keyboardputscancode``

* the VM's serial port is **not** the console.  Measured on a booting AR clone,
  ``\\\\.\\pipe\\<name>`` carries a fixed 4-byte heartbeat frame once per second
  and never any ASCII.  An earlier version of this project drove that pipe; it
  cannot work.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Matched by exact file name. A prefix match would make this server detect
# itself: it normally runs as ``ensp-mcp.exe``, which starts with "ensp".
ENSP_PROCESS_NAMES = frozenset(
    {
        "ensp_client.exe",
        "ensp_vboxserver.exe",
        "ensp_router.exe",
        "ensp_switch.exe",
        "ensp_firewall.exe",
        "ensp_pcclient.exe",
        "ensp_pcserver.exe",
    }
)
VBOX_PROCESS_NAMES = frozenset(
    {"vboxheadless.exe", "vboxsvc.exe", "vboxsds.exe", "virtualbox.exe"}
)


class VBoxError(RuntimeError):
    pass


def _run(args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise VBoxError(f"executable not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VBoxError(f"{args[0]} timed out after {timeout}s") from exc


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def find_vboxmanage() -> str:
    candidates: list[str] = []
    if os.name == "nt":
        try:
            import winreg

            for hive, key in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Oracle\VirtualBox"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Oracle\VirtualBox"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        install_dir, _ = winreg.QueryValueEx(k, "InstallDir")
                        candidates.append(os.path.join(install_dir, "VBoxManage.exe"))
                except OSError:
                    pass
        except ImportError:
            pass
        candidates.append(r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe")
    found = shutil.which("VBoxManage") or shutil.which("VBoxManage.exe")
    if found:
        candidates.append(found)
    for c in candidates:
        if c and Path(c).is_file():
            return c
    raise VBoxError(
        "VBoxManage.exe not found. Install Oracle VirtualBox or set VBOXMANAGE "
        "to its full path."
    )


def find_ensp_client() -> str | None:
    candidates: list[str] = []
    if os.name == "nt":
        try:
            import winreg

            for hive, key in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                (
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
                ),
            ):
                try:
                    with winreg.OpenKey(hive, key) as root:
                        for i in range(winreg.QueryInfoKey(root)[0]):
                            try:
                                with winreg.OpenKey(root, winreg.EnumKey(root, i)) as sub:
                                    name, _ = winreg.QueryValueEx(sub, "DisplayName")
                                    if "eNSP" in str(name):
                                        loc, _ = winreg.QueryValueEx(sub, "InstallLocation")
                                        candidates.append(os.path.join(str(loc), "eNSP_Client.exe"))
                            except OSError:
                                continue
                except OSError:
                    pass
        except ImportError:
            pass
        for root in (r"C:\Program Files\Huawei\eNSP", r"C:\Program Files (x86)\Huawei\eNSP"):
            candidates.append(os.path.join(root, "eNSP_Client.exe"))
    for c in candidates:
        if Path(c).is_file():
            return c
    return None


def vboxmanage_path() -> str:
    return os.environ.get("VBOXMANAGE") or find_vboxmanage()


# --------------------------------------------------------------------------
# VM inventory
# --------------------------------------------------------------------------

@dataclass
class VmInfo:
    name: str
    uuid: str
    state: str
    cfg_file: str

    @property
    def running(self) -> bool:
        return self.state == "running"


def list_vms() -> list[VmInfo]:
    vm = vboxmanage_path()
    out = _run([vm, "list", "vms"]).stdout
    vms: list[VmInfo] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        try:
            name = line[1 : line.index('"', 1)]
            uuid = line[line.rindex("{") + 1 : line.rindex("}")]
        except ValueError:
            continue
        state = "unknown"
        cfg = ""
        info = _run([vm, "showvminfo", name, "--machinereadable"])
        if info.returncode == 0:
            for ln in info.stdout.splitlines():
                if ln.startswith("VMState="):
                    state = ln.split("=", 1)[1].strip('"')
                elif ln.startswith("CfgFile="):
                    cfg = ln.split("=", 1)[1].strip('"')
        vms.append(VmInfo(name=name, uuid=uuid, state=state, cfg_file=cfg))
    return vms


def machine_readable(vm_name: str) -> dict[str, str]:
    vm = vboxmanage_path()
    res = _run([vm, "showvminfo", vm_name, "--machinereadable"])
    if res.returncode != 0:
        raise VBoxError(f"showvminfo {vm_name} failed: {res.stderr.strip() or res.stdout.strip()}")
    out: dict[str, str] = {}
    for line in res.stdout.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def vm_state(vm_name: str) -> str:
    return machine_readable(vm_name).get("VMState", "unknown")


def console_channels(vm_name: str) -> dict:
    """Describe how to reach a device's console.

    ``screen`` is the channel that works; ``serial_pipe`` is reported because it
    looks plausible and is a trap (it carries a heartbeat, not a console).
    """
    info = machine_readable(vm_name)
    serial = None
    for n in (1, 2, 3, 4):
        mode = info.get(f"uartmode{n}", "")
        if mode.startswith("server,"):
            serial = mode.split(",", 1)[1]
            break
    return {
        "vm": vm_name,
        "state": info.get("VMState", "unknown"),
        "screen": "VBoxManage controlvm screenshotpng + keyboardputstring/scancode",
        "vrdp_port": _vrde_port(info),
        "serial_pipe": serial,
        "serial_pipe_is_console": False,
    }


def _vrde_port(info: dict[str, str]) -> int | None:
    # With ``--vrde-port 0`` VirtualBox picks a free port; the effective value is
    # not exposed through --machinereadable, so read the listening socket instead.
    for key in ("vrdeport", "vrde_port"):
        raw = info.get(key)
        if raw and raw.isdigit() and int(raw) > 0:
            return int(raw)
    return None


# --------------------------------------------------------------------------
# console primitives
# --------------------------------------------------------------------------

def take_screenshot(vm_name: str, out_path: str | Path, display: int = 0, timeout: float = 30.0) -> Path:
    """Capture the VM's virtual screen to a PNG."""
    vm = vboxmanage_path()
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    res = _run([vm, "controlvm", vm_name, "screenshotpng", str(path), str(display)], timeout=timeout)
    if res.returncode != 0 or not path.is_file():
        detail = (res.stderr or res.stdout).strip()
        raise VBoxError(
            f"screenshot of {vm_name} failed"
            + (f": {detail}" if detail else " (is the VM running?)")
        )
    return path


def keyboard_put_string(vm_name: str, text: str) -> None:
    """Type literal text into the VM's virtual keyboard."""
    if not text:
        return
    vm = vboxmanage_path()
    # The guest keyboard is US layout; anything outside it cannot be typed.
    res = _run([vm, "controlvm", vm_name, "keyboardputstring", text])
    if res.returncode != 0:
        raise VBoxError(f"keyboardputstring failed: {(res.stderr or res.stdout).strip()}")


def keyboard_put_scancode(vm_name: str, *codes: str) -> None:
    """Send raw set-1 scancodes, e.g. ``1c 9c`` for Enter (make + break)."""
    flat = [c for group in codes for c in str(group).split()]
    if not flat:
        return
    vm = vboxmanage_path()
    res = _run([vm, "controlvm", vm_name, "keyboardputscancode", *flat])
    if res.returncode != 0:
        raise VBoxError(f"keyboardputscancode failed: {(res.stderr or res.stdout).strip()}")


# --------------------------------------------------------------------------
# eNSP launcher
# --------------------------------------------------------------------------

def launch_ensp(topo_path: str | Path | None = None, wait: float = 0.0) -> dict:
    """Start the eNSP GUI, optionally opening a topology file."""
    exe = find_ensp_client()
    if not exe:
        raise VBoxError("eNSP_Client.exe not found; is eNSP installed?")
    args = [exe]
    if topo_path is not None:
        path = Path(topo_path).resolve()
        if not path.is_file():
            raise VBoxError(f"topology file does not exist: {path}")
        args.append(str(path))
    proc = subprocess.Popen(args, close_fds=True)
    if wait:
        time.sleep(wait)
    return {"pid": proc.pid, "exe": exe, "topology": str(topo_path) if topo_path else None}


def ensp_processes() -> list[dict]:
    """List running eNSP processes (best effort, Windows only)."""
    return [p for p in _tasklist() if p["name"].lower() in ENSP_PROCESS_NAMES]


def vbox_processes() -> list[dict]:
    """List running VirtualBox processes that back eNSP devices."""
    return [p for p in _tasklist() if p["name"].lower() in VBOX_PROCESS_NAMES]


def _tasklist() -> list[dict]:
    if os.name != "nt":
        return []
    res = _run(["tasklist", "/FO", "CSV", "/NH"])
    if res.returncode != 0:
        return []
    out: list[dict] = []
    for line in res.stdout.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        out.append({"name": parts[0], "pid": int(parts[1]) if parts[1].isdigit() else None})
    return out
