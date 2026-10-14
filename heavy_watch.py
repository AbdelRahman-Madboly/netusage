#!/usr/bin/python3
"""Log which process is behind a heavy download.

Every INTERVAL seconds the Wi-Fi receive counter is compared with the last
reading. When more than THRESHOLD bytes arrived, the established connections
are listed with the bytes each one received since the previous sample, grouped
by process, and the top ones are appended to the log. Connections owned by
other users (docker pulls run by root) show up as "unknown".
"""
import os, re, subprocess, sys, time

IFACE = os.environ.get("HEAVY_IFACE", "wlan0")
INTERVAL = 30
THRESHOLD = 30 * 1024 ** 2
LOG = os.path.expanduser("~/.local/state/netusage/heavy.log")


def rx_bytes():
    with open(f"/sys/class/net/{IFACE}/statistics/rx_bytes") as f:
        return int(f.read())


def connections():
    out = subprocess.run(["ss", "-tinpH", "state", "established"],
                         capture_output=True, text=True).stdout.splitlines()
    conns = {}
    i = 0
    while i + 1 < len(out):
        head, info = out[i], out[i + 1]
        i += 2
        fields = head.split()
        if len(fields) < 4:
            continue
        m = re.search(r"bytes_received:(\d+)", info)
        if not m:
            continue
        proc = re.search(r'users:\(\("([^"]+)",pid=(\d+)', head)
        conns[(fields[2], fields[3])] = (int(m.group(1)),
                                         (proc.group(1), int(proc.group(2))) if proc else ("unknown", 0))
    return conns


def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline") as f:
            return f.read().replace("\0", " ").strip()[:160]
    except OSError:
        return ""


def main():
    last_rx, last = rx_bytes(), connections()
    while True:
        time.sleep(INTERVAL)
        rx, now = rx_bytes(), connections()
        got = rx - last_rx
        if got > THRESHOLD:
            per = {}
            for key, (b, who) in now.items():
                d = b - last.get(key, (0, who))[0]
                if d > 0:
                    per.setdefault(who, [0, set()])
                    per[who][0] += d
                    per[who][1].add(key[1].rsplit(":", 1)[0])
            top = sorted(per.items(), key=lambda kv: -kv[1][0])[:3]
            seen = sum(v[0] for _, v in top)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG, "a") as f:
                f.write(f"{stamp} {got / 1024 ** 2:.0f} MB in {INTERVAL}s "
                        f"({seen / 1024 ** 2:.0f} MB attributed)\n")
                for (name, pid), (b, peers) in top:
                    f.write(f"    {b / 1024 ** 2:7.1f} MB  {name}[{pid}]  {cmdline(pid)}  <- {', '.join(sorted(peers))[:80]}\n")
        last_rx, last = rx, now


if __name__ == "__main__":
    sys.exit(main())
