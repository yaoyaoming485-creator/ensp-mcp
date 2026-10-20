# Reverse-engineering evidence

These files are the raw measurements behind the claims in the top-level
[README](../README.md).

Measured on Windows 11 (build 22621) with eNSP `V1.3.00.100` and Oracle
VirtualBox `7.2.18 r175117`, against device VM `R1`, an eNSP AR clone whose disk
chain resolves to `C:\Program Files\Huawei\eNSP\VBoxServer\AR_Base\AR_Base.vdi`.

## `agentd-protocol.md` — the guest agent

The full protocol, recovered by disassembling the guest's `agentd` binary and
confirmed against a live guest.

`agentd` is a vendor file from the eNSP device image, so it is **not
redistributed in this repository** (see `.gitignore`). The notes identify it
exactly — 41,599 bytes, ELF 32-bit LSB i386, not stripped, SHA-256
`c2f686c9d2dfd512978d5b6f0b3611b3e0a9404bc2417efdc83a73ca2f2c9ad5` — and it
can be pulled out of any eNSP install (it lives at `dev/upload/agentd` inside
the device image).

`agentd-protocol.md` documents the four channels the guest uses to talk to
the host, the byte layouts, the state machine, and the two traps that cost
the most time:

* the media port in the handshake is **network byte order** (`ntohs` is applied
  by the guest, so writing it little-endian makes the guest log
  `Media port is: 53255` for a port of 2000);
* the host pipe that VirtualBox creates only grants the host **read** access,
  so the roles have to be inverted (`server="false"` + a host-side pipe
  server) before the handshake can be answered at all.

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

