#!/usr/bin/env bash
# Install (or update) the netusage collector and CLI.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/netusage
CONF=/etc/netusage.conf
UNIT=/etc/systemd/system/netusage.service
BIN=/usr/local/bin/netusage

if [ "$(id -u)" -ne 0 ]; then
    echo "This installer needs root (systemd unit + eBPF probes)." >&2
    echo "Re-running with sudo..." >&2
    exec sudo -- "$0" "$@"
fi

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }

# --- dependencies ----------------------------------------------------------
missing=()
command -v bpftrace >/dev/null 2>&1 || missing+=(bpftrace)
command -v vnstat   >/dev/null 2>&1 || missing+=(vnstat)
command -v nethogs  >/dev/null 2>&1 || missing+=(nethogs)

if [ ${#missing[@]} -gt 0 ]; then
    say "installing: ${missing[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
else
    say "dependencies already present"
fi

# --- interpreter -----------------------------------------------------------
# Deliberately the system interpreter, not whatever is on the caller's PATH:
# the daemon starts at boot, before any user environment exists.
PYTHON=/usr/bin/python3
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3 || true)"
    [ -n "$PYTHON" ] || { echo "no python3 found" >&2; exit 1; }
fi
say "interpreter: $PYTHON ($("$PYTHON" --version 2>&1))"

# --- files -----------------------------------------------------------------
say "installing to $DEST"
install -d -m 0755 "$DEST"
install -m 0755 "$SRC/collector.py" "$DEST/collector.py"
install -m 0755 "$SRC/cli.py"       "$DEST/cli.py"
install -m 0644 "$SRC/netusage_data.py" "$DEST/netusage_data.py"
for probe in "$SRC"/probe_*.bt; do
    install -m 0644 "$probe" "$DEST/$(basename "$probe")"
done
[ -f "$SRC/README.md" ] && install -m 0644 "$SRC/README.md" "$DEST/README.md"

install -d -m 0755 /var/lib/netusage

# Limits the dashboard can change without root. Owned by the desktop user;
# the collector only ever reads it and re-reads it when it changes.
LIMITS=/var/lib/netusage/limits.conf
OWNER="${SUDO_USER:-$(logname 2>/dev/null || true)}"
if [ ! -f "$LIMITS" ]; then
    say "writing $LIMITS"
    limit=$(sed -n 's/^[[:space:]]*daily_limit_gb[[:space:]]*=[[:space:]]*\([0-9.]*\).*/\1/p' "$SRC/netusage.conf" "$CONF" 2>/dev/null | tail -1)
    notify=$(sed -n 's/^[[:space:]]*notify_at[[:space:]]*=[[:space:]]*\([0-9, ]*\).*/\1/p' "$SRC/netusage.conf" "$CONF" 2>/dev/null | tail -1)
    plan=0
    if [ -n "$OWNER" ]; then
        home=$(getent passwd "$OWNER" | cut -d: -f6)
        dash="$home/.config/netusage/dashboard.json"
        [ -f "$dash" ] && plan=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("plan_gb", 0) or 0)' "$dash" 2>/dev/null || echo 0)
    fi
    cat > "$LIMITS" <<LIMEOF
# Notification limits. Edited by the Net Usage dashboard (Preferences);
# the collector picks up changes within one interval, no restart needed.
[limits]
daily_limit_gb = ${limit:-10}
notify_at = ${notify:-80,100,150,200}
cycle_limit_gb = ${plan}
cycle_notify_at = 75,90,100
remind_every_min = 0
LIMEOF
fi
if [ -n "$OWNER" ] && [ "$OWNER" != root ]; then
    chown "$OWNER" "$LIMITS"
fi
chmod 0644 "$LIMITS"

# Network names and merges set from the dashboard's Networks page, same ownership.
NETWORKS=/var/lib/netusage/networks.json
if [ ! -f "$NETWORKS" ]; then
    say "writing $NETWORKS"
    printf '{\n  "labels": {},\n  "merge": {}\n}\n' > "$NETWORKS"
fi
if [ -n "$OWNER" ] && [ "$OWNER" != root ]; then
    chown "$OWNER" "$NETWORKS"
fi
chmod 0644 "$NETWORKS"

# Family phones allowed to report, each with its own token. Owned by the desktop
# user so a phone can be added without sudo; the collector re-reads it per request.
DEVICES=/var/lib/netusage/devices.json
USER_DEVICES=""
if [ -n "$OWNER" ] && [ "$OWNER" != root ]; then
    USER_DEVICES="$(getent passwd "$OWNER" | cut -d: -f6)/.config/netusage/devices.json"
fi
# android/tools/add_family_phone.sh keeps the list in the user's config too
if [ -n "$USER_DEVICES" ] && [ -s "$USER_DEVICES" ]; then
    install -m 0600 "$USER_DEVICES" "$DEVICES"
elif [ ! -f "$DEVICES" ]; then
    say "writing $DEVICES"
    printf '{\n  "members": {}\n}\n' > "$DEVICES"
fi
chmod 0600 "$DEVICES"
[ -n "$OWNER" ] && [ "$OWNER" != root ] && chown "$OWNER" "$DEVICES"

# Token the phone app uses to read usage over the home network. Kept in the
# user's config so rebuilding the phone app can pick it up; the collector reads
# its own copy here.
TOKEN=/var/lib/netusage/share.token
USER_TOKEN=""
if [ -n "$OWNER" ] && [ "$OWNER" != root ]; then
    USER_TOKEN="$(getent passwd "$OWNER" | cut -d: -f6)/.config/netusage/share.token"
fi
if [ -n "$USER_TOKEN" ] && [ -s "$USER_TOKEN" ]; then
    install -m 0600 "$USER_TOKEN" "$TOKEN"
elif [ ! -s "$TOKEN" ]; then
    say "creating $TOKEN"
    head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$TOKEN"
    chmod 0600 "$TOKEN"
fi
[ -n "$OWNER" ] && [ "$OWNER" != root ] && chown "$OWNER" "$TOKEN"

# Let phones on the local network reach the share port, and nothing else.
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    for net in 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12; do
        ufw allow from "$net" to any port 8765 proto tcp comment "netusage phone app" >/dev/null
    done
    say "firewall: port 8765 open to the local network only"
fi

if [ -f "$CONF" ]; then
    say "keeping existing $CONF"
    # Add settings introduced after this config was first written, without
    # touching anything the user may have changed.
    if ! grep -q '^[[:space:]]*daily_limit_gb' "$CONF"; then
        say "adding daily limit settings to $CONF"
        cat >> "$CONF" <<'ADDEOF'

# Desktop notification when today's usage crosses these percentages of the
# daily limit. Set daily_limit_gb = 0 to turn notifications off entirely.
daily_limit_gb = 10
notify_at = 80,100,150,200
ADDEOF
    fi
else
    say "writing $CONF"
    install -m 0644 "$SRC/netusage.conf" "$CONF"
fi

# --- service ---------------------------------------------------------------
say "installing systemd unit"
sed "s|__PYTHON__|$PYTHON|" "$SRC/netusage.service" > "$UNIT"
chmod 0644 "$UNIT"

ln -sf "$DEST/cli.py" "$BIN"
say "CLI linked at $BIN"

# Per-app rows written before the receive probes were width-corrected can hold
# impossible values (exact multiples of 2^32). Drop any day whose per-app total
# exceeds twice what the interface actually carried; payload can never do that.
#
# This does not undo the loopback problem: before probe_filtered.bt, per-app
# rows also counted loopback, docker bridge and tailscale traffic. Those bytes
# cannot be separated out after the fact, so days under the 2x line are kept
# as they are. The collector records the first day the filtered probe ran in
# meta `app_filter_since`, and anything earlier should be read with that in
# mind.
if [ -f /var/lib/netusage/netusage.db ]; then
    "$PYTHON" - <<'PYFIX'
import sqlite3
db = sqlite3.connect("/var/lib/netusage/netusage.db", timeout=15)
db.execute("PRAGMA busy_timeout = 10000")
# The width bug was fixed on 31 Aug 2026, so only days up to then can hold
# its values. Later days can also exceed 2x (local traffic, see above), and
# deleting those would throw away real per-app history on every reinstall.
bad = [r[0] for r in db.execute("""
    SELECT a.day FROM (SELECT day, SUM(rx+tx) t FROM app_daily GROUP BY day) a
    JOIN (SELECT day, SUM(rx+tx) t FROM iface_daily GROUP BY day) i
      ON a.day = i.day WHERE a.t > i.t * 2 AND a.day <= '2026-08-31' """)]
for day in bad:
    db.execute("DELETE FROM app_daily WHERE day = ?", (day,))
if bad:
    print(f"==> repaired per-app data for: {', '.join(bad)}")

# Older collectors recorded two names the classifier now resolves: the
# versioned assistant-cli binary (~/.local/share/assistant/versions/2.1.260) under
# its version number, and Chrome threads under their thread name once the
# process had exited. Fold those rows into the names used from now on.
import re
renames = {}
for (app,) in db.execute("SELECT DISTINCT app FROM app_daily"):
    if re.fullmatch(r"\d+(\.\d+)+", app):
        renames[app] = "assistant-cli"
    elif app.startswith(("Chrome_", "chrome_")):
        renames[app] = "chrome"
for old, new in renames.items():
    db.execute("""
        INSERT INTO app_daily(day, app, rx, tx)
        SELECT day, ?, rx, tx FROM app_daily WHERE app = ?
        ON CONFLICT(day, app) DO UPDATE SET rx = rx + excluded.rx, tx = tx + excluded.tx
    """, (new, old))
    db.execute("DELETE FROM app_daily WHERE app = ?", (old,))
if renames:
    print(f"==> merged {len(renames)} legacy app name(s): {', '.join(sorted(renames))}")
db.commit()
PYFIX
fi

systemctl daemon-reload
systemctl enable netusage.service >/dev/null 2>&1
systemctl restart netusage.service

# vnstat is an independent cross-check on the interface totals
systemctl enable vnstat >/dev/null 2>&1 || true
systemctl start  vnstat >/dev/null 2>&1 || true

sleep 3
if systemctl is-active --quiet netusage; then
    say "netusage is running and enabled at boot"
else
    echo "netusage failed to start. Logs:" >&2
    journalctl -u netusage -n 30 --no-pager >&2
    exit 1
fi

echo
"$BIN" status || true
echo "  Run 'netusage' any time for the cycle report."
echo
