"""
Read-only queries over the netusage database, shared by the dashboard.

The collector owns the database; nothing here writes to it. Every function
takes an open connection so the caller decides how often to reconnect.

Periods are half-open date ranges [start, end). A period's "kind" decides how
it is bucketed for charts: a single day by hour, anything up to a few months
by day, all time by month.
"""

import configparser
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

CONF_PATH = os.environ.get("NETUSAGE_CONF", "/etc/netusage.conf")
DEFAULT_DB = "/var/lib/netusage/netusage.db"
LIMITS_PATH = os.environ.get("NETUSAGE_LIMITS", "/var/lib/netusage/limits.conf")

KINDS = ("day", "week", "month", "cycle", "all")


def load_config():
    cfg = {"db": DEFAULT_DB, "cycle_day": 25}
    parser = configparser.ConfigParser()
    if os.path.exists(CONF_PATH):
        parser.read(CONF_PATH)
        if parser.has_section("general"):
            if parser.has_option("general", "db"):
                cfg["db"] = parser.get("general", "db").strip()
            if parser.has_option("general", "cycle_day"):
                cfg["cycle_day"] = int(parser.get("general", "cycle_day").strip())
    cfg["cycle_day"] = max(1, min(28, cfg["cycle_day"]))
    return cfg


def connect(path):
    """Read-only connection, or None if the collector has not created it yet."""
    if not os.path.exists(path):
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def human(num, precision=2):
    """1024-based, the same convention as the CLI and vnstat."""
    num = float(num or 0)
    for unit, size in (("TB", 1024 ** 4), ("GB", 1024 ** 3),
                       ("MB", 1024 ** 2), ("KB", 1024)):
        if num >= size:
            return f"{num / size:,.{precision}f} {unit}"
    return f"{int(num)} B"


def human_rate(bytes_per_sec):
    return human(bytes_per_sec, 1) + "/s"


# --------------------------------------------------------------------------
# periods
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Period:
    kind: str
    start: date
    end: date            # exclusive

    @property
    def days(self):
        return (self.end - self.start).days

    def contains(self, day):
        return self.start <= day < self.end

    @property
    def label(self):
        s, e = self.start, self.end - timedelta(days=1)
        if self.kind == "day":
            today = date.today()
            if s == today:
                return "Today"
            if s == today - timedelta(days=1):
                return "Yesterday"
            return s.strftime("%A, %d %B %Y")
        if self.kind == "month":
            return s.strftime("%B %Y")
        if self.kind == "all":
            return "All time"
        if s.year == e.year:
            return f"{s.strftime('%d %b')} – {e.strftime('%d %b %Y')}"
        return f"{s.strftime('%d %b %Y')} – {e.strftime('%d %b %Y')}"


def _add_months(day, months, dom=None):
    y, m = day.year, day.month + months
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    return date(y, m, dom or day.day)


def cycle_bounds(today, cycle_day):
    """(start, end) of the billing cycle containing `today`; end exclusive."""
    if today.day >= cycle_day:
        start = date(today.year, today.month, cycle_day)
    else:
        start = _add_months(date(today.year, today.month, cycle_day), -1)
    return start, _add_months(start, 1)


def period_for(kind, anchor, cycle_day, first_day=None):
    """The period of `kind` containing `anchor`.

    "week" is the seven days ending on the anchor rather than a calendar week,
    so on any day it answers "what did the last week cost".
    """
    if kind == "day":
        return Period("day", anchor, anchor + timedelta(days=1))
    if kind == "week":
        return Period("week", anchor - timedelta(days=6), anchor + timedelta(days=1))
    if kind == "month":
        start = anchor.replace(day=1)
        return Period("month", start, _add_months(start, 1))
    if kind == "cycle":
        return Period("cycle", *cycle_bounds(anchor, cycle_day))
    if kind == "all":
        start = first_day or anchor
        return Period("all", start, max(anchor, date.today()) + timedelta(days=1))
    raise ValueError(kind)


def shift(period, steps, cycle_day):
    """The period `steps` before (negative) or after (positive) this one."""
    if period.kind == "day":
        anchor = period.start + timedelta(days=steps)
    elif period.kind == "week":
        anchor = period.end - timedelta(days=1) + timedelta(days=7 * steps)
    elif period.kind == "month":
        anchor = _add_months(period.start, steps)
    elif period.kind == "cycle":
        anchor = _add_months(period.start, steps, cycle_day)
    else:
        return period
    return period_for(period.kind, anchor, cycle_day)


def is_current(period):
    return period.contains(date.today()) or period.kind == "all"


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------

def _range(period):
    return period.start.isoformat(), period.end.isoformat()


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (name,)).fetchone() is not None


def meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def first_day(db):
    row = db.execute("SELECT MIN(day) FROM iface_daily").fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def totals(db, period):
    """(rx, tx) carried on the physical interfaces during the period."""
    row = db.execute(
        "SELECT COALESCE(SUM(rx), 0), COALESCE(SUM(tx), 0) FROM iface_daily "
        "WHERE day >= ? AND day < ?", _range(period)).fetchone()
    return row[0], row[1]


def days_with_data(db, period):
    return db.execute(
        "SELECT COUNT(DISTINCT day) FROM iface_daily WHERE day >= ? AND day < ?",
        _range(period)).fetchone()[0]


def daily(db, period):
    """{date: (rx, tx)} for days that have data."""
    rows = db.execute(
        "SELECT day, SUM(rx), SUM(tx) FROM iface_daily WHERE day >= ? AND day < ? "
        "GROUP BY day", _range(period)).fetchall()
    return {date.fromisoformat(d): (rx, tx) for d, rx, tx in rows}


def hourly(db, day):
    """{hour: (rx, tx)} for one day, or None when hourly data is not kept."""
    if not has_table(db, "iface_hourly"):
        return None
    rows = db.execute(
        "SELECT hour, SUM(rx), SUM(tx) FROM iface_hourly "
        "WHERE hour >= ? AND hour < ? GROUP BY hour",
        (f"{day.isoformat()} 00", f"{(day + timedelta(days=1)).isoformat()} 00")
    ).fetchall()
    return {int(h[-2:]): (rx, tx) for h, rx, tx in rows}


@dataclass
class Bucket:
    label: str           # short axis label
    title: str           # tooltip heading
    rx: int
    tx: int
    current: bool = False
    start: date | None = None   # first day the bucket covers, for drill-down

    @property
    def total(self):
        return self.rx + self.tx


def series(db, period):
    """Chart buckets for the period: hours for a day, days, or months."""
    today = date.today()
    if period.kind == "day":
        hours = hourly(db, period.start)
        if hours is None:
            return []
        now = datetime.now().hour if period.start == today else 24
        return [Bucket(f"{h:02d}", f"{h:02d}:00 – {h:02d}:59",
                       *hours.get(h, (0, 0)), current=(h == now))
                for h in range(24)]

    if period.kind == "all":
        rows = db.execute(
            "SELECT substr(day, 1, 7) m, SUM(rx), SUM(tx) FROM iface_daily "
            "GROUP BY m ORDER BY m").fetchall()
        out = []
        for m, rx, tx in rows:
            d = date.fromisoformat(m + "-01")
            out.append(Bucket(d.strftime("%b"), d.strftime("%B %Y"), rx, tx,
                              current=(d.year, d.month) == (today.year, today.month),
                              start=d))
        return out

    per_day = daily(db, period)
    out = []
    d = period.start
    while d < period.end:
        rx, tx = per_day.get(d, (0, 0))
        label = d.strftime("%a") if period.kind == "week" else str(d.day)
        out.append(Bucket(label, d.strftime("%A %d %B"), rx, tx, current=(d == today),
                          start=d))
        d += timedelta(days=1)
    return out


def apps(db, period):
    """[(app, rx, tx)] largest first."""
    return db.execute(
        "SELECT app, SUM(rx) r, SUM(tx) t FROM app_daily WHERE day >= ? AND day < ? "
        "GROUP BY app HAVING r + t > 0 ORDER BY (r + t) DESC", _range(period)
    ).fetchall()


def app_daily(db, app, period):
    """{date: (rx, tx)} for one application."""
    rows = db.execute(
        "SELECT day, rx, tx FROM app_daily WHERE app = ? AND day >= ? AND day < ?",
        (app, *_range(period))).fetchall()
    return {date.fromisoformat(d): (rx, tx) for d, rx, tx in rows}


def app_first_seen(db, app):
    row = db.execute("SELECT MIN(day), MAX(day) FROM app_daily WHERE app = ?",
                     (app,)).fetchone()
    return row if row and row[0] else (None, None)


def cycles(db, cycle_day, count=12):
    """[(Period, rx, tx)] newest first, skipping empty past cycles."""
    current = period_for("cycle", date.today(), cycle_day)
    out = []
    for back in range(count):
        p = shift(current, -back, cycle_day)
        rx, tx = totals(db, p)
        if back and rx + tx == 0:
            continue
        out.append((p, rx, tx))
    return out


def meta_int(db, key):
    raw = meta(db, key)
    return int(raw) if raw and str(raw).isdigit() else 0


def daily_limit(db):
    """The daily limit the collector is enforcing right now."""
    return meta_int(db, "daily_limit_bytes")


def cycle_limit(db):
    """The billing-cycle (data plan) limit the collector is enforcing."""
    return meta_int(db, "cycle_limit_bytes")


# --------------------------------------------------------------------------
# notification limits, shared with the collector through LIMITS_PATH
# --------------------------------------------------------------------------

LIMIT_DEFAULTS = {
    "daily_limit_gb": 10.0,
    "notify_at": [80, 100, 150, 200],
    "cycle_limit_gb": 0.0,
    "cycle_notify_at": [75, 90, 100],
    "remind_every_min": 0,
}


def _percents(raw):
    return sorted({int(p) for p in str(raw).replace(" ", "").split(",")
                   if p.strip().isdigit() and 0 < int(p) <= 1000})


def read_limits(path=None):
    """Limits as the dashboard edits them. Missing keys take the defaults."""
    import configparser
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in LIMIT_DEFAULTS.items()}
    parser = configparser.ConfigParser()
    try:
        with open(path or LIMITS_PATH) as f:
            parser.read_file(f)
    except (OSError, configparser.Error):
        return out
    if not parser.has_section("limits"):
        return out
    sec = parser["limits"]
    for key in ("daily_limit_gb", "cycle_limit_gb"):
        try:
            out[key] = max(0.0, float(sec.get(key, out[key])))
        except ValueError:
            pass
    for key in ("notify_at", "cycle_notify_at"):
        if key in sec:
            out[key] = _percents(sec[key])
    try:
        out["remind_every_min"] = max(0, int(float(sec.get("remind_every_min", 0))))
    except ValueError:
        pass
    return out


def limits_writable(path=None):
    return os.access(path or LIMITS_PATH, os.W_OK)


def write_limits(limits, path=None):
    """Rewrite the limits file in place.

    In place rather than write-and-rename: the file's directory belongs to
    root, and the collector ignores a file it cannot parse, so a read that
    lands mid-write just keeps the previous limits for one more interval.
    """
    text = (
        "# Notification limits. Edited by the Net Usage dashboard (Preferences);\n"
        "# the collector picks up changes within one interval, no restart needed.\n"
        "[limits]\n"
        f"daily_limit_gb = {float(limits['daily_limit_gb']):g}\n"
        f"notify_at = {','.join(map(str, _percents(','.join(map(str, limits['notify_at'])))))}\n"
        f"cycle_limit_gb = {float(limits['cycle_limit_gb']):g}\n"
        f"cycle_notify_at = "
        f"{','.join(map(str, _percents(','.join(map(str, limits['cycle_notify_at'])))))}\n"
        f"remind_every_min = {int(limits['remind_every_min'])}\n")
    with open(path or LIMITS_PATH, "w") as f:
        f.write(text)


@dataclass
class Health:
    state: str           # running | stalled | waiting
    age: float | None
    backend: str
    interfaces: str
    data_from: str | None
    data_to: str | None
    data_days: int


def health(db):
    beat = meta(db, "heartbeat")
    age = time.time() - float(beat) if beat else None
    if age is None:
        state = "waiting"
    elif age > 120:
        state = "stalled"
    else:
        state = "running"
    row = db.execute("SELECT MIN(day), MAX(day), COUNT(DISTINCT day) "
                     "FROM iface_daily").fetchone()
    return Health(state, age, meta(db, "backend", "unknown") or "unknown",
                  meta(db, "interfaces", "?") or "?", row[0], row[1], row[2] or 0)


def coverage(db, period):
    """Share of interface bytes explained by per-app bytes, on days with both.

    None when there is nothing to compare. Values above 1 mean the per-app
    figures include traffic that never left the machine (loopback, bridges).
    """
    app_total = db.execute(
        "SELECT COALESCE(SUM(rx + tx), 0) FROM app_daily WHERE day >= ? AND day < ?",
        _range(period)).fetchone()[0]
    iface = db.execute(
        "SELECT COALESCE(SUM(rx + tx), 0) FROM iface_daily WHERE day IN "
        "(SELECT DISTINCT day FROM app_daily WHERE day >= ? AND day < ?)",
        _range(period)).fetchone()[0]
    if not app_total or not iface:
        return None
    return app_total / iface


def projection(db, cycle):
    """Projected cycle total at the current average daily rate, or None."""
    rx, tx = totals(db, cycle)
    covered = days_with_data(db, cycle)
    if not covered:
        return None
    return (rx + tx) / covered * cycle.days


# --------------------------------------------------------------------------
# live throughput, straight from the kernel counters
# --------------------------------------------------------------------------

def read_counters(interfaces):
    rx = tx = 0
    for iface in interfaces:
        try:
            with open(f"/sys/class/net/{iface}/statistics/rx_bytes") as f:
                rx += int(f.read())
            with open(f"/sys/class/net/{iface}/statistics/tx_bytes") as f:
                tx += int(f.read())
        except (OSError, ValueError):
            continue
    return rx, tx


class LiveMeter:
    """Bytes per second over the physical interfaces, sampled on demand."""

    def __init__(self, interfaces, history=60):
        self.interfaces = interfaces
        self.history = history
        self.samples = []        # [(rx_rate, tx_rate)]
        self._last = None

    def tick(self):
        now = time.monotonic()
        rx, tx = read_counters(self.interfaces)
        if self._last is not None:
            t0, rx0, tx0 = self._last
            dt = max(now - t0, 1e-3)
            # a counter reset (resume, reconnect) would read as a huge negative
            rate = (max(rx - rx0, 0) / dt, max(tx - tx0, 0) / dt)
            self.samples.append(rate)
            del self.samples[:-self.history]
        self._last = (now, rx, tx)
        return self.samples[-1] if self.samples else (0.0, 0.0)


def export_csv(db, period, path):
    """Per-day interface totals and per-app totals for the period."""
    import csv
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "key", "download_bytes", "upload_bytes", "total_bytes"])
        per_day = daily(db, period)
        for d in sorted(per_day):
            rx, tx = per_day[d]
            w.writerow(["day", d.isoformat(), rx, tx, rx + tx])
        for app, rx, tx in apps(db, period):
            w.writerow(["app", app, rx, tx, rx + tx])
