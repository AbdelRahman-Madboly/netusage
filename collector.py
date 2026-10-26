#!/usr/bin/env python3
"""
netusage collector.

Runs as a systemd service. Two independent jobs:

  1. Interface accounting - reads kernel byte counters for physical NICs only
     and stores per-day deltas. This is the authoritative total: it is
     independent of which network you are attached to, and survives reboots
     because the last raw counter value is checkpointed to the database.

  2. Per-network accounting - the same interface deltas, filed under the
     network they went over: a Wi-Fi router, keyed by its BSSID so renaming
     the network or changing its password keeps the same entry, or a wired
     network, keyed by its gateway's MAC address.

  3. Per-process accounting - attributes bytes to a process name using eBPF
     kprobes on the socket send/receive paths (falls back to nethogs, then to
     disabled). Only sockets routed through the counted NICs are kept, so
     loopback, docker bridge and tunnel traffic stay out. These are payload
     bytes, so they sum to slightly less than the interface total; they are a
     breakdown, not the headline figure.
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
from datetime import date, datetime

CONF_PATH = os.environ.get("NETUSAGE_CONF", "/etc/netusage.conf")
# Limits the desktop user may change from the dashboard without root. The
# installer makes it owned by that user; values here override the conf file.
LIMITS_PATH = os.environ.get("NETUSAGE_LIMITS", "/var/lib/netusage/limits.conf")
HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "interfaces": "auto",
    "cycle_day": "25",
    "interval": "10",
    "db": "/var/lib/netusage/netusage.db",
    "backend": "auto",
    "daily_limit_gb": "0",
    "notify_at": "80,100,150,200",
    "share": "on",
    "share_port": "8765",
}
# Secret the phone app must present to read usage over the home network.
SHARE_TOKEN_PATH = os.environ.get("NETUSAGE_SHARE_TOKEN", "/var/lib/netusage/share.token")
# Family phones: {"members": {id: {"name": ..., "token": ...}}}. Owned by the desktop user.
DEVICES_PATH = os.environ.get("NETUSAGE_DEVICES", "/var/lib/netusage/devices.json")


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
    cfg["share"] = str(cfg["share"]).lower() in ("on", "yes", "true", "1")
    cfg["share_port"] = int(cfg["share_port"])
    cfg["interval"] = max(2, int(cfg["interval"]))
    cfg["daily_limit_gb"] = max(0.0, float(cfg["daily_limit_gb"]))
    cfg["notify_at"] = sorted({
        int(p) for p in str(cfg["notify_at"]).replace(" ", "").split(",")
        if p.strip().isdigit()
    })
    return cfg


def _percent_list(raw):
    return sorted({int(p) for p in str(raw).replace(" ", "").split(",")
                   if p.strip().isdigit() and 0 < int(p) <= 1000})


def load_limits(cfg, path=None):
    """Notification limits: the conf file's, overridden by the limits file.

    A limits file that cannot be read or parsed is ignored as a whole rather
    than half-applied, so a save caught mid-write keeps the previous values.
    """
    limits = {
        "daily_limit_bytes": int(cfg["daily_limit_gb"] * 1024 ** 3),
        "notify_at": list(cfg["notify_at"]),
        "cycle_limit_bytes": 0,
        "cycle_notify_at": [75, 90, 100],
        "remind_minutes": 0,
    }
    path = path or LIMITS_PATH
    parser = configparser.ConfigParser()
    try:
        with open(path) as f:
            parser.read_file(f)
    except (OSError, configparser.Error):
        return limits
    if not parser.has_section("limits"):
        return limits
    sec = parser["limits"]
    try:
        update = {}
        if "daily_limit_gb" in sec:
            update["daily_limit_bytes"] = int(max(0.0, float(sec["daily_limit_gb"])) * 1024 ** 3)
        if "notify_at" in sec:
            update["notify_at"] = _percent_list(sec["notify_at"])
        if "cycle_limit_gb" in sec:
            update["cycle_limit_bytes"] = int(max(0.0, float(sec["cycle_limit_gb"])) * 1024 ** 3)
        if "cycle_notify_at" in sec:
            update["cycle_notify_at"] = _percent_list(sec["cycle_notify_at"])
        if "remind_every_min" in sec:
            update["remind_minutes"] = max(0, int(float(sec["remind_every_min"])))
    except ValueError:
        return limits
    limits.update(update)
    return limits


def cycle_start(today, cycle_day):
    """First day of the billing cycle containing `today`."""
    if today.day >= cycle_day:
        return date(today.year, today.month, cycle_day)
    if today.month > 1:
        return date(today.year, today.month - 1, cycle_day)
    return date(today.year - 1, 12, cycle_day)


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
CREATE TABLE IF NOT EXISTS iface_hourly (
    hour  TEXT    NOT NULL,
    iface TEXT    NOT NULL,
    rx    INTEGER NOT NULL DEFAULT 0,
    tx    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour, iface)
);
CREATE TABLE IF NOT EXISTS app_daily (
    day TEXT    NOT NULL,
    app TEXT    NOT NULL,
    rx  INTEGER NOT NULL DEFAULT 0,
    tx  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, app)
);
CREATE TABLE IF NOT EXISTS net_hourly (
    hour TEXT    NOT NULL,
    net  TEXT    NOT NULL,
    rx   INTEGER NOT NULL DEFAULT 0,
    tx   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour, net)
);
CREATE TABLE IF NOT EXISTS networks (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    label      TEXT NOT NULL,
    names      TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_usage (
    device TEXT    NOT NULL,
    hour   INTEGER NOT NULL,
    net    TEXT    NOT NULL,
    key    TEXT    NOT NULL,
    rx     INTEGER NOT NULL DEFAULT 0,
    tx     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device, hour, net, key)
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

    def add_iface_hourly(self, hour, iface, rx, tx):
        """hour is local time as 'YYYY-MM-DD HH'; the dashboard reads it as-is."""
        if rx == 0 and tx == 0:
            return
        self.db.execute(
            "INSERT INTO iface_hourly(hour, iface, rx, tx) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(hour, iface) DO UPDATE SET rx = rx + excluded.rx, "
            "tx = tx + excluded.tx",
            (hour, iface, rx, tx),
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

    def add_network(self, hour, key, rx, tx):
        if rx == 0 and tx == 0:
            return
        self.db.execute(
            "INSERT INTO net_hourly(hour, net, rx, tx) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(hour, net) DO UPDATE SET rx = rx + excluded.rx, "
            "tx = tx + excluded.tx",
            (hour, key, rx, tx),
        )

    def see_network(self, key, kind, name):
        """Remember a network and every name it has gone by; label is the latest."""
        now = time.time()
        row = self.db.execute("SELECT names FROM networks WHERE key = ?", (key,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO networks(key, kind, label, names, first_seen, last_seen) "
                "VALUES(?, ?, ?, ?, ?, ?)", (key, kind, name, name, now, now))
            return
        names = [n for n in row[0].split("\n") if n]
        if name and name not in names:
            names.append(name)
        self.db.execute(
            "UPDATE networks SET label = COALESCE(NULLIF(?, ''), label), names = ?, "
            "last_seen = ? WHERE key = ?", (name, "\n".join(names), now, key))

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


def _run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def router_key(bssid):
    """Same key the phone app uses: the BSSID without its first and last octets,
    which are the parts that differ between one router's 2.4 GHz and 5 GHz radios."""
    parts = bssid.lower().split(":")
    return "ap:" + ":".join(parts[1:-1]) if len(parts) == 6 else ""


def _gateway(iface):
    for line in _run("ip", "-4", "route", "show", "default", "dev", iface).splitlines():
        m = re.search(r"via (\S+)", line)
        if m:
            return m.group(1)
    return None


def _neighbour_mac(ip, iface):
    m = re.search(r"lladdr (\S+)", _run("ip", "neigh", "show", ip, "dev", iface))
    return m.group(1).lower() if m else None


def identify_network(iface):
    """(key, kind, name) of the network behind iface, or None when not connected.

    Wi-Fi is keyed by router (BSSID), so a new SSID or password on the same
    router stays one network. Wired is keyed by the gateway's MAC address."""
    if os.path.isdir(f"/sys/class/net/{iface}/wireless"):
        out = _run("nmcli", "-t", "-f", "IN-USE,BSSID,SSID", "dev", "wifi", "list",
                   "ifname", iface, "--rescan", "no")
        for line in out.splitlines():
            if not line.startswith("*"):
                continue
            # nmcli -t escapes the colons inside the BSSID as "\:"
            fields = re.split(r"(?<!\\):", line)
            if len(fields) >= 3:
                bssid = fields[1].replace("\\:", ":")
                ssid = ":".join(fields[2:]).replace("\\:", ":")
                key = router_key(bssid)
                if key:
                    return key, "wifi", ssid
        return None
    gw = _gateway(iface)
    if not gw:
        return None
    mac = _neighbour_mac(gw, iface)
    conn = _run("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", iface).strip()
    return ("gw:" + mac if mac else f"if:{iface}:{gw}"), "wired", conn or f"Ethernet via {gw}"


class NetworkWatcher:
    """Caches identify_network per interface; nmcli is cheap but not free."""

    TTL = 30

    def __init__(self):
        self.cache = {}

    def get(self, iface):
        hit = self.cache.get(iface)
        now = time.monotonic()
        if hit and now - hit[0] < self.TTL and hit[1] is not None:
            return hit[1]
        net = identify_network(iface)
        self.cache[iface] = (now, net)
        return net

    def invalidate(self):
        self.cache.clear()


class InterfaceSampler:
    """Turns monotonic kernel counters into per-day deltas across reboots."""

    def __init__(self, store, interfaces):
        self.store = store
        self.interfaces = interfaces
        self.last = store.load_counters()
        self.networks = NetworkWatcher()

    def sample(self, day, hour=None):
        """Record one interval. Returns the (rx, tx) all counted interfaces carried in it."""
        # The hour key must name the same day as `day`, so it is built from it
        # when the caller does not supply one.
        if hour is None:
            hour = f"{day} {datetime.now():%H}"
        total_rx = total_tx = 0
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
            total_rx += d_rx
            total_tx += d_tx
            self.store.add_iface(day, iface, d_rx, d_tx)
            self.store.add_iface_hourly(hour, iface, d_rx, d_tx)
            self.store.save_counter(iface, rx, tx)
            if d_rx or d_tx:
                net = self.networks.get(iface)
                if net:
                    key, kind, name = net
                    self.store.add_network(hour, key, d_rx, d_tx)
                    self.store.see_network(key, kind, name)
        return total_rx, total_tx


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

# Versioned installs run a binary named after its version
# (~/.local/share/assistant/versions/2.1.260). The name is the directory above.
_VERSION = re.compile(r"^\d+(\.\d+)+$")
VERSION_DIRS = {"versions", "version", "releases"}

# With the process gone, only the kernel comm is left. Chrome names its
# threads "Chrome_ChildIOThread" and similar, which the kernel truncates to
# 15 characters. Electron apps share those thread names, so this is a best
# guess, used only when there is no command line to go on.
COMM_PREFIXES = (("Chrome_", "chrome"), ("chrome_", "chrome"))


def _named_parent(path):
    """Nearest directory above `path` that identifies something, or None."""
    for parent in reversed(os.path.dirname(path).strip("/").split("/")):
        low = parent.lower()
        if (parent and low not in GENERIC_DIRS and low not in VERSION_DIRS
                and not _VERSION.match(parent)):
            return parent
    return None


def _script_name(argv):
    """For an interpreter invocation, name the thing being interpreted."""
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        base = _STRIP_SUFFIX.sub("", os.path.basename(arg.rstrip("/")))
        if not base:
            continue
        if base.lower() in GENERIC_SCRIPTS or _VERSION.match(base):
            # "server/index.js" says nothing; the directory above it does.
            parent = _named_parent(arg)
            if parent:
                return parent
        return base
    return None


def classify(pid, comm):
    argv = _cmdline(pid)

    # Kernel threads have no command line. Their comm uses a "name/cpu"
    # convention where the slash is part of the name, not a path separator.
    if not argv:
        for prefix, label in COMM_PREFIXES:
            if comm and comm.startswith(prefix):
                return label
        return (comm or "unknown")[:40]

    joined = " ".join(argv)
    lowered = joined.lower()

    # Some daemons rewrite argv[0] into a status line ("postgres: walwriter").
    head = argv[0].split()[0].rstrip(":") if argv[0].strip() else ""
    exe = os.path.basename(head)
    if _VERSION.match(exe):
        exe = _named_parent(head) or exe

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

# Per-app figures are payload, so in any interval they sit below what the
# network cards carried. The slack covers a probe dump landing on the other
# side of an interval boundary; the floor keeps a quiet or first interval
# (interface delta 0) from wiping real traffic.
APP_SLACK = 1.5
APP_FLOOR = 64 * 1024 ** 2


def app_cap(iface_bytes):
    return int(iface_bytes * APP_SLACK) + APP_FLOOR


def _bound(values, cap):
    values = [min(v, cap) for v in values]          # no one app above the interfaces
    total = sum(values)
    if total > cap:                                 # nor all of them together
        values = [v * cap // total for v in values]
    return values


def bound_apps(batch, iface_rx, iface_tx):
    """Clamp one interval's per-app (rx, tx) to what the interfaces carried.
    A run of bad probe readings once credited one process with far more
    upload than the cards carried that day; each reading was under
    SANE_BATCH_LIMIT, so only a comparison with the interfaces catches it."""
    apps = list(batch)
    rx = _bound([batch[a][0] for a in apps], app_cap(iface_rx))
    tx = _bound([batch[a][1] for a in apps], app_cap(iface_tx))
    out = dict(zip(apps, zip(rx, tx)))
    if out != batch:
        log(f"per-app clamped to interfaces ({iface_rx} rx, {iface_tx} tx): "
            + ", ".join(f"{a} {batch[a]}" for a in apps if out[a] != batch[a]))
    return out

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


FILTERED_PROBE = "probe_filtered.bt"


def _probe_sources(filtered=True):
    """Probe scripts to try, best first.

    probe_filtered.bt counts only traffic routed through the counted physical
    interfaces, so it leads whenever their indexes are known. It reads kernel
    structs the other two do not, so if it fails to attach the unfiltered
    probes remain behind it.

    Iterating a map with `for` needs bpftrace 0.21. Offering it to an older
    build only produces a syntax error in the log on every start, so it is
    only listed when the installed version can actually run it.
    """
    version = _bpftrace_version()
    if version is None or version >= (0, 21):
        sources = ["probe_for.bt", "probe_print.bt"]
    else:
        sources = ["probe_print.bt"]
    return ([FILTERED_PROBE] if filtered else []) + sources


def interface_indexes(interfaces):
    """Kernel ifindex of each interface that currently exists."""
    found = []
    for iface in interfaces:
        try:
            with open(f"/sys/class/net/{iface}/ifindex") as f:
                found.append(int(f.read()))
        except (OSError, ValueError):
            continue
    return sorted(set(found))


def render_probe(text, interval, ifindexes=()):
    """Fill in a probe script's placeholders."""
    text = text.replace("__INTERVAL__", str(interval))
    if "__IFINDEX_TEST__" in text:
        if not ifindexes:
            raise ValueError("filtered probe needs at least one ifindex")
        test = " || ".join(f"$idx == {int(i)}" for i in ifindexes)
        text = text.replace("__IFINDEX_TEST__", test)
    return text


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

    def __init__(self, accumulator, names, interval, interfaces=()):
        super().__init__(name="ebpf")
        self.acc = accumulator
        self.names = names
        self.interval = interval
        # Resolved once, like the interface list itself: an adapter plugged
        # in later is not in the filter until the service restarts.
        self.ifindexes = interface_indexes(interfaces)
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
        with open(os.path.join(HERE, source)) as f:
            text = render_probe(f.read(), self.interval, self.ifindexes)
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
        sources = _probe_sources(filtered=bool(self.ifindexes) and
                                 os.path.exists(os.path.join(HERE, FILTERED_PROBE)))
        if FILTERED_PROBE in sources:
            log(f"filtering per-app traffic to ifindex "
                f"{', '.join(str(i) for i in self.ifindexes)}")
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
        backend = EbpfBackend(accumulator, names, cfg["interval"], interfaces)
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
                                "-u", urgency, "-i", "com.madboly.NetUsage",
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


class LimitNotifier:
    """Fires once per threshold per period when usage crosses it.

    `scope` is "day" (key: the date) or "cycle" (key: the cycle's first day).
    Which thresholds have fired is kept in the database, not in memory, so a
    restart does not replay notifications the user already saw. Changing the
    limit starts the record afresh, since 80% of a new limit is a new event.

    With `remind_minutes` set, being over the limit repeats a reminder at
    that interval until the period ends.
    """

    def __init__(self, store, limit_bytes, thresholds, remind_minutes=0,
                 scope="day", clock=time.time):
        self.store = store
        self.limit = limit_bytes
        self.thresholds = thresholds
        self.remind = remind_minutes * 60
        self.scope = scope
        self.clock = clock

    def _key(self, key):
        # "notified:<day>" predates cycle limits; keep it so history carries over
        return f"notified:{key}" if self.scope == "day" else f"notified-{self.scope}:{key}"

    def _fired(self, key):
        stored_limit = self.store.get_meta(self._key(key) + ":limit")
        if stored_limit is not None and stored_limit != str(self.limit):
            return set()
        raw = self.store.get_meta(self._key(key), "") or ""
        return {int(p) for p in raw.split(",") if p.strip().isdigit()}

    def _text(self, top, used, cap, reminder=False):
        if self.scope == "day":
            what, period = "daily limit", "today"
        else:
            what, period = "data plan", "this billing cycle"
        if reminder:
            return (f"Still over your {what} - {used}",
                    f"You have used {used} {period}, {top}% of your {cap} {what}.",
                    "critical")
        if top >= 100:
            title = f"{what.capitalize()} reached - {used}"
            body = (f"You have used {used} {period}, past your {cap} {what}."
                    if top == 100 else
                    f"You have used {used} {period} - {top}% of your {cap} {what}.")
            return title, body, "critical"
        return (f"Data usage at {top}% of your {what}",
                f"{used} of {cap} used {period}.", "normal")

    def check(self, key, total):
        if self.limit <= 0:
            return
        now = self.clock()
        fired = self._fired(key)
        due = [t for t in self.thresholds
               if t not in fired and total >= self.limit * t / 100.0]
        used, cap = human(total), human(self.limit)
        if due:
            # Only announce the highest threshold crossed, but record them all
            # so a sudden spike does not produce a burst of notifications.
            top = max(due)
            notify(*self._text(top, used, cap))
            log(f"notified: {top}% of {self.scope} limit ({used} of {cap})")
            self.store.set_meta(self._key(key), ",".join(
                str(t) for t in sorted(fired | set(due))))
            self.store.set_meta(self._key(key) + ":limit", self.limit)
            if total >= self.limit:
                self.store.set_meta(self._key(key) + ":reminded", now)
            return
        if self.remind and total >= self.limit:
            last = float(self.store.get_meta(self._key(key) + ":reminded", 0) or 0)
            if now - last >= self.remind:
                pct = int(total * 100 // self.limit)
                notify(*self._text(pct, used, cap, reminder=True))
                log(f"reminded: {pct}% of {self.scope} limit ({used} of {cap})")
                self.store.set_meta(self._key(key) + ":reminded", now)


def DailyLimitNotifier(store, limit_bytes, thresholds, remind_minutes=0):
    return LimitNotifier(store, limit_bytes, thresholds, remind_minutes, "day")


class Limits:
    """Reloads the limits file when it changes and publishes the result.

    The values go into meta as well, so the CLI and dashboard show what the
    collector is actually enforcing, not what a file says.
    """

    def __init__(self, store, cfg):
        self.store = store
        self.cfg = cfg
        self.stamp = None
        self.daily = self.cycle = None
        self.reload(force=True)

    def _stamp(self):
        try:
            st = os.stat(LIMITS_PATH)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def reload(self, force=False):
        stamp = self._stamp()
        if not force and stamp == self.stamp:
            return False
        self.stamp = stamp
        lim = load_limits(self.cfg)
        self.daily = LimitNotifier(self.store, lim["daily_limit_bytes"], lim["notify_at"],
                                   lim["remind_minutes"], "day")
        self.cycle = LimitNotifier(self.store, lim["cycle_limit_bytes"], lim["cycle_notify_at"],
                                   lim["remind_minutes"], "cycle")
        for key, value in (("daily_limit_bytes", lim["daily_limit_bytes"]),
                           ("notify_at", ",".join(map(str, lim["notify_at"]))),
                           ("cycle_limit_bytes", lim["cycle_limit_bytes"]),
                           ("cycle_notify_at", ",".join(map(str, lim["cycle_notify_at"]))),
                           ("remind_minutes", lim["remind_minutes"])):
            self.store.set_meta(key, value)
        parts = []
        if lim["daily_limit_bytes"]:
            parts.append(f"daily {human(lim['daily_limit_bytes'])} at "
                         f"{', '.join(str(t) + '%' for t in lim['notify_at'])}")
        if lim["cycle_limit_bytes"]:
            parts.append(f"cycle {human(lim['cycle_limit_bytes'])} at "
                         f"{', '.join(str(t) + '%' for t in lim['cycle_notify_at'])}")
        if lim["remind_minutes"]:
            parts.append(f"remind every {lim['remind_minutes']} min")
        log("limits: " + ("; ".join(parts) if parts else "none"))
        return True

    def check(self, today):
        day = today.isoformat()
        self.daily.check(day, self.store.day_total(day))
        if self.cycle.limit:
            start = cycle_start(today, self.cfg["cycle_day"]).isoformat()
            row = self.store.db.execute(
                "SELECT COALESCE(SUM(rx + tx), 0) FROM iface_daily WHERE day >= ? AND day <= ?",
                (start, day)).fetchone()
            self.cycle.check(start, row[0])


# --------------------------------------------------------------------------
# sharing with the phone app
# --------------------------------------------------------------------------

def usage_export(db_path, since):
    """This laptop's usage since an epoch second, per network, for the phone app.

    Rows are [hour_epoch, net, key, rx, tx]; see netusage_data.laptop_rows for
    how untracked Wi-Fi is placed."""
    sys.path.insert(0, HERE)
    import netusage_data as nd
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        networks = [{"key": k, "kind": kind, "label": lab,
                     "names": [n for n in names.split("\n") if n]}
                    for k, kind, lab, names in db.execute(
                        "SELECT key, kind, label, names FROM networks")]
        return {"version": 2, "device": os.uname().nodename, "now": int(time.time()),
                "networks": networks, "hours": nd.laptop_rows(db, since)}
    finally:
        db.close()


SHARE_WINDOW = 300   # seconds a signed request stays valid


def share_signature(token, *parts):
    """HMAC-SHA256 of the parts, one per line. The token itself never crosses the network."""
    import hashlib
    import hmac
    msg = "\n".join(str(p) for p in parts).encode()
    return hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()


def start_share_server(cfg):
    """Read-only HTTP endpoint for the phone app on the home network.

    GET /v1/usage?since=<epoch>&ts=<epoch>&nonce=<random>&sig=<hmac> where sig
    signs path, since, ts and nonce with the token in SHARE_TOKEN_PATH. The
    token is never sent, so a device that answers the phone while it searches
    the network learns nothing it can reuse; a nonce is accepted once and only
    within SHARE_WINDOW. The reply carries X-Netusage-Sig, the HMAC of the
    nonce and body, so the phone can tell the real laptop from an impostor.
    Only private-network clients are answered."""
    import hmac
    import ipaddress
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    try:
        with open(SHARE_TOKEN_PATH) as f:
            token = f.read().strip()
    except OSError:
        token = ""
    if len(token) < 16:
        log(f"sharing off: no token in {SHARE_TOKEN_PATH}")
        return None
    seen = {}            # nonce -> expiry
    seen_lock = threading.Lock()

    def fresh(nonce, ts):
        now = time.time()
        if abs(now - ts) > SHARE_WINDOW or not 16 <= len(nonce) <= 64:
            return False
        with seen_lock:
            for n in [n for n, exp in seen.items() if exp < now]:
                del seen[n]
            if nonce in seen:
                return False
            seen[nonce] = now + 2 * SHARE_WINDOW
        return True

    def members():
        """Family phones from DEVICES_PATH: {id: (name, token)}. Re-read on every
        request so a phone can be added without restarting anything."""
        try:
            with open(DEVICES_PATH) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        out = {}
        for mid, m in (data.get("members") or {}).items():
            tok = str(m.get("token", ""))
            if len(tok) >= 16:
                out[str(mid)] = (str(m.get("name") or mid)[:32], tok)
        return out

    def set_meta(db, key, value):
        db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) "
                   "DO UPDATE SET value = excluded.value", (key, str(value)))

    def store_report(body, device, master):
        """A phone's own usage per network. Only the master phone's report also
        carries the plan (size, extra bundle, ISP reading), which every family
        phone then reads back through /v1/plan."""
        rows = body.get("hours") or []
        db = sqlite3.connect(cfg["db"], timeout=15)
        try:
            if rows:
                db.execute("DELETE FROM remote_usage WHERE device = ? AND hour >= ?",
                           (device, min(int(r[0]) for r in rows)))
                db.executemany(
                    "INSERT OR REPLACE INTO remote_usage(device, hour, net, key, rx, tx) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    [(device, int(h), str(n), str(k), int(rx), int(tx)) for h, n, k, rx, tx in rows])
            if master:
                plan = body.get("plan") or {}
                for key in ("plan_gb", "extra_gb", "extra_cycle", "isp_used_gb", "isp_at",
                            "plan_key", "cycle_day"):
                    if key in plan:
                        set_meta(db, f"phone_{key}", plan[key])
                set_meta(db, "phone_device", device)
                set_meta(db, "phone_report_at", int(time.time()))
            else:
                set_meta(db, f"member_report_at:{device}", int(time.time()))
            db.commit()
        finally:
            db.close()

    def plan_summary():
        """Only the bundle's totals, which is all a family phone is shown."""
        sys.path.insert(0, HERE)
        import netusage_data as nd
        db = sqlite3.connect(f"file:{cfg['db']}?mode=ro", uri=True, timeout=10)
        try:
            h = nd.household(db, cfg["cycle_day"])
            if h is None:
                return {"plan": None}
            return {"plan": {"total": h.plan, "used": h.used, "remaining": h.remaining,
                             "cycle_start": h.cycle.start.isoformat(),
                             "renews": h.cycle.end.isoformat(),
                             "isp_at": h.reported_at, "plan_key": nd.meta(db, "phone_plan_key", ""),
                             "updated": int(time.time())}}
        finally:
            db.close()

    def member_rows(since):
        db = sqlite3.connect(f"file:{cfg['db']}?mode=ro", uri=True, timeout=10)
        try:
            names = {name for name, _ in members().values()}
            out = {}
            for dev, h, n, k, rx, tx in db.execute(
                    "SELECT device, hour, net, key, rx, tx FROM remote_usage WHERE hour >= ?", (since,)):
                if dev in names:
                    out.setdefault(dev, []).append([h, n, k, rx, tx])
            return {"devices": [{"name": d, "hours": sorted(r)} for d, r in out.items()]}
        finally:
            db.close()

    class Handler(BaseHTTPRequestHandler):
        """Every request names who sends it (dev=<member id>, or none for the
        master phone) and is signed with that phone's own token. Family phones
        may only send their usage and read the plan's totals."""

        def _auth(self, *signed):
            try:
                private = ipaddress.ip_address(self.client_address[0]).is_private
            except ValueError:
                private = False
            if not private:
                self._send(403, {"error": "forbidden"})
                return None
            q = self.q
            dev = q.get("dev", "")
            if dev:
                m = members().get(dev)
                if m is None:
                    self._send(401, {"error": "unknown phone"})
                    return None
                name, tok = m
            else:
                name, tok = None, token
            try:
                ts = int(q.get("ts", "0"))
            except ValueError:
                self._send(400, {"error": "bad request"})
                return None
            nonce = q.get("nonce", "")
            want = share_signature(tok, self.url.path, *signed, ts, nonce)
            if not hmac.compare_digest(q.get("sig", ""), want):
                self._send(401, {"error": "bad signature"})
                return None
            if not fresh(nonce, ts):
                self._send(401, {"error": "stale or repeated request"})
                return None
            self.tok, self.nonce = tok, nonce
            return name or "", dev == ""

        def _parse(self):
            self.url = urlparse(self.path)
            self.q = {k: v[0] for k, v in parse_qs(self.url.query).items()}
            self.tok = self.nonce = None

        def do_POST(self):
            """POST /v1/phone: signed like a GET, plus the SHA-256 of the body."""
            import hashlib
            self._parse()
            if self.url.path != "/v1/phone":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 20 * 1024 * 1024:
                return self._send(413, {"error": "too large"})
            raw = self.rfile.read(length)
            who = self._auth(hashlib.sha256(raw).hexdigest())
            if who is None:
                return
            name, master = who
            try:
                body = json.loads(raw)
                device = str(body.get("device") or "phone")[:64] if master else name
                store_report(body, device, master)
            except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                return self._send(400, {"error": str(exc)})
            self._send(200, {"ok": True})

        def do_GET(self):
            self._parse()
            try:
                since = int(self.q.get("since", "0"))
            except ValueError:
                return self._send(400, {"error": "bad request"})
            who = self._auth(since)
            if who is None:
                return
            name, master = who
            try:
                if self.url.path == "/v1/ping":
                    return self._send(200, {"device": os.uname().nodename, "name": name})
                if self.url.path == "/v1/plan":
                    return self._send(200, plan_summary())
                if not master:
                    return self._send(403, {"error": "not allowed for this phone"})
                if self.url.path == "/v1/usage":
                    return self._send(200, usage_export(cfg["db"], since))
                if self.url.path == "/v1/phones":
                    return self._send(200, member_rows(since))
                self._send(404, {"error": "not found"})
            except sqlite3.Error as exc:
                self._send(500, {"error": str(exc)})

        def _send(self, code, body):
            data = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if self.tok and self.nonce:
                # signed with the asking phone's token, so it knows the reply is the laptop's
                self.send_header("X-Netusage-Sig", share_signature(self.tok, self.nonce, data.decode()))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    try:
        server = ThreadingHTTPServer(("0.0.0.0", cfg["share_port"]), Handler)
    except OSError as exc:
        log(f"sharing off: port {cfg['share_port']}: {exc.strerror}")
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="share").start()
    log(f"sharing usage with the phone app on port {cfg['share_port']}")
    return server


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

    limits = Limits(store, cfg)
    store.commit()

    sampler = InterfaceSampler(store, interfaces)
    accumulator = AppAccumulator()
    names = NameCache()
    backend = start_backend(cfg, accumulator, names, interfaces)
    share = start_share_server(cfg) if cfg["share"] else None

    # A stop flag, not a run flag: Event.wait() returns immediately when the
    # event is already set, so waiting on a "still running" event would never
    # sleep and the loop would spin flat out.
    stopping = threading.Event()

    def shutdown(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Per-app rows from before this date include loopback, bridge and tunnel
    # traffic; the dashboard says so. Written once, the first time the
    # filtered probe is seen running, and never moved forward afterwards.
    filter_since = store.get_meta("app_filter_since")

    while not stopping.is_set():
        if stopping.wait(cfg["interval"]):
            break
        now = datetime.now()
        today = now.date().isoformat()
        try:
            iface_rx, iface_tx = sampler.sample(today, now.strftime("%Y-%m-%d %H"))
            for app, (rx, tx) in bound_apps(accumulator.drain(), iface_rx, iface_tx).items():
                store.add_app(today, app, rx, tx)
            store.set_meta("heartbeat", time.time())
            state = backend.state if backend else "off"
            store.set_meta("backend", state)
            if state == FILTERED_PROBE and not filter_since:
                filter_since = today
                store.set_meta("app_filter_since", today)
            limits.reload()
            limits.check(now.date())
            store.commit()
        except sqlite3.Error as exc:
            log(f"database error: {exc}")
            time.sleep(2)

    log("shutting down")
    if share:
        share.shutdown()
    if backend:
        backend.stop.set()
        proc = getattr(backend, "proc", None)
        if proc and proc.poll() is None:
            proc.terminate()
    # final flush so the last interval is not lost
    now = datetime.now()
    today = now.date().isoformat()
    try:
        iface_rx, iface_tx = sampler.sample(today, now.strftime("%Y-%m-%d %H"))
        for app, (rx, tx) in bound_apps(accumulator.drain(), iface_rx, iface_tx).items():
            store.add_app(today, app, rx, tx)
        store.commit()
    except sqlite3.Error:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
