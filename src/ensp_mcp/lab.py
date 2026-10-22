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
#: the guest's own ports: interface n sends its traffic from 10000 + n
LOCAL_PORT_BASE = 10000
#: the host port a device's interface n sends to: one port per
#: (device, interface), so the peer can tell the interfaces apart
INTERFACE_PORT_BASE = 30000
INTERFACES_PER_DEVICE = 16

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
    #: the topology's links: {"a": name, "b": name, "a_port": int, "b_port": int}
    links: list[dict] = field(default_factory=list)

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
            "links": self.links,
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
            links=list(data.get("links", [])),
        )

    def device(self, name: str) -> LabDevice:
        for d in self.devices:
            if d.name == name or d.vm == name:
                return d
        raise LabError(f"no device named {name!r} in lab {self.name!r}")

    def peers(self, dev: LabDevice) -> dict[int, tuple[str, int]]:
        """Where the traffic of each of ``dev``'s interfaces has to go.

        The guest sends the traffic of interface *n* from its local port
        ``10000 + n`` to the host; the host has to forward it to the peer
        device's local port for the interface the link connects.
        """
        return {i: (ip, port) for i, (ip, port, _ssid) in self.peer_links(dev).items()}

    def peer_links(self, dev: LabDevice) -> dict[int, tuple[str, int, str]]:
        """Like :meth:`peers`, but with the peer's ssid.

        The guest addresses its peer by the peer's ssid: the agent puts the
        traffic into the message queue that is named after the receiver, and
        the host routes the frames by the ssid they carry.  A device that is
        never told the ssid of its peer cannot address it at all - the
        interface comes up but nothing is ever delivered.
        """
        out: dict[int, tuple[str, int, str]] = {}
        for link in self.links:
            a, b = link.get("a"), link.get("b")
            if a == dev.name:
                other, mine, theirs = b, link.get("a_port"), link.get("b_port")
            elif b == dev.name:
                other, mine, theirs = a, link.get("b_port"), link.get("a_port")
            else:
                continue
            if other is None or mine is None or theirs is None:
                continue
            try:
                peer = self.device(other)
            except LabError:
                continue
            out[int(mine)] = (peer.ip, LOCAL_PORT_BASE + int(theirs), peer.device_id)
        return out

    def device_index(self, dev: LabDevice) -> int:
        for i, d in enumerate(self.devices):
            if d.name == dev.name:
                return i
        return 0

    def interface_ports(self, dev: LabDevice) -> dict[int, int]:
        """The host port each of the device's interfaces sends to.

        One port per (device, interface): the peer's VRP has to be able to
        tell the interfaces of the sender apart, and the eNSP host gives
        every device its own block of ports ("sockoffset").
        """
        base = INTERFACE_PORT_BASE + self.device_index(dev) * INTERFACES_PER_DEVICE
        return {n: base + n for n in range(INTERFACES_PER_DEVICE)}

    def relay_links(self, dev: LabDevice) -> dict[int, tuple[int, str, int]]:
        """What the relay needs: interface -> (host port, peer, peer port)."""
        ports = self.interface_ports(dev)
        peers = self.peer_links(dev)
        out: dict[int, tuple[int, str, int]] = {}
        for iface, (peer_ip, peer_port, _ssid) in peers.items():
            out[iface] = (ports.get(iface, INTERFACE_PORT_BASE + iface),
                          peer_ip, peer_port)
        return out

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
        for link in parsed.get("links", []):
            lab.links.append({
                "a": link.get("a"), "b": link.get("b"),
                "a_port": link.get("a_port"), "b_port": link.get("b_port"),
                "a_if": link.get("a_if"), "b_if": link.get("b_if"),
                "type": link.get("type"),
            })
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
        one the guest boots from.  A disk that does not live in this lab is
        ignored - a machine with the same name may still be registered from
        an earlier lab.
        """
        attached = self._attached_disk(dev)
        if attached is not None and attached.is_file():
            try:
                attached.relative_to(self.root)
            except ValueError:
                attached = None
            else:
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

    def needs_patch(self, dev: LabDevice, disk: Path | None,
                    dest_port: bool = True,
                    port_map: dict[int, int] | None = None) -> bool:
        """Does this device's ``resource.cfg`` still point at the old addresses?

        Rewriting the image is expensive (a VDI -> raw -> VDI round trip), so
        the result is remembered next to the machine and the work is skipped
        when nothing changed.  The marker also records *how* the file was
        written: a change in the rewrite itself has to force a new one.
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
        if bool(state.get("dest_port")) != bool(dest_port):
            return True
        if port_map is not None:
            if state.get("ports") != {str(k): v for k, v in port_map.items()}:
                return True
        return not (state.get("ip") == dev.ip
                    and state.get("host") == self.host_ip
                    and state.get("disk") == disk.name
                    and state.get("size") == size)

    def mark_patched(self, dev: LabDevice, disk: Path,
                     dest_port: bool = True,
                     port_map: dict[int, int] | None = None) -> None:
        marker = self.patch_marker(dev)
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = disk.stat().st_size
        except OSError:
            size = -1
        marker.write_text(json.dumps({
            "ip": dev.ip, "host": self.host_ip,
            "disk": disk.name, "size": size,
            "dest_port": bool(dest_port),
            "ports": {str(k): v for k, v in (port_map or {}).items()},
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

    def restart_vm(self, dev: LabDevice) -> bool:
        """Power-cycle one machine.

        A guest that loses the control handshake aborts its agent and powers
        itself off, so a restart is all it takes to give it another chance.
        """
        try:
            if vbox.vm_state(dev.vm) == "running":
                self._run(["controlvm", dev.vm, "poweroff"], timeout=120)
                time.sleep(2)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._run(["startvm", dev.vm, "--type", "headless"], timeout=300)
        except LabError as exc:
            if "lock" not in str(exc).lower():
                raise
            return False
        return True

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
            "supervisor": self.supervisor_state(),
            "devices": rows,
        }

    def supervisor_state(self) -> dict:
        """Is a supervisor driving this lab, and is it healthy?"""
        from . import supervise
        root = self.root
        pids = supervise.supervisor_pids(root)
        alive = [p for p in pids if supervise.process_alive(p)]
        return {
            "running": bool(alive),
            "pids": alive,
            "log": str(supervise.log_file(root)),
        }

    def print_status(self) -> None:
        st = self.status()
        print(f"lab {st['lab']}  ({st['root']})")
        sup = st.get("supervisor") or {}
        if sup.get("running"):
            print(f"supervisor: running (pid {', '.join(str(p) for p in sup['pids'])})")
        else:
            print("supervisor: not running")
        print(f"{'device':<10} {'vm':<22} {'ip':<16} {'state':<10} agentd  vrp")
        for r in st["devices"]:
            print(f"{r['device']:<10} {r['vm']:<22} {r['ip']:<16} {r['state']:<10} "
                  f"{'yes' if r['agentd'] else '-':<7} {'yes' if r['vrp'] else '-'}")
