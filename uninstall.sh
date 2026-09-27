#!/usr/bin/env bash
# Remove netusage. Keeps the database unless --purge is given.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || exec sudo -- "$0" "$@"

systemctl disable --now netusage.service 2>/dev/null || true
rm -f /etc/systemd/system/netusage.service /usr/local/bin/netusage
rm -rf /opt/netusage
systemctl daemon-reload

if [ "${1:-}" = "--purge" ]; then
    rm -rf /var/lib/netusage /etc/netusage.conf
    echo "netusage removed, database and config deleted."
else
    echo "netusage removed. Data kept in /var/lib/netusage (use --purge to delete)."
fi
