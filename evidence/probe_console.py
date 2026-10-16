"""Probe: can we talk to a running eNSP device over its VirtualBox UART named pipe?"""
import os
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PIPE = sys.argv[1] if len(sys.argv) > 1 else r"\\.\pipe\OSPF_R1"
READ_SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0

buf = bytearray()
lock = threading.Lock()
stop = threading.Event()

print(f"[probe] opening {PIPE}")
t0 = time.time()
fd = None
last_err = None
while time.time() - t0 < 15:
    try:
        fd = os.open(PIPE, os.O_RDWR | os.O_BINARY)
        break
    except OSError as e:
        last_err = e
        time.sleep(0.3)

if fd is None:
    print(f"[probe] OPEN FAILED after 15s: {last_err}")
    sys.exit(1)

print(f"[probe] connected in {time.time()-t0:.2f}s")


def reader():
    while not stop.is_set():
        try:
            chunk = os.read(fd, 4096)
        except OSError as e:
            print(f"[probe] read error: {e}")
            return
        if not chunk:
            print("[probe] EOF")
            return
        with lock:
            buf.extend(chunk)


threading.Thread(target=reader, daemon=True).start()

# Nudge the console: a bare CR is what eNSP's own terminal sends to wake it.
time.sleep(2)
os.write(fd, b"\r")
print("[probe] sent CR, draining...")

time.sleep(READ_SECONDS)

with lock:
    data = bytes(buf)
    buf.clear()

text = data.decode("ascii", errors="replace")
print(f"[probe] received {len(data)} bytes")
print(f"[probe] hex: {data[:128].hex(' ')}")
print(f"[probe] repr: {data[:128]!r}")
print("=" * 70)
print(text[-4000:] if len(text) > 4000 else text)
print("=" * 70)

# Try one real command if we saw a prompt
if "<" in text and ">" in text:
    os.write(fd, b"\r")
    time.sleep(1)
    with lock:
        buf.clear()
    os.write(fd, b"display version\r")
    time.sleep(6)
    with lock:
        out = bytes(buf).decode("ascii", errors="replace")
    print("[probe] --- display version ---")
    print(out[-2500:])

stop.set()
try:
    os.close(fd)
except OSError:
    pass
print("[probe] done")
