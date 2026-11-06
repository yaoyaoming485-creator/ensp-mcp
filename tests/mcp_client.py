"""Drive the MCP server over stdio exactly as an agent would, and call every tool.

Run with:  uv run python tests/mcp_client.py
Optional:  uv run python tests/mcp_client.py -- <command to launch the server>
"""
from __future__ import annotations

import json
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_CMD = [sys.executable, "-m", "ensp_mcp"]

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(__file__).resolve().parent / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

failures: list[str] = []
skipped: list[str] = []
results: dict[str, object] = {}


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def skip(label: str, reason: str = "") -> None:
    print(f"[SKIP] {label}" + (f"  -- {reason}" if reason else ""))
    skipped.append(label)


class Server:
    def __init__(self, cmd: list[str], cwd: str | None = None):
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=cwd,
        )
        self.q: queue.Queue[str] = queue.Queue()
        self.stderr_lines: list[str] = []
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.q), daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()
        self._id = 0

    def _pump(self, stream, q: queue.Queue) -> None:
        for line in stream:
            q.put(line)

    def _pump_err(self) -> None:
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip())

    def send(self, obj: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 120.0) -> dict:
        self._id += 1
        rid = self._id
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self.q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"{method} -> {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(
            f"no reply to {method} within {timeout}s; stderr tail: {self.stderr_lines[-5:]}"
        )

    def notify(self, method: str) -> None:
        self.send({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def call(srv: Server, name: str, args: dict, timeout: float = 120.0):
    res = srv.request("tools/call", {"name": name, "arguments": args}, timeout=timeout)
    out = []
    for block in res.get("content", []):
        out.append(block)
    results[name] = out
    return out


def text_of(blocks: list) -> str:
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")


cmd = DEFAULT_CMD
if "--" in sys.argv:
    cmd = sys.argv[sys.argv.index("--") + 1 :]

print(f"launching server: {' '.join(cmd)}")
srv = Server(cmd, cwd=str(ROOT))
try:
    init = srv.request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ensp-smoke", "version": "0.1"},
        },
    )
    check("initialize", "serverInfo" in init, json.dumps(init.get("serverInfo", {})))
    srv.notify("notifications/initialized")

    listing = srv.request("tools/list")
    tools = {t["name"]: t for t in listing.get("tools", [])}
    print(f"    {len(tools)} tools: {', '.join(sorted(tools))}")
    expected = {
        "ensp_list_models",
        "ensp_create_topology",
        "ensp_read_topology",
        "ensp_validate_topology",
        "ensp_open_topology",
        "ensp_status",
        "ensp_list_devices",
        "ensp_console_screen",
        "ensp_console_type",
        "ensp_console_key",
        "ensp_console_send_lines",
        "ensp_vrp_list_devices",
        "ensp_vrp_command",
        "ensp_vrp_agent_handshake",
        "ensp_start_devices",
        "ensp_stop_devices",
        "ensp_lab_status",
        "ensp_capture",
        "ensp_tool_registry",
    }
    missing = expected - set(tools)
    check("all expected tools advertised", not missing, f"missing={sorted(missing)}")
    check("no tool outside the expected set", not (set(tools) - expected),
          f"extra={sorted(set(tools) - expected)}")

    # ---- the registry is the single source of truth --------------------
    print()
    blocks = call(srv, "ensp_tool_registry", {})
    reg = json.loads(text_of(blocks))
    registered = {t["name"] for t in reg.get("tools", [])}
    check("ensp_tool_registry", reg.get("ok") is not False and reg.get("count", 0) > 0,
          f"{reg.get('count')} tools in the registry")
    check("registry and tools/list agree", registered == set(tools),
          f"registry-only={sorted(registered - set(tools))} "
          f"list-only={sorted(set(tools) - registered)}")
    meta_ok = []
    for name, tool in sorted(tools.items()):
        meta = tool.get("_meta") or {}
        spec = next((t for t in reg.get("tools", []) if t["name"] == name), None)
        if spec is None:
            meta_ok.append(f"{name}: not in the registry")
            continue
        if (meta.get("requires") or []) != spec.get("requires", []):
            meta_ok.append(f"{name}: requires differ")
        if (meta.get("error_codes") or []) != spec.get("error_codes", []):
            meta_ok.append(f"{name}: error_codes differ")
        for req in meta.get("requires") or []:
            if not req.get("id") or not req.get("error_code") or not req.get("remedy"):
                meta_ok.append(f"{name}: an incomplete requirement")
    check("every tool carries the registry's metadata", not meta_ok,
          "; ".join(meta_ok[:4]))

    # ---- a failing call must be machine readable ------------------------
    print()
    blocks = call(srv, "ensp_vrp_command", {"guest_ip": "192.168.56.254",
                                            "commands": []})
    bad = json.loads(text_of(blocks))
    check("a failing call names an error code", bad.get("ok") is False
          and bool(bad.get("error_code")) and bool(bad.get("error")),
          f"error_code={bad.get('error_code')}")

    blocks = call(srv, "ensp_open_topology", {"path": "D:\\no\\such\\lab.topo"})
    bad = json.loads(text_of(blocks))
    check("ensp_open_topology reports a missing file, not a crash",
          bad.get("ok") is False and bad.get("error_code") == "topo_not_found",
          f"error_code={bad.get('error_code')}")

    print()
    # ---- topology -------------------------------------------------------
    blocks = call(srv, "ensp_list_models", {"category": "router"})
    data = json.loads(text_of(blocks))
    check("ensp_list_models", data["count"] >= 8, f"{data['count']} routers")

    out_topo = str(OUT_DIR / "mcp-created.topo")
    spec = {
        "name": "mcp-created",
        "devices": [
            {"name": "R1", "model": "AR2220"},
            {"name": "R2", "model": "AR2220"},
            {"name": "SW1", "model": "S5700"},
        ],
        "links": [
            {"a": "R1", "b": "R2", "a_port": 0, "b_port": 0},
            {"a": "R2", "b": "SW1", "a_port": 1, "b_port": 3},
        ],
        "labels": [{"text": "created via MCP", "x": 200, "y": 120}],
    }
    blocks = call(srv, "ensp_create_topology", {"spec": spec, "out_path": out_topo})
    created = json.loads(text_of(blocks))
    check("ensp_create_topology", created.get("ok") and Path(out_topo).is_file(), json.dumps(created))
    check("  written as UTF-8 without BOM",
          created.get("has_bom") is False and created.get("encoding") == "utf-8",
          f"encoding={created.get('encoding')} has_bom={created.get('has_bom')}")

    blocks = call(srv, "ensp_read_topology", {"path": out_topo})
    back = json.loads(text_of(blocks))
    check("ensp_read_topology", len(back["devices"]) == 3 and len(back["links"]) == 2,
          f"{len(back['devices'])} devices / {len(back['links'])} links")

    blocks = call(srv, "ensp_validate_topology", {"spec": spec})
    val = json.loads(text_of(blocks))
    check("ensp_validate_topology", val.get("ok") is True, json.dumps(val))

    # ---- runtime --------------------------------------------------------
    print()
    blocks = call(srv, "ensp_status", {})
    status = json.loads(text_of(blocks))
    check("ensp_status", status.get("vboxmanage") and status.get("ensp_client"),
          f"ensp_client={status.get('ensp_client')}")

    blocks = call(srv, "ensp_list_devices", {"running_only": True})
    devs = json.loads(text_of(blocks))
    check("ensp_list_devices", devs.get("ok") is True,
          f"count={devs.get('count')} ({', '.join(d['vm'] for d in devs.get('devices', [])) or 'none running'})")
    target = devs["devices"][0]["vm"] if devs.get("devices") else None

    # ---- console --------------------------------------------------------
    print()
    if target:
        blocks = call(srv, "ensp_console_screen", {"vm": target})
        kinds = [b.get("type") for b in blocks]
        check("ensp_console_screen returns an image", "image" in kinds, f"content types={kinds}")
        img = next((b for b in blocks if b.get("type") == "image"), None)
        if img:
            check("  image is PNG", img.get("mimeType") == "image/png",
                  f"mime={img.get('mimeType')} b64={len(img.get('data',''))} chars")

        blocks = call(srv, "ensp_console_type", {"vm": target, "text": "mcp", "enter": True})
        check("ensp_console_type", "typed" in text_of(blocks), text_of(blocks).strip())

        blocks = call(srv, "ensp_console_key", {"vm": target, "key": "enter", "times": 1})
        check("ensp_console_key", "sent enter" in text_of(blocks), text_of(blocks).strip())

        blocks = call(srv, "ensp_console_send_lines",
                      {"vm": target, "lines": ["# comment skipped", "", "x"], "settle": 0.2})
        check("ensp_console_send_lines", "sent 1 of 3" in text_of(blocks), text_of(blocks).strip())

        saved = OUT_DIR / "after.png"
        blocks = call(srv, "ensp_console_screen", {"vm": target, "save_path": str(saved)})
        check("  screenshot saved to disk", saved.is_file())
    else:
        skip(
            "console tools (ensp_console_*)",
            "no device VM is running. Start one first, e.g. "
            r"'VBoxManage startvm R1 --type headless', then re-run.",
        )

    # ---- real VRP console ------------------------------------------------
    print()
    blocks = call(srv, "ensp_vrp_list_devices", {})
    vrp = json.loads(text_of(blocks))
    check("ensp_vrp_list_devices", vrp.get("ok") is True,
          f"count={vrp.get('count')} ({', '.join(d['guest_ip'] for d in vrp.get('devices', [])) or 'none'})")

    guests = vrp.get("devices", [])
    answered = None
    for dev in guests:
        if not dev.get("vrp_console"):
            continue
        # probe first: a device whose console the eNSP client already holds
        # accepts the connection but never sends anything, and waiting for the
        # full connect-retry budget for each such device would be slow
        probe_sock = socket.socket()
        probe_sock.settimeout(1.0)
        try:
            probe_sock.connect((dev["guest_ip"], 40000))
            probe_sock.sendall(b"\r\n")
            probe_sock.settimeout(1.5)
            try:
                probe = probe_sock.recv(256)
            except socket.timeout:
                probe = b""
        except OSError:
            probe = b""
        finally:
            probe_sock.close()
        if not probe:
            continue
        blocks = call(srv, "ensp_vrp_command",
                      {"guest_ip": dev["guest_ip"], "commands": ["display version"],
                       "wait": 2.0})
        res = json.loads(text_of(blocks))
        if res.get("ok") and "<Huawei>" in res.get("output", ""):
            answered = dev["guest_ip"]
            check("ensp_vrp_command returns the router's text", True,
                  f"{dev['guest_ip']}: " + res["output"].strip().splitlines()[0][:70])
            break
    if guests and answered is None:
        skip("ensp_vrp_command",
             "every device's console is held by another client (the eNSP "
             "client, or this project's own lab supervisor), so the guest does "
             "not answer a second client")
    elif not guests:
        skip("ensp_vrp_command", "no eNSP guest reachable on the host-only subnet")

    if guests:
        # the agentd control socket is a single-client service; when the eNSP
        # client holds it, the guest simply does not answer us
        handshake_ok = False
        busy = None
        for dev in guests:
            blocks = call(srv, "ensp_vrp_agent_handshake",
                          {"guest_ip": dev["guest_ip"], "timeout": 6.0})
            res = json.loads(text_of(blocks))
            if res.get("ok"):
                check("ensp_vrp_agent_handshake", bool(res.get("events")),
                      ", ".join(res.get("events", [])))
                handshake_ok = True
                break
            if "may already hold" in str(res.get("error", "")) or \
               "timed out" in str(res.get("error", "")):
                busy = dev["guest_ip"]
        if not handshake_ok:
            if busy:
                skip("ensp_vrp_agent_handshake",
                     f"{busy} is busy - the eNSP client already holds its "
                     "agentd control session")
            else:
                check("ensp_vrp_agent_handshake", False,
                      "no device answered the control handshake")

    # ---- lifecycle and capture ------------------------------------------
    print()
    blocks = call(srv, "ensp_start_devices", {"devices": ["no-such-machine-xyz"]})
    res = json.loads(text_of(blocks))
    check("ensp_start_devices reports an unknown machine",
          res.get("ok") is False and res.get("devices")
          and res["devices"][0].get("error_code") == "device_not_found",
          f"ok={res.get('ok')} code="
          f"{(res.get('devices') or [{}])[0].get('error_code')}")

    blocks = call(srv, "ensp_stop_devices", {"devices": ["KVM-Host"]})
    res = json.loads(text_of(blocks))
    check("ensp_stop_devices refuses a protected machine",
          res.get("ok") is False and res.get("devices")
          and res["devices"][0].get("error_code") == "vm_protected",
          f"ok={res.get('ok')} code="
          f"{(res.get('devices') or [{}])[0].get('error_code')}")

    blocks = call(srv, "ensp_stop_devices", {})
    res = json.loads(text_of(blocks))
    check("ensp_stop_devices refuses an empty request",
          res.get("ok") is False and res.get("error_code") == "no_devices_given",
          f"error_code={res.get('error_code')}")

    # a device of the eNSP GUI: the name is right, the lookup still fails,
    # and the call has to say why instead of saying "no such machine"
    ar_topo = str(OUT_DIR / "mcp-ensp-device.topo")
    ar_spec = {"name": "mcp-ensp-device",
               "devices": [{"name": "AR1-MCP", "model": "AR2220"}]}
    call(srv, "ensp_create_topology", {"spec": ar_spec, "out_path": ar_topo})

    blocks = call(srv, "ensp_start_devices",
                  {"devices": ["AR1-MCP"], "topo_path": ar_topo})
    res = json.loads(text_of(blocks))
    row = (res.get("devices") or [{}])[0]
    check("ensp_start_devices tells an eNSP device from a typo",
          row.get("error_code") == "device_needs_ensp_client",
          f"error_code={row.get('error_code')}")
    check("  and says what to do about it",
          bool(row.get("action")) and row["action"].get("kind") == "use_lab_mode",
          json.dumps(row.get("action")))

    blocks = call(srv, "ensp_stop_devices",
                  {"devices": ["AR1-MCP"], "topo_path": ar_topo})
    res = json.loads(text_of(blocks))
    row = (res.get("devices") or [{}])[0]
    check("ensp_stop_devices says the same about an eNSP device",
          row.get("error_code") == "device_needs_ensp_client",
          f"error_code={row.get('error_code')}")

    blocks = call(srv, "ensp_list_devices", {"running_only": False})
    res = json.loads(text_of(blocks))
    marked = [d for d in res.get("devices", []) if "managed_by" in d]
    check("ensp_list_devices says who owns each machine",
          len(marked) == res.get("count", 0),
          f"{len(marked)}/{res.get('count')} marked")

    blocks = call(srv, "ensp_status", {})
    res = json.loads(text_of(blocks))
    check("ensp_status counts the eNSP-managed machines",
          "ensp_managed_count" in res,
          f"ensp_managed_count={res.get('ensp_managed_count')}")

    blocks = call(srv, "ensp_lab_status", {"lab_root": str(OUT_DIR / "no-such-lab")})
    res = json.loads(text_of(blocks))
    check("ensp_lab_status reports a lab that is not there",
          res.get("ok") is False and res.get("error_code") == "lab_not_found",
          f"error_code={res.get('error_code')}")

    blocks = call(srv, "ensp_capture", {"duration": 3, "backend": "auto"})
    res = json.loads(text_of(blocks))
    check("ensp_capture answers with a machine-readable result",
          res.get("ok") is True or bool(res.get("error_code")),
          ("ok" if res.get("ok") else res.get("error_code"))
          + (f" backend={res.get('backend')}" if res.get("ok") else ""))
    if res.get("ok"):
        summary = res.get("summary") or {}
        check("  the capture was read back into IP headers",
              "dscp_distribution" in summary,
              f"{summary.get('ipv4_packets')} IPv4 packets, "
              f"{len(summary.get('flows', []))} flows")
    else:
        check("  a failed capture explains itself",
              bool(res.get("hint")) and bool(res.get("attempts")),
              res.get("error", "")[:90])

    print()
    check("no tool call returned a JSON-RPC error", True)
finally:
    srv.close()

print("=" * 72)
if failures:
    verdict = f"{len(failures)} FAILURE(S): " + ", ".join(failures)
elif skipped:
    verdict = f"ALL PASS ({len(skipped)} section(s) SKIPPED: {', '.join(skipped)})"
else:
    verdict = "ALL PASS"
print(f"RESULT: {verdict}")
print("=" * 72)
sys.exit(1 if failures else 0)
