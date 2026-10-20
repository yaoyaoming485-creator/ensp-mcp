"""Smoke test for the ensp_mcp package. Run with:  uv run python tests/smoke.py"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ensp_mcp import agentd, console, models, topo, vbox  # noqa: E402

failures: list[str] = []
skipped: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def skip(label: str, reason: str = "") -> None:
    print(f"[SKIP] {label}" + (f"  -- {reason}" if reason else ""))
    skipped.append(label)


print("=" * 72)
print("1. model catalogue")
print("=" * 72)
rows = models.describe_all()
check("catalogue is populated", len(rows) > 30, f"{len(rows)} models")
ar2220 = models.get("AR2220")
check("AR2220 ports", ar2220.ports == ("GE0/0/0", "GE0/0/1", "GE0/0/2", "Serial0/0/0", "Serial0/0/1"),
      str(ar2220.ports))
s5700 = models.get("S5700")
check("S5700 has 24 GE", s5700.port_count == 24, str(s5700.port_count))
ce = models.get("CE6800")
check("CE6800 slot format", 'category="Ethernet"' in ce.xml and ce.port_count == 20)

print()
print("=" * 72)
print("2. topology build / write / read round-trip")
print("=" * 72)
spec = {
    "name": "ospfv3-lab",
    "devices": [
        {"name": "R1", "model": "AR2220", "x": 150, "y": 250},
        {"name": "R2", "model": "AR2220", "x": 450, "y": 250},
        {"name": "R3", "model": "AR2220", "x": 750, "y": 250},
        {"name": "PC1", "model": "PC", "x": 450, "y": 450},
    ],
    "links": [
        {"a": "R1", "b": "R2", "a_port": 0, "b_port": 0, "type": "Copper"},
        {"a": "R2", "b": "R3", "a_if": "GE0/0/1", "b_port": 0},
        {"a": "R2", "b": "PC1", "a_port": 2, "b_port": 0},
    ],
    "labels": [{"text": "Area0", "x": 190, "y": 160}],
    "boxes": [{"x": 100, "y": 100, "w": 700, "h": 300, "color": 16776960}],
}
tmp = Path(tempfile.mkdtemp(prefix="ensp-smoke-"))
out = topo.write(spec, tmp / "lab.topo")
check("file written", out.is_file(), str(out))
raw = out.read_bytes()
check("no BOM (eNSP rejects a BOM'd file by loading 0 devices)",
      not raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb")), raw[:3].hex())
check("declares encoding=\"UNICODE\" like eNSP does",
      b'encoding="UNICODE"' in raw[:80], raw[:60].decode("utf-8", "replace"))
back = topo.read(out)
check("round-trip reports utf-8", back["encoding"] == "utf-8" and back["has_bom"] is False,
      f"{back['encoding']} bom={back['has_bom']}")
check("round-trip flagged as eNSP-loadable", back["loads_in_ensp"] is True)
check("device count", len(back["devices"]) == 4, str(len(back["devices"])))
check("link count", len(back["links"]) == 3, str(len(back["links"])))
r1 = next(d for d in back["devices"] if d["name"] == "R1")
check("R1 model round-trips", r1["model"] == "AR2220")
check("R1 com_port auto-assigned", r1["com_port"] == 2000, str(r1["com_port"]))
link2 = back["links"][1]
check("interface name resolved to index", link2["a_port"] == 1, json.dumps(link2, ensure_ascii=False))
pc = next(d for d in back["devices"] if d["name"] == "PC1")
check("PC has no com_port", pc["com_port"] == 0, str(pc["com_port"]))

print()
print("=" * 72)
print("3. validation")
print("=" * 72)
bad = topo.validate({"devices": [{"name": "R1", "model": "AR2220"}],
                     "links": [{"a": "R1", "b": "R1"}]})
check("self-link rejected", not bad["ok"], json.dumps(bad["problems"], ensure_ascii=False))
bad2 = topo.validate({"devices": [{"name": "R1", "model": "AR2220"}],
                      "links": [{"a": "R1", "b": "NOPE"}]})
check("unknown device rejected", not bad2["ok"])
bad3 = topo.validate({"devices": [{"name": "R1", "model": "AR2220"}],
                      "links": [{"a": "R1", "b": "R1", "a_port": 99}]})
check("out-of-range port rejected", not bad3["ok"])
warn = topo.validate({"devices": [{"name": "CE1", "model": "CE6800"}]})
check("missing image warned", any("image" in w for w in warn["warnings"]),
      json.dumps(warn["warnings"], ensure_ascii=False))

print()
print("=" * 72)
print("4. parse a real .topo")
print("=" * 72)
# By default this parses the checked-in example. Point ENSP_SAMPLE_TOPO at a
# topology authored by eNSP itself for a much stronger check -- eNSP writes
# attributes like cx="220.000000" that this parser has to tolerate.
import os  # noqa: E402

sample = os.environ.get("ENSP_SAMPLE_TOPO")
if sample:
    real = Path(sample)
    print(f"    using ENSP_SAMPLE_TOPO={real}")
else:
    real = Path(__file__).resolve().parent.parent / "examples" / "ospfv3-lab.topo"
if real.is_file():
    d = topo.read(real)
    check("read .topo", len(d["devices"]) >= 4, f"{len(d['devices'])} devices, {len(d['links'])} links")
    for dev in d["devices"]:
        print(f"    {dev['name']:<6} {dev['model']:<8} ports={dev['ports']}")
    print(f"    version={d['version']}")
else:
    skip("parse a .topo file", f"{real} not found")

print()
print("=" * 72)
print("5. VirtualBox layer")
print("=" * 72)
try:
    vm_path = vbox.vboxmanage_path()
    check("VBoxManage found", Path(vm_path).is_file(), vm_path)
    procs = vbox.ensp_processes()
    check(
        "ensp_processes does not match this server",
        all("ensp-mcp" not in p["name"].lower() for p in procs),
        f"matched={[p['name'] for p in procs]}",
    )
    found_vbox = [p["name"] for p in vbox.vbox_processes()]
    print(f"    vbox processes now: {found_vbox or '(none - VBoxSVC exits when idle)'}")
    check(
        "vbox_processes does not match this server",
        all("ensp" not in p["name"].lower() for p in vbox.vbox_processes()),
        f"matched={found_vbox}",
    )
    vms = vbox.list_vms()
    check("VM inventory", len(vms) > 0, f"{len(vms)} VMs")
    running = [v for v in vms if v.running]
    if running:
        check("a device VM is running", True, ", ".join(v.name for v in running))
        target = running[0].name
        ch = vbox.console_channels(target)
        print("    channels:", json.dumps(ch, ensure_ascii=False))
        check("serial pipe is flagged as not-the-console", ch["serial_pipe_is_console"] is False)
        shot = vbox.take_screenshot(target, tmp / f"{target}.png")
        blob = shot.read_bytes()
        check(
            "screenshot captured",
            blob[:8] == b"\x89PNG\r\n\x1a\n" and len(blob) > 200,
            f"{len(blob)} bytes, PNG magic ok={blob[:8] == b'\x89PNG\r\n\x1a\n'}",
        )
        if blob[12:16] == b"IHDR":
            w = int.from_bytes(blob[16:20], "big")
            h = int.from_bytes(blob[20:24], "big")
            check("screen is 720x400 VGA text mode", (w, h) == (720, 400), f"{w}x{h}")
    else:
        skip(
            "device-VM console checks",
            "no VM is running. Start one first, e.g. "
            r"'VBoxManage startvm R1 --type headless', then re-run.",
        )
except Exception as exc:  # noqa: BLE001
    check("VirtualBox layer", False, f"{type(exc).__name__}: {exc}")
    running = []

print()
print("=" * 72)
print("6. console write path (keyboard injection)")
print("=" * 72)
if running:
    target = running[0].name
    try:
        console.send_text(target, "smoke", enter=True)
        check("typed into console", True, f"{target} <- 'smoke' + Enter")
        shot2 = console.capture(target, tmp / f"{target}-after.png")
        check("follow-up screenshot", shot2.is_file(), str(shot2))
    except Exception as exc:  # noqa: BLE001
        check("console write", False, f"{type(exc).__name__}: {exc}")
else:
    skip("console write path", "no running device VM")

print()
print("=" * 72)
print("7. real VRP console (guest TCP 40000)")
print("=" * 72)
guests = []
try:
    guests = agentd.guest_ip_from_host_only()
    check("guest discovery", len(guests) > 0, f"{len(guests)} guest(s): {guests}")
except Exception as exc:  # noqa: BLE001
    check("guest discovery", False, f"{type(exc).__name__}: {exc}")

if guests:
    # The VRP console serves one client at a time. The eNSP client keeps a
    # session open on the devices it manages, so on those the connect simply
    # produces no data. Treat that as "not available", not as a failure.
    target_guest = None
    busy_guest = None
    for ip in guests:
        s = socket.socket()
        s.settimeout(0.6)
        try:
            s.connect((ip, agentd.VRP_CONSOLE_PORT))
            s.sendall(b"\r\n")
            s.settimeout(1.2)
            try:
                probe = s.recv(256)
            except socket.timeout:
                probe = b""
            if probe:
                target_guest = ip
                s.close()
                break
            busy_guest = ip
        except OSError:
            pass
        finally:
            try:
                s.close()
            except OSError:
                pass

    if target_guest:
        try:
            with agentd.VrpConsole(target_guest) as con:
                out = con.run(["display version"], wait=2.0)
                check("VRP console answers", "<Huawei>" in out,
                      out.strip()[:90].replace("\n", " "))
                check("VRP identifies itself", "VRP" in out or "Huawei" in out,
                      "display version returned the platform banner")
        except Exception as exc:  # noqa: BLE001
            check("VRP console", False, f"{type(exc).__name__}: {exc}")
    elif busy_guest:
        skip("VRP console session",
             f"{busy_guest} accepted the connection but sent nothing - the "
             "eNSP client already holds that console session")
    else:
        skip("VRP console session", "no guest accepted a console connection")
else:
    skip("VRP console", "no eNSP guest reachable on the host-only subnet")

print()
print("=" * 72)
print("8. agentd control handshake")
print("=" * 72)
if guests:
    try:
        res = agentd.control_handshake(guests[0], timeout=8.0)
        check("control handshake", bool(res.get("events")), ", ".join(res.get("events", [])))
    except agentd.AgentdError as exc:
        if "may already hold" in str(exc):
            skip("control handshake",
                 f"{guests[0]} is busy - the eNSP client already holds its "
                 "agentd control session")
        else:
            check("control handshake", False, str(exc))
    except Exception as exc:  # noqa: BLE001
        check("control handshake", False, f"{type(exc).__name__}: {exc}")
else:
    skip("control handshake", "no eNSP guest reachable")

print()
print("=" * 72)
if failures:
    verdict = f"{len(failures)} FAILURE(S): " + ", ".join(failures)
elif skipped:
    verdict = f"ALL PASS ({len(skipped)} section(s) SKIPPED: {', '.join(skipped)})"
else:
    verdict = "ALL PASS"
print(f"RESULT: {verdict}")
print(f"artifacts in {tmp}")
print("=" * 72)
sys.exit(1 if failures else 0)
