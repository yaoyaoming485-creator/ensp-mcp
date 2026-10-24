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

## 2. The startup gate: three events the client owns

`CAgentServer::InitCmdService` fails unless some other process has already
created a named event:

```asm
4083ac  push 0x45f4c0            ; L"VBOXSERVEROK"
4083b1  push 1                   ; bInheritHandle
4083b3  push 0x1f0003            ; EVENT_ALL_ACCESS
4083b8  call ds:0x45a2f8         ; OpenEventW
4083c0  test esi,esi
4083c8  push 0x45f4dc            ; "CAgentServer::InitCmdService - Failed to OpenEvent"
```

`0x45f4dc` is that error string verbatim, which is exactly the line the log
shows. Three events are involved, and the server only ever *opens* them:

| Event | Name VA | Created by | Opened by |
| --- | --- | --- | --- |
| `VBOXSERVEROK` | `0x45f4c0` | client `0x43c08e` | server `0x4083b8` |
| `MEDIAPORT` | `0x45f244` | client `0x43c017` | server `0x408141` |
| `WAITFORVBOXINIT` | `0x462c0c` | client | server `0x414cab` |

The client side is explicit about which string it creates — e.g. at
`0x43c00c`:

```asm
43c006  mov  edi,ds:0x593434     ; KERNEL32!CreateEventW
43c00c  push 0x5a407c            ; L"MEDIAPORT"
43c011  push 0                   ; bInitialState
43c013  push 1                   ; bManualReset
43c015  push 0                   ; lpEventAttributes
43c017  call edi
```

and the same shape at `0x43c083` for `L"VBOXSERVEROK"` (call at `0x43c08e`).

`VBOXSERVEROK` has **exactly one cross-reference in the whole image** (the
`push` at `0x4083ad`). That single fact settles the next question on its own:
there is no second code path that could name the event differently, so nothing
can route around it.

Observed failure, in this order:

```
[ERROR] CAgentServer::InitCmdService - Failed to OpenEvent
[ERROR] CAgentServer::Startup - Failed to InitCmdService.
[ERROR] CVBoxServer::Startup - Failed to Startup.
```

## 3. What `manual` actually means

`GetPrivateProfileIntW(L"con_server", L"manual", 0, <path>)` sits at
`0x408281`-`0x4082c9` and its result is tested at `0x4082c9`:

```asm
4082c9  test ebx,ebx
4082cb  jne  0x4083f4            ; manual != 0
```

**`manual == 0`** (`0x4082d1`): the server picks a port, then *writes* it into
both `[vbox_server] listenport` and `[sim_server] LISTENPORT` of
`config_server.ini`.

**`manual != 0`** (`0x4083f4`): it *reads* `[vbox_server] listenport` and binds
it:

```asm
4083f4  mov  ecx,ds:0x477ac4     ; cfg path
4083fb  push 0                   ; default
4083fd  push 0x45f430            ; L"listenport"
408402  push 0x45f2b4            ; L"vbox_server"
408407  call esi                 ; GetPrivateProfileIntW
40842b  call ds:0x45a574         ; htons(port)
408441  call ds:0x45a564         ; bind(sock, {AF_INET, 0.0.0.0:port}, 16)
408447  cmp  eax,ebp
408449  jne  0x4083ac            ; success -> the OpenEvent above
```

On failure it logs `"CAgentServer::InitCmdService - Failed to bind cmd socket
,ErrorCode(%d)"` (`0x45f478`). On success it falls into `0x4083ac`, the same
`OpenEventW`. `manual=1` **cannot** bypass the event gate; all it changes is
where the port comes from.

That reading is confirmed by the stock values: one launch left
`listenport=9236` / `LISTENPORT=9236`, the next left `61793` — a fresh port
every run, written back into both keys. So `manual=1` means "keep the port I
pinned instead of choosing one", nothing more.

Note also that the command port is **independent of `[con_server] MNG_PORT`**
(65514, read by the client at `0x48644e`): the stock pair was 9236, not 65514.
There is no `MNG_PORT` string anywhere in `eNSP_VBoxServer.exe`.

## 4. The command channel is UDP datagrams, not XML

This is a single fixed-size request/reply protocol on a datagram socket:

| What | Where | Evidence |
| --- | --- | --- |
| socket | `0x40823c` | `WS2_32!#23` = `socket`; handle stored at object `+0x4c` |
| receive | `0x40853a` | `WS2_32!#17` = `recvfrom` |
| reply | `0x408ab7` | `push 0x10; push &addr; push 0; push 0x114; push buf; push sock; call #20` = `sendto(sock, buf, 276, 0, &addr, 16)`. 276 is the StartDevice reply only — the length is per-op, see §5 |
| request size | `0x4086d0` | `push 0xa3c` (`operator new`), then `rep movs` of `0x28a` dwords = **2600 bytes** |
| client send | `0x4eb4f7` | `push 0xa28; ...; call sendto` |

One datagram is one message: no length prefix, no NUL, no stream framing. The
client then reads the reply with a `0x1000` buffer at `0x4eb522`.

The reply length is **not** fixed. `0x114` is what StartDevice returns; the
memory-status opcode returns 10 bytes and the file-transmit query 296. Each
handler passes its own length to `sendto`, and the destination is a sockaddr
kept in the server object (`+0x54` for the memory-status reply at
`0x409c33`), i.e. the last requester — not an address rebuilt from the request.

The embedded TinyXML is used for device configuration files and XML printing.
It has nothing to do with this socket.

## 5. Opcode dispatch

The opcode is the **big-endian** 16-bit word at request offset 4:

```asm
4085f4  movzx eax,WORD PTR [ebx+0x4]
4085fa  call  ds:0x45a594        ; ntohs
408606  cmp   eax,0x13           ; 19
40860d  sub   eax,5
408610  cmp   eax,5
408613  ja    0x4086a8           ; op < 5 or op > 10
408619  jmp   DWORD PTR [eax*4+0x4086b0]
```

The table at `0x4086b0` holds six entries, and the rest are compare chains —
`19` at `0x40864d`, `21` at `0x40867c`, `22` at `0x408670`, `25` at `0x40868b`,
and `0x4d1` (1233) at `0x4086a3`. Anything else returns the opcode unchanged
without doing work.

| Opcode | Handler | Method (from the strings it logs) | Reply |
| --- | --- | --- | --- |
| 5, 6, 7 | `0x4086d0` (via `0x408620`) | `CAgentServer::StartDevice` | 276 |
| 8 | `0x408e10` (via `0x40862f`) | `StopDevice` | — |
| 9 | `0x409100` (via `0x40863e`) | `DeleteDevice` | — |
| 10 | `0x4093e0` (via `0x40867c`) | `SyncCfg` | — |
| 19 | `0x409c60` | `QueryFileTransmitPort` | 296 |
| 21 | `0x4093e0` (same as 10) | `SyncCfg` | — |
| 22 | `0x409a00` | `Shutdown` | 276 |
| 25 | `0x4096f0` | `ExportCfg` | — |
| 1233 | `0x409be0` | `GetMemoryStatus` | 10 |

The method names are the ones the handlers themselves print, so they are read
off the binary rather than guessed from the client's command names. Note that
5/6/7 share one entry point (`0x4086d0` only allocates `0xa3c` and copies the
2600-byte request — the enqueue step), so a single entry does not mean a single
command: the worker that picks the request up dispatches again.

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

When `manual == 0` the port it picks comes from a base table at `0x473224`,
13 words: `2001 3001 4001 5001 6001 7001 8001 9001` then
`60001 61001 62001 63001 64001`. Observed picks — 4474, 61793, 7390, 9904,
9368, 64824 — all sit in those ranges.

`[con_server] MNG_PORT` (65514) is outside every one of them, so the allocator
can never collide with it. A `manual=1` port is whatever the ini says and can
therefore collide with anything, `MNG_PORT` included.

## 6. Starting it standalone

The recipe that works, verified end to end:

1. Create the three events (`CreateEventW(NULL, FALSE, TRUE, name)`), and hold
   them open for as long as the server runs.
2. Write `manual=1` and a pinned `[vbox_server] listenport` with
   `WritePrivateProfileStringW`.
3. Start `eNSP_VBoxServer.exe`.

Measured result: the process stays alive, `0.0.0.0:<port>` is a UDP listener
owned by it, the log gains no `[ERROR]` lines, no VM is touched and
`eNSP_Client.exe` is not needed.

Step 2 must be done with the profile API. Rewriting the ini as plain text drops
the section headers — that happened during this work and produced a
`config_server.ini` whose keys sat outside any section, which the server then
read as `manual=0` and silently took the wrong branch.

## 7. Not established

* The layout of the 2600-byte request beyond `+4` = opcode, and the fields of
  each reply.
* **What the eNSP client answers the guest.** `ProcCtrlCmd` type 0 registers
  the caller as the client and `ProcData` forwards the probe, so the reply is
  produced outside this binary; its bytes are still unknown. This is the last
  piece of `agentd-protocol.md` §③c.
* Who reads the peer dwords at `CIface+0x00`/`+0x04`. `ProcData` does not, so
  either another routine does or they are written for a path not traced yet.
* Which port an interface socket (`CIface+0x10`/`+0x14`) binds to. It is not
  written back to any ini, so a caller that wants to register as the client has
  to know it some other way — most likely it is the `dest:` port the host
  itself wrote into the guest's `resource.cfg`.
* Whether `[con_server] MNG_PORT` is ever bound by the client. It is read at
  `0x48644e`; the allocator in §5c cannot produce 65514, but that says nothing
  about the client.
* The second channel behind `"@@@@%d"` (`0x45e398`) and the map built at
  `0x41cd20`, which the client-side command names (`IsRegister`, `Init`,
  `CreateDevice`, `CleanResource`, …) are thought to use. Not traced.

## 8. The log lies while the process runs

`%LOCALAPPDATA%\eNSP\Log\VBoxServer_<date>.log` is **not** flushed while the
server runs. A live process left the file's timestamp sitting at the moment of
the previous run for over an hour, with the process healthy and listening the
whole time.

So "no new `[ERROR]` lines" is not evidence that a request was handled — or
that startup succeeded. Judge startup by the process and its listening socket,
and judge a request by its reply. The lines do appear once the process exits.

The server also has its own logger (`0x4256c0`) and does not use
`log4cxx.xml`; `log4cxx.dll` in that directory belongs to something else.
