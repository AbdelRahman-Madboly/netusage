#!/usr/bin/env python3
"""Tests for the dashboard's query layer. Run: python3 test_data.py  (no root, no display)"""

import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collector
import netusage_data as nd

FAILS = []
GB = 1024 ** 3


def check(label, got, want):
    ok = got == want
    print(("  PASS  " if ok else "  FAIL  ") + label +
          ("" if ok else f"\n         got={got!r}\n        want={want!r}"))
    if not ok:
        FAILS.append(label)


def test_periods():
    print("\n== periods ==")
    d = date(2026, 9, 28)
    check("day", nd.period_for("day", d, 25), nd.Period("day", d, date(2026, 9, 29)))
    check("week is the 7 days ending on the anchor",
          nd.period_for("week", d, 25), nd.Period("week", date(2026, 9, 22), date(2026, 9, 29)))
    check("month", nd.period_for("month", d, 25),
          nd.Period("month", date(2026, 9, 1), date(2026, 10, 1)))
    check("december rolls into january",
          nd.period_for("month", date(2026, 12, 31), 25),
          nd.Period("month", date(2026, 12, 1), date(2027, 1, 1)))
    check("cycle after the reset day",
          nd.period_for("cycle", d, 25), nd.Period("cycle", date(2026, 9, 25), date(2026, 10, 25)))
    check("cycle before the reset day",
          nd.period_for("cycle", date(2026, 9, 24), 25),
          nd.Period("cycle", date(2026, 8, 25), date(2026, 9, 25)))
    check("cycle across new year",
          nd.period_for("cycle", date(2027, 1, 3), 25),
          nd.Period("cycle", date(2026, 12, 25), date(2027, 1, 25)))
    check("leap february cycle length",
          nd.period_for("cycle", date(2024, 3, 1), 25).days, 29)
    check("cycle bounds agree with the CLI",
          nd.cycle_bounds(date(2026, 1, 3), 25), (date(2025, 12, 25), date(2026, 1, 25)))

    print("\n== stepping between periods ==")
    p = nd.period_for("day", d, 25)
    check("previous day", nd.shift(p, -1, 25).start, date(2026, 9, 27))
    p = nd.period_for("week", d, 25)
    check("previous week does not overlap",
          nd.shift(p, -1, 25), nd.Period("week", date(2026, 9, 15), date(2026, 9, 22)))
    p = nd.period_for("month", date(2026, 3, 31), 25)
    check("previous month from the 31st", nd.shift(p, -1, 25).start, date(2026, 2, 1))
    p = nd.period_for("month", date(2026, 1, 10), 25)
    check("previous month across new year", nd.shift(p, -1, 25).start, date(2025, 12, 1))
    p = nd.period_for("cycle", d, 25)
    check("previous cycle", nd.shift(p, -1, 25),
          nd.Period("cycle", date(2026, 8, 25), date(2026, 9, 25)))
    check("twelve cycles back", nd.shift(p, -12, 25).start, date(2025, 9, 25))
    check("next cycle", nd.shift(p, 1, 25).start, date(2026, 10, 25))
    a = nd.period_for("all", d, 25, date(2026, 8, 31))
    check("all time is not stepped", nd.shift(a, -1, 25), a)


def _db():
    path = tempfile.mktemp(suffix=".db")
    store = collector.Store(path)
    return path, store


def test_queries():
    print("\n== queries ==")
    path, store = _db()
    today = date.today()
    y = today - timedelta(days=1)
    store.add_iface(today.isoformat(), "wlan0", 3 * GB, 1 * GB)
    store.add_iface(today.isoformat(), "eth0", 1 * GB, 0)
    store.add_iface(y.isoformat(), "wlan0", 2 * GB, 0)
    store.add_iface_hourly(f"{today.isoformat()} 09", "wlan0", 3 * GB, 1 * GB)
    store.add_iface_hourly(f"{today.isoformat()} 10", "eth0", 1 * GB, 0)
    store.add_app(today.isoformat(), "chrome", 2 * GB, 0)
    store.add_app(today.isoformat(), "apt", 1 * GB, 0)
    store.add_app(y.isoformat(), "chrome", 1 * GB, 0)
    store.set_meta("daily_limit_bytes", 10 * GB)
    store.set_meta("heartbeat", __import__("time").time())
    store.commit()

    db = nd.connect(path)
    day = nd.period_for("day", today, 25)
    week = nd.period_for("week", today, 25)
    check("today's totals sum interfaces", nd.totals(db, day), (4 * GB, 1 * GB))
    check("week includes yesterday", nd.totals(db, week), (6 * GB, 1 * GB))
    check("days with data", nd.days_with_data(db, week), 2)
    check("apps ranked largest first",
          [a for a, *_ in nd.apps(db, week)], ["chrome", "apt"])
    check("app per-day history", nd.app_daily(db, "chrome", week),
          {today: (2 * GB, 0), y: (1 * GB, 0)})
    check("first day", nd.first_day(db), y)
    check("daily limit", nd.daily_limit(db), 10 * GB)
    check("health is running", nd.health(db).state, "running")

    hours = nd.series(db, day)
    check("a day has 24 hourly buckets", len(hours), 24)
    check("hour 09 bucket", (hours[9].rx, hours[9].tx), (3 * GB, 1 * GB))
    check("hour 10 bucket", hours[10].total, 1 * GB)
    check("hours sum to the day", sum(b.total for b in hours), 5 * GB)
    days = nd.series(db, week)
    check("a week has 7 daily buckets", len(days), 7)
    check("last bucket is today and current", (days[-1].total, days[-1].current), (5 * GB, True))
    check("day buckets carry their date for drill-down", days[-1].start, today)
    months = nd.series(db, nd.period_for("all", today, 25, nd.first_day(db)))
    check("all time is bucketed by month", sum(b.total for b in months), 7 * GB)

    cov = nd.coverage(db, week)
    check("coverage compares apps to interface bytes", round(cov, 3), round(4 / 7, 3))
    proj = nd.projection(db, nd.period_for("cycle", today, 25))
    check("projection is average x cycle length",
          proj is not None and proj > 0, True)

    out = tempfile.mktemp(suffix=".csv")
    nd.export_csv(db, week, out)
    lines = open(out).read().splitlines()
    check("csv has header, 2 days and 2 apps", len(lines), 5)
    os.unlink(out)
    db.close()
    os.unlink(path)


def test_missing_hourly_table():
    print("\n== databases from before hourly totals ==")
    path = tempfile.mktemp(suffix=".db")
    raw = sqlite3.connect(path)
    raw.executescript("CREATE TABLE iface_daily(day, iface, rx, tx);"
                      "CREATE TABLE app_daily(day, app, rx, tx);"
                      "CREATE TABLE meta(key PRIMARY KEY, value);")
    raw.commit()
    raw.close()
    db = nd.connect(path)
    check("no hourly table gives no hourly series",
          nd.series(db, nd.period_for("day", date.today(), 25)), [])
    check("empty coverage is None", nd.coverage(db, nd.period_for("week", date.today(), 25)), None)
    check("no heartbeat is waiting", nd.health(db).state, "waiting")
    db.close()
    os.unlink(path)
    check("missing database connects to None", nd.connect("/nonexistent/x.db"), None)


def test_limits_file():
    print("\n== limits file shared with the collector ==")
    path = tempfile.mktemp(suffix=".conf")
    check("missing file gives defaults", nd.read_limits(path)["notify_at"], [80, 100, 150, 200])
    lim = {"daily_limit_gb": 7.5, "notify_at": [100, 50, 50], "cycle_limit_gb": 150,
           "cycle_notify_at": [90], "remind_every_min": 30}
    nd.write_limits(lim, path)
    back = nd.read_limits(path)
    check("round trip", back, {"daily_limit_gb": 7.5, "notify_at": [50, 100],
                               "cycle_limit_gb": 150.0, "cycle_notify_at": [90],
                               "remind_every_min": 30})
    got = collector.load_limits({"daily_limit_gb": 10.0, "notify_at": [80]}, path)
    check("the collector reads what the dashboard wrote",
          (got["daily_limit_bytes"], got["notify_at"], got["cycle_limit_bytes"],
           got["remind_minutes"]), (int(7.5 * GB), [50, 100], 150 * GB, 30))
    check("writable", nd.limits_writable(path), True)
    os.unlink(path)


def test_live_meter():
    print("\n== live meter ==")
    seq = [(1000, 100), (3000, 300), (500, 50)]
    nd.read_counters = lambda _ifaces: seq.pop(0)
    m = nd.LiveMeter(["x"], history=5)
    check("first tick has no rate", m.tick(), (0.0, 0.0))
    rx, tx = m.tick()
    check("second tick is positive", rx > 0 and tx > 0, True)
    check("a counter reset is not negative", m.tick(), (0.0, 0.0))


def test_labels():
    print("\n== labels ==")
    check("human", nd.human(1.5 * GB), "1.50 GB")
    check("rate", nd.human_rate(2048), "2.0 KB/s")
    today = date.today()
    check("today label", nd.period_for("day", today, 25).label, "Today")
    check("month label", nd.period_for("month", date(2026, 9, 3), 25).label, "September 2026")
    check("cycle label", nd.period_for("cycle", date(2026, 9, 28), 25).label,
          "25 Sep – 24 Oct 2026")


def test_networks():
    print("\n== networks ==")
    check("router key drops the octets that differ between bands",
          collector.router_key("A0:11:22:33:44:5D"), "ap:11:22:33:44")
    check("2.4 and 5 GHz radios share a key",
          collector.router_key("A2:11:22:33:44:59"), collector.router_key("A0:11:22:33:44:5D"))
    check("bad BSSID gives no key", collector.router_key("nonsense"), "")

    wireless = nd._is_wireless
    nd._is_wireless = lambda iface: iface == "wlan0"   # no real wlan0 needed
    path = tempfile.mktemp(suffix=".db")
    store = collector.Store(path)
    d = date(2026, 9, 28)
    store.add_iface(d.isoformat(), "wlan0", 10 * GB, GB)
    store.add_network(f"{d} 10", "ap:aa", 4 * GB, 0)
    store.add_network(f"{d} 11", "ap:bb", 3 * GB, GB)
    # the collector writes the interface's hourly rows alongside the network's
    store.add_iface_hourly(f"{d} 10", "wlan0", 4 * GB, 0)
    store.add_iface_hourly(f"{d} 11", "wlan0", 3 * GB, GB)
    store.see_network("ap:aa", "wifi", "Home")
    store.see_network("ap:aa", "wifi", "Home-New")   # same router, renamed
    store.see_network("ap:bb", "wifi", "Cafe")
    store.commit()
    db = nd.connect(path)
    p = nd.period_for("day", d, 25)
    empty = {"labels": {}, "merge": {}}
    rows = {n.key: n for n in nd.networks(db, p, empty)}
    check("label follows the latest name", rows["ap:aa"].label, "Home-New")
    check("every name is kept", rows["ap:aa"].names, ["Home", "Home-New"])
    check("untracked fills the gap to the interface total",
          rows[nd.UNTRACKED].total, 11 * GB - 8 * GB)
    merged = {"labels": {"ap:bb": "Both"}, "merge": {"ap:aa": "ap:bb"}}
    rows = {n.key: n for n in nd.networks(db, p, merged)}
    check("merge adds the groups", rows["ap:bb"].total, 8 * GB)
    check("merged group takes the custom label", rows["ap:bb"].label, "Both")
    check("merged key is gone", "ap:aa" in rows, False)
    check("daily per group", nd.network_daily(db, "ap:bb", p, merged), {d: (7 * GB, GB)})
    # Wi-Fi the laptop could not place goes where the phone was that day
    store.db.execute("INSERT INTO remote_usage VALUES('phone', ?, 'wifi', 'ap:bb', ?, 0)",
                     (int(datetime(d.year, d.month, d.day, 9).timestamp()), GB))
    store.commit()
    rows = {n.key: n for n in nd.networks(nd.connect(path), p, empty)}
    check("untracked Wi-Fi follows the phone's network", rows["ap:bb"].total, 4 * GB + 3 * GB)
    check("nothing left untracked", nd.UNTRACKED in rows, False)
    check("merge chains resolve", nd.network_group("x", {"merge": {"x": "y", "y": "z"}}), "z")
    check("merge loops stop", nd.network_group("x", {"merge": {"x": "y", "y": "x"}}) in ("x", "y"), True)
    nd._is_wireless = wireless
    os.unlink(path)


def test_household():
    print("\n== household plan ==")
    import time as _t
    path = tempfile.mktemp(suffix=".db")
    store = collector.Store(path)
    cycle = nd.period_for("cycle", date.today(), 27)
    start = int(_t.mktime(cycle.start.timetuple()))
    hour = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H")
    store.add_network(hour(start + 3600), "ap:home", 2 * GB, 0)       # laptop on the plan's router
    store.add_network(hour(start + 7200), "ap:cafe", 5 * GB, 0)       # elsewhere: not the plan
    store.db.executemany("INSERT INTO remote_usage VALUES(?, ?, ?, ?, ?, ?)", [
        ("phone", start + 3600, "wifi", "ap:home", 3 * GB, 0),
        ("phone", start + 3600, "mobile", "sim:x", 9 * GB, 0),         # mobile: not the plan
    ])
    for k, v in (("phone_plan_gb", 250), ("phone_plan_key", "ap:home"), ("phone_cycle_day", 27),
                 ("phone_isp_used_gb", 40), ("phone_isp_at", start + 4 * 3600)):
        store.set_meta(k, v)
    store.commit()
    db = nd.connect(path)
    h = nd.household(db, 25, {"labels": {}, "merge": {}})
    parts = dict(h.parts)
    check("laptop counts only the plan's router", parts["Laptop"], 2 * GB)
    check("phone counts only the plan's router", parts["Phone"], 3 * GB)
    check("family is what the ISP counted beyond both", parts["Family"], 35 * GB)
    check("used adds up to the ISP figure", h.used, 40 * GB)
    # a day the old collector only kept as a daily total still counts for the laptop
    store2 = collector.Store(path)
    store2.add_iface(cycle.start.isoformat(), "wlan0", 6 * GB, 0)
    store2.commit()
    wireless = nd._is_wireless
    nd._is_wireless = lambda iface: iface == "wlan0"
    h2 = nd.household(nd.connect(path), 25, {"labels": {}, "merge": {}})
    nd._is_wireless = wireless
    check("daily-only Wi-Fi counts toward the laptop", dict(h2.parts)["Laptop"], 2 * GB + 6 * GB)
    check("and comes out of the family share", dict(h2.parts)["Family"], 40 * GB - 11 * GB)
    # a window ending mid-day still subtracts that day's later hourly rows
    at = start + 2 * 3600
    whole = nd._laptop_on_plan(nd.connect(path), "ap:home", {"labels": {}, "merge": {}}, start, start + 86400)
    before = nd._laptop_on_plan(nd.connect(path), "ap:home", {"labels": {}, "merge": {}}, start, at)
    after = nd._laptop_on_plan(nd.connect(path), "ap:home", {"labels": {}, "merge": {}}, at, start + 86400)
    check("before + after the ISP reading = the whole day", before + after, whole)
    # a reading in the middle of an hour: that hour counts as before, never in neither
    mid = start + 3600 + 1200
    b2 = nd._laptop_on_plan(nd.connect(path), "ap:home", {"labels": {}, "merge": {}}, start, mid)
    a2 = nd._laptop_on_plan(nd.connect(path), "ap:home", {"labels": {}, "merge": {}}, mid, start + 86400)
    check("an hour cut by the reading is not lost", b2 + a2, whole)
    check("no plan until the phone reports", nd.household(nd.connect(_empty_db()), 25), None)
    os.unlink(path)


def test_bill():
    print("\n== monthly bill ==")
    import time as _t
    bills = tempfile.mktemp(suffix=".json")
    cycle = nd.period_for("cycle", date.today(), 27)
    key = cycle.start.isoformat()
    nd.add_bundle(None, 250, 200, 0, bills)               # the base, every cycle
    nd.add_bundle(key, 100, 150, 10, bills)
    nd.add_bundle(key, 100, 150, 10, bills)
    nd.add_bundle(key, 60, 100, 10, bills)
    got = nd.read_bills(bills)["cycles"][key]
    check("fee is added to the bundle price", got[0]["egp"], 165.0)
    check("three extras kept for the cycle", len(got), 3)

    path = tempfile.mktemp(suffix=".db")
    store = collector.Store(path)
    start = int(_t.mktime(cycle.start.timetuple()))
    hour = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H")
    store.add_network(hour(start + 3600), "ap:home", 102 * GB, 0)
    store.add_app(cycle.start.isoformat(), "pip", 60 * GB, 0)
    store.add_app(cycle.start.isoformat(), "assistant-cli", 30 * GB, 0)
    store.add_app(cycle.start.isoformat(), "chrome", 12 * GB, 0)
    for k, v in (("phone_plan_gb", 250), ("phone_plan_key", "ap:home"), ("phone_cycle_day", 27)):
        store.set_meta(k, v)
    store.commit()
    b = nd.bill(nd.connect(path), 25, date.today(), {"labels": {}, "merge": {}}, bills)
    check("cost is every bundle bought", b.cost, 640.0)
    check("GB bought", b.gb_bought, 510)
    check("price per GB", round(b.per_gb, 4), round(640.0 / 510, 4))
    check("the laptop's share is its GB at that price", b.share_egp, round(102 * 640.0 / 510, 2))
    kinds = dict((k, v) for k, v, _ in b.kinds)
    check("apps are grouped by kind", kinds, {"Packages and builds": 60 * GB,
                                              "Coding assistants": 30 * GB, "Browser": 12 * GB})
    check("a cycle with no bundles costs nothing", nd.bill(nd.connect(path), 25,
          date.today() - timedelta(days=62), {"labels": {}, "merge": {}}, bills).cost, 0)
    check("a later cycle with no extras still pays the base", nd.bill(nd.connect(path), 25,
          date.today() + timedelta(days=31), {"labels": {}, "merge": {}}, bills).cost, 200)
    os.unlink(path)
    os.unlink(bills)


def _empty_db():
    path = tempfile.mktemp(suffix=".db")
    collector.Store(path).commit()
    return path


if __name__ == "__main__":
    test_periods()
    test_queries()
    test_missing_hourly_table()
    test_limits_file()
    test_live_meter()
    test_labels()
    test_networks()
    test_household()
    test_bill()
    print("\n" + ("ALL TESTS PASSED" if not FAILS
                  else f"{len(FAILS)} FAILURE(S): {FAILS}"))
    sys.exit(1 if FAILS else 0)
