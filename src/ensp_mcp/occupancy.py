"""Who is holding a device's console right now.

The VRP console is a plain-text service the guest serves on TCP 40000, and it
**accepts one client at a time**.  The eNSP client opens such a session for
every device whose CLI tab is open, and it keeps the socket for as long as
eNSP itself runs: closing the console window does not release it, only
quitting the client does.

A second client -- this MCP server -- therefore connects, receives nothing and
times out, and the caller is left guessing whether the device is broken, the
guest is still booting, or somebody else got there first.  That guess costs a
lot of time in a lab where the eNSP GUI is open the whole time.

This module answers the question *before* the call is made.  It reads the
host's TCP table, finds the connections that end at the guest's console port
and resolves the process that owns them.

Windows is read with ``netstat -ano``; Linux is read from ``/proc/net/tcp``
and ``/proc/net/tcp6``.  Nothing here talks to the device, so it is safe to
call on a live lab.
"""

from __future__ import annotations

import os
import re
import socket
import struct
import subprocess
from dataclasses import dataclass, field

from . import agentd, vbox

#: the port the guest serves the VRP console on
CONSOLE_PORT = agentd.VRP_CONSOLE_PORT

#: the port the guest serves the agentd control channel on
CONTROL_PORT = agentd.AGENTD_CONTROL_PORT

#: eNSP processes that may hold a console session
ENSP_HOLDERS = ("ensp_client.exe", "ensp_vboxserver.exe")

_ESTABLISHED = {"ESTABLISHED", "01"}
_LISTENING = {"LISTENING", "0A"}


@dataclass
class Connection:
    """One row of the host's TCP table."""

    local_ip: str
    local_port: int
    remote_ip: str
    remote_port: int
    state: str
    pid: int | None = None
    process: str = ""

    @property
    def local(self) -> str:
        return f"{self.local_ip}:{self.local_port}"

    @property
    def remote(self) -> str:
        return f"{self.remote_ip}:{self.remote_port}"

    @property
    def established(self) -> bool:
        return self.state.upper() in _ESTABLISHED

    @property
    def held_by_ensp(self) -> bool:
        return self.process.lower() in ENSP_HOLDERS

    def as_dict(self) -> dict:
        return {
            "local": self.local,
            "remote": self.remote,
            "state": self.state,
            "pid": self.pid,
            "process": self.process,
            "held_by_ensp": self.held_by_ensp,
        }


# --------------------------------------------------------------------------
# the TCP table
# --------------------------------------------------------------------------

def _split_addr(text: str) -> tuple[str, int] | None:
    """Split ``"1.2.3.4:80"`` / ``"[::1]:80"`` into ``(host, port)``."""
    text = text.strip()
    if not text:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return None
        host = text[1:end]
        rest = text[end + 1:]
        if not rest.startswith(":"):
            return None
        port = rest[1:]
    else:
        host, _, port = text.rpartition(":")
        if not host:
            return None
    if not port.isdigit():
        return None
    return host, int(port)


def _netstat_connections() -> list[Connection]:
    """Read the TCP table with ``netstat -ano`` (Windows)."""
    try:
        res = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=vbox.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[Connection] = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() != "TCP":
            continue
        local = _split_addr(parts[1])
        remote = _split_addr(parts[2])
        if not local or not remote:
            continue
        state = parts[3]
        pid: int | None = None
        if len(parts) >= 5 and parts[4].isdigit():
            pid = int(parts[4])
        out.append(Connection(local[0], local[1], remote[0], remote[1], state, pid))
    return out


def _hex_addr(text: str) -> tuple[str, int]:
    """Decode a ``/proc/net/tcp`` address: little-endian hex IP and port."""
    addr, _, port = text.partition(":")
    try:
        port_no = int(port, 16)
    except ValueError:
        port_no = 0
    raw = bytes.fromhex(addr)
    if len(raw) == 4:
        ip = socket.inet_ntoa(raw[::-1])
    elif len(raw) == 16:
        # /proc stores IPv6 as four little-endian 32-bit words
        words = struct.unpack("<4I", raw)
        ip = socket.inet_ntop(socket.AF_INET6, struct.pack(">4I", *words))
    else:
        ip = addr
    return ip, port_no


def _proc_connections() -> list[Connection]:
    """Read the TCP table from ``/proc/net/tcp`` (Linux)."""
    inodes: dict[str, int] = {}
    for base in ("/proc",):
        try:
            pids = [n for n in os.listdir(base) if n.isdigit()]
        except OSError:
            pids = []
        for pid in pids:
            fddir = os.path.join(base, pid, "fd")
            try:
                fds = os.listdir(fddir)
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(os.path.join(fddir, fd))
                except OSError:
                    continue
                if target.startswith("socket:["):
                    inodes[target[8:-1]] = int(pid)
    out: list[Connection] = []
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r", encoding="ascii", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            cols = line.split()
            if len(cols) < 10:
                continue
            local = _hex_addr(cols[1])
            remote = _hex_addr(cols[2])
            state = cols[3]
            inode = cols[9]
            out.append(Connection(local[0], local[1], remote[0], remote[1],
                                  state, inodes.get(inode)))
    return out


def tcp_connections() -> list[Connection]:
    """Every TCP connection the host currently has."""
    if os.name == "nt":
        rows = _netstat_connections()
    else:
        rows = _proc_connections()
    names = vbox.process_names()
    for row in rows:
        if row.pid is not None:
            row.process = names.get(row.pid, "")
    return rows


# --------------------------------------------------------------------------
# the question
# --------------------------------------------------------------------------

def console_holders(guest_ip: str, port: int = CONSOLE_PORT) -> list[Connection]:
    """The connections that currently hold a device's console.

    A device behind NAT is reached through a forwarded port on the loopback,
    so the holder's *remote* address is the loopback and the port carries the
    device index.  Both shapes are matched: the guest's own address, and the
    loopback with the console port of that device.
    """
    return _holders_from(tcp_connections(), guest_ip, port)


def _holders_from(rows: list[Connection], guest_ip: str,
                  port: int = CONSOLE_PORT) -> list[Connection]:
    """Pick the holders of one device out of an already-read TCP table.

    Only the device's **own** console port may match.  Matching "any address
    on the host-only subnet" would blame every device for every other
    device's session, which is worse than saying nothing.
    """
    host, _ = agentd.split_endpoint(guest_ip, port)
    index = None
    if ":" in guest_ip:
        _, _, tail = guest_ip.rpartition(":")
        if tail.isdigit():
            index = int(tail) - CONSOLE_PORT
    out: list[Connection] = []
    for row in rows:
        if not row.established:
            continue
        if row.remote_port != port:
            continue
        if row.remote_ip == host:
            out.append(row)
        elif index is not None and row.remote_ip in ("127.0.0.1", "::1") \
                and row.remote_port == CONSOLE_PORT + index:
            out.append(row)
    return out


def _describe(guest_ip: str, holders: list[Connection]) -> dict:
    by_ensp = [h for h in holders if h.held_by_ensp]
    by_other = [h for h in holders if not h.held_by_ensp]
    result: dict = {
        "guest_ip": guest_ip,
        "occupied": bool(holders),
        "held_by_ensp_client": bool(by_ensp),
        "held_by_other_client": bool(by_other),
        "holders": [h.as_dict() for h in holders],
        "checked": "host TCP table",
    }
    if by_ensp:
        names = sorted({h.process or "ensp" for h in by_ensp})
        result["reason"] = (
            "the eNSP client holds this device's console session ("
            + ", ".join(names) + "); the VRP console serves one client at a time"
        )
        result["hint"] = (
            "Close the eNSP client (closing the console tab is not enough - "
            "the socket stays open until eNSP exits), or run the command "
            "through the eNSP console tab instead."
        )
        result["action"] = {
            "kind": "stop_process",
            "processes": names,
            "note": "quitting eNSP releases every console session",
        }
    elif by_other:
        names = sorted({h.process or f"pid {h.pid}" for h in by_other})
        result["reason"] = (
            "another client holds this device's console session ("
            + ", ".join(names) + ")"
        )
        result["hint"] = (
            "Wait for that session to end, or use a different device. A "
            "console session is released when its client disconnects."
        )
        result["action"] = {"kind": "wait", "clients": names}
    else:
        result["reason"] = "the console is free"
    return result


def occupancy(guest_ip: str, port: int = CONSOLE_PORT) -> dict:
    """Is this device's console free, and if not, who has it?

    The answer is the whole point of the module: the caller can say *why* the
    device did not answer, and what to do about it, instead of timing out.
    """
    holders = console_holders(guest_ip, port)
    result = _describe(guest_ip, holders)
    result["port"] = port
    return result


def compact(occupancy: dict) -> dict:
    """The short form, for a table with one row per device.

    The full form repeats the same holders for every device of a list, which
    turns a readable answer into a wall of text.
    """
    return {
        "occupied": occupancy.get("occupied", False),
        "held_by_ensp_client": occupancy.get("held_by_ensp_client", False),
        "held_by_other_client": occupancy.get("held_by_other_client", False),
        "reason": occupancy.get("reason", ""),
        "holders": occupancy.get("holders", []),
    }


def occupancy_many(guest_ips: list[str], port: int = CONSOLE_PORT) -> dict[str, dict]:
    """The occupancy of several devices, from a single read of the TCP table."""
    rows = tcp_connections()
    out: dict[str, dict] = {}
    for ip in guest_ips:
        result = _describe(ip, _holders_from(rows, ip, port))
        result["port"] = port
        out[ip] = result
    return out


def summarize() -> dict:
    """Every console session the host currently holds, grouped by device.

    This is the *global* view: it names the devices whose console the eNSP
    client is sitting on, without having to know their addresses up front.
    """
    rows = [r for r in tcp_connections()
            if r.established and r.remote_port >= CONSOLE_PORT
            and r.remote_port < CONSOLE_PORT + 64]
    by_remote: dict[str, list[Connection]] = {}
    for row in rows:
        by_remote.setdefault(row.remote, []).append(row)
    devices = []
    for remote, holders in sorted(by_remote.items()):
        by_ensp = [h for h in holders if h.held_by_ensp]
        devices.append({
            "device": remote,
            "occupied": True,
            "held_by_ensp_client": bool(by_ensp),
            "holders": [h.as_dict() for h in holders],
        })
    return {
        "occupied_devices": devices,
        "count": len(devices),
        "checked": "host TCP table",
    }
