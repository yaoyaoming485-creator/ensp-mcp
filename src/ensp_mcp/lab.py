"""Turn a topology into a running lab.

A *lab* is a directory that owns everything needed to run a topology without
the eNSP client:

    <lab>/
      lab.json          the lab's manifest (devices, IPs, ports, VM names)
      lab.topo          the topology itself
      vms/<name>/       one VirtualBox machine per device

For each device the lab owns a VirtualBox VM that is:

* cloned from eNSP's own device image, so the guest really is an eNSP device;
* given a host-only NIC on the 192.168.56.0/24 segment, with the lab
  assigning each device a free address;
* given a COM2 host pipe in **client** mode, so VirtualBox dials the
  host-side pipe server we run (see ``agentd`` for why the roles must be
  inverted: a pipe VirtualBox creates only grants the host read access);
* given a ``resource.cfg`` whose interface sockets point at the addresses the
  lab actually handed out -- otherwise VRP exits with
  ``VPORT_Hal_PortEnable failed!``.

The lab is deliberately independent of the eNSP application: it can be brought
up next to a running eNSP, or on its own.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import agentd, topo, vbox

DEFAULT_SUBNET = "192.168.56"
DEFAULT_HOST_IP = "192.168.56.1"
DEFAULT_BASE_IP = 101
DEFAULT_MEDIA_PORT = 20002
DEFAULT_PIPE_PREFIX = "enspctl"

# eNSP's own AR device image, used as the base disk for new device VMs.
# The install keeps a base disk plus a *difference* disk; the difference disk
# is the one that actually carries the guest's /dev/upload contents (agentd,
# resource.cfg, the VRP binaries).
#
# `clonevm` can only clone a registered *machine*, never a bare disk file, so
# the base is cloned as the machine "AR_Base" -- that keeps the whole disk
# chain (base + difference) intact.
AR_MACHINE_CANDIDATES = ["AR_Base"]
AR_IMAGE_CANDIDATES = [
    Path(r"C:\Program Files\Huawei\eNSP\VBoxServer\AR_Base\AR_Base.vdi"),
    Path(r"C:\Program Files\Huawei\eNSP\vboxserver\devices\AR\AR\AR_Base.vdi"),
]


def _difference_disk(base: Path) -> Path:
    """Find the difference disk that sits on top of the base image.

    Cloning the base image alone would give a guest with no /dev/upload
    contents at all - no agentd, no resource.cfg, no VRP binaries.
    """
    snap_dir = base.parent / "Snapshots"
    if snap_dir.is_dir():
        diffs = sorted(snap_dir.glob("*.vdi"), key=lambda p: p.stat().st_size)
        if diffs:
            return diffs[0]
    return base


class LabError(RuntimeError):
    """Raised when a lab operation cannot be completed."""


@dataclass
class LabDevice:
    name: str
    model: str
    vm: str
    ip: str
    pipe: str
    media_port: int
    device_id: str
    com_port: int = 0


@dataclass
class Lab:
    name: str
    root: Path
    topo_path: Path
    devices: list[LabDevice] = field(default_factory=list)
    subnet: str = DEFAULT_SUBNET
    host_ip: str = DEFAULT_HOST_IP

    # ------------------------------------------------------------------ io --
    @property
    def manifest(self) -> Path:
        return self.root / "lab.json"

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = {
            "name": self.name,
            "subnet": self.subnet,
            "host_ip": self.host_ip,
            "topo": self.topo_path.name,
            "devices": [asdict(d) for d in self.devices],
        }
        self.manifest.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, root: str | Path) -> "Lab":
        root = Path(root)
        mf = root / "lab.json"
        if not mf.is_file():
            raise LabError(f"no lab.json in {root} - not a lab directory")
        data = json.loads(mf.read_text(encoding="utf-8"))
        devs = [LabDevice(**d) for d in data.get("devices", [])]
        return cls(
            name=data.get("name", root.name),
            root=root,
            topo_path=root / data.get("topo", "lab.topo"),
            devices=devs,
            subnet=data.get("subnet", DEFAULT_SUBNET),
            host_ip=data.get("host_ip", DEFAULT_HOST_IP),
        )

    def device(self, name: str) -> LabDevice:
        for d in self.devices:
            if d.name == name or d.vm == name:
                return d
        raise LabError(f"no device named {name!r} in lab {self.name!r}")

    # ------------------------------------------------------------- create --
    @classmethod
    def create(cls, name: str, root: str | Path, topo_file: str | Path,
               subnet: str = DEFAULT_SUBNET,
               host_ip: str = DEFAULT_HOST_IP,
               start_ip: int = DEFAULT_BASE_IP,
               media_port: int = DEFAULT_MEDIA_PORT) -> "Lab":
        """Create a lab from an existing .topo file."""
        root = Path(root)
        if (root / "lab.json").exists():
            raise LabError(f"{root} already contains a lab")
        root.mkdir(parents=True, exist_ok=True)

        topo_src = Path(topo_file)
        if not topo_src.is_file():
            raise LabError(f"topology not found: {topo_src}")
        topo_dst = root / "lab.topo"
        shutil.copyfile(topo_src, topo_dst)

        parsed = topo.read(topo_dst)
        if not parsed.get("loads_in_ensp", True):
            raise LabError(
                f"{topo_src} is not loadable by eNSP (encoding={parsed.get('encoding')}, "
                f"has_bom={parsed.get('has_bom')}) - rewrite it as UTF-8 without BOM")

        lab = cls(name=name, root=root, topo_path=topo_dst,
                  subnet=subnet, host_ip=host_ip)
        for i, dev in enumerate(parsed.get("devices", [])):
            vm = f"{name}-{dev['name']}"
            # the ssid is 37 bytes: the 36-character UUID plus a NUL
            uuid = (dev.get("id") or "").strip().upper()
            if len(uuid) != 36:
                uuid = uuid.ljust(36, "0")[:36]
            lab.devices.append(LabDevice(
                name=dev["name"],
                model=dev.get("model", "AR2220"),
                vm=vm,
                ip=f"{subnet}.{start_ip + i}",
                pipe=f"\\\\.\\pipe\\{DEFAULT_PIPE_PREFIX}-{name}-{dev['name']}",
                media_port=media_port + i,
                device_id=uuid + "\x00",
                com_port=dev.get("com_port", 0),
            ))
        lab.save()
        return lab

    # ------------------------------------------------------------- vms ----
    def base_machine(self) -> str:
        """The VirtualBox machine to clone device VMs from."""
        out = self._run(["list", "vms"], timeout=120)
        registered = set(re.findall(r'^"(.+?)"', out, re.M))
        for cand in AR_MACHINE_CANDIDATES:
            if cand in registered:
                return cand
        raise LabError(
            "eNSP's base machine was not found (looked for: "
            + ", ".join(AR_MACHINE_CANDIDATES)
            + "). Registered machines: " + ", ".join(sorted(registered)))

    def base_image(self) -> Path:
        for cand in AR_IMAGE_CANDIDATES:
            if cand.is_file():
                return _difference_disk(cand)
        raise LabError(
            "eNSP's AR device image was not found; looked at:\n  "
            + "\n  ".join(str(c) for c in AR_IMAGE_CANDIDATES))

    def vbox(self) -> str:
        return vbox.vboxmanage_path()

    def _run(self, args: list[str], timeout: float = 900.0) -> str:
        proc = subprocess.run([self.vbox()] + args, capture_output=True,
                              text=True, errors="replace", timeout=timeout)
        if proc.returncode != 0:
            raise LabError(f"vboxmanage {' '.join(args[:2])} failed:\n"
                           f"{proc.stdout}{proc.stderr}")
        return proc.stdout

    def vm_exists(self, dev: LabDevice) -> bool:
        out = self._run(["list", "vms"], timeout=120)
        return f'"{dev.vm}"' in out

    def create_vms(self) -> None:
        """Clone one VM per device out of eNSP's base machine."""
        base = self.base_machine()
        for dev in self.devices:
            if self.vm_exists(dev):
                print(f"  {dev.vm}: already exists, skipped")
                continue
            folder = self.root / "vms" / dev.name
            folder.mkdir(parents=True, exist_ok=True)
            print(f"  {dev.vm}: cloning from {base} ...")
            self._run([
                "clonevm", base, "--name", dev.vm, "--register",
                "--basefolder", str(folder),
            ])
            self._configure_vm(dev)

    def _configure_vm(self, dev: LabDevice) -> None:
        """Give the VM a host-only NIC and a COM2 pipe in client mode."""
        self._run(["modifyvm", dev.vm, "--memory", "256", "--cpus", "1"])
        self._run(["modifyvm", dev.vm, "--vram", "8"])
        self._run(["modifyvm", dev.vm, "--uart2", "0x2f8", "3"])
        self._run(["modifyvm", dev.vm, "--uart1", "off"])
        self._run(["modifyvm", dev.vm, "--nic1", "hostonly",
                   "--hostonlyadapter1", "VirtualBox Host-Only Ethernet Adapter"])
        # --uart-modeN cannot express hostMode="HostPipe", so patch the XML.
        self._patch_uart(dev)

    def _patch_uart(self, dev: LabDevice) -> None:
        cfg = self._run(["showvminfo", dev.vm, "--machinereadable"])
        # VirtualBox's machinereadable output is not case-consistent: it prints
        # "name=" but "CfgFile=".
        m = re.search(r'^[Cc]fg[Ff]ile="(.+)"', cfg, re.M)
        if not m:
            raise LabError(f"cannot find the .vbox path of {dev.vm}")
        path = Path(m.group(1))
        text = path.read_text(encoding="utf-8", errors="replace")
        pipe = dev.pipe.replace("\\", "\\\\")
        new_port = (f'<Port slot="1" enabled="true" IOBase="0x2f8" IRQ="3" '
                    f'server="false" path="{pipe}" hostMode="HostPipe"/>')
        text, n = re.subn(r'(?s)<Port slot="1"[^>]*/>', new_port, text)
        if n == 0:
            text = text.replace("</Hardware>", "  " + new_port + "\n  </Hardware>")
        path.write_text(text, encoding="utf-8", newline="\n")
        # make VirtualBox re-read the file
        self._run(["unregistervm", dev.vm])
        self._run(["registervm", str(path)])

    def device_disk(self, dev: LabDevice) -> Path | None:
        """Locate the device's disk image inside the lab directory.

        The disk that is actually attached to the machine wins: after a patch
        the lab directory can hold several images, and only one of them is the
        one the guest boots from.
        """
        attached = self._attached_disk(dev)
        if attached is not None and attached.is_file():
            return attached
        folder = self.root / "vms" / dev.name
        if not folder.is_dir():
            return None
        vdis = sorted(folder.rglob("*.vdi"), key=lambda p: p.stat().st_size)
        return vdis[-1] if vdis else None

    def _attached_disk(self, dev: LabDevice) -> Path | None:
        """The disk image the VM has attached, as VirtualBox reports it."""
        try:
            info = vbox.machine_readable(dev.vm)
        except Exception:  # noqa: BLE001
            return None
        for key, value in info.items():
            if not key.startswith(("SATA-", "IDE-", "SCSI-")):
                continue
            if not value.lower().endswith(".vdi"):
                continue
            path = Path(value)
            if not path.is_absolute():
                path = self.root / path
            return path
        return None

    # ------------------------------------------------------------- patch --
    def patch_marker(self, dev: LabDevice) -> Path:
        return self.root / "vms" / dev.name / ".enspctl-patched.json"

    def needs_patch(self, dev: LabDevice, disk: Path | None) -> bool:
        """Does this device's ``resource.cfg`` still point at the old addresses?

        Rewriting the image is expensive (a VDI -> raw -> VDI round trip), so
        the result is remembered next to the machine and the work is skipped
        when nothing changed.
        """
        marker = self.patch_marker(dev)
        if disk is None or not marker.is_file():
            return True
        try:
            state = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        try:
            size = disk.stat().st_size
        except OSError:
            return True
        return not (state.get("ip") == dev.ip
                    and state.get("host") == self.host_ip
                    and state.get("disk") == disk.name
                    and state.get("size") == size)

    def mark_patched(self, dev: LabDevice, disk: Path) -> None:
        marker = self.patch_marker(dev)
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = disk.stat().st_size
        except OSError:
            size = -1
        marker.write_text(json.dumps({
            "ip": dev.ip, "host": self.host_ip,
            "disk": disk.name, "size": size,
        }, indent=2), encoding="utf-8")

    def destroy_vms(self) -> None:
        for dev in self.devices:
            if not self.vm_exists(dev):
                continue
            print(f"  {dev.vm}: removing")
            self._run(["unregistervm", dev.vm, "--delete"], timeout=300)

    # ----------------------------------------------------------- runtime --
    def start_vms(self) -> None:
        for dev in self.devices:
            # do not ask showvminfo first: a machine started by another
            # process is locked to that process and the query is refused
            try:
                state = vbox.vm_state(dev.vm)
            except Exception:  # noqa: BLE001
                state = "unknown"
            if state == "running":
                print(f"  {dev.vm}: already running")
                continue
            print(f"  {dev.vm}: starting")
            try:
                self._run(["startvm", dev.vm, "--type", "headless"], timeout=300)
            except LabError as exc:
                # "already locked" means somebody else has it running
                if "lock" not in str(exc).lower():
                    raise
                print(f"  {dev.vm}: already locked by another process, skipped")

    def stop_vms(self) -> None:
        for dev in self.devices:
            try:
                state = vbox.vm_state(dev.vm)
            except Exception:  # noqa: BLE001
                state = "unknown"
            if state != "running":
                continue
            print(f"  {dev.vm}: stopping")
            try:
                self._run(["controlvm", dev.vm, "acpipowerbutton"], timeout=120)
            except LabError:
                pass
        time.sleep(3)
        for dev in self.devices:
            try:
                if vbox.vm_state(dev.vm) == "running":
                    self._run(["controlvm", dev.vm, "poweroff"], timeout=120)
            except Exception:  # noqa: BLE001
                pass

    # --------------------------------------------------------- diagnostics --
    def status(self) -> dict:
        """Report each device's state.

        The guest ports are the real liveness signal, and they can be probed
        from any process.  ``showvminfo`` cannot always be used here: a
        machine that another process started (the lab supervisor) is locked
        to that process, and a second query is refused with E_ACCESSDENIED.
        """
        rows = []
        for dev in self.devices:
            state = "unknown"
            try:
                state = vbox.vm_state(dev.vm)
            except Exception:  # noqa: BLE001
                pass
            ports = agentd.probe_guest_ports(dev.ip)
            alive = ports.get(agentd.AGENTD_CONTROL_PORT, False) or \
                ports.get(agentd.VRP_CONSOLE_PORT, False)
            if alive and state == "unknown":
                state = "running"
            rows.append({
                "device": dev.name,
                "vm": dev.vm,
                "ip": dev.ip,
                "state": state,
                "agentd": ports.get(agentd.AGENTD_CONTROL_PORT, False),
                "vrp": ports.get(agentd.VRP_CONSOLE_PORT, False),
            })
        return {
            "lab": self.name,
            "root": str(self.root),
            "subnet": self.subnet,
            "devices": rows,
        }

    def print_status(self) -> None:
        st = self.status()
        print(f"lab {st['lab']}  ({st['root']})")
        print(f"{'device':<10} {'vm':<22} {'ip':<16} {'state':<10} agentd  vrp")
        for r in st["devices"]:
            print(f"{r['device']:<10} {r['vm']:<22} {r['ip']:<16} {r['state']:<10} "
                  f"{'yes' if r['agentd'] else '-':<7} {'yes' if r['vrp'] else '-'}")
