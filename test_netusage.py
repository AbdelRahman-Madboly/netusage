#!/usr/bin/env python3
"""Tests for netusage. Run: python3 test_netusage.py  (no root needed)"""

import configparser
import os
import re
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


def test_hourly():
    print("\n== hourly interface rows ==")
    path = tempfile.mktemp(suffix=".db")
    store = collector.Store(path)
    sampler = collector.InterfaceSampler(store, ["test0"])
    seq = []
    collector.read_counter = lambda i: seq.pop(0)

    seq[:] = [(1000, 500)]
    sampler.sample("2026-09-28", "2026-09-28 13")      # first sight
    seq[:] = [(1600, 700)]
    sampler.sample("2026-09-28", "2026-09-28 14")
    seq[:] = [(1900, 800)]
    sampler.sample("2026-09-28", "2026-09-28 14")      # same hour accumulates
    seq[:] = [(2000, 850)]
    sampler.sample("2026-09-28")                        # hour derived from day
    store.commit()

    rows = {h: (r, t) for h, i, r, t in
            store.db.execute("SELECT hour, iface, rx, tx FROM iface_hourly")}
    check("first sight writes no hour", "2026-09-28 13" in rows, False)
    check("hour key and sums", rows.get("2026-09-28 14"), (900, 300))
    derived = [h for h in rows if h != "2026-09-28 14"]
    check("derived hour key format",
          len(derived) == 1 and bool(re.match(r"^2026-09-28 \d\d$", derived[0])),
          True)
    day = store.db.execute(
        "SELECT rx, tx FROM iface_daily WHERE day='2026-09-28'").fetchone()
    hours = store.db.execute(
        "SELECT SUM(rx), SUM(tx) FROM iface_hourly "
        "WHERE hour LIKE '2026-09-28 %'").fetchone()
    check("hours sum to the day", hours, day)
    os.unlink(path)


def test_probes():
    print("\n== probe selection and rendering ==")
    real_version = collector._bpftrace_version
    try:
        collector._bpftrace_version = lambda: (0, 20)
        check("0.20: filtered, then print", collector._probe_sources(),
              ["probe_filtered.bt", "probe_print.bt"])
        collector._bpftrace_version = lambda: (0, 21)
        check("0.21: filtered, for, print", collector._probe_sources(),
              ["probe_filtered.bt", "probe_for.bt", "probe_print.bt"])
        check("no ifindex: unfiltered only",
              collector._probe_sources(filtered=False),
              ["probe_for.bt", "probe_print.bt"])
    finally:
        collector._bpftrace_version = real_version

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "probe_filtered.bt")) as f:
        source = f.read()
    text = collector.render_probe(source, 10, [2, 3])
    check("ifindex test substituted",
          text.count("if ($idx == 2 || $idx == 3) { $keep = 1; }"), 4)
    check("interval substituted", "interval:s:10 {" in text, True)
    check("no placeholder left", re.findall(r"__[A-Z_]+__", text), [])
    check("udp recv stash is deleted", text.count("delete(@u"), 2)
    try:
        collector.render_probe(source, 10, [])
        refused = False
    except ValueError:
        refused = True
    check("filtered probe refuses an empty ifindex list", refused, True)
    with open(os.path.join(here, "probe_print.bt")) as f:
        check("unfiltered probe ignores ifindexes",
              collector.render_probe(f.read(), 5, [2]).count("interval:s:5"), 1)

    check("ifindex read from sysfs",
          collector.interface_indexes(["lo", "no-such-if0"]), [1])


def test_classify():
    print("\n== process naming ==")
    case([], "cpuhp/2", "cpuhp/2")
    case([], "nvidia-drm/timeline-23", "nvidia-drm/timeline-23")
    case(["postgres: walwriter"], "postgres", "postgres")
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
    case(["/home/u/.local/share/acme-cli/versions/2.1.260", "--print"],
         "2.1.260", "acme-cli")
    case(["/opt/tool/versions/1.4/tool-agent"], "tool-agent", "tool-agent")
    case(["/opt/acme/releases/3.2.1"], "3.2.1", "acme")
    case(["node", "/home/u/.cache/foo/versions/1.2.3/index.js"], "node", "foo")
    case([], "Chrome_ChildIOT", "chrome")
    case([], "chrome_crashpad", "chrome")
    case([], "kworker/0:1", "kworker/0:1")


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


def test_limits():
    print("\n== limits set from the dashboard ==")
    import tempfile
    GB = 1024 ** 3
    base = {"daily_limit_gb": 10.0, "notify_at": [80, 100]}
    path = tempfile.mktemp(suffix=".conf")
    lim = collector.load_limits(base, path)
    check("no limits file keeps the conf values",
          (lim["daily_limit_bytes"], lim["notify_at"], lim["cycle_limit_bytes"]),
          (10 * GB, [80, 100], 0))
    with open(path, "w") as f:
        f.write("[limits]\ndaily_limit_gb = 2.5\nnotify_at = 50, 100\n"
                "cycle_limit_gb = 100\ncycle_notify_at = 90,100\nremind_every_min = 30\n")
    lim = collector.load_limits(base, path)
    check("limits file overrides", (lim["daily_limit_bytes"], lim["notify_at"],
                                    lim["cycle_limit_bytes"], lim["cycle_notify_at"],
                                    lim["remind_minutes"]),
          (int(2.5 * GB), [50, 100], 100 * GB, [90, 100], 30))
    with open(path, "w") as f:
        f.write("[limits]\ndaily_limit_gb = lots\n")
    check("an unparsable file is ignored whole",
          collector.load_limits(base, path)["daily_limit_bytes"], 10 * GB)
    os.unlink(path)

    sent = []
    collector.notify = lambda t, b, u="normal": sent.append((t, u)) or True
    dbpath = tempfile.mktemp(suffix=".db")
    store = collector.Store(dbpath)
    clock = [1000.0]
    n = collector.LimitNotifier(store, 10 * GB, [80, 100], remind_minutes=30,
                                scope="day", clock=lambda: clock[0])
    n.check("2026-10-01", 11 * GB)
    check("crossing the limit notifies once", len(sent), 1)
    clock[0] += 10 * 60
    n.check("2026-10-01", 11 * GB)
    check("no reminder before the interval", len(sent), 1)
    clock[0] += 21 * 60
    n.check("2026-10-01", 12 * GB)
    check("reminder after the interval", len(sent), 2)
    check("reminder says still over", sent[-1][0].startswith("Still over"), True)
    n.check("2026-10-01", 12 * GB)
    check("reminder does not repeat at once", len(sent), 2)

    raised = collector.LimitNotifier(store, 20 * GB, [80, 100], scope="day",
                                     clock=lambda: clock[0])
    raised.check("2026-10-01", 12 * GB)
    check("a raised limit is not already exceeded", len(sent), 2)
    raised.check("2026-10-01", 16 * GB)
    check("a changed limit re-arms its thresholds", len(sent), 3)

    cyc = collector.LimitNotifier(store, 100 * GB, [90, 100], scope="cycle",
                                  clock=lambda: clock[0])
    cyc.check("2026-09-25", 95 * GB)
    check("cycle limit notifies", sent[-1][0], "Data usage at 90% of your data plan")
    cyc.check("2026-09-25", 96 * GB)
    check("cycle threshold fires once", len(sent), 4)
    check("cycle start mid-cycle", collector.cycle_start(date(2026, 10, 3), 25),
          date(2026, 9, 25))
    check("cycle start in january", collector.cycle_start(date(2027, 1, 3), 25),
          date(2026, 12, 25))
    os.unlink(dbpath)


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


def test_apps_bounded_by_interfaces():
    print("\n== per-app bounded by what the network cards carried ==")
    G = 1024 ** 3
    batch = {"curl": (0, 50 * G), "chrome": (2 * G, G // 4)}
    out = collector.bound_apps(batch, 3 * G, G // 2)
    check("a bogus upload is cut to the interface figure",
          out["curl"][1] <= collector.app_cap(G // 2), True)
    check("downloads untouched when under the interface", out["chrome"][0], 2 * G)
    check("apps together never pass the cap",
          sum(tx for _, tx in out.values()) <= collector.app_cap(G // 2), True)
    normal = {"pip": (G, 1024), "git": (G // 2, 2048)}
    check("normal traffic passes unchanged", collector.bound_apps(normal, 2 * G, G // 8), normal)
    check("an idle interval still lets a little through",
          collector.bound_apps({"x": (1000, 1000)}, 0, 0), {"x": (1000, 1000)})


def test_bill_add_writes_bills_file():
    print("\n== bill add and base write the bills file ==")
    import argparse, io, contextlib, json, tempfile
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import netusage_data as nd
    path = tempfile.mktemp(suffix=".json")
    saved, nd.BILLS_PATH = nd.BILLS_PATH, path
    try:
        for rest, fee in ((["base", "200", "100"], 0), (["add", "50", "100"], 10)):
            args = argparse.Namespace(rest=rest, back=0, fee=fee, json=False, notify=False)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_bill(None, {"cycle_day": 1}, args)
        with open(path) as f:
            data = json.load(f)
        check("base bundle recorded", data["base"], [{"gb": 200.0, "egp": 100.0}])
        check("extra bundle recorded with its fee",
              [r for rows in data["cycles"].values() for r in rows], [{"gb": 50.0, "egp": 110.0}])
    finally:
        nd.BILLS_PATH = saved
        if os.path.exists(path):
            os.unlink(path)


def test_aliases_from_config():
    print("\n== aliases from the config ==")
    parser = configparser.ConfigParser()
    parser.read_string("[aliases]\nMy-Helper = my-tool\nempty =\n")
    check("aliases are read lower-case and blanks skipped",
          collector.read_aliases(parser), {"my-helper": "my-tool"})
    check("no section, no aliases", collector.read_aliases(configparser.ConfigParser()), {})


if __name__ == "__main__":
    test_cycles()
    test_counters()
    test_hourly()
    test_probes()
    test_classify()
    test_live_processes()
    test_notifier()
    test_limits()
    test_loop_paces()
    test_formatting()
    test_apps_bounded_by_interfaces()
    test_bill_add_writes_bills_file()
    test_aliases_from_config()
    print("\n" + ("ALL TESTS PASSED" if not FAILS
                  else f"{len(FAILS)} FAILURE(S): {FAILS}"))
    sys.exit(1 if FAILS else 0)
