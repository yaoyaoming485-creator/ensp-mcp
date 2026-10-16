"""Decisive experiment: capture the device UART from t=0 of the VM boot.

If the VirtualBox UART pipe carries the VRP console, we must see ASCII boot text
from the very first seconds. If we only ever see a periodic pattern, the console
is not on this pipe.
"""
import os
import subprocess
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VBOXMANAGE = os.environ.get(
    "VBOXMANAGE", r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
)
VM = sys.argv[1] if len(sys.argv) > 1 else "R1"
PIPE = sys.argv[2] if len(sys.argv) > 2 else r"\\.\pipe\OSPF_R1"
SECONDS = float(sys.argv[3]) if len(sys.argv) > 3 else 240.0
LOG = sys.argv[4] if len(sys.argv) > 4 else "boot_capture.log"

stop = threading.Event()


def list_pipes():
    try:
        return [p for p in os.listdir(r"\\.\pipe\\")]
    except OSError as e:
        return [f"<cannot list pipes: {e}>"]


with open(LOG, "w", encoding="utf-8") as log:
    def emit(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    emit(f"pipes before start: {[p for p in list_pipes() if VM.lower() in p.lower() or 'OSPF' in p]}")

    emit(f"powering off {VM} if running")
    subprocess.run([VBOXMANAGE, "controlvm", VM, "poweroff"],
                   capture_output=True, text=True)
    time.sleep(4)

    emit(f"starting {VM}")
    r = subprocess.run([VBOXMANAGE, "startvm", VM, "--type", "headless"],
                       capture_output=True, text=True)
    emit(f"startvm rc={r.returncode} {(r.stdout or '').strip()} {(r.stderr or '').strip()}")

    # Connect as fast as possible, then keep reconnecting if the pipe drops.
    fd = None
    t_open = time.time()
    while time.time() - t_open < 20 and fd is None:
        try:
            fd = os.open(PIPE, os.O_RDWR | os.O_BINARY)
        except OSError:
            time.sleep(0.05)

    if fd is None:
        emit(f"could not open {PIPE} within 20s; pipes now: {list_pipes()}")
        raise SystemExit(1)

    emit(f"connected after {time.time()-t_open:.2f}s")

    buf = []
    lock = threading.Lock()

    def reader():
        while not stop.is_set():
            try:
                chunk = os.read(fd, 512)
            except OSError as e:
                with lock:
                    buf.append((time.time(), f"<read error {e}>".encode()))
                return
            if not chunk:
                with lock:
                    buf.append((time.time(), b"<EOF>"))
                return
            with lock:
                buf.append((time.time(), chunk))

    threading.Thread(target=reader, daemon=True).start()

    t0 = time.time()
    total = 0
    last_report = t0
    while time.time() - t0 < SECONDS:
        time.sleep(1.0)
        with lock:
            batch, buf[:] = list(buf), []
        for ts, chunk in batch:
            total += len(chunk)
            printable = sum(1 for b in chunk if 32 <= b < 127 or b in (9, 10, 13))
            log.write(f"  +{ts-t0:7.2f}s len={len(chunk):5d} printable={printable:5d} "
                      f"hex={chunk[:64].hex(' ')}\n")
        if time.time() - last_report > 30:
            emit(f"t={time.time()-t0:6.1f}s total={total} bytes")
            last_report = time.time()

    emit(f"capture finished: {total} bytes in {SECONDS}s")
    stop.set()
    try:
        os.close(fd)
    except OSError:
        pass

print(f"log written to {LOG}")
