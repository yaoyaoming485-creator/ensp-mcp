# ensp-mcp

An MCP server for Huawei **eNSP** (Enterprise Network Simulation Platform).
It gives an agent three things eNSP does not expose on its own: the ability to
**author topologies as files**, the ability to **read and drive any running
device's console**, and the ability to **talk to the router itself** and get
its exact output as text.

Everything here was reverse-engineered against a real install — eNSP
`V1.3.00.100` with Oracle VirtualBox `7.2.18` on Windows 11. No eNSP SDK, no
undocumented DLL calls, no GUI automation: just the `.topo` format, the guest
agent's wire protocol, and the VirtualBox control interface eNSP already drives.

## What was reverse-engineered

### 1. The `.topo` format

A topology is XML. It declares `<?xml version="1.0" encoding="UNICODE" ?>` —
**and that declaration is a lie.** The file must be **UTF-8 without a BOM**.
All 40 example topologies shipped with eNSP 1.3.00.100 are UTF-8, as are the
files eNSP itself saves, and an older release (V1.2.00.390) is the same.

Get this wrong and eNSP shows **no error at all**: it loads the topology as
zero devices and the status bar reads `总数：0`. A UTF-16LE file — which is what
the `encoding="UNICODE"` attribute suggests, and what some third-party
generators emit — is silently discarded. This server wrote UTF-16 until that
was measured; `ensp_read_topology` now reports `encoding`, `has_bom` and
`loads_in_ensp` so the mistake is visible.

The `<topo>` element's `version` attribute is **optional** — plenty of shipped
examples have a bare `<topo>`. When present it is written to match the installed
eNSP (`ensp_status` reports it), falling back to `1.3.00.100`.

```xml
<?xml version="1.0" encoding="UNICODE" ?>
<topo version="1.3.00.100">
  <devices>
    <dev id="UUID" name="R1" poe="0" model="AR2220" settings="" system_mac=""
         com_port="2000" bootmode="0" cx="150.000000" cy="250.000000"
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

Coordinates are floats (`cx="150.000000"`); the inline-editor offsets
`edit_left`/`edit_top` are integers.

The one non-obvious rule: **`srcIndex`/`tarIndex` are indices into the device
model's flattened interface list**, in the order the `<interface>` elements
appear — not slot numbers, not port numbers on the silkscreen. `models.py`
carries that index space per model.

### 2. The device console is a TCP service, not a screen

This is the finding that matters, and it corrects an earlier conclusion in
this project (and in other write-ups of eNSP) that the console could only be
reached by screenshotting the VirtualBox screen.

**The VRP process runs inside the guest, and it serves a plain-text console on
guest TCP 40000.** On a live eNSP host the console connection shows up in
`netstat` as:

```
TCP  192.168.56.1:51442  192.168.56.2:40000  ESTABLISHED  <eNSP_VBoxServer>
```

so the eNSP client itself talks to the router over this socket. Connecting to
it directly returns the router's real output:

```
<Huawei>display interface brief
Interface                   PHY   Protocol InUti OutUti   inErrors  outErrors
Ethernet0/0/0               down  down        0%     0%          0          0
...
Ethernet0/0/8               down  down        0%     0%          0          0
NULL0                       up    up(s)       0%     0%          0          0
<Huawei>
```

That is exact text — no screenshots, no OCR, no window automation, and no
elevation. It also removes the character-drop and paging problems that make
screen-scraping unreliable.

**Reading policy: text only.** Device state is read over this socket and
nowhere else. The screen is never scraped, and the text console is never
allowed to fall back to it: if the console cannot be reached, or a command
does not come back, the call fails loudly (non-zero exit / `ok: false`)
instead of returning a picture or a boot banner. A freshly booted device
answers with `Press any key to get started`, and a single throw-away
newline is not enough to get past it — if that newline arrives before the
device reaches the banner it is lost, and the first command is then eaten as
the key press. The client therefore waits for a real prompt and verifies
that every command was echoed back before it reports success.

The VirtualBox virtual screen is a *different* thing: for an AR device it
shows the **Tiny Core Linux guest** (`Core Linux`, kernel `3.0.21-tinycore`,
hostname `box`) sitting at a login prompt, because the router runs in a
separate process on that same machine. The screen tools
(`ensp_console_screen` / `ensp_console_type` / ...) therefore drive the guest,
not the router — they are kept as **boot diagnostics** ("did the guest come
up?"), and they are not a way to read device state.

### 3. How the guest agent talks to the host

The guest boots Tiny Core Linux and runs `agentd static` (see
`/opt/bootlocal.sh` inside the device image). `agentd` is the piece that
negotiates with the host, and it uses four channels:

| # | Channel | Direction | Purpose |
| --- | --- | --- | --- |
| 1 | serial (`/dev/ttyS1` ↔ host COM2 pipe) | both | configuration handshake |
| 2 | control (`guest:56789`) | host → guest | start the VRP processes |
| 3 | media (`host:192.168.56.1:<port>`) | guest → host | the guest dials **back** to the host |
| 4 | console (`guest:40000`) | host → guest | the router's own VRP CLI |

**Channel 1 — the configuration handshake.** `agentd` writes a 4-byte
challenge `htonl(rand())` to the serial port and then blocks reading the
reply. The reply is 28 bytes followed by 27 bytes:

| Offset | Size | Meaning |
| --- | --- | --- |
| `A[0:4]` | 4 | the challenge, echoed verbatim (otherwise `Invalid key.`) |
| `A[4:6]` | 2 | media port, **network byte order** (the guest applies `ntohs`) |
| `A[8:12]` | 4 | the guest's own IP |
| `A[12:16]` | 4 | netmask (0 ⇒ the guest skips `SIOCSIFNETMASK`) |
| `A[16:26]` | 10 | device id, first 10 bytes |
| `B[0:27]` | 27 | device id, last 27 bytes — 37 bytes total, the "ssid" key |

The guest then configures `eth0` itself with
`ioctl(SIOCSIFADDR/SIOCSIFNETMASK/SIOCSIFFLAGS)` using the IP we supplied, and
runs `ip_test`/`ping_test`. **The only value that has to be correct is the
challenge**; everything else is ours to choose.

**Channel 2 — control.** The guest *listens* on TCP 56789 and speaks fixed
44-byte frames: `[u32 big-endian code][37-byte ssid][3 bytes padding]`.

| Code | Direction | Meaning |
| --- | --- | --- |
| 0 | guest → host | boot (carries the ssid) |
| 1 | host → guest | "go" — releases the start state |
| 2 / 3 | guest → host | prepare OK / failed |
| 4 / 5 | guest → host | DEV0 started / failed |
| 6 / 7 | guest → host | DEV1 started / failed |
| 8 / 9 | guest → host | DEV2 started / failed |

The socket must stay open for the life of the device: `agentd` registers
`atexit(power_off)` and joins its message thread, so closing the control
socket powers the whole guest off.

**Channel 3 — the media server.** This is the counter-intuitive one: the
guest **connects back to the host** on `192.168.56.1:<media port>`, where the
port is the one we handed over in `A[4:6]`. `send_ssid()` then pushes every
SysV message-queue item of type 7 as a 244-byte frame
(`[240 bytes payload][37-byte ssid at offset 178]`). With no listener the
guest logs *"Failed to connect to remote media server: Connection refused."*
and the SSID never reaches the host.

**Device images.** The router itself is three ELF binaries per device —
`dev0`, `dev1`, `dev2` — unpacked at boot from `dev_base_0/1/2` by `agentd`'s
`decompress()`. They are genuine Huawei VRP (the image contains
`hi3ms.huawei.com` endpoints and a GVRP stack). Their `resource.cfg` pins the
interface sockets to a fixed address; if that address is not the one the guest
actually owns, VRP exits with `Start port 0 fail` /
`VPORT_Hal_PortEnable failed!`. `scripts/patch_resource.py` rewrites it.

## Install

Requires Python 3.10+ and Oracle VirtualBox. eNSP itself is only needed for the
runtime tools.

```bash
uv sync
uv run ensp-mcp          # the MCP server
uv run enspctl --help    # the command line
```

## `enspctl` — drive eNSP from the command line

The MCP server is for agents. `enspctl` is the same thing for your shell: it
authors topologies, brings a whole topology up as a **lab** (one VirtualBox
machine per device, cloned from eNSP's own device image), and talks to the
routers.

```console
$ enspctl topo create --spec spec.json -o lab.topo
wrote lab.topo (1767 bytes, utf-8)
  2 devices, 1 links, loads_in_ensp=True

$ enspctl lab create demo --topo lab.topo --root ./demo --with-vms
lab 'demo' created in ./demo
  R1         model=AR2220     vm=demo-R1   ip=192.168.56.101   pipe=\\.\pipe\enspctl-demo-R1
  R2         model=AR2220     vm=demo-R2   ip=192.168.56.102   pipe=\\.\pipe\enspctl-demo-R2
  demo-R1: cloning from AR_Base ...
  demo-R2: cloning from AR_Base ...

$ enspctl lab up --root ./demo
patching resource.cfg ...
  R1: 9 socket line(s) -> local 192.168.56.101, dest 192.168.56.1
  R2: 9 socket line(s) -> local 192.168.56.102, dest 192.168.56.1
preparing the guest agent drivers ...
starting machines ...
  demo-R1: starting
  demo-R2: starting
R1: control: <- boot
R1: control: -> 1 (go)
R1: control: <- prepare OK
R1: control: <- DEV0 started
...
  R1  started
  R2  started
lab is up; press Ctrl-C to stop the drivers

$ enspctl run R1 "display version" --root ./demo
=== display version
Huawei Versatile Routing Platform Software
VRP (R) software, Version 5.130 (AR200 V200R003C00)
...

$ enspctl lab status --root ./demo
lab demo  (./demo)
device     vm                    ip               state      agentd vrp
R1         demo-R1               192.168.56.101   running    yes    yes
R2         demo-R2               192.168.56.102   running    yes    yes

$ enspctl lab down --root ./demo
```

### Commands

| Command | Purpose |
| --- | --- |
| `enspctl topo create --spec s.json -o x.topo` | build a topology from a JSON spec |
| `enspctl topo read x.topo` | parse a topology into JSON |
| `enspctl topo validate --spec s.json` | check a spec |
| `enspctl lab create NAME --topo x.topo --root DIR` | create a lab (add `--with-vms` to clone the machines) |
| `enspctl lab up --root DIR` | patch, start and drive the whole lab |
| `enspctl lab up --root DIR --no-patch` | start without rewriting the guest images |
| `enspctl lab down --root DIR` | stop the machines |
| `enspctl lab status --root DIR` | show the devices and whether they answer |
| `enspctl lab patch --root DIR` | rewrite each device's `resource.cfg` |
| `enspctl lab destroy --root DIR` | stop and remove the machines |
| `enspctl dev list` | every eNSP guest on the host-only subnet |
| `enspctl dev exec R1 "display version"` | run VRP commands, get the exact text |
| `enspctl dev shell R1` | interactive VRP session |
| `enspctl run R1 "display version"` | shorthand for `dev exec` |
| `enspctl status` | the eNSP / VirtualBox environment |
| `enspctl models [category]` | the device model catalogue |
| `enspctl open [lab.topo]` | launch the eNSP application |
| `enspctl handshake 192.168.56.2` | run the agentd control handshake by hand |

That is the whole MCP surface except the guest-screen tools: `ensp_console_screen`,
`ensp_console_type`, `ensp_console_key` and `ensp_console_send_lines` stay on the MCP
server, because the screen is a bitmap of the guest's Linux console and must never be
used to read device state.

### What a lab does

A lab owns a directory with the topology, one VirtualBox machine per device
and a manifest. For every device it:

* clones eNSP's own `AR_Base` machine, so the guest really is an eNSP device
  (the difference disk carries `/dev/upload`: `agentd`, `resource.cfg`, the VRP
  binaries);
* puts it on the host-only segment and assigns it a free address;
* puts its COM2 UART in **client** mode, so VirtualBox dials a pipe server the
  lab runs — preferred over letting VirtualBox create the pipe, since the
  host then owns the instance and a guest that connects first cannot lock it
  out;
* rewrites `resource.cfg` so VRP's interface sockets point at the addresses
  the lab handed out — otherwise VRP exits with
  `VPORT_Hal_PortEnable failed!`. The rewrite is remembered next to the
  machine, so `lab up` does not pay for the (slow) image round trip twice, and
  the image keeps a stable name instead of growing a `-patched` suffix on
  every run. If a rewrite fails, `lab up` aborts instead of starting a lab
  whose guests cannot bind their sockets.

`lab up` then drives every guest the way the eNSP client would: the serial
handshake, the control frames, and the media server.

> The lab's machines are **not** the ones the eNSP application manages, and
> they do not appear in the eNSP canvas. `enspctl` is a parallel, scriptable
> path to the same devices.

### NAT mode, for when the host-only adapter is broken

A host-only NIC in VirtualBox needs an NDIS filter. Once that filter stops
working, **every machine with a host-only NIC refuses to start**:

```
VBoxManage.exe: error: Failed to open/create the internal network
'HostInterfaceNetworking-VirtualBox Host-Only Ethernet Adapter'
(VERR_INTNET_FLT_IF_NOT_FOUND)
```

`enspctl lab create ... --nic nat` puts the devices on VirtualBox's NAT
instead. Every service the host needs — the VRP console, the agentd control
channel and the per-interface traffic ports — is forwarded with `--natpf1` to a
port on the loopback, the guest's view of the host becomes the NAT gateway
alias `10.0.2.2`, and `resource.cfg`'s `dest:` is rewritten to match.

```console
$ enspctl lab create mux --topo lab.topo --root ./mux --nic nat \
      --subnet 10.0.2 --start-ip 101
$ enspctl lab up --root ./mux
  R1: 9 socket line(s) -> local 10.0.2.101, dest 10.0.2.2
  ...
  R1: started
$ enspctl run R1 "display version" --root ./mux
```

The price: the guest **hard-codes** `192.168.56.1` (the host address of the
media server and of the interface traffic), so the media channel does not work
behind NAT. The devices boot, stay up and answer on the text console, but the
layer-2 traffic between two devices still does not come up.

### Pushing a whole configuration

```console
$ enspctl run SW1 --file examples/mux-vlan/mux-vlan-sw1.cfg --root ./mux
```

`--file` takes one command per line; blank lines and lines starting with `#`
are skipped.

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

**Console — the real VRP CLI**

| Tool | Purpose |
| --- | --- |
| `ensp_vrp_list_devices` | find the devices on the host-only subnet and their open ports |
| `ensp_vrp_command` | run VRP commands, get the router's **exact text** back |
| `ensp_vrp_agent_handshake` | run the guest agentd control handshake and report what the device says |

**Console — the VirtualBox screen (diagnostic only)**

| Tool | Purpose |
| --- | --- |
| `ensp_console_screen` | capture the guest's screen; returns the PNG |
| `ensp_console_type` | type text (+ optional Enter) |
| `ensp_console_key` | send a named key (`ctrl-z`, arrows, `F1`…) |
| `ensp_console_send_lines` | send a configuration block, one line at a time |

These drive the *guest's* Linux console, not the router. They are here to
answer "did the device boot?", and they are **not** a way to read device
state: the screen is a bitmap, and this project does not OCR it. Use
`ensp_vrp_command` for anything that talks to the router — it returns exact
text, it does not drop characters, and it is not affected by the paging
prompt.

```jsonc
// ensp_vrp_command(guest_ip="192.168.56.101", commands=["display version"])
{
  "ok": true,
  "output": "=== display version\nHuawei Versatile Routing Platform Software\n\
             VRP (R) software, Version 5.130 (AR200 V200R003C00)\n..."
}
```

### Driving a device without the eNSP client

`scripts/host_side.py` replaces the eNSP client for one device: it is the
pipe server, the media server and the control client in one process.

```bash
# the device's UART must point at a pipe we create (see evidence/agentd-protocol.md)
VBoxManage startvm MYR2 --type headless
python scripts/host_side.py --pipe '\\.\pipe\MYR2-COM2' --guest-ip 192.168.56.101
python scripts/vrp_cli.py --guest 192.168.56.101 display version
```

`scripts/patch_resource.py` rewrites a guest's `resource.cfg` so VRP's
interface sockets point at an address the guest actually owns — without it
VRP exits with `VPORT_Hal_PortEnable failed!`.

### The guest's payload format

The device software in `/dev/upload/dev_base_0|1|2` is **not** compressed:
the guest's `agentd` decrypts it with a plain XOR against a 4096-byte key
that lives in agentd's `.data` section (the key restarts at the beginning of
every 4096-byte block). `scripts/pack_payload.py` takes the key straight out
of an `agentd` binary and encrypts or decrypts a payload file with it — a
round trip is byte-identical, which is how the format was confirmed.

```bash
python scripts/pack_payload.py key path/to/agentd
python scripts/pack_payload.py decrypt dev_base_2 dev2 --agentd path/to/agentd
file dev2      # ELF 32-bit LSB executable, Intel i386
```

### A worked example

```console
$ python scripts/vrp_cli.py --list
192.168.56.2
192.168.56.3
192.168.56.4
192.168.56.5
192.168.56.101

$ python scripts/vrp_cli.py --guest 192.168.56.101 'display version' 'display clock'
=== display version
display version

Huawei Versatile Routing Platform Software

VRP (R) software, Version 5.130 (AR200 V200R003C00)

Copyright (C) 2011-2012 HUAWEI TECH CO., LTD

Huawei AR201 Router uptime is 0 week, 0 day, 0 hour, 1 minute
...
<Huawei>
=== display clock
display clock

2026-10-20 20:51:22

Tuesday

Time Zone(China-Standard-Time) : UTC-08:00

<Huawei>
```

## Verification

Three suites, all green:

| Suite | What it covers |
| --- | --- |
| `tests/smoke.py` | module level: catalogue, `.topo` build/write/read round-trip, validation failures, parsing a real eNSP-authored topology, VirtualBox inventory, screenshot, keyboard injection |
| `tests/test_console_text.py` | the text console: the boot banner must not swallow a command, a command that did not run must fail loudly, and the text path must never touch the screen code |
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

Three bugs were found and fixed, and the third is the instructive one:

1. **Port numbering.** eNSP numbers ports continuously across the `<interface>`
   elements that declare them, so AR2220's "GE x1" followed by "GE x2" is
   GE0/0/0, GE0/0/1, GE0/0/2. Numbering each group from zero produced a duplicate
   GE0/0/0 and shifted every link index after it.
2. **`ensp_processes` matched itself.** The server runs as `ensp-mcp.exe`, so a
   *prefix* match on `"ensp"` made `ensp_status` report `ensp_running: true` with
   eNSP closed. It now matches exact process names.
3. **Wrong file encoding.** The writer emitted UTF-16LE, copying a third-party
   generator instead of reading what eNSP produces. eNSP accepted the file
   silently and showed zero devices. Caught by opening a generated topology in
   eNSP on a second machine — the one thing the automated suites could not do,
   because eNSP requires elevation and refuses injected input from an
   unelevated process. The suites now assert the absence of a BOM, and
   `ensp_read_topology` reports `loads_in_ensp`.

The lesson worth keeping: the test suites verified structure, round-tripping and
the MCP protocol, but never that eNSP *accepted* a generated file. Format
conformance is not the same as the consumer accepting the output.

## Known limitations

* **The screen tools are diagnostic only.** `ensp_console_screen` is a bitmap
  of the guest's Linux console and `ensp_console_type` types into that
  console, so they drop characters and give no text back. They are never used
  to read device state and never act as a fallback for the text console —
  use `ensp_vrp_command` instead, it returns exact text.
* **The VRP console serves one client at a time.** When the eNSP client is
  driving a device it holds that device's console session, and a second
  client connects but receives nothing. The tools report that rather than
  hanging.
* **Devices whose images are absent.** `NE40E`, `NE5000E`, `NE9000`, `CE6800`,
  `CE12800`, `CX` and `USG6000V` need an external image that the eNSP installer
  does not ship (`CE.img`, `NE40E.img`, `vfw_usg.vdi`…). Topologies using them
  are valid but those devices cannot start until the image is imported.
* **Windows-oriented.** Discovery uses the Windows registry and `tasklist`; the
  topology tools are portable.

## Provenance

All measurements were taken on a real installation.

* [`evidence/agentd-protocol.md`](evidence/agentd-protocol.md) — the guest
  agent's four channels, recovered by disassembling the guest's `agentd`
  binary and confirmed against a live guest. (The binary itself is a vendor
  file from the eNSP image and is not redistributed; the notes give its
  SHA-256 and where to extract it.)
* [`evidence/boot_capture.log`](evidence/boot_capture.log), produced by
  [`evidence/capture_boot.py`](evidence/capture_boot.py) — the serial pipe
  captured from the moment the VM is powered on.
* [`evidence/console-*.png`](evidence/) — the guest's virtual screen.
* [`evidence/host-cmd-channel.md`](evidence/host-cmd-channel.md) — the host
  component the eNSP client drives (`eNSP_VBoxServer.exe`): where it reads its
  configuration, the events it waits for, and the fixed-size UDP datagrams it
  exchanges. Not used by `ensp-mcp`; it documents what a GUI-free replacement
  would have to reproduce.

MIT.
