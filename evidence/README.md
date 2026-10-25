# Reverse-engineering evidence

These files are the raw measurements behind the claims in the top-level
[README](../README.md).

Measured on Windows 11 (build 22621) with eNSP `V1.3.00.100` and Oracle
VirtualBox `7.2.18 r175117`, against device VM `R1`, an eNSP AR clone whose disk
chain resolves to `C:\Program Files\Huawei\eNSP\VBoxServer\AR_Base\AR_Base.vdi`.

## `host-cmd-channel.md` — the host side

The other end of the same problem: `eNSP_VBoxServer.exe`, the component the
eNSP client drives to create and start devices. It is not on this project's
path — `ensp-mcp` starts the VMs itself and then speaks `agentd` — but it
explains what the GUI is doing, and it records the two things that cost the
most time here:

* configuration is read from `%LOCALAPPDATA%\eNSP\` and **never** from
  `Program Files`, so a copied tree on another drive changes nothing;
* the command channel is fixed-size **UDP datagrams** (2600-byte request,
  framed reply, big-endian opcode at offset 4) — not XML, despite the embedded
  TinyXML;
* it only starts serving if the `MEDIAPORT` event it opens is **already
  signalled**: otherwise it blocks before creating the thread that reads the
  socket, so the port is bound and every request is silently dropped.

It also documents the startup gate (the server only *opens* the three named
events, so it cannot be started on its own without creating them first), the
opcode table, and the 16-byte control message that lets a replacement host
register itself as the client and receive the guest's "where is my peer"
probes.

## `agentd-protocol.md` — the guest agent

The full protocol, recovered by disassembling the guest's `agentd` binary and
confirmed against a live guest.

It documents, among other things, that **the guest's control port is accepted
exactly once** (`accept()` is called a single time, at `0x0804b309`, with a
128-deep listen backlog at `0x0804b157`), that a lost control connection ends
the channel for good without killing the guest, and that the host therefore
**must not probe port 56789** — it has to read liveness from the control
connection it already holds.

`agentd` is a vendor file from the eNSP device image, so it is **not
redistributed in this repository** (see `.gitignore`). The notes identify it
exactly — 41,599 bytes, ELF 32-bit LSB i386, not stripped, SHA-256
`c2f686c9d2dfd512978d5b6f0b3611b3e0a9404bc2417efdc83a73ca2f2c9ad5` — and it
can be pulled out of any eNSP install (it lives at `dev/upload/agentd` inside
the device image).

The guest's **VPORT** binary, `dev0`, is in the same position: it is the
process that actually owns the interface sockets and answers the link
handshake, and it is **not** redistributed here. It ships inside the device
image as `dev/upload/dev_base_0`, compressed with a repeating 4096-byte XOR
key (`agentd`'s `decompress`, `0x0804c2df`); the decrypted file is 237,484
bytes, ELF 32-bit LSB i386, SHA-256
`7817a46b2637792064bc33e5078cc87a0a4e2a0525928c4780e75cadb5ef2d36`. The key
needs no assumption: `dev/upload/dev_base_1` is the same transform applied to
`dev/upload/dev1`, and XORing the two yields it directly.

```python
# dev_base_* and dev* are all inside dev/upload/ of the device image
key = bytes(a ^ b for a, b in zip(open('dev_base_1', 'rb').read(),
                                  open('dev1', 'rb').read()))[:4096]
open('dev0', 'wb').write(
    bytes(c ^ key[i % 4096]
          for i, c in enumerate(open('dev_base_0', 'rb').read())))
```

`agentd-protocol.md` documents the four channels the guest uses to talk to
the host, the byte layouts, the state machine, and the two traps that cost
the most time:

* the media port in the handshake is **network byte order** (`ntohs` is applied
  by the guest, so writing it little-endian makes the guest log
  `Media port is: 53255` for a port of 2000);
* the pipe direction is easy to get wrong: a VirtualBox-created host pipe can
  be opened `O_RDWR` while its instance is free, so inverting the roles
  (`server="false"` + a host-side pipe server) is the robust choice rather
  than a requirement — the host then owns the instance and its security
  descriptor, so a guest that connects first cannot lock it out.

It also supersedes the negative result below: the serial pipe *is* worth
reading — it carries the configuration handshake — but it is **not** where the
router's CLI lives.

The protocol is implemented in `src/ensp_mcp/agentd.py` and driven from the
command line by `enspctl` (see `lab.py`, `host.py`, `supervise.py`):

## `capture_boot.py` + `boot_capture.log`

The decisive experiment for the *screen* question. The script powers the VM
off, starts it, connects to the console pipe as fast as it can (measured:
**0.00 s**, the pipe already exists), and logs every byte with a timestamp for
240 seconds — covering the whole boot.

Result: from `t=7.5 s` onward the pipe emits one fixed 4-byte frame per second,
and nothing else.

```
+   7.51s len=    1 printable=    1 hex=6a
+   7.51s len=    1 printable=    0 hex=07
+   7.51s len=    1 printable=    0 hex=e7
+   7.51s len=    1 printable=    0 hex=ab
...
t= 180.1s total=692 bytes
```

Every 4-byte group in the file is `6a 07 e7 ab`. Zero ASCII across a full boot.
The frame differs between runs (`56 a9 8b f8`, `3e 3f 3f`, `5b 6a`, `6a 07 e7 ab`),
which is why the earlier `probe_console.py` output looked like plausible-but-garbled
console text rather than obvious noise.

> **Correction.** The "4-byte frame" is the *configuration challenge*
> `htonl(rand())` that `agentd` writes to the serial port and waits to have
> echoed back. It is not console traffic — but it is also not a heartbeat, and
> treating it as a dead end was wrong. Answering it properly is what brings the
> device up. See `agentd-protocol.md` §1.

Reproduce with:

```bash
python capture_boot.py R1 '\\.\pipe\OSPF_R1' 240 boot_capture.log
```

## `probe_console.py`

The first, simpler probe: open the pipe, send a `\r` to wake the console, drain
for N seconds, and try `display version` if a `<...>` prompt appears. This is the
script that produced the garbage readings that motivated the full capture above.

## `console-*.png`

Screenshots of the same VM's **virtual screen** (720x400, 80x25 VGA text mode):

| File | What it shows |
| --- | --- |
| `console-01-login-prompt.png` | The AR guest boots Tiny Core Linux (`Core Linux`, kernel `3.0.21-tinycore`, hostname `box`) and stops at `box login:` |
| `console-02-typed-root.png` | After `VBoxManage controlvm R1 keyboardputstring "root"` + Enter — **proof that keyboard injection reaches the guest** |
| `console-03-login-incorrect.png` | The `Login incorrect` response; root has no empty password |
| `console-04-live.png` | A later live capture |

Taken with:

```
VBoxManage controlvm R1 screenshotpng <file>
```

The same screen is what eNSP presents over VRDP (the VM listens on TCP 3389).
Note that this is the *guest's* screen, not the router's: the router runs as
a separate process on the same machine and serves its CLI on TCP 40000.

