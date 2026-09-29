#!/usr/bin/env bash
# Register a family phone with the laptop and build its copy of the app.
#   tools/add_family_phone.sh Dad [history.json]
# history.json (optional) is that phone's own past usage per network, made with
# tools/build_import.py from its `adb shell dumpsys netstats --full`.
# The phone gets its own token: it can send its own usage and read the home
# bundle's totals, nothing else. Install the APK printed at the end on that
# phone with USB debugging on (adb install -r --user 0 <apk>). No sudo needed:
# the list is kept in ~/.config/netusage/devices.json and copied to the laptop.
set -euo pipefail

NAME="${1:?usage: add_family_phone.sh <name> [history.json]}"
HISTORY="${2:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The list lives in my config; the laptop's copy is updated when it is writable,
# otherwise the next sudo ./install.sh copies it over.
DEVICES="$HOME/.config/netusage/devices.json"
LAPTOP_DEVICES=/var/lib/netusage/devices.json
mkdir -p "$(dirname "$DEVICES")"
[ -s "$DEVICES" ] || { [ -r "$LAPTOP_DEVICES" ] && cp "$LAPTOP_DEVICES" "$DEVICES" || echo '{"members": {}}' > "$DEVICES"; }
chmod 600 "$DEVICES"

ID="$(echo "$NAME" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')"
TOKEN="$(/usr/bin/python3 - "$DEVICES" "$ID" "$NAME" <<'PY'
import json, secrets, sys
path, mid, name = sys.argv[1:]
data = json.load(open(path))
members = data.setdefault("members", {})
m = members.get(mid) or {"name": name, "token": secrets.token_urlsafe(24)}
m["name"] = name
members[mid] = m
with open(path, "w") as f:          # in place: the directory belongs to root
    json.dump(data, f, indent=2)
print(m["token"])
PY
)"
if [ -w "$LAPTOP_DEVICES" ]; then
    cat "$DEVICES" > "$LAPTOP_DEVICES"
else
    echo "note: run sudo ./install.sh so the laptop accepts $NAME's phone" >&2
fi

HOST="$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
CYCLE_DAY="$(sed -n 's/^[[:space:]]*cycle_day[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p' /etc/netusage.conf | tail -1)"
ASSETS="$(mktemp -d)"
trap 'rm -rf "$ASSETS"' EXIT
/usr/bin/python3 - "$ASSETS/config.json" "$ID" "$NAME" "$TOKEN" "$HOST" "${CYCLE_DAY:-1}" <<'PY'
import json, sys
path, mid, name, token, host, day = sys.argv[1:]
json.dump({"version": 1, "role": "member", "member_id": mid, "device_name": name,
           "laptop_token": token, "laptop_host": host, "cycle_day": int(day)}, open(path, "w"), indent=2)
PY
chmod 600 "$ASSETS/config.json"
[ -n "$HISTORY" ] && cp "$HISTORY" "$ASSETS/import.json"

cd "$HERE"
./gradlew -q assembleRelease -Pnetusage.assets="$ASSETS"
OUT="$HERE/build/family"
mkdir -p "$OUT"
cp app/build/outputs/apk/release/app-release.apk "$OUT/netusage-$ID.apk"
# rebuild my own copy so the next plain build is not a family one
./gradlew -q assembleRelease
echo "$OUT/netusage-$ID.apk"
