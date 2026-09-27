#!/usr/bin/env python3
"""
netusage collector.

Runs as a systemd service. Two independent jobs:

  1. Interface accounting - reads kernel byte counters for physical NICs only
     and stores per-day deltas. This is the authoritative total: it is
     independent of which network you are attached to, and survives reboots
     because the last raw counter value is checkpointed to the database.

  2. Per-process accounting - attributes bytes to a process name using eBPF
     kprobes on the socket send/receive paths (falls back to nethogs, then to
     disabled). These are payload bytes, so they sum to slightly less than the
     interface total; they are a breakdown, not the headline figure.
"""

import configparser
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date

CONF_PATH = os.environ.get("NETUSAGE_CONF", "/etc/netusage.conf")
HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "interfaces": "auto",
    "cycle_day": "25",
    "interval": "10",
    "db": "/var/lib/netusage/netusage.db",
    "backend": "auto",
    "daily_limit_gb": "0",
    "notify_at": "80,100,150,200",
}


def log(msg):
    print(f"[netusage] {msg}", flush=True)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULTS)
    parser = configparser.ConfigParser()
    if os.path.exists(CONF_PATH):
        parser.read(CONF_PATH)
        if parser.has_section("general"):
            for key in DEFAULTS:
                if parser.has_option("general", key):
                    cfg[key] = parser.get("general", key).strip()
    cfg["cycle_day"] = max(1, min(28, int(cfg["cycle_day"])))
    cfg["interval"] = max(2, int(cfg["interval"]))
    cfg["daily_limit_gb"] = max(0.0, float(cfg["daily_limit_gb"]))
    cfg["notify_at"] = sorted({
        int(p) for p in str(cfg["notify_at"]).replace(" ", "").split(",")
        if p.strip().isdigit()
    })
    return cfg


def physical_interfaces():
    """Physical NICs only: a /device link excludes bridges, veth, tun and lo."""
    found = []
    for name in sorted(os.listdir("/sys/class/net")):
        if name == "lo":
            continue
        if os.path.exists(f"/sys/class/net/{name}/device"):
            found.append(name)
    return found


def resolve_interfaces(cfg):
    if cfg["interfaces"].strip().lower() == "auto":
        return physical_interfaces()
    return [i.strip() for i in cfg["interfaces"].split(",") if i.strip()]


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS iface_daily (
    day   TEXT    NOT NULL,
    iface TEXT    NOT NULL,
    rx    INTEGER NOT NULL DEFAULT 0,
    tx    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, iface)
);
CREATE TABLE IF NOT EXISTS app_daily (
    day TEXT    NOT NULL,
    app TEXT    NOT NULL,
    rx  INTEGER NOT NULL DEFAULT 0,
    tx  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, app)
);
CREATE TABLE IF NOT EXISTS counter_state (
    iface   TEXT PRIMARY KEY,
    rx      INTEGER NOT NULL,
    tx      INTEGER NOT NULL,
    updated REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=15)
        self.db.execute("PRAGMA busy_timeout = 10000")
        self.db.executescript(SCHEMA)
        self.db.commit()
        try:
            os.chmod(path, 0o644)          # readable by the unprivileged CLI
            os.chmod(os.path.dirname(path), 0o755)
        except OSError:
            pass

    def set_meta(self, key, value):
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def load_counters(self):
        rows = self.db.execute("SELECT iface, rx, tx FROM counter_state").fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def save_counter(self, iface, rx, tx):
        self.db.execute(
            "INSERT INTO counter_state(iface, rx, tx, updated) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(iface) DO UPDATE SET rx=excluded.rx, tx=excluded.tx, "
            "updated=excluded.updated",
            (iface, rx, tx, time.time()),
        )

    def add_iface(self, day, iface, rx, tx):
        if rx == 0 and tx == 0:
            return
        self.db.execute(
            "INSERT INTO iface_daily(day, iface, rx, tx) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(day, iface) DO UPDATE SET rx = rx + excluded.rx, "
            "tx = tx + excluded.tx",
            (day, iface, rx, tx),
        )

    def add_app(self, day, app, rx, tx):
        if rx == 0 and tx == 0:
            return
        self.db.execute(
            "INSERT INTO app_daily(day, app, rx, tx) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(day, app) DO UPDATE SET rx = rx + excluded.rx, "
            "tx = tx + excluded.tx",
            (day, app, rx, tx),
        )

    def day_total(self, day):
        row = self.db.execute(
            "SELECT COALESCE(SUM(rx), 0) + COALESCE(SUM(tx), 0) "
            "FROM iface_daily WHERE day = ?", (day,)).fetchone()
        return row[0] if row else 0

    def get_meta(self, key, default=None):
        row = self.db.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def commit(self):
        self.db.commit()


# --------------------------------------------------------------------------
# interface sampling
# --------------------------------------------------------------------------

def read_counter(iface):
    try:
        with open(f"/sys/class/net/{iface}/statistics/rx_bytes") as f:
            rx = int(f.read())
        with open(f"/sys/class/net/{iface}/statistics/tx_bytes") as f:
            tx = int(f.read())
        return rx, tx
    except (OSError, ValueError):
        return None


class InterfaceSampler:
    """Turns monotonic kernel counters into per-day deltas across reboots."""

    def __init__(self, store, interfaces):
        self.store = store
        self.interfaces = interfaces
        self.last = store.load_counters()

    def sample(self, day):
        for iface in self.interfaces:
            current = read_counter(iface)
            if current is None:
                continue                      # interface vanished (usb unplug)
            rx, tx = current
            prev = self.last.get(iface)
            if prev is None:
                # First sight of this interface. Its counter already holds
                # traffic from before we started, which we cannot claim.
                d_rx = d_tx = 0
            elif rx < prev[0] or tx < prev[1]:
                # Counter went backwards: reboot or interface reset. Everything
                # currently on the counter was accumulated since that reset.
                d_rx, d_tx = rx, tx
            else:
                d_rx, d_tx = rx - prev[0], tx - prev[1]
            self.last[iface] = (rx, tx)
            self.store.add_iface(day, iface, d_rx, d_tx)
            self.store.save_counter(iface, rx, tx)


# --------------------------------------------------------------------------
# process name resolution
# --------------------------------------------------------------------------

def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]


def _starttime(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except OSError:
        return None
    # comm may contain spaces/parens; fields are counted after the last ')'
    close = data.rfind(b")")
    if close < 0:
        return None
    fields = data[close + 2:].split()
    return fields[19].decode() if len(fields) > 19 else None


# Executable name -> reported name. Exact match on the basename of argv[0].
EXE_ALIASES = {
    "assistant": "assistant-cli",
    "google-chrome": "chrome", "google-chrome-stable": "chrome",
    "chrome": "chrome", "chromium": "chrome", "chromium-browser": "chrome",
    "chrome_crashpad_handler": "chrome", "chrome-sandbox": "chrome",
    "firefox": "firefox", "firefox-bin": "firefox",
    "code": "vscode", "code-insiders": "vscode",
    "apt": "apt", "apt-get": "apt", "aptd": "apt", "dpkg": "apt",
    "packagekitd": "apt", "unattended-upgrade": "apt",
    "unattended-upgrades": "apt",
    "snap": "snapd", "snapd": "snapd", "snap-store": "snapd",
    "dockerd": "docker", "docker": "docker", "docker-proxy": "docker",
    "containerd": "docker", "containerd-shim-runc-v2": "docker",
    "git": "git", "git-remote-http": "git", "git-remote-https": "git",
    "fwupd": "system-update", "fwupdmgr": "system-update",
}

# Substring match on the whole command line. Only distinctive markers, checked
# after the exact-name table so a rename of the binary cannot mislead them.
CMDLINE_RULES = [
    ("assistant-cli", ("@example/assistant-cli", "assistant-cli", "/bin/assistant")),
    ("chrome", ("/google-chrome", "/chromium")),
    ("vscode", ("vscode-server", "/usr/share/code", ".vscode-server")),
    ("apt", ("/usr/lib/apt/", "unattended-upgr")),
]

# argv[0] of a script run through one of these says nothing useful, so the
# script it is running is used instead.
INTERPRETERS = {"node", "nodejs", "python3", "python2", "perl", "ruby",
                "php", "bash", "sh", "dash", "zsh", "java"}

# Script names too generic to identify anything; fall back to the directory.
GENERIC_SCRIPTS = {"index", "main", "app", "server", "cli", "run", "start",
                   "__main__", "manage", "bin", "serve", "daemon", "init"}

# Directory names that identify nothing either, so the search walks past them.
GENERIC_DIRS = {"bin", ".bin", "src", "lib", "dist", "build", "out",
                "node_modules", "current", "usr", "local", "opt", "home",
                "var", "tmp", "srv", "packages"}

_STRIP_SUFFIX = re.compile(r"(\.bin|-real|-wrapper|\.sh|\.js|\.py|\.ts)$")


def _script_name(argv):
    """For an interpreter invocation, name the thing being interpreted."""
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        base = _STRIP_SUFFIX.sub("", os.path.basename(arg.rstrip("/")))
        if not base:
            continue
        if base.lower() in GENERIC_SCRIPTS:
            # "server/index.js" says nothing; the directory above it does.
            for parent in reversed(os.path.dirname(arg).strip("/").split("/")):
                if parent and parent.lower() not in GENERIC_DIRS:
                    return parent
        return base
    return None


def classify(pid, comm):
    argv = _cmdline(pid)

    # Kernel threads have no command line. Their comm uses a "name/cpu"
    # convention where the slash is part of the name, not a path separator.
    if not argv:
        return (comm or "unknown")[:40]

    joined = " ".join(argv)
    lowered = joined.lower()

    # Some daemons rewrite argv[0] into a status line ("postgres: walwriter").
    head = argv[0].split()[0].rstrip(":") if argv[0].strip() else ""
    exe = os.path.basename(head)

    alias = EXE_ALIASES.get(exe.lower())
    if alias:
        return alias

    for label, needles in CMDLINE_RULES:
        for needle in needles:
            if needle in lowered:
                return label

    exe_lower = exe.lower()
    is_python = exe_lower.startswith("python")
    if exe_lower in INTERPRETERS or is_python:
        script = _script_name(argv)
        if script:
            return EXE_ALIASES.get(script.lower(), script)[:40]
        if is_python:
            return "python3"          # collapse python3.12 / python3.13 / python

    name = _STRIP_SUFFIX.sub("", exe) or exe or comm
    return (name or "unknown")[:40]


class NameCache:
    """pid -> friendly name, invalidated when a pid is recycled."""

    def __init__(self):
        self.entries = {}

    def get(self, pid, comm):
        start = _starttime(pid)
        cached = self.entries.get(pid)
        if cached and (start is None or cached[0] == start):
            return cached[1]
        name = classify(pid, comm)
        if start is not None:
            self.entries[pid] = (start, name)
        if len(self.entries) > 4096:
            self.entries.clear()
        return name


# --------------------------------------------------------------------------
# per-process backends
# --------------------------------------------------------------------------

class AppAccumulator:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}

    def add(self, app, rx, tx):
        with self.lock:
            cur = self.data.get(app)
            if cur:
                self.data[app] = (cur[0] + rx, cur[1] + tx)
            else:
                self.data[app] = (rx, tx)

    def drain(self):
        with self.lock:
            out, self.data = self.data, {}
        return out


# No process can move this much in one batch; anything larger is a probe
# reading a kernel value at the wrong width, not real traffic.
SANE_BATCH_LIMIT = 64 * 1024 ** 3

FOR_LINE = re.compile(r"^([TR])\s+(\d+)\s+(\S+)\s+(\d+)\s*$")
PRINT_LINE = re.compile(r"^@(tx|rx)\[(\d+),\s*(.*?)\]:\s*(\d+)\s*$")


def _bpftrace_version():
    """(major, minor) of the installed bpftrace, or None if it cannot be read."""
    try:
        out = subprocess.run(["bpftrace", "--version"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"v?(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _probe_sources():
    """Probe scripts to try, best first.

    Iterating a map with `for` needs bpftrace 0.21. Offering it to an older
    build only produces a syntax error in the log on every start, so it is
    only listed when the installed version can actually run it.
    """
    version = _bpftrace_version()
    if version is None or version >= (0, 21):
        return ["probe_for.bt", "probe_print.bt"]
    return ["probe_print.bt"]


def _error_lines(text, limit=4):
    """The interesting part of a bpftrace failure, not just its last line."""
    if not text:
        return []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    flagged = [l for l in lines if "ERROR" in l or "error:" in l]
    return [l[:200] for l in (flagged or lines)[:limit]]


class EbpfBackend(threading.Thread):
    """Supervises a bpftrace program and folds its output into the accumulator."""

    daemon = True

    def __init__(self, accumulator, names, interval):
        super().__init__(name="ebpf")
        self.acc = accumulator
        self.names = names
        self.interval = interval
        self.stop = threading.Event()
        self.proc = None
        self.source = None
        self._produced = False
        self._spawn_time = 0.0
        self._failed = False
        self.discarded = 0

    @property
    def state(self):
        """What `netusage status` reports. Only claims success once earned."""
        if self._produced:
            return self.source
        if self._failed:
            return "failed"
        alive = self.proc is not None and self.proc.poll() is None
        if alive and self.source and time.time() - self._spawn_time > 20:
            # Attached and stayed up; quiet simply means no traffic yet.
            return self.source
        return "starting"

    def _script(self, source):
        text = open(os.path.join(HERE, source)).read()
        text = text.replace("__INTERVAL__", str(self.interval))
        path = f"/run/netusage-{source}"
        try:
            with open(path, "w") as f:
                f.write(text)
        except OSError:
            path = f"/tmp/netusage-{source}"
            with open(path, "w") as f:
                f.write(text)
        return path

    def _spawn(self, source):
        script = self._script(source)
        cmd = ["bpftrace", script]
        if os.path.exists("/usr/bin/stdbuf"):
            cmd = ["stdbuf", "-oL", "-e0"] + cmd     # keep output line-buffered
        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

    def _consume(self, proc):
        """Read one bpftrace session. Returns True if it produced usable data."""
        produced = False
        for line in proc.stdout:
            if self.stop.is_set():
                break
            line = line.rstrip("\n")
            match = FOR_LINE.match(line)
            if match:
                kind, pid, comm, value = match.groups()
                value = int(value)
                if value > SANE_BATCH_LIMIT:
                    self.discarded += 1
                    continue
                name = self.names.get(int(pid), comm)
                self.acc.add(name, value if kind == "R" else 0,
                             value if kind == "T" else 0)
                produced = self._produced = True
                continue
            match = PRINT_LINE.match(line)
            if match:
                kind, pid, comm, value = match.groups()
                value = int(value)
                if value > SANE_BATCH_LIMIT:
                    self.discarded += 1
                    continue
                name = self.names.get(int(pid), comm.strip())
                self.acc.add(name, value if kind == "rx" else 0,
                             value if kind == "tx" else 0)
                produced = self._produced = True
        return produced

    def run(self):
        sources = _probe_sources()
        index = 0
        while not self.stop.is_set():
            source = sources[min(index, len(sources) - 1)]
            try:
                proc = self._spawn(source)
            except FileNotFoundError:
                log("bpftrace not installed; per-process accounting disabled")
                self._failed = True
                return
            self.proc = proc
            started = self._spawn_time = time.time()
            self.source = source
            log(f"per-process backend: ebpf ({source})")
            self._consume(proc)
            proc.wait()
            err = (proc.stderr.read() or "").strip() if proc.stderr else ""
            alive = time.time() - started
            if self.stop.is_set():
                return
            if alive < 15 and index < len(sources) - 1:
                log(f"{source} failed after {alive:.1f}s, trying next variant")
                for line in _error_lines(err):
                    log(f"  bpftrace: {line}")
                index += 1
                continue
            for line in _error_lines(err):
                log(f"  bpftrace: {line}")
            if not self._produced and alive < 15:
                self._failed = True     # never attached; stop claiming success
            time.sleep(5)                             # restart loop


NETHOGS_LINE = re.compile(r"^(.*?)/(\d+)/(\d+)\s+([\d.]+)\s+([\d.]+)\s*$")


class NethogsBackend(threading.Thread):
    """Fallback: parse `nethogs -t` cumulative KB columns."""

    daemon = True

    def __init__(self, accumulator, names, interval, interfaces):
        super().__init__(name="nethogs")
        self.acc = accumulator
        self.names = names
        self.interval = interval
        self.interfaces = interfaces
        self.stop = threading.Event()
        self.seen = {}
        self._produced = False
        self._failed = False

    @property
    def state(self):
        if self._produced:
            return "nethogs"
        return "failed" if self._failed else "starting"

    def run(self):
        cmd = ["nethogs", "-t", "-d", str(self.interval)] + self.interfaces
        while not self.stop.is_set():
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, bufsize=1)
            except FileNotFoundError:
                log("nethogs not installed; per-process accounting disabled")
                self._failed = True
                return
            log("per-process backend: nethogs")
            for line in proc.stdout:
                if self.stop.is_set():
                    break
                match = NETHOGS_LINE.match(line.strip())
                if not match:
                    continue
                path, pid, _uid, sent_kb, recv_kb = match.groups()
                pid = int(pid)
                name = self.names.get(pid, os.path.basename(path)) if pid else \
                    os.path.basename(path) or "unknown"
                sent, recv = float(sent_kb) * 1024, float(recv_kb) * 1024
                key = (pid, path)
                prev = self.seen.get(key, (0.0, 0.0))
                # nethogs reports cumulative totals per process; diff them.
                d_sent = sent - prev[0] if sent >= prev[0] else sent
                d_recv = recv - prev[1] if recv >= prev[1] else recv
                self.seen[key] = (sent, recv)
                self.acc.add(name, int(d_recv), int(d_sent))
                self._produced = True
            proc.wait()
            if self.stop.is_set():
                return
            time.sleep(5)


def start_backend(cfg, accumulator, names, interfaces):
    choice = cfg["backend"].strip().lower()
    if choice == "off":
        log("per-process accounting disabled by config")
        return None
    have_bpftrace = any(
        os.path.exists(os.path.join(p, "bpftrace"))
        for p in os.environ.get("PATH", "/usr/bin:/usr/sbin").split(":")
    )
    if choice in ("auto", "ebpf") and have_bpftrace:
        backend = EbpfBackend(accumulator, names, cfg["interval"])
    elif choice in ("auto", "nethogs"):
        backend = NethogsBackend(accumulator, names, cfg["interval"], interfaces)
    else:
        log(f"backend '{choice}' unavailable; per-process accounting disabled")
        return None
    backend.start()
    return backend



# --------------------------------------------------------------------------
# daily limit notifications
# --------------------------------------------------------------------------

def _desktop_sessions():
    """(uid, username, bus address) for every logged-in graphical session.

    The collector runs as root, which has no session bus of its own, so a
    notification has to be handed to the desktop user's bus explicitly.
    """
    import pwd
    sessions = []
    run_user = "/run/user"
    if not os.path.isdir(run_user):
        return sessions
    for entry in os.listdir(run_user):
        if not entry.isdigit():
            continue
        uid = int(entry)
        bus = os.path.join(run_user, entry, "bus")
        if not os.path.exists(bus):
            continue
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            continue
        sessions.append((uid, name, f"unix:path={bus}"))
    return sessions


def notify(title, body, urgency="normal"):
    """Best-effort desktop notification. Never raises into the collector."""
    sent = False
    for uid, user, bus in _desktop_sessions():
        env = dict(os.environ)
        env.update({
            "DBUS_SESSION_BUS_ADDRESS": bus,
            "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        })
        launchers = [["runuser", "-u", user, "--"], ["sudo", "-u", user]]
        if os.geteuid() == uid:
            launchers.insert(0, [])          # already the desktop user
        for launcher in launchers:
            try:
                result = subprocess.run(
                    launcher + ["notify-send", "-a", "netusage",
                                "-u", urgency, "-i", "network-wireless",
                                title, body],
                    env=env, timeout=10,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                if result.returncode == 0:
                    sent = True
                    break
            except (OSError, subprocess.SubprocessError):
                continue
    return sent


def human(num):
    num = float(num or 0)
    for unit, size in (("TB", 1024 ** 4), ("GB", 1024 ** 3),
                       ("MB", 1024 ** 2), ("KB", 1024)):
        if num >= size:
            return f"{num / size:.2f} {unit}"
    return f"{int(num)} B"


class DailyLimitNotifier:
    """Fires once per threshold per day when today's usage crosses it.

    Which thresholds have fired is kept in the database, not in memory, so a
    restart mid-day does not replay notifications the user already saw.
    """

    def __init__(self, store, limit_bytes, thresholds):
        self.store = store
        self.limit = limit_bytes
        self.thresholds = thresholds

    def _fired(self, day):
        raw = self.store.get_meta(f"notified:{day}", "") or ""
        return {int(p) for p in raw.split(",") if p.strip().isdigit()}

    def check(self, day, total):
        if self.limit <= 0 or not self.thresholds:
            return
        fired = self._fired(day)
        due = [t for t in self.thresholds
               if t not in fired and total >= self.limit * t / 100.0]
        if not due:
            return
        # Only announce the highest threshold crossed, but record them all so
        # a sudden spike does not produce a burst of notifications.
        top = max(due)
        used, cap = human(total), human(self.limit)
        if top >= 100:
            title = f"Daily data limit reached - {used}"
            body = (f"You have used {used} today, past your {cap} daily limit."
                    if top == 100 else
                    f"You have used {used} today - {top}% of your {cap} limit.")
            urgency = "critical"
        else:
            title = f"Data usage at {top}% of today's limit"
            body = f"{used} of {cap} used today."
            urgency = "normal"

        notify(title, body, urgency)
        log(f"notified: {top}% of daily limit ({used} of {cap})")
        self.store.set_meta(f"notified:{day}", ",".join(
            str(t) for t in sorted(fired | set(due))))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    cfg = load_config()
    interfaces = resolve_interfaces(cfg)
    if not interfaces:
        log("no physical interfaces found; nothing to account")
        return 1

    store = Store(cfg["db"])
    store.set_meta("cycle_day", cfg["cycle_day"])
    store.set_meta("interfaces", ",".join(interfaces))
    store.set_meta("started", time.time())
    store.commit()

    log(f"counting interfaces: {', '.join(interfaces)}")

    limit_bytes = int(cfg["daily_limit_gb"] * 1024 ** 3)
    store.set_meta("daily_limit_bytes", limit_bytes)
    if limit_bytes:
        log(f"daily limit: {human(limit_bytes)} "
            f"(notify at {', '.join(str(t) + '%' for t in cfg['notify_at'])})")
    notifier = DailyLimitNotifier(store, limit_bytes, cfg["notify_at"])

    sampler = InterfaceSampler(store, interfaces)
    accumulator = AppAccumulator()
    names = NameCache()
    backend = start_backend(cfg, accumulator, names, interfaces)

    # A stop flag, not a run flag: Event.wait() returns immediately when the
    # event is already set, so waiting on a "still running" event would never
    # sleep and the loop would spin flat out.
    stopping = threading.Event()

    def shutdown(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while not stopping.is_set():
        if stopping.wait(cfg["interval"]):
            break
        today = date.today().isoformat()
        try:
            sampler.sample(today)
            for app, (rx, tx) in accumulator.drain().items():
                store.add_app(today, app, rx, tx)
            store.set_meta("heartbeat", time.time())
            store.set_meta("backend",
                           backend.state if backend else "off")
            notifier.check(today, store.day_total(today))
            store.commit()
        except sqlite3.Error as exc:
            log(f"database error: {exc}")
            time.sleep(2)

    log("shutting down")
    if backend:
        backend.stop.set()
        proc = getattr(backend, "proc", None)
        if proc and proc.poll() is None:
            proc.terminate()
    # final flush so the last interval is not lost
    today = date.today().isoformat()
    try:
        sampler.sample(today)
        for app, (rx, tx) in accumulator.drain().items():
            store.add_app(today, app, rx, tx)
        store.commit()
    except sqlite3.Error:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
