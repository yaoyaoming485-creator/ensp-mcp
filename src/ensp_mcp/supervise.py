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

from . import lab as labmod
from .host import HostDevice, HostOptions


def pid_file(root: Path) -> Path:
    return root / "enspctl-supervisor.pid"


def log_file(root: Path) -> Path:
    return root / "enspctl-supervisor.log"


def is_running(root: Path) -> bool:
    pf = pid_file(root)
    if not pf.is_file():
        return False
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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
    root = Path(root)
    pf = pid_file(root)
    if not pf.is_file():
        print("no supervisor recorded")
        return 0
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        pf.unlink(missing_ok=True)
        return 0
    try:
        os.kill(pid, signal.SIGTERM if os.name != "nt" else signal.SIGTERM)
    except OSError:
        pass
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
    pf.unlink(missing_ok=True)
    print(f"supervisor {pid} stopped")
    return 0


def run_foreground(root: Path, verbose: bool = True) -> int:
    lab = labmod.Lab.load(root)
    pid_file(root).write_text(str(os.getpid()), encoding="utf-8")

    devs: list[HostDevice] = []
    for d in lab.devices:
        opts = HostOptions(
            guest_ip=d.ip, pipe_path=d.pipe, media_port=d.media_port,
            host_ip=lab.host_ip, device_id=d.device_id.encode("latin-1"),
            verbose=verbose,
        )
        devs.append(HostDevice(opts=opts, log_prefix=f"{d.name}: ")
                    .start(wait_ready=False))
    print("drivers ready; starting machines", flush=True)
    lab.start_vms()
    for d in devs:
        d.ready.wait(300)
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
    while not stop_flag["now"]:
        time.sleep(1.0)
    for d in devs:
        d.stop()
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
