"""Real VRP console access for eNSP guest devices.

Everything in this module was recovered by reverse engineering the guest
binary `agentd` (ELF i386, unstripped, inside the eNSP AR image) and by
observing a live eNSP installation.

WHY THIS EXISTS
---------------
The eNSP AR guest is a Tiny Core Linux appliance that runs `agentd static`
(from /opt/bootlocal.sh).  `agentd` is the piece that talks to the host:

  CHANNEL 1 - SERIAL  (guest /dev/ttyS1  <->  host COM2 pipe)
      agentd -> host : 4 bytes  htonl(rand())                  (challenge)
      host -> agentd : 28 bytes A, then 27 bytes B
          A[0:4]   challenge echoed verbatim   (else "Invalid key.")
          A[4:6]   media port, NETWORK byte order (agentd applies ntohs)
          A[8:12]  guest IP
          A[12:16] netmask       (0 => agentd skips SIOCSIFNETMASK)
          A[16:26] device id, first 10 bytes
          B[0:27]  device id, last 27 bytes   (37 bytes = the ssid key)
      agentd then configures eth0 with ioctl(SIOCSIFADDR/SIOCSIFNETMASK/
      SIOCSIFFLAGS) using the IP we supplied, and runs ip_test/ping_test.

  CHANNEL 2 - CONTROL (host -> guest TCP 56789; the GUEST listens)
      44-byte frames: [u32 big-endian code][37-byte ssid][3 pad]
      guest -> host : 0 boot | 2 prepare OK | 3 prepare failed
                     4 DEV0 ok | 5 DEV0 failed | 6 DEV1 ok | 7 DEV1 failed
                     8 DEV2 ok | 9 DEV2 failed
      host -> guest : 1 "go"
      The socket must stay open: agentd registers atexit(power_off) and joins
      its proc_msg thread, so closing it powers the whole guest off.

  CHANNEL 3 - MEDIA  (guest -> host TCP 192.168.56.1:<media port>)
      send_ssid() dials back to the host and forwards SysV message-queue
      items of type 7 as 244-byte frames:
          [240 bytes payload][37-byte ssid at offset 178]

  CHANNEL 4 - CONSOLE (host -> guest TCP 40000)
      The VRP process itself listens here and speaks plain-text VRP.  This is
      the channel the real eNSP client uses: on a live host, netstat shows
      `192.168.56.1:<ephemeral> -> 192.168.56.x:40000 ESTABLISHED` owned by
      eNSP_VBoxServer.  It gives exact text - no screenshots, no OCR.

The guest's own resource.cfg must point its interface sockets at an address
the guest actually owns, otherwise VRP exits with
"Start port 0 fail" / "VPORT_Hal_PortEnable failed".
"""
from __future__ import annotations

import ctypes
import errno
import socket
import struct
import time
from pathlib import Path

# ------------------------------------------------------------------ guest --

AGENTD_CONTROL_PORT = 56789
VRP_CONSOLE_PORT = 40000
DEFAULT_MEDIA_PORT = 20002
DEFAULT_DEVICE_ID = b"4CA76BCE-449E-4dc0-B3E3-E86C85B97001\x00"

CONTROL_CODES = {
    0: "boot",
    2: "prepare OK",
    3: "prepare FAILED",
    4: "DEV0 started",
    5: "DEV0 FAILED",
    6: "DEV1 started",
    7: "DEV1 FAILED",
    8: "DEV2 started",
    9: "DEV2 FAILED",
}

PROMPT_MARK = "<Huawei>"


class AgentdError(RuntimeError):
    """Raised when the guest agent refuses or misbehaves."""


class VrpConsoleError(RuntimeError):
    """Raised when the VRP console service is unreachable."""


# ------------------------------------------------------------------ pipes --

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateNamedPipeW.restype = ctypes.c_void_p
k32.CreateNamedPipeW.argtypes = [
    ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
]
k32.ConnectNamedPipe.restype = ctypes.c_int
k32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k32.ReadFile.restype = ctypes.c_int
k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                         ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
k32.WriteFile.restype = ctypes.c_int
k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                          ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
k32.DisconnectNamedPipe.argtypes = [ctypes.c_void_p]

_INVALID_HANDLE = ctypes.c_void_p(-1).value
_ERROR_PIPE_CONNECTED = 535
_PIPE_ACCESS_DUPLEX = 0x3


def _pipe_read(handle, want: int, timeout: float) -> bytes:
    buf = ctypes.create_string_buffer(4096)
    cnt = ctypes.c_uint32(0)
    out = b""
    deadline = time.time() + timeout
    while len(out) < want and time.time() < deadline:
        if not k32.ReadFile(handle, buf, want - len(out), ctypes.byref(cnt), None):
            time.sleep(0.05)
            continue
        if cnt.value:
            out += buf.raw[:cnt.value]
    return out


def _pipe_write(handle, data: bytes) -> int:
    buf = ctypes.create_string_buffer(data, len(data))
    cnt = ctypes.c_uint32(0)
    k32.WriteFile(handle, buf, len(data), ctypes.byref(cnt), None)
    return cnt.value


class SerialPipeServer:
    """Act as the named-pipe server so VirtualBox dials us.

    A VirtualBox host pipe created by VBox itself only grants the host
    GENERIC_READ (RDWR fails with ERROR_ACCESS_DENIED).  Configuring the UART
    with server="false" inverts the roles: VirtualBox opens the pipe as a
    client and we own the instance and its security descriptor.
    """

    def __init__(self, pipe_path: str):
        self.pipe_path = pipe_path
        self.handle: int | None = None

    def open(self) -> int:
        handle = k32.CreateNamedPipeW(
            self.pipe_path, _PIPE_ACCESS_DUPLEX, 0, 255, 65536, 65536, 0, None)
        if not handle or handle == _INVALID_HANDLE:
            raise AgentdError(
                f"CreateNamedPipe({self.pipe_path}) failed: {ctypes.get_last_error()}")
        self.handle = handle
        return handle

    def wait_client(self, timeout: float = 120.0) -> int:
        handle = self.open()
        deadline = time.time() + timeout
        while time.time() < deadline:
            ok = k32.ConnectNamedPipe(handle, None)
            if ok or ctypes.get_last_error() == _ERROR_PIPE_CONNECTED:
                return handle
            time.sleep(0.2)
        raise AgentdError("timed out waiting for VirtualBox to connect the pipe")

    def close(self) -> None:
        if self.handle is not None:
            k32.DisconnectNamedPipe(self.handle)
            k32.CloseHandle(self.handle)
            self.handle = None


# ---------------------------------------------------------------- handshake --

def build_handshake_reply(challenge: bytes,
                          guest_ip: str,
                          netmask: str = "255.255.255.0",
                          media_port: int = DEFAULT_MEDIA_PORT,
                          device_id: bytes = DEFAULT_DEVICE_ID) -> bytes:
    """Build the 28 + 27 byte reply that agentd expects.

    The media port is NETWORK byte order: agentd reads it as a host u16 and
    then applies ntohs(), so writing it little-endian makes the guest see a
    byte-swapped value (2000 -> 53255).
    """
    if len(challenge) != 4:
        raise AgentdError(f"challenge must be 4 bytes, got {len(challenge)}")
    if len(device_id) != 37:
        raise AgentdError(f"device id must be 37 bytes, got {len(device_id)}")
    a = bytearray(28)
    a[0:4] = challenge
    a[4:6] = struct.pack(">H", media_port)
    a[8:12] = socket.inet_aton(guest_ip)
    a[12:16] = socket.inet_aton(netmask)
    a[16:26] = device_id[0:10]
    return bytes(a) + device_id[10:37]


class MediaServer:
    """The 'remote media server' the guest dials back to.

    agentd's send_ssid() connects to 192.168.56.1:<media port> and pushes
    every SysV message-queue item of type 7 as a 244-byte frame.  Without a
    listener the guest logs "Failed to connect to remote media server".
    """

    def __init__(self, host_ip: str = "192.168.56.1",
                 port: int = DEFAULT_MEDIA_PORT):
        self.host_ip = host_ip
        self.port = port
        self.sock: socket.socket | None = None
        self.frames: list[bytes] = []

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self.host_ip, self.port))
        except OSError as exc:
            raise AgentdError(f"media server bind {self.host_ip}:{self.port}: {exc}") from exc
        s.listen(4)
        s.settimeout(120.0)
        self.sock = s

    def accept(self, timeout: float = 120.0) -> tuple[socket.socket, tuple[str, int]]:
        if self.sock is None:
            raise AgentdError("media server not started")
        self.sock.settimeout(timeout)
        try:
            conn, peer = self.sock.accept()
        except socket.timeout as exc:
            raise AgentdError("guest did not connect to the media server") from exc
        conn.settimeout(2.0)
        return conn, peer

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None


def _frames(conn: socket.socket, count: int, timeout: float) -> list[bytes]:
    out: list[bytes] = []
    buf = b""
    end = time.time() + timeout
    conn.settimeout(1.0)
    while len(out) < count and time.time() < end:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while len(buf) >= 244:
            out.append(buf[:244])
            buf = buf[244:]
    return out


# ------------------------------------------------------------------ console --

class VrpConsole:
    """Plain-text VRP console over the guest's TCP 40000.

    The service accepts a single client at a time, so keep one connection open
    and issue every command over it.  A fresh session needs one throw-away
    newline: the first keystroke can otherwise be swallowed while VRP is still
    printing its banner.
    """

    def __init__(self, guest_ip: str, port: int = VRP_CONSOLE_PORT,
                 timeout: float = 10.0, connect_retries: int = 8,
                 retry_delay: float = 2.0):
        self.guest_ip = guest_ip
        self.port = port
        self.timeout = timeout
        self.connect_retries = connect_retries
        self.retry_delay = retry_delay
        self.sock: socket.socket | None = None
        self.banner = ""

    def connect(self) -> "VrpConsole":
        """Open the console, retrying while the previous session is released.

        The VRP console service accepts one client at a time.  Right after a
        session ends the guest keeps the port in TIME_WAIT for a few seconds,
        so a single connect attempt is not enough.
        """
        last: OSError | None = None
        for attempt in range(1, self.connect_retries + 1):
            s = socket.socket()
            s.settimeout(self.timeout)
            try:
                s.connect((self.guest_ip, self.port))
            except OSError as exc:
                last = exc
                s.close()
                if attempt < self.connect_retries:
                    time.sleep(self.retry_delay)
                continue
            self.sock = s
            time.sleep(0.4)
            # drain the banner
            self.banner = self._drain(0.8)
            # wake the session: VRP may still be at the "press any key" prompt
            try:
                self.sock.sendall(b"\r\n")
            except OSError:
                pass
            self._drain(0.6)
            return self
        raise VrpConsoleError(
            f"cannot reach VRP console at {self.guest_ip}:{self.port} "
            f"after {self.connect_retries} attempts: {last}")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self) -> "VrpConsole":
        return self.connect()

    def __exit__(self, *_exc) -> None:
        self.close()

    def _drain(self, seconds: float) -> str:
        if self.sock is None:
            return ""
        end = time.time() + seconds
        out = b""
        while time.time() < end:
            self.sock.settimeout(max(0.05, end - time.time()))
            try:
                chunk = self.sock.recv(8192)
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        return out.decode("utf-8", "replace")

    def send(self, text: str) -> None:
        if self.sock is None:
            raise VrpConsoleError("console not connected")
        self.sock.sendall(text.encode("utf-8"))

    def command(self, cmd: str, wait: float = 1.5) -> str:
        self.send(cmd.rstrip("\r\n") + "\r\n")
        return self._drain(wait)

    def _drain_until_prompt(self, seconds: float) -> str:
        """Read until the VRP prompt reappears, or the window expires.

        VRP echoes the command, prints its output and then the prompt again.
        Waiting for the prompt is far more reliable than a fixed sleep: a
        'display' of a big table can take much longer than a short command.
        """
        out = ""
        end = time.time() + seconds
        while time.time() < end:
            out += self._drain(0.3)
            # the prompt is the last non-empty line we have seen
            lines = [ln for ln in out.splitlines() if ln.strip()]
            if lines and lines[-1].strip().endswith("]"):
                break
        return out

    def run(self, commands: list[str], wait: float = 1.5,
            until_prompt: bool = True) -> str:
        """Run several commands on one session and return everything.

        Each command is followed by a read that waits for the VRP prompt (or
        the timeout), so a slow command is never cut off half way.
        """
        chunks = []
        for cmd in commands:
            chunks.append(f"=== {cmd}\n")
            self.send(cmd.rstrip("\r\n") + "\r\n")
            if until_prompt:
                chunks.append(self._drain_until_prompt(max(wait, 6.0)))
            else:
                chunks.append(self._drain(wait))
        return "".join(chunks)


# --------------------------------------------------------------- discovery --

def probe_guest_ports(guest_ip: str, ports: tuple[int, ...] = (AGENTD_CONTROL_PORT,
                                                               VRP_CONSOLE_PORT),
                      timeout: float = 0.4) -> dict[int, bool]:
    """Report which guest services answer."""
    out: dict[int, bool] = {}
    for port in ports:
        s = socket.socket()
        s.settimeout(timeout)
        try:
            out[port] = s.connect_ex((guest_ip, port)) == 0
        except OSError:
            out[port] = False
        finally:
            s.close()
    return out


def guest_ip_from_host_only(prefix: str = "192.168.56.",
                            ports: tuple[int, ...] = (AGENTD_CONTROL_PORT,
                                                      VRP_CONSOLE_PORT),
                            timeout: float = 0.12) -> list[str]:
    """List guests on the host-only subnet that answer on any of ``ports``.

    A guest exposes the agentd control port (56789) and the VRP console port
    (40000); either one identifies a device.
    """
    found = []
    for i in range(2, 255):
        ip = f"{prefix}{i}"
        for port in ports:
            s = socket.socket()
            s.settimeout(timeout)
            try:
                if s.connect_ex((ip, port)) == 0:
                    found.append(ip)
                    break
            except OSError:
                pass
            finally:
                s.close()
    return found


def control_handshake(guest_ip: str, device_id: bytes = DEFAULT_DEVICE_ID,
                      timeout: float = 30.0) -> dict:
    """Perform the 44-byte control exchange and report what the guest said.

    The guest listens on 56789 and sends code 0 (boot) as soon as we connect.
    Answering with code 1 releases the start state; the guest then reports
    prepare / DEV0 / DEV1 / DEV2.
    """
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((guest_ip, AGENTD_CONTROL_PORT))
    except OSError as exc:
        raise AgentdError(f"control connect to {guest_ip}: {exc}") from exc
    events: list[str] = []
    buf = b""
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            while len(buf) < 44:
                try:
                    chunk = sock.recv(44 - len(buf))
                except socket.timeout as exc:
                    raise AgentdError(
                        f"no control frame from {guest_ip} within {timeout}s - "
                        "another client (the eNSP client) may already hold the "
                        "agentd control session") from exc
                if not chunk:
                    raise AgentdError("control socket closed by the guest")
                buf += chunk
            frame, buf = buf[:44], buf[44:]
            code = struct.unpack(">I", frame[0:4])[0]
            events.append(CONTROL_CODES.get(code, f"code {code}"))
            if code == 0:
                sock.sendall(struct.pack(">I", 1) + frame[4:41] + b"\x00" * 3)
                events.append("host sent 1 (go)")
            if code in (8, 9):
                break
    finally:
        sock.close()
    return {"guest": guest_ip, "events": events}


# ------------------------------------------------------------- VM plumbing --

def pipe_path_for(vm_name: str) -> str:
    """The named pipe VirtualBox exposes for a device's COM2."""
    return f"\\\\.\\pipe\\{vm_name}-COM2"


def vbox_uart_mode(vm_name: str) -> str:
    """Read uartmode2 out of VBoxManage's machine-readable output."""
    import subprocess
    from . import vbox

    try:
        exe = vbox.vboxmanage_path()
    except Exception:  # noqa: BLE001
        return ""
    out = subprocess.run(
        [exe, "showvminfo", vm_name, "--machinereadable"],
        capture_output=True, text=True, timeout=60,
    ).stdout
    for line in out.splitlines():
        if line.startswith("uartmode2="):
            return line.split("=", 1)[1].strip('"')
    return ""
