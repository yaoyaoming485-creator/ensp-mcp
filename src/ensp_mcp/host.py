"""Host-side driver for one eNSP guest device.

This is the part that replaces the eNSP client. It runs three things for a
single device:

  1. a **named-pipe server** for the device's COM2 UART, so VirtualBox
     connects to us;
  2. a **media server** on the host-only adapter, which the guest dials back
     to;
  3. a **control client** on guest TCP 56789, answering the guest's boot
     frame with code 1 so the VRP processes start.

The protocol (recovered from the guest's ``agentd`` binary) is documented in
``evidence/agentd-protocol.md``.

The device should be configured with a COM2 pipe in *client* mode, i.e. its
``.vbox`` has::

    <Port slot="1" ... server="false" path="\\\\.\\pipe\\NAME" hostMode="HostPipe"/>

because in client mode the host owns the pipe instance, so a guest that
connects first cannot lock it out.  It is not the only working arrangement: a
pipe VirtualBox creates itself also opens read-write from the host while its
instance is free, so client mode is preferred for robustness rather than
required.
"""
from __future__ import annotations

import ctypes
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field

from .agentd import (AGENTD_CONTROL_PORT, CONTROL_CODES, MediaServer,
                     SerialPipeServer, build_handshake_reply)
from .lab import LOCAL_PORT_BASE

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.ReadFile.restype = ctypes.c_int
k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                         ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
k32.PeekNamedPipe.restype = ctypes.c_int
k32.PeekNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                              ctypes.POINTER(ctypes.c_uint32),
                              ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
k32.WriteFile.restype = ctypes.c_int
k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                          ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]

#: Windows errors that mean "the other end of the pipe is gone"
_PIPE_GONE = (109, 232, 233)   # BROKEN_PIPE, NO_DATA, PIPE_NOT_CONNECTED


class PipeClosed(RuntimeError):
    """The guest closed the serial pipe."""


class MediaHub:
    """Routes the media frames between the devices of a lab.

    The guest hands its messages to the host as 244-byte frames; the ssid of
    the device they are meant for sits at offset 178.  The host has to give
    the frame to that device's media connection - dropping it leaves the
    devices without a working link.
    """

    FRAME_LENGTH = 244
    SSID_OFFSET = 178
    SSID_LENGTH = 37

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._targets: dict[bytes, socket.socket] = {}
        self.forwarded = 0
        self.dropped = 0

    def register(self, ssid: bytes, conn: socket.socket) -> None:
        if not ssid:
            return
        with self._lock:
            self._targets[bytes(ssid)] = conn

    def unregister(self, conn: socket.socket) -> None:
        with self._lock:
            for ssid, target in list(self._targets.items()):
                if target is conn:
                    del self._targets[ssid]

    def route(self, frame: bytes) -> bool:
        """Hand a frame to the device it is addressed to."""
        if len(frame) < self.FRAME_LENGTH:
            return False
        ssid = frame[self.SSID_OFFSET:self.SSID_OFFSET + self.SSID_LENGTH]
        with self._lock:
            target = self._targets.get(ssid)
        if target is None:
            self.dropped += 1
            return False
        try:
            target.sendall(frame)
        except OSError:
            self.dropped += 1
            return False
        self.forwarded += 1
        return True


class UdpRelay:
    """Relay the VRP's interface traffic between linked devices.

    The guest sends the traffic of interface *n* as a UDP datagram from its
    own port ``10000 + n`` to the host.  The host has to forward it to the
    peer device's port for the interface the link connects - without that
    the interfaces never come up, and the device's interface process
    eventually dies.

    The datagram's source port tells which interface it belongs to, so one
    socket per device is enough.
    """

    def __init__(self, host_ip: str, links: dict[int, tuple[int, str, int]],
                 log_prefix: str = ""):
        self.host_ip = host_ip
        #: interface -> (host port, peer address, peer local port)
        self.links = links
        self.log_prefix = log_prefix
        self.socks: dict[int, socket.socket] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.forwarded = 0
        self.received = 0
        self.answered = 0

    def log(self, msg: str) -> None:
        if self.log_prefix:
            print(f"[{time.strftime('%H:%M:%S')}] {self.log_prefix}{msg}", flush=True)

    def start(self) -> None:
        for iface, (port, _peer_ip, _peer_port) in self.links.items():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((self.host_ip, port))
            except OSError as exc:
                self.log(f"udp relay: cannot bind {self.host_ip}:{port}: {exc}")
                s.close()
                continue
            self.socks[iface] = s
            t = threading.Thread(target=self._run, args=(iface,), daemon=True)
            t.start()
            self._threads.append(t)
        if self.socks:
            ports = ", ".join(str(p) for p, _, _ in self.links.values())
            self.log(f"udp relay: {len(self.socks)} interface socket(s) on "
                     f"{self.host_ip}:{ports}")

    def _run(self, iface: int) -> None:
        s = self.socks[iface]
        _port, peer_ip, peer_port = self.links[iface]
        while not self._stop.is_set():
            s.settimeout(1.0)
            try:
                data, peer = s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            self.received += 1
            # A four-byte datagram is the interface's link handshake, not a
            # question.  The guest's VPORT process sends `htonl(1)` per
            # interface, once a second, and validates whatever comes back in
            # `0x0804bbb0`: four bytes whose big-endian value is 1, 2 or 3 -
            # anything else, including a peer address, returns -1 and is
            # dropped (read out of the decrypted `dev0`).
            #
            # The answer that actually keeps the interface usable is 2.  The
            # port holds a counter at `0x08044a0 + 4*port` which the sender
            # bumps on every handshake (`0x0804bd9a`); while it is above 4 the
            # port sits in control state and every datagram longer than four
            # bytes is dropped, which is why only handshakes ever get through.
            # A `2` from the host resets that counter to 0 (`0x0804bcd8`), and
            # a counter of 4 or less puts the port back into data state.
            if len(data) == 4:
                try:
                    s.sendto(b"\x00\x00\x00\x02", peer)
                    self.answered += 1
                except OSError as exc:
                    self.log(f"udp relay: cannot answer interface {iface}: {exc}")
                try:
                    s.sendto(data, (peer_ip, peer_port))
                    self.forwarded += 1
                except OSError:
                    pass
                continue
            try:
                # the source port stays the one the guest addressed: that is
                # how the peer knows which interface the packet belongs to
                s.sendto(data, (peer_ip, peer_port))
                self.forwarded += 1
            except OSError as exc:
                self.log(f"udp relay: cannot forward interface {iface} "
                         f"to {peer_ip}:{peer_port}: {exc}")

    def stop(self) -> None:
        self._stop.set()
        for s in self.socks.values():
            try:
                s.close()
            except OSError:
                pass
        self.socks.clear()


@dataclass
class HostOptions:
    """Everything the host side needs to drive one guest."""

    guest_ip: str
    pipe_path: str
    media_port: int
    host_ip: str = "192.168.56.1"
    netmask: str = "255.255.255.0"
    device_id: bytes = b"4CA76BCE-449E-4dc0-B3E3-E86C85B97001\x00"
    verbose: bool = True
    #: interface -> (host port, peer address, peer local port) for the UDP relay
    peers: dict[int, tuple[int, str, int]] = field(default_factory=dict)
    #: the ssids of the linked devices - the guest needs them to address them
    peer_ssids: list[bytes] = field(default_factory=list)
    #: (address, ssid) of the linked devices, for the announcement frame
    peer_infos: list[tuple[str, bytes]] = field(default_factory=list)
    #: routes the media frames between the devices of the lab
    hub: "MediaHub | None" = None
    #: where the host reaches the guest's agentd control channel.  Empty
    #: means "the guest's own address" - behind NAT the host uses a
    #: forwarded port on the loopback instead.
    agent_endpoint: str = ""
    #: where the host binds its own sockets (the media server, the relay)
    bind_host: str = ""


@dataclass
class HostDevice:
    """Drives one guest. ``start()`` returns once the device reports DEV2."""

    opts: HostOptions
    log_prefix: str = ""
    _threads: list[threading.Thread] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    #: set once the serial handshake is done and the guest listens
    _serial_ready: threading.Event = field(default_factory=threading.Event)
    #: set once the guest has connected to the media server
    _media_ready: threading.Event = field(default_factory=threading.Event)
    events: list[str] = field(default_factory=list)
    ready: threading.Event = field(default_factory=threading.Event)
    failed: str = ""
    #: when the control channel was last tried
    last_try: float = 0.0
    #: relays the VRP's interface traffic to the linked devices
    relay: "UdpRelay | None" = None

    # ------------------------------------------------------------------ --
    def log(self, msg: str) -> None:
        if self.opts.verbose:
            print(f"[{time.strftime('%H:%M:%S')}] {self.log_prefix}{msg}", flush=True)

    def _agent_target(self) -> tuple[str, int]:
        """Where the host reaches the guest's agentd control channel.

        Normally that is the guest's own address; behind NAT the host goes
        through a forwarded port on the loopback instead.
        """
        if self.opts.agent_endpoint:
            host, _, port = self.opts.agent_endpoint.rpartition(":")
            try:
                return host, int(port)
            except ValueError:
                pass
        return self.opts.guest_ip, AGENTD_CONTROL_PORT

    # ------------------------------------------------------------------ --
    def _read(self, handle, want: int, timeout: float) -> bytes:
        """Read from the serial pipe without blocking.

        ``ReadFile`` on a pipe blocks until data shows up, which would hide
        the fact that the guest is gone: the pipe instance would stay
        connected, and the next machine could not open the same pipe
        (VERR_PIPE_BUSY).  Peeking first keeps the loop responsive and
        tells us when the other end has closed.
        """
        buf = ctypes.create_string_buffer(4096)
        cnt = ctypes.c_uint32(0)
        avail = ctypes.c_uint32(0)
        out = b""
        deadline = time.time() + timeout
        while len(out) < want and time.time() < deadline:
            if not k32.PeekNamedPipe(handle, None, 0, None,
                                     ctypes.byref(avail), None):
                if ctypes.get_last_error() in _PIPE_GONE:
                    raise PipeClosed("the guest closed the serial pipe")
                time.sleep(0.05)
                continue
            if not avail.value:
                time.sleep(0.05)
                continue
            if not k32.ReadFile(handle, buf, min(want - len(out), avail.value),
                                ctypes.byref(cnt), None):
                if ctypes.get_last_error() in _PIPE_GONE:
                    raise PipeClosed("the guest closed the serial pipe")
                time.sleep(0.05)
                continue
            if cnt.value:
                out += buf.raw[:cnt.value]
        return out

    def _write(self, handle, data: bytes) -> None:
        buf = ctypes.create_string_buffer(data, len(data))
        cnt = ctypes.c_uint32(0)
        k32.WriteFile(handle, buf, len(data), ctypes.byref(cnt), None)

    # ------------------------------------------------------------------ --
    def _serial_worker(self) -> None:
        while not self._stop.is_set():
            # a new server for every attempt: the guest that comes back after
            # a restart opens the pipe again
            server = SerialPipeServer(self.opts.pipe_path)
            try:
                handle = server.wait_client(timeout=300.0)
            except Exception as exc:  # noqa: BLE001
                self.log(f"pipe: {exc}")
                return
            self.log("pipe: VirtualBox connected")
            try:
                challenge = self._read(handle, 4, timeout=180.0)
                if len(challenge) < 4:
                    self.log(f"pipe: short challenge {challenge!r}")
                    continue
                self.log(f"pipe: challenge {challenge.hex(' ')}")
                reply = build_handshake_reply(
                    challenge, self.opts.guest_ip, self.opts.netmask,
                    self.opts.media_port, self.opts.device_id)
                self._write(handle, reply)
                self.log(f"pipe: sent A+B (media_port={self.opts.media_port} "
                         f"ip={self.opts.guest_ip})")
                # The guest only starts listening for the control connection
                # after it has this handshake.  Connecting earlier leaves
                # half-open connections behind that the guest later accepts
                # and then cannot write to ("Send boot message to control
                # socket failed: Bad file descriptor"), which kills the device.
                self._serial_ready.set()
                while not self._stop.is_set():
                    chunk = self._read(handle, 4096, timeout=1.0)
                    if chunk:
                        printable = sum(1 for b in chunk if 32 <= b < 127 or b in (9, 10, 13))
                        if printable > len(chunk) * 0.5:
                            self.log(f"pipe RX| {chunk.decode('ascii', 'replace')!r}")
                        else:
                            self.log(f"pipe RX HEX| {chunk[:48].hex(' ')}")
            except Exception as exc:  # noqa: BLE001
                self.log(f"pipe: {type(exc).__name__}: {exc}")
            finally:
                server.close()
                self._serial_ready.clear()
                time.sleep(1.0)

    def _announce_peers(self, conn: socket.socket) -> None:
        """Tell the guest which devices it is linked to.

        A frame the host sends to a guest is addressed to *that* guest: the
        ssid at offset 178 selects the message queue the agent puts it into.
        The peer is named in the payload, so the guest's VRP learns the
        address it has to ARP for.

        The exact layout is not known yet - the eNSP client builds the same
        thing at runtime ("ExportCfg" in eNSP_VBoxServer.exe) - so this is
        the best guess so far.  The guest ignores a frame it does not
        understand, so sending it does no harm.
        """
        own = self.opts.device_id
        for address, ssid in self.opts.peer_infos:
            if not ssid or not own:
                continue
            frame = bytearray(MediaHub.FRAME_LENGTH)
            # the payload names the peer: its ssid first, then its address
            frame[0:len(ssid)] = ssid
            text = address.encode("ascii")
            frame[len(ssid):len(ssid) + len(text)] = text
            # ... and the frame itself is addressed to us
            frame[MediaHub.SSID_OFFSET:MediaHub.SSID_OFFSET + len(own)] = own
            try:
                conn.sendall(bytes(frame))
            except OSError as exc:
                self.log(f"media: cannot announce the peer: {exc}")
                return
            self.log(f"media: announced the peer {address} "
                     f"({ssid[:8].decode('ascii', 'replace')}...)")

    def _media_worker(self) -> None:
        # The listening socket stays open for the whole life of the driver:
        # the guest's media client connects more than once, and a refused
        # re-connect makes the guest give up and power itself off.
        server = MediaServer(self.opts.bind_host or self.opts.host_ip,
                             self.opts.media_port)
        try:
            server.start()
        except Exception as exc:  # noqa: BLE001
            self.log(f"media: {exc}")
            return
        self.log(f"media: listening on {self.opts.bind_host or self.opts.host_ip}:"
                 f"{self.opts.media_port}")
        try:
            while not self._stop.is_set():
                try:
                    conn, peer = server.accept(timeout=300.0)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"media: {exc}")
                    return
                self.log(f"media: guest connected from {peer}")
                self._media_ready.set()
                if self.opts.hub is not None:
                    self.opts.hub.register(self.opts.device_id, conn)
                self._announce_peers(conn)
                conn.settimeout(2.0)
                buf = b""
                frames = 0
                while not self._stop.is_set():
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= MediaHub.FRAME_LENGTH:
                        frame, buf = buf[:MediaHub.FRAME_LENGTH], buf[MediaHub.FRAME_LENGTH:]
                        frames += 1
                        payload = frame[:240]
                        text = payload.decode("ascii", "replace")
                        printable = sum(1 for c in text
                                        if 32 <= ord(c) < 127 or c in "\r\n\t")
                        if printable > len(text) * 0.5:
                            self.log(f"media[{frames}] TEXT| {text!r}")
                        else:
                            self.log(f"media[{frames}] HEX | {payload[:64].hex(' ')}")
                        if self.opts.hub is not None:
                            self.opts.hub.route(frame)
                try:
                    conn.close()
                except OSError:
                    pass
                if self.opts.hub is not None:
                    self.opts.hub.unregister(conn)
                # the guest is gone - it may come back after a restart
                self._media_ready.clear()
                time.sleep(0.5)
        finally:
            server.close()

    def _warm_arp(self) -> None:
        """Make sure the host can reach the guest's address.

        The guest only got its address a moment ago, so the first connect
        attempt cannot even send a SYN - the address is not resolved yet.
        A connect that times out during that phase cancels the resolution
        again, and the host never gets to talk to the guest at all.  One
        throw-away UDP packet makes the host resolve the guest's MAC
        address; the reply is handled by the kernel even when the guest's
        firewall drops the packet itself.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        try:
            target = self._agent_target()
            s.sendto(b"\x00", (target[0], target[1]))
        except OSError:
            pass
        finally:
            s.close()
        time.sleep(0.3)

    def _control_worker(self) -> None:
        deadline = time.time() + 600
        sock = None
        # The guest has to be dialled at the very moment it starts listening.
        # It creates its control thread about three seconds after it has
        # configured its address and gives up shortly after: when the host
        # is late, or when an abandoned connect attempt is still queued and
        # the guest accepts that dead connection, the guest aborts with
        # "Send boot message to control socket failed" and powers itself
        # off.  The supervisor restarts such a device, so this loop only
        # has to wait for the next guest.
        while not self._stop.is_set():
            if not self._serial_ready.wait(300):
                if self._stop.is_set():
                    return
                self.failed = "the guest never completed the serial handshake"
                self.ready.set()
                return
            # The guest's address is a moment old - resolve it before the
            # first connect attempt, otherwise that attempt burns the whole
            # window on the address resolution and never sends a SYN.
            self._warm_arp()
            sock = None
            deadline = time.time() + 120
            complained = False
            while time.time() < deadline and not self._stop.is_set():
                self.last_try = time.time()
                try:
                    # The connect timeout must NOT cut the attempt short.
                    # The guest creates its control thread about two seconds
                    # after the handshake and accepts the connection then; a
                    # connect that is abandoned in that very moment leaves
                    # the guest with a dead socket, its accept() fails and it
                    # aborts ("Send boot message to control socket failed").
                    # So the attempt is started early and kept alive: it
                    # completes as soon as the guest starts listening.
                    sock = socket.create_connection(
                        self._agent_target(), timeout=30.0)
                    self.log(f"control: connected {self._agent_target()[0]}:"
                             f"{self._agent_target()[1]}")
                    break
                except OSError as exc:
                    if not complained:
                        self.log(f"control: {type(exc).__name__}: {exc}")
                        complained = True
                    time.sleep(0.2)
            if sock is None:
                self.failed = "the guest never answered on the agentd control port"
                self.ready.set()
                return
            # The control channel is silent once the device is up: agentd only sends
            # the boot/prepare/DEV0/DEV1/DEV2 frames and then waits.  A read timeout
            # therefore means "nothing to say", *not* "the device is gone" - and it
            # must not close the socket: agentd powers the guest off as soon as the
            # control connection drops.
            sock.settimeout(5.0)
            buf = b""
            try:
                while not self._stop.is_set():
                    while len(buf) < 44:
                        if self._stop.is_set():
                            return
                        try:
                            chunk = sock.recv(44 - len(buf))
                        except socket.timeout:
                            continue
                        if not chunk:
                            self.log("control: peer closed")
                            self.failed = "the guest closed the control connection"
                            break
                        buf += chunk
                    if len(buf) < 44:
                        break
                    frame, buf = buf[:44], buf[44:]
                    code = struct.unpack(">I", frame[0:4])[0]
                    name = CONTROL_CODES.get(code, f"code {code}")
                    self.events.append(name)
                    self.log(f"control: <- {name}")
                    if code == 0:
                        sock.sendall(struct.pack(">I", 1) + frame[4:41] + b"\x00" * 3)
                        self.log("control: -> 1 (go)")
                    if code in (8, 9):
                        self.ready.set()
            except OSError as exc:
                self.log(f"control: {type(exc).__name__}: {exc}")
                # A failure is *not* a ready device: the supervisor restarts
                # the machine, so "ready" must stay clear.
                self.failed = str(exc)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
                # the guest is gone - it may come back after a restart
                self._serial_ready.clear()
                self._media_ready.clear()
                time.sleep(1.0)

    # ------------------------------------------------------------------ --
    def start(self, wait_ready: bool = True, ready_timeout: float = 240.0) -> "HostDevice":
        self.relay = UdpRelay(self.opts.bind_host or self.opts.host_ip,
                              self.opts.peers, self.log_prefix)
        self.relay.start()
        for fn in (self._serial_worker, self._media_worker, self._control_worker):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        if wait_ready and not self.ready.wait(ready_timeout):
            self.failed = self.failed or "timed out waiting for the device to start"
        return self

    def stop(self) -> None:
        self._stop.set()
        if self.relay is not None:
            self.relay.stop()

    def reset(self) -> None:
        """Get ready for another attempt.

        A guest that loses the control handshake aborts and powers itself
        off; when the machine is started again the drivers have to be ready
        for the next handshake.
        """
        self.ready.clear()
        self.failed = ""
        self.events.clear()
        self.last_try = 0.0
        self._serial_ready.clear()
        self._media_ready.clear()

    @property
    def started(self) -> bool:
        return "DEV2 started" in self.events


def drive_many(options: list[HostOptions], prefixes: list[str] | None = None,
               wait_ready: bool = True) -> list[HostDevice]:
    """Drive several devices at once."""
    devices: list[HostDevice] = []
    for i, opt in enumerate(options):
        prefix = (prefixes[i] if prefixes and i < len(prefixes) else "")
        devices.append(HostDevice(opts=opt, log_prefix=prefix).start(wait_ready=False))
    if wait_ready:
        for dev in devices:
            dev.ready.wait(240)
    return devices
