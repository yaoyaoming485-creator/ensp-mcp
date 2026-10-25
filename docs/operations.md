# Operations — run an eNSP lab with `enspctl`

This is the runbook: how to bring a lab up, configure it, verify it, and what to
check when it misbehaves. The reverse-engineering detail lives in
[`README.md`](../README.md) and [`evidence/`](../evidence/).

Every command runs on Windows. **The eNSP client GUI is not needed.**

---

## 0. Prerequisites

| Needed | Why |
| --- | --- |
| VirtualBox | `VBoxManage.exe` must be runnable |
| eNSP | installed once, so the base machine images (`AR_Base`, …) are registered; it does not have to be running |
| WSL | rewriting `resource.cfg` inside a disk image mounts it in WSL (`wsl -u root` must work) |
| Python | use the project's own venv, `.venv\Scripts\enspctl.exe` — not `uvx` |

```powershell
enspctl status      # VBoxManage path, whether eNSP is running, machine count
enspctl models      # the device catalogue
```

> `status` reports `eNSP running: True` whenever `eNSP_VBoxServer.exe` exists. That
> process is eNSP's own host service and does not get in the way; the GUI is
> `eNSP_Client.exe`, which is a different process.

---

## 1. A lab in five minutes

### 1.1 Describe the topology

```json
{
  "devices": [
    {"name": "R1", "model": "AR1220", "ports": ["GE0/0/0"]},
    {"name": "R2", "model": "AR1220", "ports": ["GE0/0/0"]}
  ],
  "links": [
    {"a": "R1", "b": "R2", "type": "Copper", "a_port": 0, "b_port": 0,
     "a_guest_port": 8, "b_guest_port": 8}
  ]
}
```

```powershell
enspctl topo validate --spec spec.json
enspctl topo create --spec spec.json -o lab.topo
```

> `a_guest_port` / `b_guest_port` matter — see §2. They are written to the
> `.topo` as `guestSrc` / `guestTar`. Without them a panel port maps to the
> guest port with the same number, which is what the real client does.

### 1.2 Create the lab (clones the machines)

```powershell
enspctl lab create mylab --topo lab.topo --root D:\work\mylab --with-vms
```

`--with-vms` clones; without it only the lab metadata is written. `--nic hostonly`
is the default; use `--nic nat` when the host-only adapter is broken (§4.1).

### 1.3 Start it

```powershell
enspctl lab up --root D:\work\mylab
```

What that does, without eNSP:

1. rewrites each guest's `resource.cfg` so its sockets point at this host;
2. serves a serial named pipe per device and answers the guest's handshake
   (IP / netmask / media port / device id);
3. dials the guest's control channel on `56789`, sends `go`, waits for
   `prepare OK` → `DEV0` → `DEV1` → `DEV2`;
4. starts the media server the guest connects back to;
5. opens one UDP relay socket per interface (`30000 + device*16 + iface`) and
   relays frames to the peer guest.

`lab is up` means ready. Add `--foreground` to watch it.

### 1.4 Configure

```powershell
enspctl run --root D:\work\mylab R1 "display ip interface brief"
enspctl run --root D:\work\mylab R1 "system-view" "interface Ethernet0/0/8" "ip address 10.0.0.1 24" "quit"
enspctl run --root D:\work\mylab R2 "system-view" "interface Ethernet0/0/8" "ip address 10.0.0.2 24" "quit"
```

### 1.5 Verify: ping

```powershell
enspctl run --root D:\work\mylab R1 "ping -c 4 10.0.0.2"
```

Measured on this project:

```
Reply from 10.0.0.2: bytes=56 Sequence=1..4 ttl=255
4 packet(s) transmitted, 4 packet(s) received, 0.00% packet loss
```

### 1.6 Tear down

```powershell
enspctl lab down --root D:\work\mylab
```

---

## 2. Interface names, and why the address goes on `Ethernet0/0/8`

The stock image runs the **AR201 board** — eight switch ports plus one uplink —
and says so:

```
AR201's Device status: ... 0 - AR201 Present PowerOn Registered Normal Master
```

| Symptom | Cause |
| --- | --- |
| `display interface brief` lists `Ethernet0/0/0` … `Ethernet0/0/8` | they are all physical ports |
| `display ip interface brief` lists **only** `Ethernet0/0/8` | the first eight are switch ports and cannot hold an address |
| `interface Ethernet0/0/0` answers `Unrecognized command` | same |

So, until the board can be selected from the host:

- put the address on **`Ethernet0/0/8`**, the only routed port;
- wire the link to that guest port with `a_guest_port: 8` / `b_guest_port: 8`;
- once `Ethernet0/0/8` is `Physical up / Protocol up`, ping works.

> Four attempts to move the guest onto the model's board — `device_type`, a
> ten-line AR1220 interface table, an official `vrpcfg.zip`, and a
> `producttype.efs` dword — were all measured **negative**. They are written up
> in [`evidence/agentd-protocol.md`](../evidence/agentd-protocol.md) §③f-1…③f-3.
> Do not repeat them.

---

## 3. Links and port mapping

- `a_port` / `b_port` are the *panel* port numbers of the topology;
- `a_guest_port` / `b_guest_port` are the VPORT indices inside the image;
- the real client makes the two the same number; when the image's board does not
  match the model you have to say which guest port the panel port is wired to.

Relay side: panel port N = guest port N = the N-th physical port in the VRP.

---

## 4. Troubleshooting

### 4.1 `VERR_INTNET_FLT_IF_NOT_FOUND`, the machine will not start

```
Failed to open/create the internal network 'HostInterfaceNetworking-VirtualBox Host-Only Ethernet Adapter'
```

VirtualBox is classifying that adapter as **bridged** (`modifyvm` warns
`Interface "..." is of type bridged`), so host-only can never open it.

```powershell
# A. use NAT — the tool sets the forwards up itself
enspctl lab create ... --nic nat

# B. create a second host-only adapter and point the machines at it
VBoxManage hostonlyif create                                     # -> "...#2"
VBoxManage hostonlyif ipconfig "VirtualBox Host-Only Ethernet Adapter" --ip 192.168.99.1 --netmask 255.255.255.0
VBoxManage hostonlyif ipconfig "VirtualBox Host-Only Ethernet Adapter #2" --ip 192.168.56.1 --netmask 255.255.255.0
VBoxManage modifyvm <vm> --nic1 hostonly --hostonlyadapter1 "VirtualBox Host-Only Ethernet Adapter #2"
```

Undo it afterwards: remove `#2` and put the original adapter back on
`192.168.56.1`.

### 4.2 `the device disappeared - restarting it`, over and over

This was a real bug: the supervisor dialled the guest's `56789` every ten seconds
to check liveness, while the guest's `agentd` calls `accept()` **exactly once**
for its whole life. Unaccepted connections stay in the 128-deep accept queue, so
after about two minutes the queue was full, handshakes started failing, and a
perfectly healthy device was power-cycled — which looked like "the device dies
after ~190 s".

Now liveness is read from the control connection the host already holds, and any
remaining probe closes with a RST (`SO_LINGER {1,0}`). If the message comes back,
look for a re-introduced "connect and close" probe.

### 4.3 The console connects but commands are not executed

`192.168.56.101:40000` accepting a TCP connection and then resetting usually
means the guest is dying (VPORT or VRP exited), not that the VRP refused. Check
the lab log for a restart first.

### 4.4 Every interface is `down/down`

Writing an interface table that disagrees with the guest's board makes
`VPORT_Hal_PortEnable` fail and leaves every port down. Do not use `--board`
on a guest whose board does not match the model; use the guest-port override in
§2 instead.

### 4.5 `lab up` drops what `lab patch` wrote

That used to happen: `lab up` re-patched the image from the pristine base for the
addresses, without the board. Fixed — the patch marker remembers a board and
later patches keep writing it. If it comes back, check
`lab.marker_board_state()`.

### 4.6 The lab is up but ping fails

In this order:

1. `display ip interface brief` — is the port `Physical up / Protocol up` and the
   address right?
2. is the address on the guest port the link is actually wired to (§2, §3)?
3. the relay census: `udp relay: iface N: … four-byte … longer`. Only four-byte
   datagrams means the interface is still in control state; `longer` means real
   frames are being relayed;
4. `… orphaned` means that interface has no peer, so its frames are dropped —
   normal for an unlinked port.

---

## 5. Do not

- modify anything under `C:\Program Files\Huawei\eNSP` (analysis is read-only);
- change eNSP's own `ini` (`manual`, `LISTENPORT`, …) — it is needed by the GUI;
- start or stop machines somebody else is using without asking;
- run `unregistervm --delete` outside your own temporary lab — it deletes disks.

---

## 6. What is actually verified

| Item | State |
| --- | --- |
| serial handshake, control channel, `DEV0/1/2` boot | ✅ measured |
| interfaces leave control state and relay real frames (arp-request/reply, icmp) | ✅ measured |
| two AR1220s ping each other | ✅ 4/4, 0% loss |
| devices stay up | ✅ 330 s without a restart |
| no eNSP client GUI at all | ✅ command line only |
| the guest presents the model's board (`GigabitEthernet0/0/0`) | ❌ unsolved — use the guest-port override |
