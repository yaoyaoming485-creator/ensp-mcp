"""Background supervisor for a lab.

The guests power themselves off as soon as the agentd control socket closes,
so the drivers have to outlive the command that started the lab.  This module
runs them in a detached background process and records its pid, so
``enspctl lab up`` can return immediately and the lab stays up.

    python -m ensp_mcp.supervise <lab-root> [--verbose]

The supervisor re-creates the pipe servers and media servers, starts the
machines, and keeps the control connections alive.  ``enspctl lab down`` stops
it.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import agentd
from . import lab as labmod
from .host import HostDevice, HostOptions, MediaHub


def pid_file(root: Path) -> Path:
    return root / "enspctl-supervisor.pid"


def log_file(root: Path) -> Path:
    return root / "enspctl-supervisor.log"


def process_alive(pid: int) -> bool:
    """Is ``pid`` a live process?

    ``os.kill(pid, 0)`` cannot be used for this on Windows: CPython rejects
    the signal with ``WinError 87`` (and other versions turn it into a
    terminate), so the check would always answer "not running" - or worse,
    kill the process it was asked about.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True,
                                 timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        low = out.lower()
        if "no tasks" in low or low.startswith("info:"):
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def supervisor_pids(root: Path) -> list[int]:
    """Every process that runs the supervisor for ``root``.

    The supervisor is started as a detached launcher that spawns the real
    interpreter, and the two pids do not match, so the pid file alone is not
    enough to find them all.
    """
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*ensp_mcp.supervise*' -and "
        f"$_.CommandLine -like '*{root}*' }} | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.split() if x.strip().isdigit()]


def is_running(root: Path) -> bool:
    pf = pid_file(root)
    if not pf.is_file():
        return False
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    return process_alive(pid)


def spawn(root: str | Path, verbose: bool = True) -> int:
    """Start the supervisor as a detached background process."""
    root = Path(root).resolve()
    if is_running(root):
        print(f"lab supervisor already running (pid {pid_file(root).read_text().strip()})")
        return 0
    root.mkdir(parents=True, exist_ok=True)
    log = log_file(root)
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    with log.open("a", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "ensp_mcp.supervise", str(root),
             "--foreground", *(["--quiet"] if not verbose else [])],
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, creationflags=_DETACHED,
        )
    pid_file(root).write_text(str(proc.pid), encoding="utf-8")
    print(f"lab supervisor started (pid {proc.pid}, log {log})")
    return 0


def wait_until_up(root: Path, deadline_seconds: float = 300.0,
                  poll: float = 3.0) -> tuple[bool, list[dict]]:
    """Wait until every device answers on its VRP console port.

    Returns (ready, rows) where rows carry each device's state.
    """
    from . import lab as labmod
    lab = labmod.Lab.load(root)
    end = time.time() + deadline_seconds
    rows: list[dict] = []
    while time.time() < end:
        rows = lab.status().get("devices", [])
        if rows and all(r.get("vrp") for r in rows):
            return True, rows
        time.sleep(poll)
    return False, rows


_DETACHED = 0
if os.name == "nt":
    _DETACHED = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))


def stop(root: str | Path) -> int:
    """Stop the supervisor (and every process that belongs to it)."""
    root = Path(root)
    pf = pid_file(root)
    pids: list[int] = []
    if pf.is_file():
        try:
            pids.append(int(pf.read_text(encoding="utf-8").strip()))
        except (ValueError, OSError):
            pass
    # the detached launcher and the interpreter it spawned are two processes
    pids.extend(p for p in supervisor_pids(root) if p not in pids)
    for pid in pids:
        if not process_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True)
    # a launcher that outlived its worker would keep the pipes open
    time.sleep(0.5)
    for pid in supervisor_pids(root):
        if process_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True)
    pf.unlink(missing_ok=True)
    if pids:
        print(f"supervisor {', '.join(str(p) for p in pids)} stopped")
    else:
        print("no supervisor recorded")
    return 0


def run_foreground(root: Path, verbose: bool = True) -> int:
    lab = labmod.Lab.load(root)
    pid_file(root).write_text(str(os.getpid()), encoding="utf-8")

    devs: list[HostDevice] = []
    hub = MediaHub()
    for d in lab.devices:
        peer_links = lab.peer_links(d)
        opts = HostOptions(
            guest_ip=d.ip, pipe_path=d.pipe, media_port=d.media_port,
            host_ip=lab.host_ip, device_id=d.device_id.encode("latin-1"),
            verbose=verbose, peers=lab.relay_links(d), hub=hub,
            peer_ssids=[ssid.encode("latin-1") for _ip, _port, ssid in peer_links.values()],
            peer_infos=[(ip, ssid.encode("latin-1")) for ip, _port, ssid in peer_links.values()],
        )
        devs.append(HostDevice(opts=opts, log_prefix=f"{d.name}: ")
                    .start(wait_ready=False))
    print("drivers ready; starting machines", flush=True)
    lab.start_vms()

    # A supervisor whose devices are gone has nothing left to drive: the
    # guests power themselves off when the control socket closes, so a
    # supervisor that keeps running would only leak processes and hold the
    # named pipes.
    state = {"seen_up": False, "gone_since": None}

    def devices_up() -> int:
        up = 0
        for d in lab.devices:
            ports = agentd.probe_guest_ports(
                d.ip, (agentd.AGENTD_CONTROL_PORT,), timeout=0.3)
            if ports.get(agentd.AGENTD_CONTROL_PORT):
                up += 1
        return up

    def all_gone() -> bool:
        """Have all the devices been gone for a while?"""
        if devices_up():
            state["seen_up"] = True
            state["gone_since"] = None
            return False
        if not state["seen_up"]:
            return False
        if state["gone_since"] is None:
            state["gone_since"] = time.time()
            return False
        return time.time() - state["gone_since"] > 60

    # Wait for the devices, but keep an eye on them: a device that dies while
    # the supervisor is still waiting for DEV2 must not strand it here.
    #
    # A guest that loses the control handshake aborts its agent and powers
    # itself off, so such a device is simply given another try: the machine
    # is power-cycled and the drivers redo the handshake.
    attempts = {d.log_prefix: 0 for d in devs}
    deadline = time.time() + 300
    while time.time() < deadline:
        if all(d.ready.is_set() for d in devs):
            break
        # A guest that lost the control handshake aborts its agent and powers
        # itself off, so such a device is simply given another try: the
        # machine is power-cycled and the drivers redo the handshake.  This
        # comes before the "the devices are gone" check - a device that died
        # on us must not be mistaken for a lab that was switched off.
        restarting = False
        for dev, drv in zip(lab.devices, devs):
            if drv.ready.is_set():
                continue
            # A device that reports a failure, or that has made no progress
            # for a while, is given another try: the machine is power-cycled
            # and the drivers redo the handshake.
            stale = drv.last_try and (time.time() - drv.last_try) > 45
            if not drv.failed and not stale:
                continue
            if attempts[drv.log_prefix] >= 5:
                continue
            attempts[drv.log_prefix] += 1
            reason = drv.failed or "no progress"
            print(f"  {dev.name}: {reason} - restarting the device "
                  f"(attempt {attempts[drv.log_prefix]}/5)", flush=True)
            drv.reset()
            try:
                lab.restart_vm(dev)
            except Exception as exc:  # noqa: BLE001
                print(f"  {dev.name}: restart failed: {exc}", flush=True)
            restarting = True
        if restarting:
            time.sleep(2)
            continue
        if all_gone():
            break
        time.sleep(1.0)
    for d in devs:
        print(f"  {d.log_prefix.strip()} "
              f"{'started' if d.started else (d.failed or 'not started')}",
              flush=True)
    print("lab is up", flush=True)

    stop_flag = {"now": False}

    def _term(_sig, _frm):
        stop_flag["now"] = True

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    last_sweep = 0.0
    while not stop_flag["now"]:
        time.sleep(1.0)
        if all_gone():
            print("the devices are gone - the supervisor exits", flush=True)
            break
        # A device that was up and is gone now is started again: the guest's
        # VRP can crash and take the whole guest down with it, and a lab
        # that silently loses a device is worse than one that restarts it.
        now = time.time()
        if now - last_sweep < 10.0:
            continue
        last_sweep = now
        for dev, drv in zip(lab.devices, devs):
            if not drv.ready.is_set():
                continue
            ports = agentd.probe_guest_ports(
                dev.ip, (agentd.AGENTD_CONTROL_PORT,), timeout=0.3)
            if ports.get(agentd.AGENTD_CONTROL_PORT):
                continue
            attempts[drv.log_prefix] += 1
            if attempts[drv.log_prefix] > 5:
                print(f"  {dev.name}: the device keeps disappearing - "
                      f"giving up after {attempts[drv.log_prefix] - 1} restarts",
                      flush=True)
                drv.ready.clear()
                continue
            print(f"  {dev.name}: the device disappeared - restarting it "
                  f"(attempt {attempts[drv.log_prefix]}/5)", flush=True)
            drv.reset()
            try:
                lab.restart_vm(dev)
            except Exception as exc:  # noqa: BLE001
                print(f"  {dev.name}: restart failed: {exc}", flush=True)
    for d in devs:
        d.stop()
    # do not leave a pid file behind: the next "lab up" must not think a
    # supervisor is still running
    try:
        if pid_file(root).read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_file(root).unlink(missing_ok=True)
    except OSError:
        pass
    print("supervisor exiting", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ensp_mcp.supervise")
    ap.add_argument("root")
    ap.add_argument("--foreground", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root)
    if args.stop:
        return stop(root)
    if args.foreground:
        return run_foreground(root, verbose=not args.quiet)
    return spawn(root)


if __name__ == "__main__":
    sys.exit(main())
