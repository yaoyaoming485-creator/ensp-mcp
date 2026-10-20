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
| `A[8:12]` | 4 | guest IP (`sockaddr_in.sin_addr`) |
| `A[12:16]` | 4 | netmask (0 ⇒ skip `SIOCSIFNETMASK`) |
| `A[16:26]` | 10 | device id, first 10 bytes |
| `B[0:27]` | 27 | device id, last 27 bytes (37 total = the ssid) |

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

**The socket must stay open.** `main()` registers `atexit(power_off)` and
`startup()` joins the `proc_msg` thread, so the moment the control socket
closes the guest powers itself off.

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
`uartmode2="server,\\.\pipe\<name>"`. A VirtualBox-created host pipe only
grants the host **read** access — opening it `O_RDWR` fails with
`ERROR_ACCESS_DENIED` (13), while `O_RDONLY` succeeds.

Inverting the roles fixes it. With the UART configured as
`server="false" hostMode="HostPipe"`, **VirtualBox connects to a pipe the
host creates**, so the host owns the instance and its ACL:

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

```bash
# 1. host-side pipe server + media server + control client
python scripts/host_side.py            # see src/ensp_mcp/agentd.py

# 2. boot the guest
VBoxManage startvm MYR2 --type headless

# 3. talk to the router
python scripts/vrp_cli.py --guest 192.168.56.101 display version
```
