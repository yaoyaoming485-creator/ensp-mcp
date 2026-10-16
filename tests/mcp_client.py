"""Drive the MCP server over stdio exactly as an agent would, and call every tool.

Run with:  uv run python tests/mcp_client.py
Optional:  uv run python tests/mcp_client.py -- <command to launch the server>
"""
from __future__ import annotations

import json
import queue
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
    }
    missing = expected - set(tools)
    check("all expected tools advertised", not missing, f"missing={sorted(missing)}")

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
    check("  written as UTF-16LE", Path(out_topo).read_bytes()[:2] == b"\xff\xfe")

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
