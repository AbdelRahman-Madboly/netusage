#!/usr/bin/env bash
# Install (or update) the Internet Usage dashboard for the current user.
# No root needed: everything goes under ~/.local and ~/Desktop.
# The collector itself is installed separately with: sudo ./install.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID=com.madboly.NetUsage
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA/netusage-dashboard"
ICON_DIR="$DATA/icons/hicolor/scalable/apps"
APPS_DIR="$DATA/applications"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
PYTHON=/usr/bin/python3

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not with sudo." >&2
    exit 1
fi

if [ "${1:-}" = "--remove" ]; then
    rm -rf "$DEST"
    rm -f "$ICON_DIR/$APP_ID.svg" "$APPS_DIR/$APP_ID.desktop" "$DESKTOP_DIR/Internet Usage.desktop"
    update-desktop-database -q "$APPS_DIR" 2>/dev/null || true
    say "dashboard removed (settings kept in ~/.config/netusage)"
    exit 0
fi

# The dashboard uses the distribution's GTK 4 and libadwaita bindings, which
# only the system interpreter has.
if ! "$PYTHON" -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' 2>/dev/null; then
    echo "GTK 4 / libadwaita Python bindings are missing. Install them with:" >&2
    echo "  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" >&2
    exit 1
fi

say "installing to $DEST"
install -d "$DEST/icons"
install -m 0755 "$SRC/gui/netusage_dashboard.py" "$DEST/netusage_dashboard.py"
install -m 0644 "$SRC/gui/charts.py"             "$DEST/charts.py"
install -m 0644 "$SRC/netusage_data.py"          "$DEST/netusage_data.py"
install -m 0644 "$SRC/gui/icons/$APP_ID.svg"     "$DEST/icons/$APP_ID.svg"

say "installing icon"
install -d "$ICON_DIR"
install -m 0644 "$SRC/gui/icons/$APP_ID.svg" "$ICON_DIR/$APP_ID.svg"
gtk-update-icon-cache -q -t -f "$DATA/icons/hicolor" 2>/dev/null || true

desktop_entry() {
    cat <<EOF
[Desktop Entry]
Type=Application
Name=Internet Usage
GenericName=Data Usage Monitor
Comment=Internet usage by day, week, month and billing cycle, and by application
Exec=$PYTHON "$DEST/netusage_dashboard.py"
Icon=$APP_ID
Terminal=false
Categories=Network;Monitor;GTK;
Keywords=network;data;bandwidth;usage;internet;traffic;
StartupNotify=true
StartupWMClass=$APP_ID
EOF
}

say "adding to the applications menu"
install -d "$APPS_DIR"
desktop_entry > "$APPS_DIR/$APP_ID.desktop"
chmod 0644 "$APPS_DIR/$APP_ID.desktop"
update-desktop-database -q "$APPS_DIR" 2>/dev/null || true

if [ -d "$DESKTOP_DIR" ]; then
    say "adding launcher to $DESKTOP_DIR"
    launcher="$DESKTOP_DIR/Internet Usage.desktop"
    desktop_entry > "$launcher"
    chmod 0755 "$launcher"
    # GNOME's desktop icons only launch entries marked as trusted
    gio set "$launcher" metadata::trusted true 2>/dev/null || true
fi

if [ ! -r /var/lib/netusage/netusage.db ]; then
    echo
    echo "  The collector has no data yet. Install it with:  sudo ./install.sh"
fi
echo
say "done. Open \"Internet Usage\" from the desktop or the applications menu."
