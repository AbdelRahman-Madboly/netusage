#!/usr/bin/env python3
"""Build app/src/main/assets/import.json for the phone app's first run.

Inputs (all read from the phone over adb, nothing on it is changed):
  netstats   `adb shell dumpsys netstats --full`: Android's own hourly history,
             the only place past Wi-Fi names and SIMs are recorded (about 3 months).
  days       My Data Manager daily totals scraped from its History screen.
  apps       My Data Manager weekly per-app totals scraped from its Apps screen.

Days before Android's history starts come from My Data Manager; from then on
Android's exact byte counts are used.
"""
import argparse, collections, datetime, json, re, time

GIB = 1024 ** 3
UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": GIB, "TB": 1024 ** 4}


def parse_netstats(text):
    """Hourly (hour, net, key) -> [rx, tx] from the Xt (device level) section.

    Handles both layouts: Android 10+ ("Uid stats:", type=1/0, wifiNetworkKey=)
    and Android 9 ("UID stats:", type=WIFI/MOBILE, networkId=)."""
    sec = re.split(r"\n(?:Uid|UID) stats:", text.split("Xt stats:")[1])[0]
    rows = collections.defaultdict(lambda: [0, 0])
    cur = None
    for line in sec.splitlines():
        if "ident=[" in line:
            first = line.split("ident=[{", 1)[1].split("}", 1)[0]
            typ = re.search(r"type=(-?\w+)", first)[1]
            key = re.search(r'(?:wifiNetworkKey|networkId)="(.*?)"', first)
            sub = re.search(r"subscriberId=(\d+)", first)
            # Rows are split by "set" (ALL, DEFAULT, ...). Despite the name, ALL does not
            # include DEFAULT here: the sum of every set is what matches My Data
            # Manager day by day, so every set is counted.
            if typ in ("1", "WIFI"):
                cur = ("wifi", f"ssid:{key[1]}" if key else "")
            elif typ == "-1" and "transports={1}" in first:
                cur = ("wifi", "")
            elif typ in ("0", "MOBILE"):
                cur = ("mobile", sub[1] if sub else "")
            else:
                cur = None  # VPN and other overlays would count traffic twice
            continue
        m = re.search(r"st=(\d+) rb=(\d+) rp=\d+ tb=(\d+)", line)
        if m and cur:
            r = rows[(int(m[1]), *cur)]
            r[0] += int(m[2])
            r[1] += int(m[3])
    return rows


def amount(value, unit):
    return int(float(value) * UNITS[unit])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--netstats", required=True)
    ap.add_argument("--days", help="My Data Manager daily totals (optional)")
    ap.add_argument("--apps", help="My Data Manager weekly per-app totals (optional)")
    ap.add_argument("--sims", required=True, help='JSON {"<imsi prefix>": "sim:<Name>"}')
    ap.add_argument("--settings", default="{}")
    ap.add_argument("--dumped-at", type=int, default=int(time.time()))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    sims = json.loads(a.sims)

    def sim_key(subscriber):
        for prefix, key in sims.items():
            if subscriber.startswith(prefix):
                return key
        return ""

    hourly = parse_netstats(open(a.netstats).read())
    current_hour = a.dumped_at - a.dumped_at % 3600
    first = min(h for h, _, _ in hourly)
    # The first local day Android has in full; My Data Manager covers everything before it.
    cutoff_day = datetime.datetime.fromtimestamp(first).date() + datetime.timedelta(days=1)
    cutoff = int(datetime.datetime.combine(cutoff_day, datetime.time()).timestamp())

    usage = collections.defaultdict(lambda: [0, 0])
    last_seen = {}
    for (hour, net, key), (rx, tx) in hourly.items():
        if hour < cutoff or hour >= current_hour:
            continue  # the hour in progress is left for the app to read itself
        if net == "mobile":
            key = sim_key(key)
        u = usage[(hour, net, key)]
        u[0] += rx
        u[1] += tx
        if key and rx + tx:
            last_seen[key] = max(last_seen.get(key, 0), hour)

    rows = [[h, n, k, rx, tx, 1] for (h, n, k), (rx, tx) in sorted(usage.items()) if rx + tx]
    android_rows = len(rows)

    days = json.load(open(a.days)) if a.days else []
    mdm_days = 0
    for d in days:
        m = re.match(r"\w+, (\d+) (\w+) (\d{4})", d["label"])
        day = datetime.datetime.strptime(f"{m[1]} {m[2][:3]} {m[3]}", "%d %b %Y").date() if m else \
            datetime.date.fromisoformat(d["day"])
        if day >= cutoff_day:
            continue
        midnight = int(datetime.datetime.combine(day, datetime.time()).timestamp())
        (mv, mu), (wv, wu), _ = d["vals"]
        for net, total in (("mobile", amount(mv, mu)), ("wifi", amount(wv, wu))):
            if total:
                rows.append([midnight, net, "", total, 0, 0])
        mdm_days += 1

    # Weekly per-app totals, spread evenly over the week's days My Data Manager covered.
    first_mdm = min((datetime.date.fromisoformat(d["day"]) for d in days), default=cutoff_day)
    apps = collections.defaultdict(int)
    for w in (json.load(open(a.apps)) if a.apps else []):
        start = datetime.date.fromisoformat(w["week"])
        span = [start + datetime.timedelta(i) for i in range(7)]
        span = [d for d in span if first_mdm <= d < cutoff_day]  # Android covers the rest
        if not span:
            continue
        for name, amt in w["apps"].items():
            value, unit = amt.split()
            total = amount(value, unit)
            for d in span:
                apps[(d.isoformat(), w["net"], name)] += total // len(span)
    app_rows = [[d, n, name, v] for (d, n, name), v in sorted(apps.items()) if v]

    labels = {k: k.split(":", 1)[1] for (_, _, k) in usage if k}
    networks = [{"key": k, "net": "wifi" if k.startswith("ssid:") else "mobile", "label": v,
                 "last_seen": last_seen.get(k, 0)} for k, v in sorted(labels.items())]

    out = {
        "until": cutoff,
        "summary": f"{mdm_days} days from My Data Manager before {cutoff_day}, "
                   f"{android_rows} hourly rows from Android, {len(app_rows)} per-app days",
        "networks": networks,
        "usage": rows,
        "apps": app_rows,
        "settings": json.loads(a.settings),
    }
    json.dump(out, open(a.out, "w"), separators=(",", ":"))
    print(out["summary"])
    tot = collections.Counter()
    for h, n, k, rx, tx, _ in rows:
        tot[(n, k)] += rx + tx
    for k, v in tot.most_common():
        print(f"  {k}: {v / GIB:.2f} GiB")


if __name__ == "__main__":
    main()
