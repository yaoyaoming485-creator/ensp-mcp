"""Packet capture and IP-header summarising.

A lab report about QoS has to show what the wire actually carried: the DSCP
in the IP header is the only place the mapping is visible, and a router's
own counters do not say how a packet looked *on the link*.  This module
captures the traffic and reads the IP header back out of it.

Two backends, in order of preference:

``pktmon``
    The Windows packet monitor.  It needs an elevated process, because it
    talks to the PktMon kernel driver; unelevated it answers
    *"cannot communicate with the PktMon driver. Access is denied."*
    It writes an ETL, which ``pktmon etl2pcap`` turns into a pcapng file.

``raw``
    A ``SIO_RCVALL`` raw socket.  This is the fallback for hosts without
    pktmon.  It needs the same elevation, and on some adapters Windows
    refuses the control code with ``WinError 10022``.

The pcap/pcapng parsing is pure Python and works on any platform, so a
capture taken elsewhere can still be summarised here.

The interesting output is the **IP header**: source, destination, protocol
and the DSCP/ToS byte.  :func:`summarize` turns a capture into that table.
"""

from __future__ import annotations

import ctypes
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

from . import vbox

# --------------------------------------------------------------- pcap magic --

_PCAPNG_MAGIC = b"\x0a\x0d\x0d\x0a"
_PCAP_MAGICS = {
    b"\xd4\xc3\xb2\xa1": ("<", 1_000_000),   # microseconds
    b"\xa1\xb2\xc3\xd4": (">", 1_000_000),
    b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000),  # nanoseconds
    b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000),
}

#: DSCP values worth naming; the rest is printed as a number
DSCP_NAMES = {
    0: "BE", 8: "CS1", 10: "AF12", 12: "AF13", 14: "AF14",
    16: "CS2", 18: "AF22", 20: "AF23", 22: "AF24",
    26: "AF31", 28: "AF32", 30: "AF33", 34: "AF41", 36: "AF42", 38: "AF43",
    40: "CS4", 46: "EF", 48: "CS5", 56: "CS6", 57: "CS7",
}

PROTO_NAMES = {1: "ICMP", 2: "IGMP", 6: "TCP", 17: "UDP", 47: "GRE",
               89: "OSPF", 103: "PIM", 112: "VRRP"}


class CaptureError(RuntimeError):
    """Raised when a capture cannot be taken."""


# ------------------------------------------------------------------ reading --

def _pcapng_packets(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield ``(link_type, packet)`` from a pcapng file."""
    off = 0
    n = len(data)
    link_types: list[int] = []
    while off + 12 <= n:
        btype = struct.unpack_from("<I", data, off)[0]
        blen = struct.unpack_from("<I", data, off + 4)[0]
        if blen < 12 or off + blen > n:
            break
        body = data[off + 8: off + blen - 4]
        if btype == 0x00000001:            # Interface Description Block
            link_types.append(struct.unpack_from("<H", body, 0)[0] if len(body) >= 2 else 1)
        elif btype == 0x00000006:          # Enhanced Packet Block
            if len(body) >= 20:
                iface, _hi, _lo, cap, _orig = struct.unpack_from("<IIIII", body, 0)
                lt = link_types[iface] if iface < len(link_types) else 1
                yield lt, body[20: 20 + cap]
        elif btype == 0x00000003:          # Simple Packet Block
            if len(body) >= 4:
                cap = struct.unpack_from("<I", body, 0)[0]
                lt = link_types[0] if link_types else 1
                yield lt, body[4: 4 + cap]
        off += blen


def _pcap_packets(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield ``(link_type, packet)`` from a classic pcap file."""
    if len(data) < 24:
        return
    magic = data[:4]
    endian, _nanos = _PCAP_MAGICS.get(magic, ("<", 1_000_000))
    link = struct.unpack_from(endian + "I", data, 20)[0]
    off = 24
    n = len(data)
    while off + 16 <= n:
        _ts, _us, cap, _orig = struct.unpack_from(endian + "IIII", data, off)
        off += 16
        if off + cap > n:
            break
        yield link, data[off: off + cap]
        off += cap


def iter_packets(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield ``(link_type, packet)`` from a pcap or pcapng buffer."""
    if data[:4] == _PCAPNG_MAGIC:
        yield from _pcapng_packets(data)
    elif data[:4] in _PCAP_MAGICS:
        yield from _pcap_packets(data)
    else:
        raise CaptureError(
            "not a pcap or pcapng file (magic bytes: "
            + " ".join(f"{b:02x}" for b in data[:4]) + ")")


# --------------------------------------------------------------- IP headers --

#: link types, and where the IP header sits behind them
LINKTYPE_NULL = 0
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 101
LINKTYPE_LOOP = 108
LINKTYPE_IPV4 = 228


def _ethernet_ip_offset(buf: bytes) -> int:
    """Where the IP header sits behind an Ethernet frame."""
    off = 14
    # 802.1Q / QinQ tags sit between the MAC addresses and the payload
    for _ in range(2):
        if off + 4 <= len(buf) and buf[off - 2:off] in (b"\x81\x00", b"\x88\xa8"):
            off += 4
    return off


def _ip_offset(link_type: int, buf: bytes) -> int | None:
    """The offset of the IP header for this link type, or None."""
    if link_type in (LINKTYPE_RAW, LINKTYPE_IPV4):
        return 0
    if link_type in (LINKTYPE_NULL, LINKTYPE_LOOP):
        return 4
    if link_type == LINKTYPE_ETHERNET:
        return _ethernet_ip_offset(buf)
    return None


def _ipv4_at(buf: bytes, i: int) -> dict | None:
    """Decode an IPv4 header at ``i``, or None when it is not one.

    The length check is what keeps a random byte pair from being read as an
    address: the total length the header declares has to fit in the bytes
    that were actually captured.
    """
    if i < 0 or i + 20 > len(buf):
        return None
    if buf[i] >> 4 != 4:
        return None
    ihl = (buf[i] & 0x0F) * 4
    if ihl < 20 or i + ihl > len(buf):
        return None
    tos = buf[i + 1]
    total = struct.unpack_from(">H", buf, i + 2)[0]
    if total < 20 or total > len(buf) - i:
        return None
    ident = struct.unpack_from(">H", buf, i + 4)[0]
    proto = buf[i + 9]
    if proto == 0:                      # reserved
        return None
    src = ".".join(str(b) for b in buf[i + 12: i + 16])
    dst = ".".join(str(b) for b in buf[i + 16: i + 20])
    if src == "0.0.0.0" or dst == "0.0.0.0":
        return None
    return {
        "version": 4, "tos": tos, "dscp": tos >> 2, "ecn": tos & 0x03,
        "total_length": total, "ident": ident, "proto": proto,
        "src": src, "dst": dst, "header_offset": i, "ihl": ihl,
    }


def ipv4_header(buf: bytes, link_type: int | None = None) -> dict | None:
    """Find and decode an IPv4 header inside ``buf``.

    When the link type is known, the header is looked for **only** where that
    link type puts it.  The alternative -- scanning the frame for a byte
    pattern that looks like an IP header -- invents addresses: a PPPoE or
    LLC frame carries an IP packet behind a header the link type does not
    describe, and a blind scan finds it at the wrong offset and reports a
    plausible-looking address that never existed.

    The scan is kept for captures whose link type is unknown, with the
    length check that makes it safe.
    """
    if link_type is not None:
        off = _ip_offset(link_type, buf)
        return _ipv4_at(buf, off) if off is not None else None
    for i in range(0, min(len(buf), 96)):
        if buf[i] >> 4 != 4:
            continue
        found = _ipv4_at(buf, i)
        if found is not None:
            return found
    return None


def ipv6_header(buf: bytes, link_type: int | None = None) -> dict | None:
    """Find and decode an IPv6 header inside ``buf``.

    Same rule as :func:`ipv4_header`: a known link type means one offset,
    not a scan.
    """
    if link_type is not None:
        off = _ip_offset(link_type, buf)
        return _ipv6_at(buf, off) if off is not None else None
    for i in range(0, min(len(buf), 96)):
        if buf[i] >> 4 != 6:
            continue
        found = _ipv6_at(buf, i)
        if found is not None:
            return found
    return None


def _ipv6_at(buf: bytes, i: int) -> dict | None:
    """Decode an IPv6 header at ``i``, or None when it is not one."""
    if i < 0 or i + 40 > len(buf):
        return None
    if buf[i] >> 4 != 6:
        return None
    tclass = buf[i + 1]
    flow = struct.unpack_from(">I", buf, i + 2)[0]
    plen = struct.unpack_from(">H", buf, i + 4)[0]
    if plen + 40 > len(buf) - i:
        return None
    nxt = buf[i + 6]
    if nxt == 0:                       # reserved
        return None
    src = socket.inet_ntop(socket.AF_INET6, buf[i + 8: i + 24])
    dst = socket.inet_ntop(socket.AF_INET6, buf[i + 24: i + 40])
    return {
        "version": 6, "dscp": tclass >> 2, "ecn": tclass & 0x03,
        "flow_label": flow & 0xFFFFF, "total_length": plen + 40,
        "ident": 0, "proto": nxt, "src": src, "dst": dst,
        "header_offset": i, "ihl": 40,
    }


def _transport_ports(buf: bytes, ip: dict) -> tuple[int | None, int | None]:
    """The source and destination port, when the transport header is there."""
    off = ip["header_offset"] + ip["ihl"]
    proto = ip["proto"]
    if proto not in (6, 17, 132) or off + 4 > len(buf):
        return None, None
    sport, dport = struct.unpack_from(">HH", buf, off)
    return sport, dport


def summarize(path: str | Path) -> dict:
    """Read a capture file and report what the IP headers say.

    The result is a per-flow table (source, destination, protocol, DSCP) with
    the counts, plus the DSCP distribution on its own.  That is the table a
    QoS lab report needs: it says what the link carried, not what the
    router believes it sent.
    """
    data = Path(path).read_bytes()
    flows: dict[tuple, dict] = {}
    dscp_counts: dict[int, int] = {}
    proto_counts: dict[int, int] = {}
    total = 0
    v4 = 0
    v6 = 0
    for lt, pkt in iter_packets(data):
        total += 1
        ip = ipv4_header(pkt, lt) or ipv6_header(pkt, lt)
        if ip is None:
            continue
        if ip["version"] == 4:
            v4 += 1
        else:
            v6 += 1
        sport, dport = _transport_ports(pkt, ip)
        key = (ip["src"], ip["dst"], ip["proto"], ip["dscp"], sport, dport)
        row = flows.get(key)
        if row is None:
            row = {
                "src": ip["src"], "dst": ip["dst"],
                "proto": PROTO_NAMES.get(ip["proto"], str(ip["proto"])),
                "proto_number": ip["proto"],
                "dscp": ip["dscp"],
                "dscp_name": DSCP_NAMES.get(ip["dscp"], "-"),
                "ecn": ip["ecn"],
                "src_port": sport, "dst_port": dport,
                "count": 0,
            }
            flows[key] = row
        row["count"] += 1
        dscp_counts[ip["dscp"]] = dscp_counts.get(ip["dscp"], 0) + 1
        proto_counts[ip["proto"]] = proto_counts.get(ip["proto"], 0) + 1

    rows = sorted(flows.values(), key=lambda r: (-r["count"], r["src"], r["dst"]))
    return {
        "file": str(path),
        "bytes": len(data),
        "packets_total": total,
        "ipv4_packets": v4,
        "ipv6_packets": v6,
        "flows": rows,
        "dscp_distribution": [
            {
                "dscp": d,
                "name": DSCP_NAMES.get(d, "-"),
                "count": c,
                "share": f"{100.0 * c / max(1, v4 + v6):.1f}%",
            }
            for d, c in sorted(dscp_counts.items(), key=lambda kv: -kv[1])
        ],
        "protocol_distribution": [
            {"proto": PROTO_NAMES.get(p, str(p)), "count": c}
            for p, c in sorted(proto_counts.items(), key=lambda kv: -kv[1])
        ],
    }


def render_table(summary: dict) -> str:
    """The summary as a plain text table."""
    lines = []
    lines.append(f"capture: {summary['file']}  "
                 f"({summary['bytes']} bytes, {summary['packets_total']} packets, "
                 f"{summary['ipv4_packets']} IPv4, {summary['ipv6_packets']} IPv6)")
    lines.append("")
    head = ["src", "dst", "proto", "port", "dscp", "name", "ecn", "count"]
    body = []
    for row in summary["flows"]:
        ports = "-"
        if row["src_port"] is not None:
            ports = f"{row['src_port']}->{row['dst_port']}"
        body.append([str(row["src"]), str(row["dst"]), str(row["proto"]), ports,
                     str(row["dscp"]), str(row["dscp_name"]), str(row["ecn"]),
                     str(row["count"])])
    # the address columns have to be as wide as the longest address, or an
    # IPv6 address runs into the next column and the table cannot be read
    widths = [len(h) for h in head]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines.append("  ".join(h.ljust(widths[i]) for i, h in enumerate(head))
                 .rjust(0) + " " * 0)
    lines.append("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in body:
        cells = []
        for i, cell in enumerate(row):
            cells.append(cell.rjust(widths[i]) if i == len(row) - 1
                         else cell.ljust(widths[i]))
        lines.append("  ".join(cells))
    lines.append("")
    lines.append("DSCP distribution:")
    for row in summary["dscp_distribution"]:
        lines.append(f"  dscp {row['dscp']:<3} {row['name']:<7} "
                     f"{row['count']:>7}  {row['share']}")
    return "\n".join(lines)


# ----------------------------------------------------------------- backends --

def is_elevated() -> bool:
    """Does this process hold an administrator token?"""
    if os.name != "nt":
        return os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=vbox.CREATE_NO_WINDOW)


def pktmon_available() -> bool:
    """Is ``pktmon`` on this host?"""
    try:
        res = _run(["pktmon", "start", "help"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0 or "pktmon" in (res.stdout + res.stderr).lower()


def pktmon_capture(duration: float, out_path: str | Path,
                   filters: list[str] | None = None,
                   work_dir: str | Path | None = None) -> Path:
    """Take a capture with pktmon and return the pcapng file it produced.

    ``filters`` are pktmon filter expressions, e.g. ``["-p 40000"]`` or
    ``["-i 192.168.56.2"]``.  With no filter everything on the wire is
    captured, which is what a first look wants.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(work_dir) if work_dir else out.parent
    work.mkdir(parents=True, exist_ok=True)
    etl = work / (out.stem + ".etl")

    if filters:
        _run(["pktmon", "filter", "remove"], timeout=30)
        for expr in filters:
            res = _run(["pktmon", "filter", "add"] + expr.split(), timeout=30)
            if res.returncode != 0:
                raise CaptureError(
                    f"pktmon rejected the filter {expr!r}: "
                    f"{(res.stderr or res.stdout).strip()}")

    start = _run(["pktmon", "start", "--capture", "--pkt-size", "0",
                  "--comp", "nics", "--file-name", str(etl)], timeout=60)
    if start.returncode != 0:
        detail = (start.stderr or start.stdout).strip()
        if "拒绝访问" in detail or "Access is denied" in detail:
            raise CaptureError(
                "pktmon refused the capture: " + detail +
                " (the PktMon driver needs an elevated process)")
        raise CaptureError("pktmon start failed: " + detail)

    try:
        time.sleep(max(0.5, duration))
    finally:
        _run(["pktmon", "stop"], timeout=120)

    if not etl.is_file():
        raise CaptureError(f"pktmon wrote no log file at {etl}")
    conv = _run(["pktmon", "etl2pcap", str(etl), "--out", str(out)], timeout=300)
    if conv.returncode != 0 or not out.is_file():
        raise CaptureError(
            "pktmon etl2pcap failed: " + (conv.stderr or conv.stdout).strip())
    return out


def _raw_capture(duration: float, out_path: str | Path,
                 interface: str = "") -> Path:
    """Capture with a promiscuous raw socket (the fallback backend).

    On Windows this needs ``SIO_RCVALL``, which needs an elevated process
    and which some adapters refuse with ``WinError 10022``.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        return _raw_capture_linux(duration, out, interface)

    sio_rcvall = 0x98000001
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
    except OSError as exc:
        raise CaptureError(
            f"the raw socket could not be opened: {exc} (a raw socket needs an "
            "elevated process)") from exc
    try:
        try:
            sock.bind((interface or "0.0.0.0", 0))
        except OSError:
            sock.bind(("", 0))
        try:
            sock.ioctl(sio_rcvall, 1)
        except OSError as exc:
            raise CaptureError(
                f"the raw socket refused SIO_RCVALL on {interface or 'the default adapter'}: "
                f"{exc} (this needs an elevated process, and some adapters do not "
                "support promiscuous mode at all)") from exc
        sock.settimeout(1.0)
        end = time.time() + duration
        frames = []
        while time.time() < end:
            try:
                frames.append(sock.recv(65535))
            except socket.timeout:
                continue
            except OSError as exc:
                raise CaptureError(f"the raw socket stopped: {exc}") from exc
    finally:
        sock.close()

    with open(out, "wb") as fh:
        # classic pcap, microsecond timestamps, LINKTYPE_ETHERNET
        fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for frame in frames:
            fh.write(struct.pack("<IIII", 0, 0, len(frame), len(frame)))
            fh.write(frame)
    return out


def _raw_capture_linux(duration: float, out_path: Path, interface: str) -> Path:
    """Capture with an ``AF_PACKET`` socket (Linux)."""
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
    except (AttributeError, OSError) as exc:
        raise CaptureError(f"AF_PACKET is not available here: {exc}") from exc
    frames = []
    try:
        if interface:
            sock.bind((interface, 0))
        sock.settimeout(1.0)
        end = time.time() + duration
        while time.time() < end:
            try:
                frames.append(sock.recv(65535))
            except socket.timeout:
                continue
    finally:
        sock.close()
    with open(out_path, "wb") as fh:
        fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for frame in frames:
            fh.write(struct.pack("<IIII", 0, 0, len(frame), len(frame)))
            fh.write(frame)
    return out_path


# ------------------------------------------------------------- elevation ---

_HELPER = Path(__file__).with_name("_capture_helper.py")


def elevated_capture(duration: float, out_path: str | Path,
                     filters: list[str] | None = None) -> Path:
    """Take the capture in a freshly elevated helper process.

    The MCP server usually runs unelevated, and pktmon needs an elevated
    process.  Rather than failing, the capture is retried in a helper that
    is started through the shell's ``runas`` verb.  On a host where the
    administrator is allowed to elevate without a prompt this is silent; where
    a prompt appears, the user sees it once.

    The helper writes the pcapng file and exits; the caller reads it.
    """
    if not _HELPER.is_file():
        raise CaptureError(f"the capture helper is missing: {_HELPER}")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # ShellExecuteW takes the interpreter as the program, so the parameters
    # start with the *script* -- not with the interpreter a second time.
    args = [str(_HELPER), "--duration", str(duration), "--out", str(out)]
    for expr in filters or []:
        args += ["--filter", expr]
    params = " ".join(f'"{a}"' for a in args)
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 0)
    except Exception as exc:  # noqa: BLE001
        raise CaptureError(f"could not start an elevated capture: {exc}") from exc
    if rc <= 32:
        raise CaptureError(
            f"the elevated capture was not started (ShellExecute returned {rc}); "
            "the user declined the elevation prompt, or the policy forbids it")
    end = time.time() + duration + 60
    error_file = out.with_suffix(".error.txt")
    started_file = out.with_suffix(".started.txt")
    while time.time() < end:
        if error_file.is_file():
            detail = error_file.read_text(encoding="utf-8", errors="replace").strip()
            error_file.unlink(missing_ok=True)
            raise CaptureError(f"the elevated capture failed: {detail}")
        if out.is_file() and out.stat().st_size > 0:
            return out
        time.sleep(0.5)
    if started_file.is_file():
        marker = started_file.read_text(encoding="utf-8", errors="replace").strip()
        started_file.unlink(missing_ok=True)
        raise CaptureError(
            f"the elevated helper started ({marker}) but produced no capture "
            f"within {duration + 60:.0f}s")
    raise CaptureError(
        f"the elevated helper did not start within {duration + 60:.0f}s")


def capture(duration: float = 10.0, out_path: str | Path = "",
            backend: str = "auto", filters: list[str] | None = None,
            elevate: bool = False, interface: str = "") -> dict:
    """Take a capture and return where it landed and how it went.

    ``backend`` is ``auto`` (pktmon, then raw), ``pktmon`` or ``raw``.
    ``elevate`` allows the capture to be retried in an elevated helper when
    this process is not elevated.
    """
    out = Path(out_path) if out_path else Path.cwd() / "ensp-capture.pcapng"
    order = ["pktmon", "raw"] if backend == "auto" else [backend]
    attempts: list[dict] = []
    for name in order:
        try:
            if name == "pktmon":
                if not is_elevated():
                    if elevate:
                        path = elevated_capture(duration, out, filters)
                    else:
                        raise CaptureError(
                            "pktmon needs an elevated process; this server is "
                            "not elevated")
                else:
                    path = pktmon_capture(duration, out, filters)
            elif name == "raw":
                if not is_elevated() and not elevate:
                    raise CaptureError(
                        "a raw socket needs an elevated process; this server "
                        "is not elevated")
                path = _raw_capture(duration, out.with_suffix(".pcap"), interface)
            else:
                raise CaptureError(f"unknown backend {name!r}")
        except CaptureError as exc:
            attempts.append({"backend": name, "ok": False, "error": str(exc)})
            if backend != "auto":
                break
            continue
        attempts.append({"backend": name, "ok": True, "file": str(path)})
        return {"ok": True, "backend": name, "file": str(path), "attempts": attempts}
    return {"ok": False, "attempts": attempts,
            "error": "; ".join(a["error"] for a in attempts)}
