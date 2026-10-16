# ensp-mcp

An MCP server for Huawei **eNSP** (Enterprise Network Simulation Platform).
It gives an agent two things eNSP does not expose on its own: the ability to
**author topologies as files**, and the ability to **read and drive any running
device's console**.

Everything here was reverse-engineered against a real install — eNSP
`V1.3.00.100` with Oracle VirtualBox `7.2.18` on Windows 11. No eNSP SDK, no
undocumented DLL calls, no GUI automation: just the `.topo` format and the
VirtualBox control interface eNSP already drives.

## What was reverse-engineered

### 1. The `.topo` format

A topology is XML, written as **UTF-16LE with a BOM**, declaring
`<?xml version="1.0" encoding="UNICODE" ?>` and `<topo version="1.3.00.100">`:

```xml
<topo version="1.3.00.100">
  <devices>
    <dev id="UUID" name="R1" model="AR2220" settings="" system_mac=""
         com_port="2000" bootmode="0" cx="150" cy="250"
         edit_left="177" edit_top="304">
      <slot number="slot17" isMainBoard="1">
        <interface sztype="Ethernet" interfacename="GE" count="1" />
        <interface sztype="Ethernet" interfacename="GE" count="2" />
        <interface sztype="Serial" interfacename="Serial" count="2" />
      </slot>
      <slot isMainBoard="0" id="3" type="521" />
    </dev>
  </devices>
  <lines>
    <line srcDeviceID="UUID-A" destDeviceID="UUID-B">
      <interfacePair lineName="Copper" srcIndex="0" tarIndex="0" />
    </line>
  </lines>
  <shapes>…</shapes>
  <txttips>…</txttips>
</topo>
```

The one non-obvious rule: **`srcIndex`/`tarIndex` are indices into the device
model's flattened interface list**, in the order the `<interface>` elements
appear — not slot numbers, not port numbers on the silkscreen. `models.py`
carries that index space per model.

### 2. The device console is a screen, not a stream

Each eNSP device is a VirtualBox VM whose **name equals the topology device
name**. The VM's virtual screen runs a 720x400 VGA text mode (80x25 cells), and
that screen *is* the console — eNSP itself shows it over VRDP.

So the console is fully driveable from the command line:

| Direction | Mechanism |
| --- | --- |
| read | `VBoxManage controlvm <vm> screenshotpng <file>` |
| write | `VBoxManage controlvm <vm> keyboardputstring` / `keyboardputscancode` |

**The serial port is a trap.** Each device VM has a host pipe on COM2
(`uartmode2="server,\\.\pipe\<name>"`), which looks exactly like a serial
console. It is not one. Measured from t=0 on a booting AR clone, that pipe emits
a fixed 4-byte frame (`6a 07 e7 ab`) once per second and **never a single byte
of ASCII** — across a whole boot. Driving it produces nothing but garbage.
(An earlier iteration of this project did exactly that and wasted a lot of time;
the measurement is in `evidence/boot_capture.log`.)

Two smaller findings worth knowing:

* the AR device guest is a **Tiny Core Linux** appliance (`Core Linux`, kernel
  `3.0.21-tinycore`, hostname `box`) that boots to a login prompt — it does not
  auto-start VRP on its own;
* `eNSP_VBoxServer.exe` listens on TCP **65510**
  (`vboxserver\config_vbox.ini`), which is eNSP's internal client/server channel.

## Install

Requires Python 3.10+ and Oracle VirtualBox. eNSP itself is only needed for the
runtime tools.

```bash
uv sync
uv run ensp-mcp
```

### Launch it from the project's own venv, not from `uvx`

`uvx --from <dir> <entrypoint>` looks like the obvious way to run this, and it
works — until you edit the source. `uv` builds a wheel, caches it under
`%LOCALAPPDATA%\uv\cache\archive-v0\<hash>\Lib\site-packages\ensp_mcp\`, and
**reuses that cached build as long as the version number is unchanged**. Editing
a module has no effect on a `uvx`-launched server, which is a genuinely
confusing failure: the process restarts, yet keeps running the old code.

The venv is an editable install, so pointing the client straight at it picks up
source changes immediately:

```yaml
- insert:
    - id: mcp-ensp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: ensp
        transport: stdio
        command: '<clone>\.venv\Scripts\python.exe'
        args: ['-m', 'ensp_mcp']
        toolCallTimeoutMs: 120000
        failOnStartupError: false
        reconnect:
          enabled: true
          maxAttempts: 10
```

Replace `<clone>` with the checkout path. Any console script that runs the same
interpreter works too — for example `uv pip install -e .` plus
`<clone>\.venv\Scripts\ensp-mcp.exe`.

If you do prefer `uvx`, add `--refresh` so each launch rebuilds.

DSH applies `cordis.patch.yml` with `patchReload: live`, so this block takes
effect without restarting the web server.

## Tools

**Topology (offline, no eNSP required)**

| Tool | Purpose |
| --- | --- |
| `ensp_list_models` | device catalogue with interface index order |
| `ensp_create_topology` | spec → `.topo` file |
| `ensp_read_topology` | `.topo` → structured JSON |
| `ensp_validate_topology` | check a spec, warn about missing device images |
| `ensp_open_topology` | launch the eNSP GUI on a file |

**Runtime**

| Tool | Purpose |
| --- | --- |
| `ensp_status` | is eNSP/VirtualBox present, what is running |
| `ensp_list_devices` | device VMs and their console channels |

**Console**

| Tool | Purpose |
| --- | --- |
| `ensp_console_screen` | capture the console; returns the PNG |
| `ensp_console_type` | type text (+ optional Enter) |
| `ensp_console_key` | send a named key (`ctrl-z`, arrows, `F1`…) |
| `ensp_console_send_lines` | send a configuration block, one line at a time |

Typical loop: `ensp_console_type(vm="R1", text="system-view")` →
`ensp_console_screen(vm="R1")` → read the image.

## Verification

Two suites, both green:

| Suite | What it covers |
| --- | --- |
| `tests/smoke.py` | module level: catalogue, `.topo` build/write/read round-trip, validation failures, parsing a real eNSP-authored topology, VirtualBox inventory, screenshot, keyboard injection |
| `tests/mcp_client.py` | drives the server over JSON-RPC stdio like an agent does: `initialize`, `tools/list`, then calls **every** tool and checks the results |

`mcp_client.py` is the real acceptance test — it asserts the server advertises all
11 tools and that `ensp_console_screen` comes back as `content types=['text', 'image']`
with `mimeType: image/png`.

The tools were then exercised through the DSH harness itself, where they register
as `mcp__ensp__*`:

* `ensp_status` → reports `VBoxManage`, the eNSP client path, running device VMs;
* `ensp_list_devices` → `R1`, with `vrdp_port: 3389` and
  `serial_pipe_is_console: false`;
* `ensp_list_models` → 41 models with per-index interface lists;
* `ensp_create_topology` → wrote a 6,274-byte UTF-16LE topology;
* `ensp_read_topology` → read it back, resolving `"GE0/0/1"` to index 1;
* `ensp_validate_topology` → warned that `CE6800` needs an external image;
* `ensp_open_topology` → rejected a missing path before spawning anything;
* `ensp_console_screen` → returned the live 720x400 screen of a running AR clone;
* `ensp_console_type` / `ensp_console_key` / `ensp_console_send_lines` → the
  typed text appeared on the guest's screen (`box login: mcp`).

Two bugs were found and fixed by these runs, both worth knowing about:

1. **Port numbering.** eNSP numbers ports continuously across the `<interface>`
   elements that declare them, so AR2220's "GE x1" followed by "GE x2" is
   GE0/0/0, GE0/0/1, GE0/0/2. Numbering each group from zero produced a duplicate
   GE0/0/0 and shifted every link index after it.
2. **`ensp_processes` matched itself.** The server runs as `ensp-mcp.exe`, so a
   *prefix* match on `"ensp"` made `ensp_status` report `ensp_running: true` with
   eNSP closed. It now matches exact process names.

## Known limitations

* **No text extraction.** The console is a bitmap; getting characters back needs
  OCR, which this server deliberately does not do. Tools return the image, and
  MCP clients can read images directly.
* **Line echo pacing.** VRP echoes every character; very long lines are chunked,
  but a fast paste into a slow emulated UART can still drop characters. Use
  `ensp_console_send_lines` (it paces) rather than one huge `ensp_console_type`.
* **Devices whose images are absent.** `NE40E`, `NE5000E`, `NE9000`, `CE6800`,
  `CE12800`, `CX` and `USG6000V` need an external image that the eNSP installer
  does not ship (`CE.img`, `NE40E.img`, `vfw_usg.vdi`…). Topologies using them
  are valid but those devices cannot start until the image is imported.
* **Windows-oriented.** Discovery uses the Windows registry and `tasklist`; the
  topology tools are portable.

## Provenance

All measurements were taken on a real installation. The console finding is
backed by [`evidence/boot_capture.log`](evidence/boot_capture.log), produced by
[`evidence/capture_boot.py`](evidence/capture_boot.py), which captures the serial
pipe from the moment the VM is powered on. Console screenshots taken along the
way are in [`evidence/`](evidence/).

MIT.
