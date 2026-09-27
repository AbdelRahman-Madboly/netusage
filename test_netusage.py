#!/usr/bin/env python3
"""Tests for netusage. Run: python3 test_netusage.py  (no root needed)"""

import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collector
import cli

FAILS = []


def check(label, got, want):
    ok = got == want
    print(("  PASS  " if ok else "  FAIL  ") + label +
          ("" if ok else f"\n         got={got!r}\n        want={want!r}"))
    if not ok:
        FAILS.append(label)


def case(argv, comm, want):
    collector._cmdline = lambda pid, a=argv: a
    check(f"{comm:<22} {' '.join(argv)[:40]:<42}", collector.classify(1, comm), want)


def test_cycles():
    print("\n== billing cycle boundaries (cycle_day=25) ==")
    check("mid-cycle", cli.cycle_bounds(date(2026, 8, 31), 25),
          (date(2026, 8, 25), date(2026, 9, 25)))
    check("on the boundary", cli.cycle_bounds(date(2026, 8, 25), 25),
          (date(2026, 8, 25), date(2026, 9, 25)))
    check("day before boundary", cli.cycle_bounds(date(2026, 8, 24), 25),
          (date(2026, 7, 25), date(2026, 8, 25)))
    check("rolls back over new year", cli.cycle_bounds(date(2026, 1, 3), 25),
          (date(2025, 12, 25), date(2026, 1, 25)))
    check("rolls forward over new year", cli.cycle_bounds(date(2026, 12, 26), 25),
          (date(2026, 12, 25), date(2027, 1, 25)))
    check("february / leap year", cli.cycle_bounds(date(2024, 3, 1), 25),
          (date(2024, 2, 25), date(2024, 3, 25)))
    check("shift back 3 months", cli.shift_cycle(date(2026, 2, 25), 25, 3),
          date(2025, 11, 25))


def test_counters():
    print("\n== interface counters: reboots and restarts ==")
    path = tempfile.mktemp(suffix=".db")
    store = collector.Store(path)
    sampler = collector.InterfaceSampler(store, ["test0"])
    seq = []
    collector.read_counter = lambda i: seq.pop(0)

    seq[:] = [(1000, 500)]
    sampler.sample("2026-08-25")          # first sight: claim nothing
    seq[:] = [(3000, 1500)]
    sampler.sample("2026-08-25")          # normal growth
    seq[:] = [(200, 100)]
    sampler.sample("2026-08-26")          # reboot: counter reset
    seq[:] = [(900, 400)]
    sampler.sample("2026-08-26")
    store.commit()

    rows = {d: (r, t) for d, r, t in
            store.db.execute("SELECT day, rx, tx FROM iface_daily")}
    check("pre-existing counter not claimed", rows["2026-08-25"], (2000, 1000))
    check("reboot counted from zero", rows["2026-08-26"], (900, 400))

    # a fresh process must resume from the checkpoint, not lose the delta
    store2 = collector.Store(path)
    sampler2 = collector.InterfaceSampler(store2, ["test0"])
    check("checkpoint reloaded", sampler2.last.get("test0"), (900, 400))
    seq[:] = [(1400, 600)]
    sampler2.sample("2026-08-26")
    store2.commit()
    row = store2.db.execute(
        "SELECT rx, tx FROM iface_daily WHERE day='2026-08-26'").fetchone()
    check("no gap across restart", row, (1400, 600))
    os.unlink(path)


def test_classify():
    print("\n== process naming ==")
    case([], "cpuhp/2", "cpuhp/2")
    case([], "nvidia-drm/timeline-23", "nvidia-drm/timeline-23")
    case(["postgres: walwriter"], "postgres", "postgres")
    case(["assistant", "--continue"], "assistant", "assistant-cli")
    case(["/home/u/.local/bin/assistant"], "assistant", "assistant-cli")
    case(["/opt/google/chrome/chrome", "--type=renderer"], "chrome", "chrome")
    case(["/opt/google/chrome/chrome_crashpad_handler"], "chrome_crashp", "chrome")
    case(["/usr/lib/firefox/firefox", "-contentproc"], "Web Content", "firefox")
    case(["/usr/lib/apt/methods/https"], "https", "apt")
    case(["/usr/bin/dockerd", "-H", "fd://"], "dockerd", "docker")
    case(["node", "/usr/local/bin/n8n"], "node", "n8n")
    case(["node", "server/index.js"], "node", "server")
    case(["node", "/home/u/myproj/src/index.js"], "node", "myproj")
    case(["python3.13", "-m", "http.server"], "python3", "http.server")
    case(["python3.12"], "python3", "python3")
    case(["/usr/bin/ssh", "host"], "ssh", "ssh")
    case(["/usr/bin/qemu-system-x86_64"], "qemu", "qemu-system-x86_64")


def test_live_processes():
    print("\n== naming every process on this machine ==")
    import importlib
    importlib.reload(collector)
    names = collector.NameCache()
    resolved = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            comm = open(f"/proc/{pid}/comm").read().strip()
        except OSError:
            continue
        resolved[collector.classify(int(pid), comm)] = int(pid)

    check("every name is non-empty", all(n.strip() for n in resolved), True)
    check("no name is a filesystem path",
          any(n.startswith("/") for n in resolved), False)
    # A slash is legal only in kernel-thread names ("cpuhp/3"), which have no
    # command line. A userspace process must never yield one.
    leaked = [n for n, pid in resolved.items()
              if "/" in n and collector._cmdline(pid)]
    check("no userspace name contains a slash", leaked, [])


def test_notifier():
    print("\n== daily limit notifications ==")
    import tempfile
    GB = 1024 ** 3
    sent = []
    collector.notify = lambda t, b, u="normal": sent.append((t, u)) or True
    path = tempfile.mktemp(suffix=".db")
    store = collector.Store(path)
    n = collector.DailyLimitNotifier(store, 10 * GB, [80, 100, 150, 200])

    def step(day, gigs, want, label):
        before = len(sent)
        n.check(day, int(gigs * GB))
        check(label, len(sent) - before, want)

    step("2026-08-31", 5, 0, "5 GB fires nothing")
    step("2026-08-31", 7.9, 0, "7.9 GB is still under 80%")
    step("2026-08-31", 8, 1, "8 GB crosses 80%")
    step("2026-08-31", 8.5, 0, "80% does not fire twice")
    step("2026-08-31", 10, 1, "10 GB crosses the limit")
    step("2026-08-31", 12, 0, "nothing between 100% and 150%")
    step("2026-08-31", 15, 1, "15 GB crosses 150%")
    step("2026-09-01", 8, 1, "a new day resets thresholds")

    # a restart must not replay what the user already saw
    fresh = collector.DailyLimitNotifier(store, 10 * GB, [80, 100, 150, 200])
    before = len(sent)
    fresh.check("2026-09-01", int(8.5 * GB))
    check("restart does not replay", len(sent) - before, 0)

    # one alert for a spike, not one per threshold crossed
    sent.clear()
    collector.DailyLimitNotifier(store, 10 * GB, [80, 100, 150, 200]).check(
        "2026-09-02", int(25 * GB))
    check("spike past all thresholds is one alert", len(sent), 1)
    check("and it is critical", sent[0][1], "critical")

    sent.clear()
    collector.DailyLimitNotifier(store, 0, [80, 100]).check(
        "2026-09-03", int(999 * GB))
    check("limit of 0 disables notifications", sent, [])
    os.unlink(path)


def test_loop_paces():
    """The main loop must sleep between samples rather than spin.

    Waiting on a "still running" event returns instantly, which turns the
    collector into a busy loop that samples thousands of times a second. That
    keeps the totals correct, so only CPU time reveals it.
    """
    print("\n== collector paces itself ==")
    import subprocess
    import tempfile
    import time

    workdir = tempfile.mkdtemp()
    conf = os.path.join(workdir, "netusage.conf")
    with open(conf, "w") as f:
        f.write(f"[general]\ninterval = 3\ndb = {workdir}/n.db\n"
                f"backend = off\ndaily_limit_gb = 0\n")

    here = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.Popen(
        [sys.executable, os.path.join(here, "collector.py")],
        env=dict(os.environ, NETUSAGE_CONF=conf),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        window = 6.0
        time.sleep(window)
        with open(f"/proc/{proc.pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        cpu = (int(fields[11]) + int(fields[12])) / ticks
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    share = cpu / window * 100
    print(f"         used {cpu:.2f}s CPU over {window:.0f}s wall ({share:.1f}% of a core)")
    check("idles below 5% of a core", share < 5.0, True)


def test_formatting():
    print("\n== byte formatting ==")
    check("gigabytes", cli.human(48.0 * 1024 ** 3), "48.00 GB")
    check("megabytes", cli.human(5 * 1024 ** 2), "5.00 MB")
    check("zero", cli.human(0), "0 B")
    check("None is safe", cli.human(None), "0 B")


if __name__ == "__main__":
    test_cycles()
    test_counters()
    test_classify()
    test_live_processes()
    test_notifier()
    test_loop_paces()
    test_formatting()
    print("\n" + ("ALL TESTS PASSED" if not FAILS
                  else f"{len(FAILS)} FAILURE(S): {FAILS}"))
    sys.exit(1 if FAILS else 0)
