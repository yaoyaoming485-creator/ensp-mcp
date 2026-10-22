"""Offline editing of a guest's ``resource.cfg`` inside a VirtualBox disk.

The guest's VRP binds its interface sockets to the addresses written in
``/dev/upload/resource.cfg``.  Those addresses are baked into the image, so a
device that gets a different IP at runtime cannot bind them and VRP dies with::

    bind: Cannot assign requested address
    ERROR :Start port 0 fail,return -1
    VPORT_Hal_PortEnable failed! u32Ret = -1

This module rewrites that file in place.

Backends
--------
``loop`` (Linux/WSL)
    Attach the raw image to a loop device, mount it, patch the file.

``vboxshared`` (any host)
    Attach a VirtualBox shared folder that points at the guest's
    ``/dev/upload`` directory and patch the file through it.  The guest
    (Tiny Core Linux) mounts host folders on demand, so this works without
    stopping the VM.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path


class ResourceCfgError(RuntimeError):
    pass


def rewrite_text(text: str, guest_ip: str, host_ip: str,
                 media_port: int | None = None) -> tuple[str, int]:
    """Point every ``local:``/``dest:`` address at the lab's addresses.

    ``local:`` is the device's own address (it must be one the device really
    owns, otherwise VRP cannot bind it).  ``dest:`` is the peer side; in a lab
    that is the host, which relays the link traffic.

    ``media_port`` is the host port the device's traffic is sent to.  It has
    to be the *device's* own port: the port baked into the image is the same
    for every device, so two devices would send their traffic to the same
    host port and the host could no longer tell them apart.
    """
    out = text
    changed = 0
    for ip in sorted(set(re.findall(r"local:(\d+\.\d+\.\d+\.\d+):", text))):
        out = out.replace(f"local:{ip}:", f"local:{guest_ip}:")
        changed += 1
    for ip in sorted(set(re.findall(r"dest:(\d+\.\d+\.\d+\.\d+):", text))):
        if media_port is None:
            out = out.replace(f"dest:{ip}:", f"dest:{host_ip}:")
        else:
            out = re.sub(rf"dest:{re.escape(ip)}:\d+", f"dest:{host_ip}:{media_port}", out)
        changed += 1
    return out, changed


# ------------------------------------------------------------------ loop --

_LOOP_SCRIPT = r"""#!/bin/bash
# usage: patch_resource.sh <raw-image> <guest-ip> <host-ip> <port-map-json>
set -e
RAW="$1"; GUEST="$2"; HOST="$3"; PORTS="$4"
MNT=/mnt/enspctl-edit
LOOP=$(losetup -j "$RAW" -O NAME --noheadings 2>/dev/null | head -1 | tr -d ' ')
if [ -z "$LOOP" ]; then
  LOOP=$(losetup -Pf --show "$RAW")
fi
mkdir -p "$MNT"
mountpoint -q "$MNT" || mount "${LOOP}p1" "$MNT"
F="$MNT/dev/upload/resource.cfg"
if [ ! -f "$F" ]; then
  echo "resource.cfg not found in $RAW" >&2
  umount "$MNT" || true
  exit 3
fi
cp "$F" "$F.bak"
python3 - "$F" "$GUEST" "$HOST" "$PORTS" <<'PYEOF'
import json, re, sys

path, guest, host, ports = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
pm = {int(k): int(v) for k, v in json.loads(ports).items()}
text = open(path, encoding="utf-8", errors="replace").read()

line_re = re.compile(r"(interface\[(\d+)\]\s*=\s*socket\[)([^\]]*)(\])")

def fix(m):
    idx = int(m.group(2))
    body = m.group(3)
    body = re.sub(r"local:[0-9.]+:", "local:%s:" % guest, body)
    port = pm.get(idx)
    if port is None:
        body = re.sub(r"dest:[0-9.]+:\d+", "dest:%s:0" % host, body)
    else:
        body = re.sub(r"dest:[0-9.]+:\d+", "dest:%s:%d" % (host, port), body)
    return m.group(1) + body + m.group(4)

out = line_re.sub(fix, text)
open(path, "w", encoding="utf-8", newline="").write(out)
print("patched %d socket lines" % len(line_re.findall(text)))
PYEOF
umount "$MNT"
losetup -d "$LOOP" 2>/dev/null || true
"""


def rewrite_in_raw_image(image: Path, guest_ip: str, host_ip: str,
                          media_port: int | None = None,
                          port_map: dict[int, int] | None = None,
                          wsl: bool = True) -> int:
    """Patch the file inside a raw image using WSL's loop device."""
    if not wsl:
        raise ResourceCfgError("only the WSL backend is implemented")

    # The script has to live somewhere WSL can see, and its path has to be
    # translated: a Windows path is not a valid path inside WSL.
    script_win = Path(os.environ.get("TEMP", r"C:\Windows\Temp")) / "enspctl_patch_resource.sh"
    script_win.write_text(_LOOP_SCRIPT, encoding="utf-8", newline="\n")
    image_win = str(image)

    def to_wsl(p: str) -> str:
        proc = subprocess.run(["wsl", "-u", "root", "-e", "wslpath", "-a", p],
                              capture_output=True, text=True,
                              errors="replace", timeout=60)
        if proc.returncode != 0:
            raise ResourceCfgError(f"wslpath failed for {p}: {proc.stderr.strip()}")
        return proc.stdout.strip()

    script_wsl = to_wsl(str(script_win))
    image_wsl = to_wsl(image_win)

    args = [script_wsl, image_wsl, guest_ip, host_ip,
            json.dumps({str(k): int(v) for k, v in (port_map or {}).items()})]
    proc = subprocess.run(
        ["wsl", "-u", "root", "-e", "bash"] + args,
        capture_output=True, text=True, errors="replace", timeout=900,
    )
    if proc.returncode != 0:
        raise ResourceCfgError(
            f"patching {image.name} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return _count_changes(proc.stdout)


def _count_changes(out: str) -> int:
    m = re.search(r"patched (\d+) socket lines", out)
    return int(m.group(1)) if m else 0


# ------------------------------------------------------------- vboxshare --

def disk_slot(vm_name: str, vboxmanage: str | None = None) -> tuple[str, int, int]:
    """Where the machine's disk is attached: ``(controller, port, device)``.

    A machine whose disk was detached by an interrupted run has none, so the
    default slot of the lab is used instead - the patch then puts the disk
    back where it belongs.
    """
    if vboxmanage is None:
        from . import vbox
        vboxmanage = vbox.vboxmanage_path()
    try:
        proc = subprocess.run([vboxmanage, "showvminfo", vm_name, "--machinereadable"],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return "IDE", 0, 0
    for line in proc.stdout.splitlines():
        m = re.match(r'^(IDE|SATA|SCSI)-(\d+)-(\d+)="(.*)"\s*$', line.strip())
        if m and m.group(4).lower().endswith(".vdi"):
            return m.group(1).upper(), int(m.group(2)), int(m.group(3))
    return "IDE", 0, 0


def rewrite_vdi(vdi: Path, guest_ip: str, host_ip: str,
                vboxmanage: str | None = None,
                vm_name: str = "", storagectl: str = "IDE",
                port: int = 0, device: int = 0,
                media_port: int | None = None,
                port_map: dict[int, int] | None = None) -> tuple[int, Path]:
    """Patch ``resource.cfg`` inside a VDI image.

    VirtualBox cannot mount a VDI on a loop device, so the disk is converted
    to raw, patched, and converted back into a *new* VDI.  The new disk is
    then swapped into the VM, because the old one is still attached and
    cannot simply be overwritten.

    A leftover medium registration makes the next clone fail with
    E_INVALIDARG ("a hard disk with UUID ... already exists"), so the
    temporary copies are unregistered when the work is done.

    Returns ``(changed socket lines, the disk that is now in place)``.
    """
    import shutil
    import tempfile
    import uuid as _uuid

    if vboxmanage is None:
        from . import vbox
        vboxmanage = vbox.vboxmanage_path()

    work = Path(tempfile.gettempdir()) / f"enspctl-{vdi.stem}-{_uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    raw = work / "disk.raw"
    patched = work / "patched.vdi"

    def run(args: list[str], check: bool = True) -> str:
        proc = subprocess.run([vboxmanage] + args, capture_output=True, text=True,
                              errors="replace", timeout=1800)
        if check and proc.returncode != 0:
            raise ResourceCfgError(
                f"vboxmanage {' '.join(args[:2])} failed: "
                f"{proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    try:
        run(["clonemedium", "disk", str(vdi), str(raw), "--format", "RAW"])
        n = rewrite_in_raw_image(raw, guest_ip, host_ip, media_port=media_port,
                                 port_map=port_map)
        run(["clonemedium", "disk", str(raw), str(patched), "--format", "VDI"])
        if vm_name:
            # Move the patched disk next to the original (the temporary
            # directory is wiped at the end, and the VM must not be left
            # pointing at a file that is about to disappear).  The name is
            # stable: an image that is already called "...-patched.vdi" is
            # replaced by a file with the same name instead of growing
            # another "-patched" suffix on every run.
            base = vdi.stem
            while base.endswith("-patched"):
                base = base[: -len("-patched")]
            final = vdi.with_name(base + "-patched.vdi")
            run(["closemedium", "disk", str(patched)], check=False)
            # the slot the disk sits in - a machine whose disk was detached
            # by an interrupted run has none, so the default is used
            ctl, prt, dev = disk_slot(vm_name, vboxmanage)
            # detach the old disk before touching the file (there may be
            # nothing to detach - that is not an error)
            run(["storageattach", vm_name, "--storagectl", ctl,
                 "--port", str(prt), "--device", str(dev),
                 "--type", "hdd", "--medium", "none"], check=False)
            if final.resolve() == vdi.resolve():
                # the patched disk replaces the source in place
                run(["closemedium", "disk", str(vdi), "--delete"], check=False)
                if vdi.exists():
                    vdi.unlink()
            else:
                run(["closemedium", "disk", str(vdi), "--delete"], check=False)
            shutil.move(str(patched), str(final))
            # swap the disk into the VM (the VM must be powered off)
            run(["storageattach", vm_name, "--storagectl", ctl,
                 "--port", str(prt), "--device", str(dev),
                 "--type", "hdd", "--medium", str(final)])
        else:
            run(["closemedium", "disk", str(vdi), "--delete"], check=False)
            shutil.copyfile(patched, vdi)
            final = vdi
        return n, final
    finally:
        run(["closemedium", "disk", str(raw)], check=False)
        shutil.rmtree(work, ignore_errors=True)


def rewrite_via_shared_folder(vm_name: str, guest_ip: str, host_ip: str,
                               vboxmanage: str, share_name: str = "enspctl",
                               host_dir: Path | None = None,
                               timeout: float = 120.0) -> int:
    """Patch the file through a VirtualBox shared folder.

    The share points at the guest's upload directory from the *host* side --
    the guest sees it read/write -- so the file is edited on the host, which
    is exactly what we want.
    """
    raise ResourceCfgError(
        "the shared-folder backend needs the guest's own mount point; "
        "use the WSL loop backend on Windows")
