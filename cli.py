#!/usr/bin/env python3
"""netusage - report network consumption for the current billing cycle."""

import configparser
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta

CONF_PATH = os.environ.get("NETUSAGE_CONF", "/etc/netusage.conf")
DEFAULT_DB = "/var/lib/netusage/netusage.db"

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else str(text)


BOLD = lambda s: c(s, "1")
DIM = lambda s: c(s, "2")
CYAN = lambda s: c(s, "36")
GREEN = lambda s: c(s, "32")
YELLOW = lambda s: c(s, "33")
RED = lambda s: c(s, "31")


# --------------------------------------------------------------------------

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
    return cfg


def connect(path):
    if not os.path.exists(path):
        sys.exit(
            f"{RED('netusage:')} no database at {path}\n"
            f"The collector has not run yet. Check it with:  systemctl status netusage"
        )
    for attempt in range(5):
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
            db.execute("SELECT 1 FROM meta LIMIT 1")
            return db
        except sqlite3.Error:
            if attempt == 4:
                sys.exit(f"{RED('netusage:')} cannot read {path}")
            time.sleep(0.3)


def human(num):
    """Byte count as a short string. 1024-based, the same convention vnstat uses."""
    num = float(num or 0)
    for unit, size in (("TB", 1024 ** 4), ("GB", 1024 ** 3),
                       ("MB", 1024 ** 2), ("KB", 1024)):
        if num >= size:
            return f"{num / size:,.2f} {unit}"
    return f"{int(num)} B"


def gb(num):
    return (num or 0) / (1024 ** 3)


# --------------------------------------------------------------------------

def cycle_bounds(today, cycle_day):
    """Return (start, end) for the cycle containing `today`; end is exclusive."""
    if today.day >= cycle_day:
        start = date(today.year, today.month, cycle_day)
    elif today.month > 1:
        start = date(today.year, today.month - 1, cycle_day)
    else:
        start = date(today.year - 1, 12, cycle_day)
    if start.month < 12:
        end = date(start.year, start.month + 1, cycle_day)
    else:
        end = date(start.year + 1, 1, cycle_day)
    return start, end


def shift_cycle(start, cycle_day, months_back):
    y, m = start.year, start.month - months_back
    while m < 1:
        m += 12
        y -= 1
    return date(y, m, cycle_day)


def iface_totals(db, start, end):
    row = db.execute(
        "SELECT COALESCE(SUM(rx), 0), COALESCE(SUM(tx), 0) FROM iface_daily "
        "WHERE day >= ? AND day < ?", (start.isoformat(), end.isoformat())
    ).fetchone()
    return row[0], row[1]


def app_totals(db, start, end):
    return db.execute(
        "SELECT app, SUM(rx) AS r, SUM(tx) AS t FROM app_daily "
        "WHERE day >= ? AND day < ? GROUP BY app ORDER BY (r + t) DESC",
        (start.isoformat(), end.isoformat())
    ).fetchall()


def daily_rows(db, start, end):
    return db.execute(
        "SELECT day, SUM(rx), SUM(tx) FROM iface_daily "
        "WHERE day >= ? AND day < ? GROUP BY day ORDER BY day",
        (start.isoformat(), end.isoformat())
    ).fetchall()


def meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


# --------------------------------------------------------------------------

def bar(fraction, width=20):
    filled = int(round(fraction * width))
    filled = max(0, min(width, filled))
    return c("█" * filled, "36") + DIM("░" * (width - filled))


def rule(width=62):
    print(DIM("─" * width))


def print_apps(rows, limit=None, indent="  "):
    if not rows:
        print(f"{indent}{DIM('no per-process data yet')}")
        return 0
    total = sum(r[1] + r[2] for r in rows) or 1
    shown = rows[:limit] if limit else rows
    width = max((len(r[0]) for r in shown), default=10)
    width = min(max(width, 10), 24)
    for app, rx, tx in shown:
        combined = rx + tx
        share = combined / total
        print(f"{indent}{app[:width]:<{width}}  {human(combined):>10}  "
              f"{bar(share)} {share * 100:4.1f}%   "
              f"{DIM('↓' + human(rx) + '  ↑' + human(tx))}")
    return total


def limit_line(db, used_today, indent="  "):
    """Today's usage against the daily limit, or nothing if no limit is set."""
    raw = meta(db, "daily_limit_bytes")
    limit = int(raw) if raw and raw.isdigit() else 0
    if limit <= 0:
        return None
    share = used_today / limit
    if share >= 1.0:
        tint, note = RED, "over limit"
    elif share >= 0.8:
        tint, note = YELLOW, "approaching limit"
    else:
        tint, note = GREEN, f"{human(limit - used_today)} left"
    return (f"{indent}{'daily limit':<14} {tint(f'{human(used_today)} / {human(limit)}')}"
            f"  {bar(min(share, 1.0))} {share * 100:4.0f}%   {DIM(note)}")


def health_note(db):
    beat = meta(db, "heartbeat")
    if not beat:
        return YELLOW("collector has not reported yet")
    age = time.time() - float(beat)
    if age > 120:
        mins = int(age // 60)
        return RED(f"collector last reported {mins} min ago - "
                   f"check: systemctl status netusage")
    return None


# --------------------------------------------------------------------------

def cmd_report(db, cfg, args):
    today = date.today()
    cycle_day = int(meta(db, "cycle_day", cfg["cycle_day"]))
    start, end = cycle_bounds(today, cycle_day)

    rx, tx = iface_totals(db, start, end)
    total = rx + tx
    span = (end - start).days
    day_no = (today - start).days + 1
    days_left = max(0, (end - today).days - 1)   # end is exclusive

    t_rx, t_tx = iface_totals(db, today, today + timedelta(days=1))
    # Averaging over elapsed days understates usage whenever collection began
    # part way through a cycle, so only days with data count.
    covered = db.execute(
        "SELECT COUNT(DISTINCT day) FROM iface_daily WHERE day >= ? AND day < ?",
        (start.isoformat(), end.isoformat())).fetchone()[0]
    elapsed = max(covered, 1)

    print()
    print(f"  {BOLD('netusage')}   {DIM(start.strftime('%d %b') + '  →  ' + end.strftime('%d %b %Y'))}")
    print(f"  {DIM(f'day {day_no} of {span} · {days_left} days left in cycle')}")
    print()
    rule()
    print(f"  {BOLD(f'{"CYCLE TOTAL":<14}')} {GREEN(f'{human(total):>13}')}")
    print(f"  {'downloaded':<14} {human(rx):>13}")
    print(f"  {'uploaded':<14} {human(tx):>13}")
    rule()
    print(f"  {'today':<14} {human(t_rx + t_tx):>13}   {DIM('↓' + human(t_rx) + '  ↑' + human(t_tx))}")
    print(f"  {'daily average':<14} {human(total / elapsed):>13}")
    limit = limit_line(db, t_rx + t_tx)
    if limit:
        print(limit)
    if days_left > 0:
        projected = (total / elapsed) * span
        print(f"  {'projected':<14} {human(projected):>13}   {DIM('at this rate, by cycle end')}")
    rule()
    print()

    if covered < day_no:
        first = db.execute(
            "SELECT MIN(day) FROM iface_daily WHERE day >= ?",
            (start.isoformat(),)).fetchone()[0]
        if first:
            since = datetime.strptime(first, "%Y-%m-%d").strftime("%d %b")
            print(DIM(f"  Collecting since {since}; the {day_no - covered} "
                      f"earlier day(s) of this cycle are not included."))
            print()

    rows = app_totals(db, start, end)
    print(f"  {BOLD('TOP APPLICATIONS')}")
    print()
    app_total = print_apps(rows, limit=args.limit)
    print()
    if rows and app_total:
        # Compare against the interface bytes for the days per-app data exists,
        # otherwise a day of interface-only history drags the ratio down and
        # looks like the probe is missing traffic.
        comparable = db.execute(
            "SELECT COALESCE(SUM(rx + tx), 0) FROM iface_daily WHERE day IN "
            "(SELECT DISTINCT day FROM app_daily WHERE day >= ? AND day < ?)",
            (start.isoformat(), end.isoformat())).fetchone()[0]
        if comparable:
            pct = min(100.0, app_total / comparable * 100)
            print(DIM(f"  Per-app figures are socket payload bytes and cover "
                      f"~{pct:.0f}% of the {human(comparable)} carried on the "
                      f"days they were recorded."))
            print(DIM("  The remainder is protocol overhead and retransmits, "
                      "which cannot be attributed to a process."))
    note = health_note(db)
    if note:
        print(f"\n  {note}")
    print()


def cmd_today(db, cfg, args):
    today = date.today()
    rx, tx = iface_totals(db, today, today + timedelta(days=1))
    print()
    print(f"  {BOLD('today')}  {DIM(today.strftime('%a %d %b %Y'))}")
    print()
    print(f"  total       {GREEN(human(rx + tx))}")
    print(f"  downloaded  {human(rx)}")
    print(f"  uploaded    {human(tx)}")
    limit = limit_line(db, rx + tx, indent="  ")
    if limit:
        print(limit)
    print()
    rows = db.execute(
        "SELECT app, rx, tx FROM app_daily WHERE day = ? ORDER BY (rx + tx) DESC",
        (today.isoformat(),)).fetchall()
    print_apps(rows, limit=args.limit)
    print()


def cmd_apps(db, cfg, args):
    today = date.today()
    cycle_day = int(meta(db, "cycle_day", cfg["cycle_day"]))
    start, end = cycle_bounds(today, cycle_day)
    rows = app_totals(db, start, end)
    print()
    print(f"  {BOLD('applications')}  {DIM(start.strftime('%d %b') + ' → ' + today.strftime('%d %b'))}")
    print()
    print_apps(rows, limit=args.limit)
    print()


def cmd_days(db, cfg, args):
    today = date.today()
    cycle_day = int(meta(db, "cycle_day", cfg["cycle_day"]))
    start, end = cycle_bounds(today, cycle_day)
    rows = daily_rows(db, start, end)
    print()
    print(f"  {BOLD('daily usage')}  {DIM(start.strftime('%d %b') + ' → ' + end.strftime('%d %b %Y'))}")
    print()
    if not rows:
        print(f"  {DIM('no data yet')}\n")
        return
    peak = max(r[1] + r[2] for r in rows) or 1
    for day, rx, tx in rows:
        combined = rx + tx
        label = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b")
        print(f"  {label:<12} {human(combined):>10}  {bar(combined / peak)}  "
              f"{DIM('↓' + human(rx) + '  ↑' + human(tx))}")
    total = sum(r[1] + r[2] for r in rows)
    print()
    print(f"  {BOLD(f'{"total":<12}')} {GREEN(f'{human(total):>10}')}")
    print()


def cmd_cycles(db, cfg, args):
    today = date.today()
    cycle_day = int(meta(db, "cycle_day", cfg["cycle_day"]))
    start, _ = cycle_bounds(today, cycle_day)
    print()
    print(f"  {BOLD('billing cycles')}")
    print()
    entries = []
    for back in range(0, 13):
        s = shift_cycle(start, cycle_day, back)
        if s.month < 12:
            e = date(s.year, s.month + 1, cycle_day)
        else:
            e = date(s.year + 1, 1, cycle_day)
        rx, tx = iface_totals(db, s, e)
        if rx + tx == 0 and back > 0:
            continue
        entries.append((s, e, rx, tx))
    if not entries:
        print(f"  {DIM('no data yet')}\n")
        return
    peak = max(r[2] + r[3] for r in entries) or 1
    for s, e, rx, tx in entries:
        combined = rx + tx
        label = f"{s.strftime('%d %b')} → {e.strftime('%d %b %y')}"
        current = DIM("  (current)") if s == start else ""
        print(f"  {label:<20} {human(combined):>10}  {bar(combined / peak)}{current}")
    print()


def cmd_status(db, cfg, args):
    print()
    print(f"  {BOLD('netusage status')}")
    print()
    beat = meta(db, "heartbeat")
    if beat:
        age = time.time() - float(beat)
        state = GREEN("running") if age < 120 else RED(f"stalled ({int(age)}s)")
    else:
        state = YELLOW("no heartbeat yet")
    backend = meta(db, "backend", "unknown")
    backend_label = {
        "probe_filtered.bt": "eBPF (kprobes, physical NICs only)",
        "probe_for.bt": "eBPF (kprobes, map iteration)",
        "probe_print.bt": "eBPF (kprobes, print fallback)",
        "nethogs": "nethogs (pcap fallback)",
        "starting": "attaching...",
        "failed": "unavailable - interface totals unaffected",
        "off": "disabled",
    }.get(backend, backend)
    print(f"  collector      {state}")
    if backend in ("failed", "off"):
        backend_label = YELLOW(backend_label)
    print(f"  per-app        {backend_label}")
    since = meta(db, "app_filter_since")
    if since:
        print(f"  local filter   since {since} "
              f"{DIM('(earlier per-app days include local traffic)')}")
    print(f"  interfaces     {meta(db, 'interfaces', '?')}")
    print(f"  cycle day      {meta(db, 'cycle_day', cfg['cycle_day'])}")
    raw = meta(db, "daily_limit_bytes")
    limit = int(raw) if raw and raw.isdigit() else 0
    points = (meta(db, "notify_at") or "").replace(",", "/")
    note = f" (notifies at {points}%)" if points else ""
    print(f"  daily limit    {human(limit) + note if limit else 'not set'}")
    raw = meta(db, "cycle_limit_bytes")
    plan = int(raw) if raw and raw.isdigit() else 0
    if plan:
        cpoints = (meta(db, "cycle_notify_at") or "").replace(",", "/")
        print(f"  data plan      {human(plan)}" + (f" (notifies at {cpoints}%)" if cpoints else ""))
    raw = meta(db, "remind_minutes")
    if raw and raw.isdigit() and int(raw):
        print(f"  reminders      every {raw} min while over a limit")
    print(f"  database       {cfg['db']}")
    row = db.execute("SELECT MIN(day), MAX(day), COUNT(DISTINCT day) "
                     "FROM iface_daily").fetchone()
    if row and row[0]:
        print(f"  data range     {row[0]} → {row[1]}  ({row[2]} days)")
    print()


def cmd_live(db, cfg, args):
    """Poll the database and show the deltas as they land."""
    print(DIM("  watching - Ctrl-C to stop\n"), flush=True)
    prev = None
    try:
        while True:
            # SELECTs run in autocommit here, so each poll sees fresh writes.
            today = date.today()
            rx, tx = iface_totals(db, today, today + timedelta(days=1))
            if prev is not None:
                d_rx, d_tx = rx - prev[0], tx - prev[1]
                secs = max(args.interval, 1)
                line = (f"  {datetime.now().strftime('%H:%M:%S')}   "
                        f"↓ {human(d_rx / secs):>11}/s   "
                        f"↑ {human(d_tx / secs):>11}/s   "
                        f"{DIM('today ' + human(rx + tx))}")
                print(line, flush=True)
            prev = (rx, tx)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()


def cmd_networks(db, cfg, args):
    """This cycle per network: Wi-Fi by router, wired by gateway. Wi-Fi from
    before per-network tracking is placed where the phone was."""
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import netusage_data as nd
    today = date.today()
    cycle_day = int(meta(db, "cycle_day", cfg["cycle_day"]))
    start, end = cycle_bounds(today, cycle_day)
    print()
    print(f"  {BOLD('networks')}  {DIM(start.strftime('%d %b') + ' → ' + today.strftime('%d %b'))}")
    print()
    rows = nd.networks(db, nd.Period("cycle", start, end))
    if not rows:
        print(f"  {DIM('no data yet')}\n")
        return
    peak = max(n.total for n in rows) or 1
    for n in rows[:args.limit]:
        print(f"  {n.label[:24]:<24} {human(n.total):>10}  {bar(n.total / peak)}")
    print()


COMMANDS = {
    "report": cmd_report,
    "today": cmd_today,
    "apps": cmd_apps,
    "days": cmd_days,
    "cycles": cmd_cycles,
    "networks": cmd_networks,
    "status": cmd_status,
    "live": cmd_live,
}

HELP = """
  netusage - internet consumption for the current billing cycle

  netusage            cycle total, today, average, top applications
  netusage today      today only, broken down by application
  netusage apps       every application this cycle
  netusage days       per-day table for this cycle
  netusage cycles     previous billing cycles
  netusage networks   this cycle per Wi-Fi router / wired network
  netusage status     collector health and backend in use
  netusage live       live throughput, Ctrl-C to stop

  options
    -n N              limit application rows (default 12)
    -i N              poll seconds for `live` (default 3)
"""


def main():
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", nargs="?", default="report")
    parser.add_argument("-n", "--limit", type=int, default=12)
    parser.add_argument("-i", "--interval", type=int, default=3)
    parser.add_argument("-h", "--help", action="store_true")
    args = parser.parse_args()

    if args.help or args.command in ("help", "--help"):
        print(HELP)
        return 0
    if args.command not in COMMANDS:
        print(HELP)
        return 1

    cfg = load_config()
    db = connect(cfg["db"])
    COMMANDS[args.command](db, cfg, args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os._exit(0)
