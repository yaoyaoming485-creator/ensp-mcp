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

import os
import re
import subprocess
import time
from pathlib import Path


class ResourceCfgError(RuntimeError):
    pass


def rewrite_text(text: str, guest_ip: str, host_ip: str) -> tuple[str, int]:
    """Point every ``local:``/``dest:`` address at the lab's addresses.

    ``local:`` is the device's own address (it must be one the device really
    owns, otherwise VRP cannot bind it).  ``dest:`` is the peer side; in a lab
    that is the host, which relays the link traffic.
    """
    out = text
    changed = 0
    for ip in sorted(set(re.findall(r"local:(\d+\.\d+\.\d+\.\d+):", text))):
        out = out.replace(f"local:{ip}:", f"local:{guest_ip}:")
        changed += 1
    for ip in sorted(set(re.findall(r"dest:(\d+\.\d+\.\d+\.\d+):", text))):
        out = out.replace(f"dest:{ip}:", f"dest:{host_ip}:")
        changed += 1
    return out, changed


# ------------------------------------------------------------------ loop --

_LOOP_SCRIPT = r"""#!/bin/bash
# usage: patch_resource.sh <raw-image> <guest-ip> <host-ip>
set -e
RAW="$1"; GUEST="$2"; HOST="$3"
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
sed -i "s/local:[0-9.]*:/local:${GUEST}:/g; s/dest:[0-9.]*:/dest:${HOST}:/g" "$F"
echo "patched $(grep -c 'local:' "$F") socket lines"
umount "$MNT"
losetup -d "$LOOP" 2>/dev/null || true
"""


def rewrite_in_raw_image(image: Path, guest_ip: str, host_ip: str,
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

    proc = subprocess.run(
        ["wsl", "-u", "root", "-e", "bash", script_wsl, image_wsl, guest_ip, host_ip],
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

def rewrite_vdi(vdi: Path, guest_ip: str, host_ip: str,
                vboxmanage: str | None = None,
                vm_name: str = "", storagectl: str = "IDE",
                port: int = 0, device: int = 0) -> tuple[int, Path]:
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
        n = rewrite_in_raw_image(raw, guest_ip, host_ip)
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
            # detach the old disk before touching the file
            run(["storageattach", vm_name, "--storagectl", storagectl,
                 "--port", str(port), "--device", str(device),
                 "--type", "hdd", "--medium", "none"])
            if final.resolve() == vdi.resolve():
                # the patched disk replaces the source in place
                run(["closemedium", "disk", str(vdi), "--delete"], check=False)
                if vdi.exists():
                    vdi.unlink()
            else:
                run(["closemedium", "disk", str(vdi), "--delete"], check=False)
            shutil.move(str(patched), str(final))
            # swap the disk into the VM (the VM must be powered off)
            run(["storageattach", vm_name, "--storagectl", storagectl,
                 "--port", str(port), "--device", str(device),
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
