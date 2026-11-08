"""Generate a synthetic threadtime logcat of a given size (for the M1 performance test).

    python scripts/gen_logcat.py OUT.log --mb 1024

The content is synthetic: realistic tags, pids and message shapes with varying numbers, some
multi-line stack traces, buffer markers and a few values the redactor must catch (MACs, IPs).
Deterministic for a given seed.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

TAGS = [
    (
        "I",
        "ActivityManager",
        "Start proc {pid}:com.example.app{n}/u0a{n} for service {{com.example.app{n}/.Svc}}",
    ),
    ("D", "CarPropertyService", "setProperty: propId=0x{hex}, areaId={n}, value={f}"),
    ("E", "HvacCtrl", "setProperty failed, status=NOT_AVAILABLE, prop=0x{hex}"),
    ("W", "VehicleHal", "onPropertyEvent: dropped {n} events, queue={n2}"),
    ("I", "AudioFlinger", "track {n} started, session {n2}, latency {n3} ms"),
    ("D", "BluetoothAdapter", "getState(): ON, peer={mac}"),
    ("I", "wpa_supplicant", "wlan0: CTRL-EVENT-CONNECTED - Connection to {mac} completed"),
    ("D", "ConnectivityService", "NetworkAgentInfo [WIFI () - {n}] validation passed, addr {ip}"),
    ("V", "InputDispatcher", "Delivering key {n} to window {hex}, seq={n2}"),
    ("I", "chatty", "uid={n}(u0_a{n2}) com.example.app identical {n3} lines"),
    ("D", "SurfaceFlinger", "Frame {n} presented at {n2}.{n3} ms, missed {n4}"),
    ("I", "PowerManagerService", "Waking up from sleep (uid={n}, reason=WAKE_REASON_POWER_BUTTON)"),
    (
        "W",
        "BroadcastQueue",
        "Background execution not allowed: receiving Intent {{ act=android.intent.action.X{n} }}",
    ),
    ("E", "CarService", "Exception in handler {n}: java.lang.IllegalStateException: state {n2}"),
    ("I", "GnssLocationProvider", "reportLocation: accuracy {n} m, satellites {n2}"),
]

TRACE = [
    "FATAL EXCEPTION: main",
    "Process: com.example.app{n}, PID: {pid}",
    "java.lang.NullPointerException: Attempt to invoke virtual method on a null object reference",
    "\tat com.example.app.Controller.update(Controller.java:{n})",
    "\tat android.os.Handler.dispatchMessage(Handler.java:106)",
    "\tat android.os.Looper.loop(Looper.java:288)",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--mb", type=float, default=100)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    rnd = random.Random(args.seed)
    target = int(args.mb * 1024 * 1024)
    pids = [rnd.randint(300, 30000) for _ in range(60)]
    written = 0
    ms = 0
    t0 = 8 * 3600 * 1000
    buffers = ["main", "system"]
    with args.out.open("w", encoding="utf-8", newline="\n") as f:
        f.write("--------- beginning of main\n")
        lines: list[str] = []
        while written < target:
            ms += rnd.randint(0, 3)
            t = t0 + ms
            day = 11 + t // 86_400_000
            t %= 86_400_000
            ts = f"10-{day:02d} {t // 3600000:02d}:{t // 60000 % 60:02d}:{t // 1000 % 60:02d}.{t % 1000:03d}"
            pid = rnd.choice(pids)
            tid = pid + rnd.randint(0, 40)
            r = rnd.random()
            if r < 0.001:
                b = rnd.choice(buffers)
                lines.append(f"--------- switch to {b}")
                continue
            if r < 0.003:
                n = rnd.randint(1, 999)
                for text in TRACE:
                    lines.append(f"{ts}  {pid:5d} {tid:5d} E AndroidRuntime: {text.format(n=n, pid=pid)}")
            else:
                lvl, tag, tmpl = rnd.choice(TAGS)
                msg = tmpl.format(
                    pid=pid,
                    n=rnd.randint(0, 5000),
                    n2=rnd.randint(0, 100000),
                    n3=rnd.randint(0, 99),
                    n4=rnd.randint(0, 9),
                    f=f"{rnd.uniform(-40, 40):.2f}",
                    hex=f"{rnd.getrandbits(32):08x}",
                    mac=":".join(f"{rnd.getrandbits(8):02x}" for _ in range(6)),
                    ip=f"10.{rnd.randint(0, 255)}.{rnd.randint(0, 255)}.{rnd.randint(1, 254)}",
                )
                lines.append(f"{ts}  {pid:5d} {tid:5d} {lvl} {tag}: {msg}")
            if len(lines) >= 10000:
                chunk = "\n".join(lines) + "\n"
                f.write(chunk)
                written += len(chunk)
                lines = []
        if lines:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
