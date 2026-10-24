# eNSP guest agent (`agentd`) — reverse-engineering notes

Everything here was recovered from the **guest binary** `agentd` that ships
inside an eNSP AR device image, plus live observation of a running eNSP
installation. Nothing is inferred from documentation.

| Item | Value |
| --- | --- |
| File | `agentd`, 41,599 bytes, ELF 32-bit LSB i386, dynamically linked, **not stripped** |
| SHA-256 | `c2f686c9d2dfd512978d5b6f0b3611b3e0a9404bc2417efdc83a73ca2f2c9ad5` |
| Source files (from `.symtab`) | `serial.c`, `ssid.c`, … |
| Started by | `/opt/bootlocal.sh` → `sudo /mnt/sda1/dev/upload/agentd static` |
| Key globals | `0x8051b20` ssid (37 B) · `0x8052784` media_port · `0x8051224` msg queue id |

The binary is not stripped, so `objdump -d` gives readable disassembly with
real symbol names. Ghidra was not usable on this host (no install, and the
Windows build rejects ELF), so the analysis was done with `objdump` plus a
small ELF section reader for resolving string addresses.

> **The binary itself is not redistributed here.** `agentd` is a vendor file
> from the eNSP device image, so it is deliberately not committed (see
> `.gitignore`). The hash above identifies it exactly, and it can be pulled
> straight out of any eNSP install:
>
> ```bash
> # Linux / WSL
> sha256sum /mnt/sda1/dev/upload/agentd
> # on the guest, or from a mounted device image
> ```
>
> The device image is a VirtualBox disk; mount its partition and the file is
> at `dev/upload/agentd`.

---

## 1. The four channels
```
        HOST                                                GUEST
  ┌──────────────┐                                ┌──────────────────┐
  │  eNSP client │                                │  Tiny Core Linux │
  └──────┬───────┘                                │   + VRP (dev0/1/2)│
         │                                        └────────┬─────────┘
   ① SERIAL   COM2 pipe  ◄──────────────────────────────►  /dev/ttyS1
   ② CONTROL  ────────────►  TCP 56789  (guest listens)
   ③ MEDIA    ◄────────────  TCP 192.168.56.1:<media port>  (guest dials back)
   ④ CONSOLE  ────────────►  TCP 40000   (VRP's own CLI)
```

### ① Serial — the configuration handshake

`static_config()` (`0x0804a4a4`) opens `/dev/ttyS1`, then:

```c
u32 challenge = htonl(rand());                 /* 4 bytes to the host  */
write_cfg_msg thread  -> writes the challenge
read(fd, A, 28);                              /* the host's reply A   */
read(fd, B, 27);                              /* and reply B          */
if (*(u32*)A != challenge) -> "Invalid key."  /* only the challenge matters */

memcpy(id,      A + 0x10, 0x0a);               /* device id, part 1    */
memcpy(id+0x0a, B,       0x1b);               /* device id, part 2    */
set_dev_id(id);                               /* -> global ssid (37 B)*/
set_media_port(*(u16*)(A + 4));               /* ntohs() applied here */
```

Layout of the 28 + 27 byte reply:

| Offset | Size | Meaning |
| --- | --- | --- |
| `A[0:4]` | 4 | challenge, echoed verbatim |
| `A[4:6]` | 2 | media port — **network byte order** (`ntohs` is applied) |
| `A[6:8]` | 2 | **padding — the guest never reads it** |
| `A[8:12]` | 4 | guest IP (`sockaddr_in.sin_addr`) |
| `A[12:16]` | 4 | netmask (0 ⇒ skip `SIOCSIFNETMASK`) |
| `A[16:26]` | 10 | device id, first 10 bytes |
| `A[26:28]` | 2 | **padding — the guest never reads it** |
| `B[0:27]` | 27 | device id, last 27 bytes (37 total = the ssid) |

The two padding gaps were confirmed in `do_static_config()`: the parser
jumps from `A+4` (the media port) straight to `A+8` (the IP) and from
`A+0x10` (the device id) to the end of the 28 bytes. **The handshake
carries no peer information at all** — the guest's address, the media port
and its own id are all it learns.

The guest then configures its own network:

```c
strncpy(ifr.ifr_name, "eth0", 15);
memcpy(ifr.ifr_addr, &addr, 16);
ioctl(s, SIOCSIFADDR    = 0x8916, &ifr);   /* "Set IP address failed"   */
if (reply[12] != 0) ioctl(s, SIOCSIFNETMASK = 0x891c, &ifr);
ifr.ifr_flags |= 0x41;                      /* IFF_UP | IFF_RUNNING      */
ioctl(s, SIOCSIFFLAGS   = 0x8914, &ifr);   /* "Bring network up failed" */
```

**The byte order is a real trap.** The media port is read as a host u16 and
then passed through `ntohs()`, so it must be written in *network* order.
Writing 2000 little-endian makes the guest log `Media port is: 53255`
(`0x07D0` byte-swapped), which is exactly what was observed.

### ② Control — 44-byte frames on TCP 56789

The guest **listens**; the host connects. Frames are
`[u32 big-endian code][37-byte ssid][3 bytes padding]`.

| Code | Direction | Meaning |
| --- | --- | --- |
| 0 | guest → host | boot (carries the ssid) |
| 1 | host → guest | "go" — releases `start_state` |
| 2 / 3 | guest → host | prepare OK / failed |
| 4 / 5 | guest → host | DEV0 started / failed |
| 6 / 7 | guest → host | DEV1 started / failed |
| 8 / 9 | guest → host | DEV2 started / failed |

State machine (`control`, `0x0804ad9b`): a function-pointer table drives
`entry_state → start_state → booting_state → rebooting_state → exit_state`,
with `lookup_transitions()` choosing the next state.

`proc_msg()` (`0x0804adf2`) owns the control socket and checks the reply:

```c
n = recv(sock, buf, 0x2c, 0);              /* 44 bytes               */
code = ntohl(*(uint32_t *)buf);
if (memcmp(buf + 4, own_ssid, 0x25) == 0 && code == 1) {
    config_received = 1;                    /* releases start_state    */
}
```

**The "go" reply must carry the guest's own ssid** — the host copies it out
of the boot frame, which carries it. A reply with a different ssid is
silently ignored and the guest never starts.

**The socket must stay open.** `main()` registers `atexit(power_off)` and
`startup()` joins the `proc_msg` thread, so the moment the control socket
closes the guest powers itself off. A *read timeout* is not a closed
connection: the channel is silent once the device is up, and treating the
timeout as a fatal error closes the socket and kills the device.

**The connect must not be abandoned either.** The guest creates its control
thread about two seconds after the serial handshake and accepts the
connection then. A connect that is cut short in that very moment leaves the
guest with a dead socket: its `accept()` fails, it aborts with
`Send boot message to control socket failed` and powers itself off. Keep the
attempt alive (a long connect timeout) and start it as early as possible.

### ③ Media — the guest dials *back* to the host

`send_ssid()` (`0x0804d730`):

```c
fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
connect(fd, {AF_INET, htons(media_port), inet_addr("192.168.56.1")}, 16);
for (;;) {
    recvfrom_msg_queue(buf, 0xf4, 7);       /* SysV queue, type 7      */
    memcpy(buf + 0xb6, ssid, 0x25);
    send(fd, buf + 4, 0xf4, MSG_NOSIGNAL);  /* 244-byte frame           */
}
```

The 244-byte frame is `[240 bytes payload][37-byte ssid at offset 178]`.
With no listener the guest logs
`Failed to connect to remote media server: Connection refused.` and the SSID
never reaches the host.

**The listening socket must stay open for the whole life of the driver.** The
guest's media client connects more than once; a server that closes its
listening socket after the first accept refuses the re-connect, and the guest
gives up and powers itself off.

The frames are the guest's messages for *other* devices: the ssid at offset
178 is the receiver's, so the host routes a frame to the device that owns
that ssid (the eNSP host does the same — `PortLink` / `comport` /
`localport` / `peerport` in `eNSP_VBoxServer.exe`).

### ③b Interface traffic — UDP, the *fourth* thing the host must do

`resource.cfg` gives every interface a UDP socket:

```
interface[N] = socket[local:<own ip>:10000+N dest:<host ip>:<port> role:client protocol:udp]
```

The VRP sends its interface traffic (a 4-byte keepalive, `00 00 00 01`, was
observed on every interface) as UDP datagrams from its own port `10000+N` to
the host. The host has to

* **give every device its own port** — the port baked into the image
  (`30000+N`) is the same for all devices, so two devices would send to the
  same host port and the host could no longer tell them apart. The rewrite
  points the device at its own media port; TCP (the agent's media channel)
  and UDP (the interface traffic) can share a port number.
* **relay the datagrams to the peer** the topology links the interface to:
  the source port `10000+N` says which interface the datagram belongs to, and
  the peer receives it on its own `10000+M`.

Without the relay the interface never comes up, the VRP's interface process
dies (a register dump in `/dev/upload/dev1.log`), the agent exits and the
guest powers itself off.

### ③c What the guest does *not* know

The guest never learns its peer's address from the handshake, from
`resource.cfg` or from the control frames — the host has to tell it, and the
exact format of that announcement is still unknown. Until it does, the VRP
sends keepalives but never ARPs for the peer, so the link is "up" while no
IP traffic flows.

Where the answer comes from is no longer a guess. `eNSP_VBoxServer` does not
answer the probe: `CInterface::ProcData` clears a counter, records the sender,
then forwards the datagram to a **client address** registered on that interface
(`sendto` at `0x4103c0`). That client is registered by a 16-byte control
message — type 0 on `CInterface::ProcCtrlCmd` (`0x410470`), which also sends a
4-byte status back to the caller. So a replacement host can stand in for the
GUI: register on the interface socket, receive the probes, answer them itself.
*What* to answer is the remaining unknown, and it lives in `eNSP_Client.exe`.
See [`host-cmd-channel.md`](host-cmd-channel.md) §5b.

`dhcp_config()` (`0x08049ae4`) shows the machinery the guest uses for
configuration: a UDP socket bound to `0.0.0.0:60001` (it walks up the port
numbers until one is free), and `send_cfg_msg()` (`0x08049814`) sending a
4-byte message to `{AF_INET, htons(56789), inet_addr(...)}`. `write_cfg_msg()`
writes 4 bytes to the "configuration socket" in a loop until a flag is set.
Those 4 bytes are the same shape as the interface keepalives, so the
configuration exchange is very likely the mechanism that hands the peer to
the device.

### ③d How the guest sets up its own network

`do_static_config()` (`0x08049d93`) reads the 28-byte reply and applies it
with `ioctl()` on the interface **`eth0`**:

| ioctl | Number | What |
| --- | --- | --- |
| `SIOCSIFADDR` | `0x8916` | the address from `A[8:12]` ("Set IP address failed") |
| `SIOCSIFNETMASK` | `0x891c` | the mask from `A[12:16]`, skipped when 0 ("Set IP netmask failed") |
| `SIOCGIFFLAGS` / `SIOCSIFFLAGS` | `0x8914` / `0x8913` | "Bring network up failed" |

So the address the host hands over in the handshake is the address the
guest's VRP really owns — `resource.cfg`'s `local:` has to match it, which
is why the rewrite changes both.

### ③e The host side of the same protocol

`eNSP_VBoxServer.exe` (the component the eNSP client drives) names the
per-interface attributes it works with:

```
comport  sockoffset  systemmac  localip  localport  name  peerip  peerport
```

`peerip` + `peerport` are the *peer's* endpoint, `localip` + `localport`
the device's own, and `sockoffset` the offset of the device's socket block:
**one host port per (device, interface)**, so the peer can tell the
interfaces of the sender apart. `CInterface::CreateInterfaces` binds
**one socket per interface**, and `CInterface::CaptureData ip is :%s`
(with the format `%u.%u.%u.%u`) shows it parsing the captured bytes as an
address. `CInterface::ProcData` then does

```c
sendto(sock, buf, len, 0, interface->sockaddr_at_0x20, 16);
```

— the destination is the address the device asked for, the port comes from
the host's own configuration.

What the device actually sends is four bytes, once a second, per
interface: `00 00 00 01` and `00 00 00 02` — it is asking the hub for
something it does not know yet, and it keeps asking. The answer's exact
layout is the one thing still missing; the address of the peer is the
obvious candidate, and this implementation sends it.

This is the per-interface traffic only. The same binary's *control* side — the
socket the eNSP client uses to create and start devices — is a different
channel and is described in [`host-cmd-channel.md`](host-cmd-channel.md).

### ③f Where it still breaks

The VRP never ARPs for its peer, so only the 4-byte keepalives travel.
The peer's address is in none of the places the guest reads at boot: not in
`resource.cfg` (eNSP's own client leaves that file untouched — it keeps the
stock `192.168.1.x` addresses), not in the handshake, not in `db.dat`. The
eNSP client injects it at run time (`CAgentServer::ExportCfg`,
`CAgentServer::SyncCfg`, `CAgentStaticCfgProcess::CfgService`), and that
injection is the one piece still missing.

The crash itself is in the VRP's main thread — `cap.log` in the guest
holds the backtrace (`./dev1() [0x815903d]` … `__libc_start_main+0x133`).

### ③g NAT mode — the same protocol, one address different

A host-only NIC in VirtualBox needs an NDIS filter
(`oracle_VBoxNetLwf`, bound to the adapter as "VirtualBox NDIS6 Bridged
Networking Driver"). When that binding is broken, *every* machine with a
host-only NIC refuses to start:

```
VBoxManage.exe: error: Failed to open/create the internal network
'HostInterfaceNetworking-VirtualBox Host-Only Ethernet Adapter'
(VERR_INTNET_FLT_IF_NOT_FOUND)
```

Nothing in the protocol changes when the guests are moved to VirtualBox's
NAT: only the addresses do.

| | host-only | NAT |
| --- | --- | --- |
| guest address | `192.168.56.101` | `10.0.2.101` |
| guest's view of the host | `192.168.56.1` | `10.0.2.2` (the NAT gateway alias) |
| VRP console | `192.168.56.101:40000` | `127.0.0.1:40000+n` (forwarded) |
| agentd control | `192.168.56.101:56789` | `127.0.0.1:56789+n` (forwarded) |
| interface traffic | `192.168.56.1:30000+n` | `10.0.2.2:30000+n` |

`resource.cfg`'s `dest:` is the guest's view of the host, so it becomes
`10.0.2.2`; the `local:` stays the guest's own address. The forwarding rules
are plain VirtualBox port forwards:

```
console,tcp,127.0.0.1,40000+n,10.0.2.101,40000
agentd,tcp,127.0.0.1,56789+n,10.0.2.101,56789
localN,udp,127.0.0.1,10000+n,10.0.2.101,10000+n
```

Measured result: both devices reach DEV2, stay up for hours (the host-only
setup used to die after 110–125 s), and the console answers on the first
command.

### ③h The guest hard-codes the host address

`192.168.56.1` appears **three times** in the guest binary
(`strings agentd | grep 192.168`), and the media server's address is one of
them. The guest therefore always dials `192.168.56.1:<media port>`, and the
interface traffic is addressed by `resource.cfg`'s `dest:` — which the host
does control.

Consequence: behind NAT the media channel cannot work (the guest does not
know the gateway alias), so the devices boot and answer, but the layer-2
traffic between them still does not.

### ③i The device identity, and why the AR image has no MUX-VLAN

`resource.cfg` carries the device's personality:

```
device_type = 33
interface[0] = socket[local:192.168.56.101:10000 dest:192.168.56.1:30000 …]
```

The guest booted from `AR_Base` reports itself as an **AR201**
(`VRP (R) software, Version 5.130 (AR200 V200R003C00)`, "Board Type: AR201")
with nine Ethernet ports — whatever the `.topo` says. That build has **no
MUX-VLAN command set**:

```
[Huawei-vlan100] mux-vlan
                ^
Error: Unrecognized command found at '^' position.

[Huawei-Ethernet0/0/1] port mux-vlan enable vlan 10
                                    ^
Error: Unrecognized command found at '^' position.
```

`/dev/upload/cmdtree.bin` does contain a MUXVLAN module ("Module ID( …
4-MUXVLAN …)") and `infoeng.res` the whole MUX-VLAN error catalogue ("Please
enable MUX-VLAN in vlan command mode.", "The separate VLAN already exists in
the principal VLAN." …), but those are the *shared* resources of all VRP
products — the AR201 personality does not enable the module. The command
tree itself is a flat table of names (`"mux-vlan"` sits in a 0x50-byte slot
at 0x60b60 of `cmdtree.bin`), so the product gate is not a flag that can be
flipped by hand.

### ③j The guest's payload format: a 4096-byte XOR key

`/dev/upload/dev_base_0`, `dev_base_1` and `dev_base_2` (237 KB, 1.8 MB,
99.5 MB) are **not** compressed and not packed in any complicated way.
agentd's `decompress()` (0x0804c2df) is a misnomer: it XORs the file with a
4096-byte key that lives in agentd's `.data` section (vaddr 0x08050220,
file offset 0x7220), restarting the key at the beginning of every
4096-byte block; the tail of the file (`size % 4096` bytes) is keyed from
offset 0 again.

XOR is its own inverse, so the same key encrypts and decrypts. Decrypted,
the three files are ordinary Linux binaries:

| file | size | what it is |
| --- | --- | --- |
| `dev_base_0` | 237 KB | the capture helper (libpcap) |
| `dev_base_1` | 1.8 MB | the **AR200 forwarding plane** ("AR200 PTM G.SHDLS …") — the process that crashes in `cap.log` |
| `dev_base_2` | 99.5 MB | the **VRP core** (the whole VOS/VRP module list) |

`scripts/pack_payload.py` implements this and is verified by a byte-identical
round trip; the key is derived from the guest's own agentd, so no vendor
file is copied into this repository.

This also explains what a **switch** guest would need: the switch's *Linux*
payloads, not the Windows one. `devices/LSW/s5700/eNSP_Switch.exe` (140 MB)
is a **Windows PE** (94 MB of `.text`, a 43 MB `.textbss` BSS) and contains
no embedded ELF — the guest needs `dev_base_*` built from the switch's Linux
VRP, which eNSP ships as a downloadable *device package* (the client offers
"Please import device %s's package." / "Click 'Yes' to download") and injects
over its file channel (`CFileTransmit::PrePut` / `PutData`).

### ③k What the host does with the guest's 4-byte query

`CInterface::ProcData` in `eNSP_VBoxServer.exe` (0x410180) receives the
guest's datagram and:

* if the length is exactly 4 → clears a flag on the interface
  (`iface+0x8 = 0`) and **does not** call `CaptureData` (so those four bytes
  are not parsed as an IP) and does not answer;
* stores the sender's socket address in the interface object
  (`iface+0x30`, 16 bytes);
* forwards the payload to the **eNSP client** (the GUI) — that is the
  "send data to client" path — and, for everything longer than 4 bytes, to
  the guest ("send data to vbox").

So the answer to the guest's "where is my peer?" is produced by the **eNSP
client**, not by the host process: the host is only the courier between the
guest and the GUI. A lab that drives the guests without the GUI has to
answer those four bytes itself — and the exact layout the client uses is
still unknown.

### ④ Console — the router's own CLI

The VRP process listens on **guest TCP 40000** and speaks plain text. On a
live host `netstat` shows the eNSP client using exactly this:

```
TCP  192.168.56.1:51442  192.168.56.2:40000  ESTABLISHED  <eNSP_VBoxServer>
```

Connecting to it returns the router's real output — no screenshots, no OCR.

---

## 2. The device images

| File | Size | Notes |
| --- | --- | --- |
| `dev_base_0` | 237,484 | compressed form of `dev0` |
| `dev_base_1` | 1,798,000 | compressed form of `dev1` |
| `dev_base_2` | 99,542,936 | compressed form of `dev2` |
| `cmdtree.bin` | 5,187,553 | VRP command tree |
| `db.dat` | 2,029,285 | VRP database |
| `infoeng.res` | 2,067,737 | VRP resources |

`agentd` unpacks them with `decompress()` (`0x0804c2df`) at boot and then
`fork()`s + `execlp("./dev0")` for each. `dev1` contains `hi3ms.huawei.com`
endpoints and a GVRP stack — they are genuine Huawei VRP (AR200 V200R003C00).

### `resource.cfg` must match the guest's real address

The shipped `resource.cfg` pins the interface sockets:

```
interface[0] = socket[local:192.168.1.3:10000 dest:192.168.1.104:30000 ...]
```

If the guest's actual IP is different, VRP cannot bind and dies:

```
bind: Cannot assign requested address
ERROR :Start port 0 fail,return -1
VPORT_Hal_PortEnable failed! u32Ret = -1
```

Rewriting `local:` / `dest:` to the addresses the host hands out fixes it —
see `scripts/patch_resource.py`.

---

## 3. Bringing a guest up without the eNSP client

The eNSP client's own device VMs use
`uartmode2="server,\\.\pipe\<name>"`. Opening such a VirtualBox-created
server pipe `O_RDWR` from the host **does** work while the instance is
free: `os.open(r"\\.\pipe\OSPF_R1", os.O_RDWR | os.O_BINARY)` succeeded,
and reads and writes both worked across many full device boots — the same
held for another VBox-created name, `\\.\pipe\R1-COM1`. An earlier revision
of this note said a VBox-created pipe only grants the host read access and
that `O_RDWR` always fails with `ERROR_ACCESS_DENIED` (13); that was too
absolute.

The denial is still seen, but the exact condition that triggers it was
**not** pinned down. The hypothesis is that a VirtualBox host pipe is a
single-instance named pipe, so the failure comes from that one instance
already being connected or owned — the guest side has connected, or another
process holds it — rather than from the pipe's ACL being read-only.

Supporting but not conclusive: a purpose-built single-instance named pipe
(`CreateNamedPipeW` with `nMaxInstances = 1`, `PIPE_ACCESS_DUPLEX`) accepted
`os.open(..., os.O_RDWR | os.O_BINARY)` from the host and returned OK while
that instance was free. That is consistent with "the instance has to be free"
and inconsistent with "the ACL is read-only", but it does not reproduce the
VirtualBox case, so the trigger stays a hypothesis.

`server="false"` is still the recommended arrangement, for robustness
rather than as a hard requirement. With the UART configured as
`server="false" hostMode="HostPipe"`, **VirtualBox connects to a pipe the
host creates**, so the host owns the instance and its security descriptor
and a guest that connects first cannot lock the host out:

```xml
<Port slot="1" enabled="true" IOBase="0x2f8" IRQ="3" server="false"
      path="\\.\pipe\MYR2-COM2" hostMode="HostPipe"/>
```

`VBoxManage modifyvm --uart-modeN` cannot express this (it writes
`hostMode="HostDevice"`), so the `.vbox` XML is edited directly and the VM is
re-registered.

With that in place a full boot succeeds:

```
pipe: VirtualBox connected
pipe: challenge 31 d2 81 20
pipe: sent A+B (media_port=20002 ip=192.168.56.101)
media: guest connected from ('192.168.56.101', 48100)
control: connected 192.168.56.101:56789
control: <- boot
control: -> 1 (go)
control: <- prepare OK
control: <- DEV0 started
control: <- DEV1 started
control: <- DEV2 started
```

and the console then answers:

```
<Huawei>display version
Huawei Versatile Routing Platform Software
VRP (R) software, Version 5.130 (AR200 V200R003C00)
Copyright (C) 2011-2012 HUAWEI TECH CO., LTD
Huawei AR201 Router uptime is 0 week, 0 day, 0 hour, 1 minute
```

---

## 4. Things that are *not* the console

* **The VirtualBox screen.** For an AR device it shows the Tiny Core Linux
  guest at a login prompt, because the router runs in a separate process on
  the same machine. `ensp_console_screen` therefore returns a Linux screen.
* **The `\\.\pipe\OSPF_*` pipe.** It carries the *configuration* handshake
  (challenge + reply), and only that. Reading it and expecting ASCII is
  reading the wrong channel.

---

## 5. Reproduction

The whole protocol is driven by `enspctl`, which brings a topology up without
the eNSP application:

```bash
# 1. build a topology
enspctl topo create --spec spec.json -o lab.topo

# 2. create the lab: one VirtualBox machine per device, cloned from
#    eNSP's AR_Base, with the UART in client mode and resource.cfg rewritten
enspctl lab create demo --topo lab.topo --root ./demo --with-vms

# 3. bring it up (the drivers keep running in a background supervisor,
#    because the guests power off as soon as the control socket closes)
enspctl lab up --root ./demo

# 4. talk to the router
enspctl run R1 "display version" --root ./demo

# 5. stop it
enspctl lab down --root ./demo
```

The pieces behind `enspctl` are in `src/ensp_mcp/`:

| Module | Role |
| --- | --- |
| `agentd.py` | the protocol itself: handshake builder, pipe server, media server, VRP console |
| `host.py` | drives one guest: pipe + media + control threads |
| `lab.py` | the lab: machines, addresses, pipes, `resource.cfg` |
| `rescfg.py` | offline rewrite of `resource.cfg` inside a disk image |
| `supervise.py` | detached supervisor that keeps the control connections alive |
| `cli.py` | the `enspctl` command line |

A single device can also be driven by hand:

```bash
# host-side pipe server + media server + control client for one device
python scripts/host_side.py --pipe '\\.\pipe\MYR2-COM2' --guest-ip 192.168.56.101
VBoxManage startvm MYR2 --type headless
python scripts/vrp_cli.py --guest 192.168.56.101 display version
```

