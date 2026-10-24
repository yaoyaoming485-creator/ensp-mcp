# The host command channel — `eNSP_VBoxServer.exe`

Everything above this file describes the **guest** agent (`agentd`), the
protocol that makes a device boot. This file describes the other end: the
host-side component the eNSP client drives to create, start and stop devices.

The two are independent. `ensp-mcp` does not use this channel — it starts the
VMs itself with `VBoxManage` and then speaks `agentd` to them — but the eNSP
client uses it for everything, so it explains what the GUI is doing and what a
GUI-free replacement would have to reproduce.

## The artifacts

| File | Size | SHA-256 |
| --- | --- | --- |
| `C:\Program Files\Huawei\eNSP\vboxserver\eNSP_VBoxServer.exe` | 503,808 | `44403E16FBF2C7E493BACD71EFFC634E1890C9290E7CCE2F4A069841B3ED83AD` |
| `C:\Program Files\Huawei\eNSP\eNSP_Client.exe` | 3,547,136 | `E2CDD4C77EACA23FBC84C9E466E6847988CC42A349DF3EDE0B65C1C1F8080B7E` |

Both are PE32 console images for i386 with image base `0x400000` and
`FileAlignment == SectionAlignment`, so **virtual address = file offset +
0x400000** for the whole image. Every address below is a virtual address and
can be read straight out of the file.

Symbols are stripped (7 entries in the symbol table), so the names used here
are ours; the anchors are addresses and imported functions.

## 1. Where its configuration really comes from

`0x424470` is a one-instruction shim around `0x4243a0`, and `0x4243a0` is the
path base:

```asm
4243db  push 0x206
4243f3  lea  eax,[esp+0x1a]
4243fc  call 0x43fd30            ; memset(buf, 0, 0x206)
424405  push 0x1c                ; CSIDL_LOCAL_APPDATA
424407  lea  ecx,[esp+0x1c]
42440c  push 0                  ; hwnd = NULL
42440d  call ds:0x45a35c        ; SHELL32!SHGetSpecialFolderPathW
424433  push 6
424435  push 0x467604           ; L"\eNSP\"  (6 wide characters)
42443c  call 0x4133a0           ; append
```

`0x45a35c` resolves to `SHELL32.dll!SHGetSpecialFolderPathW`, and `0x43fd30`
is `memset` — not `GetModuleFileNameA`, and the return value of
`SHGetSpecialFolderPathW` is not even checked. There is no module-path branch
in this function: the executable's own directory never participates.

The base is therefore **`%LOCALAPPDATA%\eNSP\`**, and the two config names in
`.rdata` are relative to it:

| String | VA | Resolved file |
| --- | --- | --- |
| `\cfg\config_server.ini` | `0x45f3a0` | `%LOCALAPPDATA%\eNSP\cfg\config_server.ini` |
| `VBoxServer\config_vbox.ini` | `0x46239c` | `%LOCALAPPDATA%\eNSP\VBoxServer\config_vbox.ini` |

The same-named files under `Program Files\Huawei\eNSP` are **never read**.
This is worth stating plainly because it is a trap: editing them changes
nothing. (Confirmed the hard way — a full copy of the tree on another drive
with `manual=1` set produced exactly the same failure, while the
`%LOCALAPPDATA%` copy is the one that gets written.)

## 2. The startup gate: three events, and one of them must be signalled

`CAgentServer::InitCmdService` does not create the events it needs; it opens
three that the **client** creates, and then it blocks on one of them:

```asm
408133  push 0x45f244            ; L"MEDIAPORT"
40813a  push 0x1f0003            ; EVENT_ALL_ACCESS
40813f  call ds:0x45a2f8         ; OpenEventW
408147  test esi,esi
408149  jne  0x40816d
408152  push 0x45f258            ; "...Failed to open media port event, ErrorCode(%d)"
...
40816d  push 0xffffffff          ; INFINITE
40816f  push esi
408170  call ds:0x45a330         ; WaitForSingleObject
408176  push esi
408177  call ds:0x45a2f0         ; CloseHandle
...
40819e  push 0x4084c0            ; the command-service thread
4081ad  call 0x43c101            ; _beginthreadex
```

**`MEDIAPORT` must be created already signalled**
(`CreateEventW(NULL, TRUE, TRUE, L"MEDIAPORT")`). Created unsignalled, the
process stops dead at `0x408170` and *never reaches* `0x40819e`: the command
socket is already created and bound — `netstat` shows the port — but no thread
ever reads it, so every request is silently dropped. The thread count gives it
away (5 or 6 threads instead of 14).

The other two are the ordinary kind: `VBOXSERVEROK` is opened at `0x4083b8`
(`push 0x45f4c0; push 1; push 0x1f0003`) and `WAITFORVBOXINIT` at `0x414cab`
(`0x462c0c`). The server opens them, signals them and closes them; the client
is the one that waits.

| Event | Name VA | Created by | Opened by |
| --- | --- | --- | --- |
| `VBOXSERVEROK` | `0x45f4c0` | client `0x43c08e` | server `0x4083b8` |
| `MEDIAPORT` | `0x45f244` | client `0x43c017` | server `0x408141`, **waited on** |
| `WAITFORVBOXINIT` | `0x462c0c` | client | server `0x414cab` |

The client side is explicit about which string it creates — at `0x43c00c`:

```asm
43c006  mov  edi,ds:0x593434     ; KERNEL32!CreateEventW
43c00c  push 0x5a407c            ; L"MEDIAPORT"
43c011  push 0                   ; bInitialState
43c013  push 1                   ; bManualReset
43c015  push 0                   ; lpEventAttributes
43c017  call edi
```

`VBOXSERVEROK` has **exactly one cross-reference in the whole image** (the
`push` at `0x4083ad`), so nothing can name it differently on another path.

Observed failure when it is missing, in this order:

```
[ERROR] CAgentServer::InitCmdService - Failed to OpenEvent
[ERROR] CAgentServer::Startup - Failed to InitCmdService.
[ERROR] CVBoxServer::Startup - Failed to Startup.
```

`0x45f4dc` is the `Failed to OpenEvent` string verbatim.

## 3. What `manual` actually means

`GetPrivateProfileIntW(L"con_server", L"manual", 0, <path>)` sits at
`0x408281`-`0x4082c9` and its result is tested at `0x4082c9`:

```asm
4082c9  test ebx,ebx
4082cb  jne  0x4083f4            ; manual != 0
```

**`manual == 0`** (`0x4082d1`): the server asks its allocator for a port, then
*writes* it into both `[vbox_server] listenport` and `[sim_server] LISTENPORT`
of `config_server.ini`.

**`manual != 0`** (`0x4083f4`): it *reads* `[vbox_server] listenport` and binds
exactly that, so the port stops moving between launches:

```asm
4083f4  mov  ecx,ds:0x477ac4     ; cfg path
4083fb  push 0                   ; default
4083fd  push 0x45f430            ; L"listenport"
408402  push 0x45f2b4            ; L"vbox_server"
408407  call esi                 ; GetPrivateProfileIntW
40842b  call ds:0x45a574         ; htons(port)
408441  call ds:0x45a564         ; bind(sock, {AF_INET, 0.0.0.0:port}, 16)
```

On failure it logs `"CAgentServer::InitCmdService - Failed to bind cmd socket
,ErrorCode(%d)"` (`0x45f478`). `manual=1` **cannot** bypass the event gate;
both branches still fall into the `OpenEventW` at `0x4083ac`.

The stock values confirm the semantics: one launch left `listenport=9236` /
`LISTENPORT=9236`, the next `61793` — a fresh port every run, written back into
both keys.

## 4. The command channel is UDP datagrams, not XML

| What | Where | Evidence |
| --- | --- | --- |
| socket | `0x40823c` | `WS2_32!#23` = `socket`; handle stored at object `+0x4c` |
| receive | `0x40853a` | `WS2_32!#17` = `recvfrom` |
| request size | `0x4086d0` | `push 0xa3c` (`operator new`), then `rep movs` of `0x28a` dwords = **2600 bytes** |
| client send | `0x4eb4f7` | `push 0xa28; ...; call sendto` |

A request is **2600 bytes**, `[0:2]` = `htons(0xa28)`, `[4:6]` = `htons(opcode)`.
One datagram is one message: no NUL, no stream framing.

Measured round trip against a live server on `127.0.0.1:65514`:

```
op 1233 ->  10 bytes  0001 000a 04d1 00001e02
op    19 -> 296 bytes  0001 0128 0013 ...
```

So the reply is framed as

| Offset | Field |
| --- | --- |
| `+0x00` | `0x0001` status |
| `+0x02` | total length, big-endian — equals the datagram size |
| `+0x04` | opcode, echoed |
| `+0x06` | payload |

and the length is **per-op**, not fixed: 276 for most commands, 296 for
`GetMemoryStatus`, 10 for `QueryFileTransmitPort`.

The destination is the sockaddr of the last requester, kept in the server
object (`+0x54`), not an address rebuilt from the request bytes.

The embedded TinyXML is used for device configuration files and XML printing.
It has nothing to do with this socket.

## 5. Opcodes

The opcode is the big-endian 16-bit word at request offset 4:

```asm
4085f4  movzx eax,WORD PTR [ebx+0x4]
4085fa  call  ds:0x45a594        ; ntohs
408606  cmp   eax,0x13           ; 19
40860d  sub   eax,5
408610  cmp   eax,5
408613  ja    0x4086a8           ; op < 5 or op > 10
408619  jmp   DWORD PTR [eax*4+0x4086b0]
```

The table at `0x4086b0` reads, as six dwords: `0x408620, 0x408620, 0x408620,
0x40862f, 0x40863e, 0x40867c`. The rest are compare chains — `19` at `0x40864d`,
`21` at `0x40867c`, `22` at `0x408670`, `25` at `0x40868b`, and `0x4d1` (1233)
at `0x4086a3`. Anything else returns the opcode unchanged.

| Opcode | Handler | Method (from the strings the handler logs) | Reply |
| --- | --- | --- | --- |
| 5, 6, 7 | `0x4086d0` (via `0x408620`) | `StartDevice` | 276 |
| 8 | `0x408e10` (via `0x40862f`) | `StopDevice` | 276 |
| 9 | `0x409100` (via `0x40863e`) | `DeleteDevice` | 276 |
| 10 | `0x4093e0` (via `0x40867c`) | `SyncCfg` | 276 |
| 19 | `0x409c60` | `GetMemoryStatus` (`GlobalMemoryStatusEx` → `MEMORYSTATUSEX`) | 296 |
| 21 | `0x4093e0` (same as 10) | `SyncCfg` | 276 |
| 22 | `0x409a00` | `Shutdown` | 276, **then the process exits** |
| 25 | `0x4096f0` | `ExportCfg` | 276 statically — **no reply was observed** in a live test |
| 1233 | `0x409be0` | `QueryFileTransmitPort` | 10 |

The method names are the ones the handlers print, so they are read off the
binary rather than guessed from the client's command vocabulary. 5/6/7 share
one entry point (`0x4086d0` only allocates and copies the request — the enqueue
step); the worker that picks the request up dispatches again.

`SyncCfg` (10 and 21) and `ExportCfg` (25) matter beyond this channel: they are
the two ways the eNSP client pushes configuration into a guest at run time, and
`agentd-protocol.md` §③f names them as the suspected carrier of the peer
address.

## 5b. The 16-byte control message on the interface socket

`CInterface::ProcData` (`0x410180`) checks the message type first — `ntohl` at
`0x41029a`, `cmp eax,0x4d2` at `0x4102a0` — and routes that one type through
`vtable+0x10` to `CInterface::ProcCtrlCmd` (`0x410470`). Everything else is
interface traffic:

* a 4-byte datagram clears the counter at `CIface+0x08` (`0x410351`);
* any datagram copies its source sockaddr into `CIface+0x30`
  (`0x410367`-`0x41037f`);
* if a client address is registered (`CIface+0x22 != 0`) the datagram is
  forwarded to it: `sendto(CIface+0x10, buf, len, 0, CIface+0x20, 16)`
  (`0x4103c0`).

So the server never answers the "where is my peer" probe itself — it forwards
it. `ProcCtrlCmd` takes a 16-byte message and accepts exactly two types:

| Field | Meaning |
| --- | --- |
| `+0x00` | type, big-endian: `0` or `1234` (`0x4d2`) |
| `+0x04` | — |
| `+0x08` | address, network order |
| `+0x0c` | port, big-endian |

* **type 0** sends a 4-byte status back to the sender (`sendto` at `0x41055e`;
  the payload is `0` or `0xffffffff` once `CIface+0x08` has been incremented
  past 10), then registers the sender as the client: `htons(ntohl(msg+0xc))`
  into `CIface+0x22` (`0x4105ee`) and `msg+0x8` verbatim into `CIface+0x24`
  (`0x410608`).
* **type 1234** writes the peer instead: `ntohl(msg+0xc)` into `CIface+0x00`
  and `ntohl(msg+0x8)` into `CIface+0x04` (`0x4104c7`-`0x4104f6`), as two
  host-order dwords rather than a sockaddr.

This is the interception point a GUI-free lab needs: send type 0 to an
interface socket, and that interface's probes start arriving at your address.
What the eNSP client then *replies to the guest* is still not known — see §7.

## 5c. The port numbers the server hands out

When `manual == 0` the port comes from a base table at `0x473224`, 13 words:
`2001 3001 4001 5001 6001 7001 8001 9001` then
`60001 61001 62001 63001 64001`. Observed picks — 4474, 61793, 7390, 9904,
9368, 64824 — all sit in those ranges. `[con_server] MNG_PORT` (65514) is
outside every one of them, so the allocator can never collide with it; a
`manual=1` port is whatever the ini says and can therefore collide with
anything.

The **interface** sockets are different. `CInterface::CreateInterfaces`
(`0x40f690`) creates one datagram socket per interface and takes its port from
the same allocator, which reads the live UDP endpoint table
(`iphlpapi!GetUdpTable`, `0x45a5bc`) to find a free one. Interfaces bind
`0.0.0.0` and `192.168.56.1`; the port is stored in `CIface+0x0c`/`+0x0e` and
is **not written back to any ini**.

That is recoverable at run time, and this is the practical way to reach them:
start the server, then enumerate the UDP endpoints owned by that process —
everything except the command port is an interface socket.

## 6. Starting it standalone

1. Create the three events, holding them open for the process's lifetime:
   `MEDIAPORT` **must** be `CreateEventW(NULL, TRUE, TRUE, name)` — already
   signalled; the other two are created unsignalled.
2. Write `manual=1` and a pinned `[vbox_server] listenport` with
   `WritePrivateProfileStringW`.
3. Start `eNSP_VBoxServer.exe`.
4. Prove it works by **getting a reply**, not by watching the port.

Measured result: the process stays alive, `0.0.0.0:<port>` is a UDP listener
owned by it, `eNSP_Client.exe` is not needed, no VM is touched, and a
2600-byte request gets a framed reply (`§4`).

Step 2 must be done with the profile API. Rewriting the ini as plain text drops
the section headers — that happened during this work and produced a
`config_server.ini` whose keys sat outside any section, which the server then
read as `manual=0` and silently took the wrong branch.

## 7. Not established

* The layout of the 2600-byte request beyond `[0:2]` length and `[4:6]`
  opcode. In particular the device name/model fields `StartDevice` needs: with
  a device name at payload `+0x6f0` the server stops answering, i.e. it went
  off to start something, so the field is close to right but the rest is not
  mapped. This is the last step before "start a device, find its interface
  socket, register as the client" can be run end to end.
* **What the eNSP client answers the guest.** `ProcCtrlCmd` type 0 registers
  the caller as the client and `ProcData` forwards the probe, so the reply is
  produced outside this binary; its bytes are still unknown. This is the last
  piece of `agentd-protocol.md` §③c.
* Who reads the peer dwords at `CIface+0x00`/`+0x04`. `ProcData` does not, so
  either another routine does or they are written for a path not traced yet.
* Whether `[con_server] MNG_PORT` is ever bound by the client. It is read at
  `0x48644e`; the allocator in §5c cannot produce 65514, but that says nothing
  about the client.

**Settled negative:** the second channel is not needed. The string-command
dispatcher at `0x41dec0` — the one that would consume the `"@@@@%d"` template
(`0x45e398`) and the map built at `0x41cd20` with the client-side command names
(`IsRegister`, `Init`, `CreateDevice`, `CleanResource`, …) — has **no callers**
in this binary, so the command channel does not use it and a GUI-free
replacement does not have to reproduce it.

## 8. Two traps

**The log lies while the process runs.** `%LOCALAPPDATA%\eNSP\Log\VBoxServer_<date>.log`
is not flushed until the process exits: a live server left the file's timestamp
sitting at the previous run for over an hour. So "no new `[ERROR]` lines" is not
evidence that startup succeeded or that a request was handled. The lines do
appear once it exits. (The server uses its own logger at `0x4256c0`, not
`log4cxx.xml`.)

**A bound socket is not a working service.** Because of the `MEDIAPORT` gate in
§2 the server can bind its command port and then never read it. Process alive
plus port listening is *not* enough: check the thread count, and then send a
request and expect a reply.
