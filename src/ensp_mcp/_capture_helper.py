# -*- coding: utf-8 -*-
"""Elevated helper for a packet capture.

Started by ``capture.elevated_capture`` through the shell's ``runas`` verb,
because the PktMon driver only talks to an elevated process.  The helper
takes the capture, writes the pcapng file and exits; the parent reads the
file afterwards.

It is deliberately standalone: it imports nothing from the package, because
it is run as a script by a path, not as a module of an installed package.
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000


def _run(cmd, timeout=300):
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=CREATE_NO_WINDOW)


def pktmon_capture(duration, out_path, filters=None):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    etl = out.with_suffix(".etl")

    warnings = []
    if filters:
        # a filter that cannot be installed must not cost the capture
        try:
            _run(["pktmon", "filter", "remove"], timeout=30)
        except Exception as exc:  # noqa: BLE001
            warnings.append("could not clear the pktmon filters: %s" % exc)
        for expr in filters:
            res = _run(["pktmon", "filter", "add"] + expr.split(), timeout=30)
            if res.returncode != 0:
                # this host refuses filter changes; the capture runs without it
                warnings.append("pktmon refused the filter %r (%s); the capture "
                               "runs without it"
                               % (expr, (res.stderr or res.stdout).strip()))
                break
    try:
        out.with_suffix(".warnings.txt").write_text(
            "\n".join(warnings), encoding="utf-8")
    except OSError:
        pass

    start = _run(["pktmon", "start", "--capture", "--pkt-size", "0",
                  "--comp", "nics", "--file-name", str(etl)], timeout=60)
    if start.returncode != 0:
        raise RuntimeError("pktmon start failed: %s"
                           % (start.stderr or start.stdout).strip())
    try:
        time.sleep(max(0.5, duration))
    finally:
        _run(["pktmon", "stop"], timeout=180)

    if not etl.is_file():
        raise RuntimeError("pktmon wrote no log file at %s" % etl)
    conv = _run(["pktmon", "etl2pcap", str(etl), "--out", str(out)], timeout=600)
    if conv.returncode != 0 or not out.is_file():
        raise RuntimeError("pktmon etl2pcap failed: %s"
                           % (conv.stderr or conv.stdout).strip())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ensp_mcp._capture_helper")
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--filter", action="append", default=[])
    args = ap.parse_args(argv)
    out = Path(args.out)
    # a marker, so the caller can tell "the helper never started" from
    # "the helper started and then failed"
    try:
        out.with_suffix(".started.txt").write_text(
            "pid=%s argv=%s\n" % (os.getpid(), sys.argv), encoding="utf-8")
    except OSError:
        pass
    try:
        path = pktmon_capture(args.duration, args.out, args.filter)
    except BaseException as exc:  # noqa: BLE001
        # the parent has to be able to tell success from failure
        try:
            out.with_suffix(".error.txt").write_text(
                "%s: %s" % (type(exc).__name__, exc), encoding="utf-8")
        except OSError:
            pass
        print("CAPTURE FAILED: %s" % exc, file=sys.stderr)
        return 1
    try:
        out.with_suffix(".started.txt").unlink(missing_ok=True)
    except OSError:
        pass
    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
