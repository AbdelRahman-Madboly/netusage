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
install -m 0644 "$SRC/probe_for.bt" "$DEST/probe_for.bt"
install -m 0644 "$SRC/probe_print.bt" "$DEST/probe_print.bt"
[ -f "$SRC/README.md" ] && install -m 0644 "$SRC/README.md" "$DEST/README.md"

install -d -m 0755 /var/lib/netusage

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
if [ -f /var/lib/netusage/netusage.db ]; then
    "$PYTHON" - <<'PYFIX'
import sqlite3
db = sqlite3.connect("/var/lib/netusage/netusage.db", timeout=15)
db.execute("PRAGMA busy_timeout = 10000")
bad = [r[0] for r in db.execute("""
    SELECT a.day FROM (SELECT day, SUM(rx+tx) t FROM app_daily GROUP BY day) a
    JOIN (SELECT day, SUM(rx+tx) t FROM iface_daily GROUP BY day) i
      ON a.day = i.day WHERE a.t > i.t * 2 """)]
for day in bad:
    db.execute("DELETE FROM app_daily WHERE day = ?", (day,))
db.commit()
if bad:
    print(f"==> repaired per-app data for: {', '.join(bad)}")
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
