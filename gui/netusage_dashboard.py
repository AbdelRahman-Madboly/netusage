#!/usr/bin/python3
"""Internet Usage - desktop dashboard for the netusage collector.

Reads the collector's SQLite database read-only and never needs root. Live
throughput comes straight from the kernel interface counters in /sys.
"""

import json
import os
import sqlite3
import sys
import traceback
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
for path in (HERE, os.path.dirname(HERE)):
    if os.path.exists(os.path.join(path, "netusage_data.py")) and path not in sys.path:
        sys.path.insert(0, path)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

import netusage_data as nd  # noqa: E402
from charts import (DOWN, UP, BarChart, CumulativeChart, Donut, Heatmap,  # noqa: E402
                    Ring, ShareBar, Sparkline, StackedBar, app_color, rgb_hex, OTHER)

# Same colours as the phone app: laptop mint, phone teal, family purple.
HH_LAPTOP = (0.24, 0.84, 0.63)
HH_PHONE = (0.08, 0.59, 0.70)
HH_FAMILY = (0.49, 0.36, 0.84)
HH_MEMBERS = [(0.36, 0.55, 0.96), (0.90, 0.42, 0.66), (0.95, 0.66, 0.23), (0.55, 0.75, 0.30)]

APP_ID = "com.madboly.NetUsage"
VERSION = "2.0"
SETTINGS_PATH = os.path.join(GLib.get_user_config_dir(), "netusage", "dashboard.json")
REFRESH_SECONDS = 10
TOP_APPS = 8

KIND_TITLES = {"day": "Day", "week": "Week", "month": "Month",
               "cycle": "Cycle", "all": "All time"}
CHART_TITLES = {"day": "Usage by hour", "week": "Usage by day", "month": "Usage by day",
                "cycle": "Usage by day", "all": "Usage by month"}

CSS = """
.hero { font-size: 30pt; font-weight: 800; letter-spacing: -0.5px; }
.stat-value { font-size: 17pt; font-weight: 700; }
.card-pad { padding: 18px; }
.card-title { font-weight: 700; font-size: 10.5pt; opacity: 0.75; }
.down { color: %(down)s; font-weight: 600; }
.up { color: %(up)s; font-weight: 600; }
.legend-dot { font-size: 14pt; }
.pill { border-radius: 999px; padding: 1px 8px; font-size: 8.5pt; font-weight: 700;
        background: alpha(@accent_bg_color, 0.18); color: @accent_color; }
.pill.warn { background: alpha(@warning_bg_color, 0.2); color: @warning_color; }
.pill.bad { background: alpha(@error_bg_color, 0.2); color: @error_color; }
.pill.good { background: alpha(@success_bg_color, 0.2); color: @success_color; }
.period-title { font-size: 20pt; font-weight: 800; }
.app-total { font-weight: 700; }
.flat-list row { padding: 4px 0; }
""" % {"down": rgb_hex(DOWN), "up": rgb_hex(UP)}


# --------------------------------------------------------------------------
# settings kept by the dashboard itself (not the collector's config)
# --------------------------------------------------------------------------

def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("kind", "cycle")
    data.setdefault("plan_gb", 0)
    return data


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------
# small widget helpers
# --------------------------------------------------------------------------

def label(text="", css=(), xalign=0.0, wrap=False, **kw):
    w = Gtk.Label(label=text, xalign=xalign, **kw)
    for c in css:
        w.add_css_class(c)
    if wrap:
        # wrapped text should follow the card's width, not decide it
        w.set_wrap(True)
        w.set_wrap_mode(2)          # word, then character
        w.set_max_width_chars(22)
        w.set_width_chars(10)
    return w


def card(title=None, spacing=10):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    box.add_css_class("card")
    box.add_css_class("card-pad")
    if title:
        box.append(label(title, ("card-title",)))
    return box


def pill(text, tone=""):
    w = label(text, ("pill",) + ((tone,) if tone else ()))
    w.set_valign(Gtk.Align.CENTER)
    return w


def legend():
    box = Gtk.Box(spacing=12, halign=Gtk.Align.END)
    for text, color in (("Download", DOWN), ("Upload", UP)):
        item = Gtk.Box(spacing=4)
        dot = Gtk.Label()
        dot.set_markup(f"<span foreground='{rgb_hex(color)}'>●</span>")
        item.append(dot)
        item.append(label(text, ("caption", "dim-label")))
        box.append(item)
    return box


def clear(box):
    child = box.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def scrolled(child, max_width=1120):
    clamp = Adw.Clamp(maximum_size=max_width, tightening_threshold=720)
    clamp.set_child(child)
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    sw.set_child(clamp)
    return sw


def page_box():
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                   margin_top=18, margin_bottom=24, margin_start=16, margin_end=16)


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Internet Usage",
                         default_width=1180, default_height=860)
        self.set_size_request(360, 480)
        self.set_icon_name(APP_ID)
        self.settings = load_settings()
        self.cfg = nd.load_config()
        self.db = None
        self.kind = self.settings["kind"] if self.settings["kind"] in nd.KINDS else "cycle"
        self.period = nd.period_for(self.kind, date.today(), self.cfg["cycle_day"])
        self.search_text = ""
        self.meter = None
        self._toggles = {}
        self._syncing = False
        self._last_day = date.today()

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        self.stack = Adw.ViewStack()
        self.toolbar = Adw.ToolbarView()
        self.toasts.set_child(self.toolbar)

        header = Adw.HeaderBar()
        self.switcher = Adw.ViewSwitcher(stack=self.stack,
                                         policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self.switcher)
        header.pack_end(self._menu_button())
        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text="Refresh (Ctrl+R)")
        refresh.connect("clicked", lambda *_: self.refresh(announce=True))
        header.pack_end(refresh)
        self.toolbar.add_top_bar(header)

        self.banner = Adw.Banner()
        self.banner.set_button_label("How to fix")
        self.banner.connect("button-clicked", lambda *_: self.stack.set_visible_child_name("status"))
        self.toolbar.add_top_bar(self.banner)

        self.period_bar = self._build_period_bar()
        self.period_revealer = Gtk.Revealer(child=self.period_bar, reveal_child=True,
                                            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.toolbar.add_top_bar(self.period_revealer)

        self.main_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.main_stack.add_named(self.stack, "content")
        self.empty = Adw.StatusPage(
            icon_name=APP_ID, title="No usage data yet",
            description=("The netusage collector has not written its database. "
                         "Install or start it with:\n\n<tt>sudo ./install.sh</tt>\n\n"
                         "in the netusage folder, then wait about ten seconds."))
        self.empty.set_child(Gtk.Button(label="Try again", halign=Gtk.Align.CENTER,
                                        css_classes=["pill", "suggested-action"]))
        self.empty.get_child().connect("clicked", lambda *_: self.refresh(announce=True))
        self.main_stack.add_named(self.empty, "empty")
        self.toolbar.set_content(self.main_stack)

        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.stack)
        self.toolbar.add_bottom_bar(self.switcher_bar)

        self._build_overview()
        self._build_apps()
        self._build_networks()
        self._build_history()
        self._build_status()
        self.stack.connect("notify::visible-child-name", self._on_page)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        narrow.add_setter(self.switcher_bar, "reveal", True)
        narrow.add_setter(header, "show-title", False)
        narrow.add_setter(self.apps_split, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(self.kind_label_box, "visible", False)
        self.add_breakpoint(narrow)

        self._install_actions()
        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._periodic)
        self._live_tick()
        GLib.timeout_add_seconds(1, self._live_tick)

    # ----------------------------------------------------------------- chrome

    def _menu_button(self):
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Export period as CSV…", "win.export")
        section.append("Copy summary", "win.copy-summary")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Preferences", "app.preferences")
        section.append("Keyboard Shortcuts", "win.show-help-overlay")
        section.append("About Internet Usage", "app.about")
        menu.append_section(None, section)
        return Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                              tooltip_text="Main menu", primary=True)

    def _build_period_bar(self):
        outer = Gtk.CenterBox(margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        nav = Gtk.Box(spacing=6)
        self.prev_btn = Gtk.Button(icon_name="go-previous-symbolic",
                                   tooltip_text="Previous period (Alt+Left)")
        self.prev_btn.add_css_class("flat")
        self.prev_btn.connect("clicked", lambda *_: self.step(-1))
        self.next_btn = Gtk.Button(icon_name="go-next-symbolic",
                                   tooltip_text="Next period (Alt+Right)")
        self.next_btn.add_css_class("flat")
        self.next_btn.connect("clicked", lambda *_: self.step(1))
        self.now_btn = Gtk.Button(label="Now", tooltip_text="Jump to the current period (Home)")
        self.now_btn.add_css_class("flat")
        self.now_btn.connect("clicked", lambda *_: self.set_kind(self.kind, date.today()))
        nav.append(self.prev_btn)
        nav.append(self.next_btn)
        nav.append(self.now_btn)

        toggles = Gtk.Box(css_classes=["linked"], halign=Gtk.Align.CENTER)
        group = None
        for i, kind in enumerate(nd.KINDS, 1):
            btn = Gtk.ToggleButton(label=KIND_TITLES[kind],
                                   tooltip_text=f"{KIND_TITLES[kind]} ({i})")
            if group:
                btn.set_group(group)
            group = group or btn
            btn.connect("toggled", self._on_kind_toggled, kind)
            self._toggles[kind] = btn
            toggles.append(btn)
        self._toggles[self.kind].set_active(True)

        self.kind_label_box = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        self.period_label = label("", ("heading",), xalign=1.0)
        self.kind_label_box.append(self.period_label)

        outer.set_start_widget(nav)
        outer.set_center_widget(toggles)
        outer.set_end_widget(self.kind_label_box)
        return outer

    def _install_actions(self):
        def add(name, cb, accels=()):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_: cb())
            self.add_action(action)
            if accels:
                self.get_application().set_accels_for_action(f"win.{name}", list(accels))

        add("refresh", lambda: self.refresh(announce=True), ["<Control>r", "F5"])
        add("export", self.export_csv, ["<Control>e"])
        add("copy-summary", self.copy_summary, ["<Control><Shift>c"])
        add("prev", lambda: self.step(-1), ["<Alt>Left"])
        add("next", lambda: self.step(1), ["<Alt>Right"])
        add("now", lambda: self.set_kind(self.kind, date.today()), ["Home"])
        add("search", self._focus_search, ["<Control>f"])
        for i, kind in enumerate(nd.KINDS, 1):
            add(f"kind-{kind}", lambda k=kind: self.set_kind(k), [f"<Alt>{i}"])
        for i, name in enumerate(("overview", "apps", "networks", "history", "status"), 1):
            add(f"page-{name}", lambda n=name: self.stack.set_visible_child_name(n),
                [f"<Control>{i}"])
        add("show-help-overlay", self._show_shortcuts, ["<Control>question"])

    def _show_shortcuts(self):
        sections = [
            ("Periods", [("<Alt>1", "Day"), ("<Alt>2", "Week"), ("<Alt>3", "Month"),
                         ("<Alt>4", "Billing cycle"), ("<Alt>5", "All time"),
                         ("<Alt>Left", "Previous period"), ("<Alt>Right", "Next period"),
                         ("Home", "Current period")]),
            ("Pages", [("<Control>1", "Overview"), ("<Control>2", "Apps"),
                       ("<Control>3", "Networks"), ("<Control>4", "History"),
                       ("<Control>5", "Status"),
                       ("<Control>f", "Search apps")]),
            ("General", [("<Control>r", "Refresh"), ("<Control>e", "Export CSV"),
                         ("<Control><Shift>c", "Copy summary"),
                         ("<Control>comma", "Preferences"), ("<Control>q", "Quit")]),
        ]
        win = Gtk.ShortcutsWindow(transient_for=self, modal=True)
        sec = Gtk.ShortcutsSection(section_name="main", visible=True)
        for title, items in sections:
            grp = Gtk.ShortcutsGroup(title=title, visible=True)
            for accel, text in items:
                grp.add_shortcut(Gtk.ShortcutsShortcut(accelerator=accel, title=text))
            sec.add_group(grp)
        win.add_section(sec)
        win.present()

    def _on_page(self, *_):
        name = self.stack.get_visible_child_name()
        self.period_revealer.set_reveal_child(name in ("overview", "apps"))

    def _on_kind_toggled(self, btn, kind):
        if btn.get_active() and not self._syncing and kind != self.kind:
            self.set_kind(kind)

    # ---------------------------------------------------------------- periods

    def set_kind(self, kind, anchor=None):
        if anchor is None:
            # keep looking at the same stretch of time when switching zoom
            anchor = min(self.period.end - timedelta(days=1), date.today())
            if self.period.kind == "all":
                anchor = date.today()
        self.kind = kind
        first = nd.first_day(self.db) if self.db else None
        self.period = nd.period_for(kind, anchor, self.cfg["cycle_day"], first)
        self.settings["kind"] = kind
        save_settings(self.settings)
        self._syncing = True
        self._toggles[kind].set_active(True)
        self._syncing = False
        self.refresh()

    def step(self, steps):
        if self.kind == "all":
            return
        nxt = nd.shift(self.period, steps, self.cfg["cycle_day"])
        if nxt.start > date.today():
            return
        self.period = nxt
        self.refresh()

    def show_day(self, day):
        self.set_kind("day", day)
        self.stack.set_visible_child_name("overview")

    # ---------------------------------------------------------------- refresh

    def _periodic(self):
        # roll the view forward at midnight if the user was watching "now"
        today = date.today()
        if today != self._last_day:
            if self.period.contains(self._last_day):
                self.period = nd.period_for(self.kind, today, self.cfg["cycle_day"],
                                            nd.first_day(self.db) if self.db else None)
            self._last_day = today
        self.refresh()
        return GLib.SOURCE_CONTINUE

    def _ensure_db(self):
        if self.db is None:
            try:
                self.db = nd.connect(self.cfg["db"])
            except Exception:
                self.db = None
        return self.db

    def refresh(self, announce=False):
        db = self._ensure_db()
        if db is None:
            self.main_stack.set_visible_child_name("empty")
            self.period_revealer.set_reveal_child(False)
            return
        try:
            if self.kind == "all":
                self.period = nd.period_for("all", date.today(), self.cfg["cycle_day"],
                                            nd.first_day(db))
            self.main_stack.set_visible_child_name("content")
            self._on_page()
            self._refresh_period_bar()
            self._refresh_overview(db)
            self._refresh_household(db)
            self._refresh_apps(db)
            self._refresh_networks(db)
            self._refresh_history(db)
            self._refresh_status(db)
        except sqlite3.OperationalError as exc:
            # "database is locked" while the collector commits: try again next tick
            self._drop_db()
            if announce:
                self.toasts.add_toast(Adw.Toast(title=f"Could not read data: {exc}"))
            return
        except Exception as exc:
            # anything else will not fix itself, so say so instead of going quiet
            traceback.print_exc()
            self._drop_db()
            self.banner.set_title(f"Could not read the usage data: {exc}")
            self.banner.set_revealed(True)
            return
        if announce:
            self.toasts.add_toast(Adw.Toast(title="Updated", timeout=1))

    def _drop_db(self):
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
        self.db = None

    def plan_bytes(self, db):
        """Data plan for the cycle: the limits file, else the old dashboard setting."""
        if os.path.exists(nd.LIMITS_PATH):
            return int(nd.read_limits()["cycle_limit_gb"] * 1024 ** 3)
        return int(float(self.settings.get("plan_gb") or 0) * 1024 ** 3)

    def _refresh_period_bar(self):
        p = self.period
        self.period_label.set_label(p.label)
        today = date.today()
        self.next_btn.set_sensitive(p.kind != "all" and p.end <= today)
        self.prev_btn.set_sensitive(p.kind != "all")
        self.now_btn.set_sensitive(not nd.is_current(p))

    # --------------------------------------------------------------- overview

    def _build_overview(self):
        box = page_box()

        head = Gtk.Box(spacing=12)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.ov_title = label("", ("period-title",), wrap=True)
        self.ov_subtitle = label("", ("dim-label",), wrap=True)
        titles.append(self.ov_title)
        titles.append(self.ov_subtitle)
        head.append(titles)
        box.append(head)

        box.append(self._build_household())

        cards = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                            min_children_per_line=1, max_children_per_line=4,
                            column_spacing=14, row_spacing=14)
        box.append(cards)

        # total for the period
        c = card("TOTAL")
        self.ov_total = label("", ("hero", "numeric"))
        c.append(self.ov_total)
        split = Gtk.Box(spacing=14)
        self.ov_down = label("", ("down", "numeric"))
        self.ov_up = label("", ("up", "numeric"))
        split.append(self.ov_down)
        split.append(self.ov_up)
        c.append(split)
        self.ov_total_note = label("", ("dim-label", "caption"), wrap=True)
        self.ov_total_note.set_vexpand(True)
        self.ov_total_note.set_valign(Gtk.Align.END)
        c.append(self.ov_total_note)
        cards.append(c)

        # today against the daily limit
        c = card("TODAY")
        self.ov_ring = Ring(size=128)
        c.append(self.ov_ring)
        self.ov_today_note = label("", ("dim-label", "caption"), xalign=0.5, wrap=True)
        c.append(self.ov_today_note)
        cards.append(c)

        # billing cycle budget
        c = card("BILLING CYCLE")
        row = Gtk.Box(spacing=8)
        self.ov_cycle = label("", ("stat-value", "numeric"), hexpand=True)
        self.ov_cycle_pill = pill("")
        row.append(self.ov_cycle)
        row.append(self.ov_cycle_pill)
        c.append(row)
        self.ov_cycle_bar = Gtk.LevelBar(min_value=0, max_value=1)
        self.ov_cycle_bar.add_offset_value("low", 0.8)
        self.ov_cycle_bar.add_offset_value("high", 1.0)
        c.append(self.ov_cycle_bar)
        self.ov_cycle_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        c.append(self.ov_cycle_rows)
        self.ov_plan_btn = Gtk.Button(label="Set monthly plan…", halign=Gtk.Align.START,
                                      valign=Gtk.Align.END, vexpand=True,
                                      tooltip_text="Add your data allowance to see a budget, "
                                                   "a safe-per-day figure and a pace line")
        self.ov_plan_btn.add_css_class("flat")
        self.ov_plan_btn.set_action_name("app.preferences")
        c.append(self.ov_plan_btn)
        cards.append(c)

        # live throughput
        c = card("RIGHT NOW")
        rates = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.live_down = label("↓ –", ("stat-value", "numeric", "down"))
        self.live_up = label("↑ –", ("stat-value", "numeric", "up"))
        rates.append(self.live_down)
        rates.append(self.live_up)
        c.append(rates)
        self.spark = Sparkline(height=64)
        self.spark.set_vexpand(True)
        self.spark.set_valign(Gtk.Align.END)
        c.append(self.spark)
        c.append(label("last 60 seconds, physical interfaces", ("dim-label", "caption")))
        cards.append(c)

        # chart
        c = card(None, spacing=6)
        head = Gtk.Box(spacing=8)
        self.chart_title = label("", ("title-4",), hexpand=True)
        head.append(self.chart_title)
        self.chart_mode = Gtk.Box(css_classes=["linked"])
        self.mode_bars = Gtk.ToggleButton(label="Bars", active=True)
        self.mode_cum = Gtk.ToggleButton(label="Cumulative", group=self.mode_bars)
        self.mode_bars.connect("toggled", lambda *_: self._sync_chart_mode())
        self.chart_mode.append(self.mode_bars)
        self.chart_mode.append(self.mode_cum)
        for b in (self.mode_bars, self.mode_cum):
            b.add_css_class("small")
        head.append(self.chart_mode)
        c.append(head)
        c.append(legend())
        self.chart_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.chart = BarChart(height=240)
        self.chart.on_activate = self._on_bucket
        self.cum_chart = CumulativeChart(height=240)
        self.chart_stack.add_named(self.chart, "bars")
        self.chart_stack.add_named(self.cum_chart, "cum")
        self.chart_empty = Adw.StatusPage(icon_name="document-open-recent-symbolic",
                                          title="No hourly data yet",
                                          description="Hourly totals start once the updated "
                                                      "collector is installed.")
        self.chart_empty.add_css_class("compact")
        self.chart_stack.add_named(self.chart_empty, "empty")
        c.append(self.chart_stack)
        self.chart_hint = label("", ("dim-label", "caption"))
        c.append(self.chart_hint)
        box.append(c)

        # top applications
        c = card(None, spacing=12)
        head = Gtk.Box()
        head.append(label("Top applications", ("title-4",), hexpand=True))
        more = Gtk.Button(label="All apps", css_classes=["flat"])
        more.connect("clicked", lambda *_: self.stack.set_visible_child_name("apps"))
        head.append(more)
        c.append(head)
        self.apps_split = Gtk.Box(spacing=24, orientation=Gtk.Orientation.HORIZONTAL)
        self.donut = Donut(size=200)
        self.donut.set_valign(Gtk.Align.START)
        self.donut.set_margin_top(8)
        self.apps_split.append(self.donut)
        self.top_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, hexpand=True)
        self.top_list.add_css_class("flat-list")
        self.top_list.connect("row-activated", self._on_app_row)
        self.apps_split.append(self.top_list)
        c.append(self.apps_split)
        self.apps_note = label("", ("dim-label", "caption"), wrap=True)
        c.append(self.apps_note)
        box.append(c)

        self.stack.add_titled_with_icon(scrolled(box), "overview", "Overview",
                                        "utilities-system-monitor-symbolic")

    def _build_household(self):
        """The home plan split into this laptop, the phone and the family. Filled in
        from what the phone app reports; hidden until it has."""
        c = card("HOME PLAN")
        top = Gtk.Box(spacing=12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.hh_left = label("", ("hero", "numeric"))
        self.hh_used = label("", ("dim-label",))
        left.append(self.hh_left)
        left.append(self.hh_used)
        top.append(left)
        self.hh_pill = pill("")
        top.append(self.hh_pill)
        c.append(top)
        self.hh_bar = StackedBar()
        c.append(self.hh_bar)
        self.hh_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        c.append(self.hh_rows)
        self.hh_note = label("", ("dim-label", "caption"), wrap=True)
        c.append(self.hh_note)
        self.hh_card = c
        c.set_visible(False)
        return c

    def _refresh_household(self, db):
        h = nd.household(db, self.cfg["cycle_day"])
        self.hh_card.set_visible(h is not None)
        if h is None:
            return
        colors = {"Laptop": HH_LAPTOP, "Phone": HH_PHONE, "Family": HH_FAMILY}
        left = h.remaining
        self.hh_left.set_label(f"{nd.human(left, 1)} left" if left >= 0 else f"{nd.human(-left, 1)} over")
        today = date.today()
        days_left = (h.cycle.end - today).days
        elapsed = (today - h.cycle.start).days + 1
        projected = h.used * (h.cycle.end - h.cycle.start).days / max(elapsed, 1)
        self.hh_used.set_label(
            f"{nd.human(h.used, 1)} used of {nd.human(h.plan, 0)} · {days_left} days left · "
            f"{nd.human(max(left, 0) / max(days_left, 1), 1)} a day is safe · "
            f"renews {h.cycle.end.strftime('%d %b')}")
        if h.used > h.plan:
            self.hh_pill.set_label("over plan")
            self.hh_pill.set_css_classes(["pill", "bad"])
        elif projected > h.plan:
            self.hh_pill.set_label("fast pace")
            self.hh_pill.set_css_classes(["pill", "warn"])
        else:
            self.hh_pill.set_label("on track")
            self.hh_pill.set_css_classes(["pill", "good"])
        member_colors = {n: HH_MEMBERS[i % len(HH_MEMBERS)] for i, (n, _) in enumerate(
            p for p in h.parts if p[0] not in colors)}
        self.hh_bar.set_data([(v, colors.get(n) or member_colors[n]) for n, v in h.parts], h.plan)
        clear(self.hh_rows)
        # My usage is this laptop and my phone together; then each family phone by
        # name; whatever the ISP counted beyond all of them is everyone else.
        parts = dict(h.parts)
        phones = [(n, v) for n, v in h.parts if n not in ("Laptop", "Phone", "Family")]
        mine = parts["Laptop"] + parts["Phone"]
        self._hh_row("My usage", mine, h.used, None, bold=True)
        self._hh_row("Laptop", parts["Laptop"], h.used, colors["Laptop"], indent=True)
        self._hh_row("My phone", parts["Phone"], h.used, colors["Phone"], indent=True)
        for i, (name, v) in enumerate(phones):
            self._hh_row(name, v, h.used, HH_MEMBERS[i % len(HH_MEMBERS)], bold=True)
        if h.reported_at is not None:
            self._hh_row("Others", parts["Family"], h.used,
                         colors["Family"], bold=True)
        bits = []
        if h.reported_at:
            bits.append("From your ISP on " + datetime.fromtimestamp(h.reported_at).strftime("%d %b %H:%M")
                        + "; family is what it counted beyond your laptop and phone.")
        else:
            bits.append("Enter the ISP's remaining quota in the phone app to include the family.")
        if h.phone_report_at:
            bits.append("Phone last reported " + datetime.fromtimestamp(h.phone_report_at).strftime("%d %b %H:%M") + ".")
        self.hh_note.set_label(" ".join(bits))

    def _hh_row(self, name, value, used, color, bold=False, indent=False):
        row = Gtk.Box(spacing=8, margin_start=22 if indent else 0)
        if color:
            dot = Gtk.Label()
            dot.set_markup(f"<span foreground='{rgb_hex(color)}'>●</span>")
            row.append(dot)
        row.append(label(name, ("heading",) if bold else ("dim-label",) if indent else (), hexpand=True))
        row.append(label(nd.human(value, 1), ("app-total", "numeric")))
        row.append(label(f"{value / max(used, 1) * 100:.0f}%", ("dim-label", "numeric"),
                         width_chars=5, xalign=1.0))
        self.hh_rows.append(row)

    def _sync_chart_mode(self):
        if self.chart_stack.get_visible_child_name() == "empty":
            return
        self.chart_stack.set_visible_child_name("bars" if self.mode_bars.get_active() else "cum")

    def _on_bucket(self, bucket):
        if bucket.start is None:
            return
        if self.kind in ("week", "month", "cycle"):
            if bucket.start <= date.today():
                self.show_day(bucket.start)
        elif self.kind == "all":
            self.set_kind("month", bucket.start)

    def _refresh_overview(self, db):
        p = self.period
        today = date.today()
        cycle_day = self.cfg["cycle_day"]
        rx, tx = nd.totals(db, p)
        total = rx + tx
        covered = nd.days_with_data(db, p)

        self.ov_title.set_label(p.label if p.kind != "cycle" else f"Billing cycle · {p.label}")
        if p.kind == "day":
            sub = today.strftime("so far today") if p.start == today else p.start.strftime("%A")
        elif p.kind == "all":
            first = nd.first_day(db)
            sub = f"since {first.strftime('%d %b %Y')} · {covered} days recorded" if first else ""
        else:
            elapsed = min((today - p.start).days + 1, p.days)
            left = max((p.end - today).days - 1, 0) if p.contains(today) else 0
            sub = f"day {elapsed} of {p.days}" if p.contains(today) else f"{p.days} days"
            if left:
                sub += f" · {left} days left"
            sub += f" · {covered} with data"
        self.ov_subtitle.set_label(sub)

        self.ov_total.set_label(nd.human(total))
        self.ov_down.set_label(f"↓ {nd.human(rx)}")
        self.ov_up.set_label(f"↑ {nd.human(tx)}")
        if p.kind == "day":
            per_day = None
            note = ""
            if p.start == today:
                week = nd.period_for("week", today - timedelta(days=1), cycle_day)
                wrx, wtx = nd.totals(db, week)
                wd = nd.days_with_data(db, week)
                if wd:
                    avg = (wrx + wtx) / wd
                    diff = total - avg
                    note = (f"{nd.human(abs(diff))} {'above' if diff > 0 else 'below'} "
                            f"your 7-day average of {nd.human(avg)}")
            self.ov_total_note.set_label(note)
        else:
            per_day = total / covered if covered else 0
            peak = max(nd.daily(db, p).items(), key=lambda kv: sum(kv[1]), default=None)
            note = f"average {nd.human(per_day)} per day"
            if peak:
                note += f"\nbusiest {peak[0].strftime('%a %d %b')} · {nd.human(sum(peak[1]))}"
            self.ov_total_note.set_label(note)

        # today ring
        t_rx, t_tx = nd.totals(db, nd.period_for("day", today, cycle_day))
        used = t_rx + t_tx
        limit = nd.daily_limit(db)
        if limit:
            frac = used / limit
            self.ov_ring.set_value(frac, f"{frac * 100:.0f}%", nd.human(used, 1))
            left = limit - used
            self.ov_today_note.set_label(
                f"{nd.human(left)} left of {nd.human(limit, 0)} daily limit" if left > 0 else
                f"{nd.human(-left)} over the {nd.human(limit, 0)} daily limit")
        else:
            self.ov_ring.set_value(0, nd.human(used, 1), "today")
            self.ov_today_note.set_label("No daily limit set")

        # billing cycle
        cyc = nd.period_for("cycle", today, cycle_day)
        c_rx, c_tx = nd.totals(db, cyc)
        c_used = c_rx + c_tx
        proj = nd.projection(db, cyc)
        days_left = max((cyc.end - today).days, 1)      # includes today
        plan = self.plan_bytes(db)
        self.ov_cycle.set_label(nd.human(c_used))
        clear(self.ov_cycle_rows)

        def stat(name, value):
            r = Gtk.Box(spacing=6)
            r.append(label(name, ("dim-label",), hexpand=True))
            r.append(label(value, ("numeric",), xalign=1.0))
            self.ov_cycle_rows.append(r)

        if plan:
            frac = c_used / plan
            self.ov_cycle_bar.set_visible(True)
            self.ov_cycle_bar.set_value(min(frac, 1.0))
            left = plan - c_used
            stat("plan", nd.human(plan, 0))
            stat("remaining", nd.human(max(left, 0)))
            stat("safe per day", nd.human(max(left, 0) / days_left))
            pace = c_used / plan / max((today - cyc.start).days + 1, 1) * cyc.days
            tone, text = (("bad", "over plan") if frac >= 1 else
                          ("warn", "fast pace") if pace > 1.0 else ("good", "on track"))
            self.ov_cycle_pill.set_label(text)
            for t in ("bad", "warn", "good"):
                self.ov_cycle_pill.remove_css_class(t)
            self.ov_cycle_pill.add_css_class(tone)
            self.ov_cycle_pill.set_visible(True)
        else:
            self.ov_cycle_bar.set_visible(False)
            self.ov_cycle_pill.set_visible(False)
        self.ov_plan_btn.set_visible(not plan)
        stat("resets", (cyc.end).strftime("%d %b") + f" · {days_left - 1} days")
        if proj is not None:
            stat("projected", nd.human(proj))

        # chart
        buckets = nd.series(db, p)
        self.chart_title.set_label(CHART_TITLES[p.kind])
        per_bucket_limit = limit if p.kind in ("week", "month", "cycle") else 0
        cum_ok = p.kind in ("week", "month", "cycle")
        self.chart_mode.set_visible(cum_ok)
        if not buckets:
            self.chart_stack.set_visible_child_name("empty")
        else:
            self.chart.set_data(buckets, per_bucket_limit)
            budget = plan if p.kind == "cycle" else 0
            self.cum_chart.set_data(buckets, budget)
            if not cum_ok:
                self.mode_bars.set_active(True)
            self.chart_stack.set_visible_child_name(
                "cum" if cum_ok and self.mode_cum.get_active() else "bars")
        self.chart_hint.set_label(
            "Click a bar to open that day" if p.kind in ("week", "month", "cycle") else
            "Click a bar to open that month" if p.kind == "all" else "")
        self.chart_hint.set_visible(bool(self.chart_hint.get_label()))

        # apps
        rows = nd.apps(db, p)
        self._fill_top_apps(db, rows, total)

    def _fill_top_apps(self, db, rows, iface_total):
        clear(self.top_list)
        app_total = sum(r[1] + r[2] for r in rows)
        shown = rows[:TOP_APPS]
        rest = sum(r[1] + r[2] for r in rows[TOP_APPS:])
        values = [r[1] + r[2] for r in shown] + ([rest] if rest else [])
        names = [r[0] for r in shown] + (["Other"] if rest else [])
        self.donut.set_data(values, names, nd.human(app_total, 1),
                            f"{len(rows)} apps")
        if not rows:
            self.top_list.append(Gtk.ListBoxRow(
                child=label("No per-app data for this period", ("dim-label",),
                            margin_top=12, margin_bottom=12), activatable=False))
        peak = max(values) if values else 1
        for i, (name, value) in enumerate(zip(names, values)):
            rx = shown[i][1] if i < len(shown) else None
            tx = shown[i][2] if i < len(shown) else None
            self.top_list.append(self._app_row(
                name, value, app_total, peak,
                OTHER if name == "Other" and i == len(names) - 1 else app_color(i),
                rx, tx, activatable=(name != "Other" or i < len(shown))))
        cov = nd.coverage(db, self.period)
        filt = nd.meta(db, "app_filter_since")
        note = "Per-app figures are socket payload bytes; the interface total is the one to trust."
        if cov is not None and cov > 1.05:
            note = (f"Per-app bytes are {cov * 100:.0f}% of the interface total for this period: "
                    "they include traffic that stayed on this machine (localhost, Docker).")
            if filt:
                note += f" Local traffic is filtered out from {filt} onward."
        elif cov is not None:
            note = (f"Apps account for {cov * 100:.0f}% of interface bytes; the rest is "
                    "protocol overhead and retransmits.")
        self.apps_note.set_label(note)

    def _app_row(self, name, value, total, peak, color, rx=None, tx=None,
                 activatable=True, badge=None):
        row = Adw.ActionRow(title=GLib.markup_escape_text(name), activatable=activatable)
        row.app_name = name if activatable else None
        if rx is not None:
            row.set_subtitle(f"↓ {nd.human(rx)}   ↑ {nd.human(tx)}")
        dot = Gtk.Label(valign=Gtk.Align.CENTER)
        dot.set_markup(f"<span foreground='{rgb_hex(color)}' size='x-large'>●</span>")
        row.add_prefix(dot)
        if badge:
            row.add_suffix(pill(badge))
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                        valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        top.append(label(f"{value / total * 100:.1f}%" if total else "", ("dim-label", "caption",
                                                                          "numeric")))
        top.append(label(nd.human(value), ("app-total", "numeric"), xalign=1.0))
        right.append(top)
        bar = ShareBar(width=150)
        bar.set_value(value / peak if peak else 0, color)
        right.append(bar)
        row.add_suffix(right)
        return row

    def _on_app_row(self, _box, row):
        name = getattr(row, "app_name", None)
        if name and self.db:
            AppDetail(self, name).present(self)

    # ------------------------------------------------------------------- apps

    def _build_apps(self):
        box = page_box()
        head = Gtk.Box(spacing=12)
        self.apps_title = label("", ("title-2",), hexpand=True, wrap=True)
        head.append(self.apps_title)
        self.sort = Gtk.DropDown.new_from_strings(["Total", "Download", "Upload", "Name"])
        self.sort.set_tooltip_text("Sort by")
        self.sort.connect("notify::selected", lambda *_: self._refresh_apps(self.db)
                          if self.db else None)
        head.append(self.sort)
        box.append(head)
        self.search = Gtk.SearchEntry(placeholder_text="Filter applications")
        self.search.connect("search-changed", self._on_search)
        box.append(self.search)
        self.apps_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.apps_list.add_css_class("boxed-list")
        self.apps_list.connect("row-activated", self._on_app_row)
        box.append(self.apps_list)
        self.apps_footer = label("", ("dim-label", "caption"), wrap=True)
        box.append(self.apps_footer)
        self.stack.add_titled_with_icon(scrolled(box, 900), "apps", "Apps",
                                        "view-app-grid-symbolic")

    def _focus_search(self):
        self.stack.set_visible_child_name("apps")
        self.search.grab_focus()

    def _on_search(self, entry):
        self.search_text = entry.get_text().strip().lower()
        if self.db:
            self._refresh_apps(self.db)

    def _refresh_apps(self, db):
        p = self.period
        rows = nd.apps(db, p)
        total = sum(r[1] + r[2] for r in rows)
        self.apps_title.set_label(f"{len(rows)} applications · {p.label}")
        key = self.sort.get_selected()
        if key == 1:
            rows = sorted(rows, key=lambda r: -r[1])
        elif key == 2:
            rows = sorted(rows, key=lambda r: -r[2])
        elif key == 3:
            rows = sorted(rows, key=lambda r: r[0].lower())
        colors = {r[0]: app_color(i) for i, r in enumerate(nd.apps(db, p)[:TOP_APPS])}
        new = set()
        if p.kind != "all":
            new = {a for (a,) in db.execute(
                "SELECT app FROM app_daily GROUP BY app HAVING MIN(day) >= ? AND MIN(day) < ?",
                (p.start.isoformat(), p.end.isoformat()))}
        if self.search_text:
            rows = [r for r in rows if self.search_text in r[0].lower()]
        clear(self.apps_list)
        peak = max((r[1] + r[2] for r in rows), default=1)
        limit = 300
        for name, rx, tx in rows[:limit]:
            self.apps_list.append(self._app_row(
                name, rx + tx, total, peak, colors.get(name, OTHER), rx, tx,
                badge="new" if name in new else None))
        if not rows:
            self.apps_list.append(Gtk.ListBoxRow(
                child=label("No matching applications", ("dim-label",), xalign=0.5,
                            margin_top=18, margin_bottom=18), activatable=False))
        extra = f"Showing the first {limit}. " if len(rows) > limit else ""
        self.apps_footer.set_label(extra + "“new” marks applications first seen in this "
                                   "period. Click any application for its history.")

    # --------------------------------------------------------------- networks

    def _build_networks(self):
        box = page_box()
        self.net_title = label("", ("title-2",), wrap=True)
        box.append(self.net_title)
        self.net_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.net_list.add_css_class("boxed-list")
        self.net_list.connect("row-activated", self._on_network_row)
        box.append(self.net_list)
        box.append(label(
            "Wi-Fi is tracked by router, so renaming a network or changing its password keeps "
            "the same entry, with every name it has had listed under it. Wired networks are "
            "tracked by their gateway. Click a network to see its days, rename it, or merge it "
            "with another.", ("dim-label", "caption"), wrap=True))
        self.stack.add_titled_with_icon(scrolled(box, 900), "networks", "Networks",
                                        "network-wireless-symbolic")

    def _refresh_networks(self, db):
        p = self.period
        rows = nd.networks(db, p)
        total = sum(n.total for n in rows) or 1
        peak = max((n.total for n in rows), default=1) or 1
        tracked = [n for n in rows if n.kind != "untracked"]
        self.net_title.set_label(f"{len(tracked)} networks · {p.label}")
        clear(self.net_list)
        for n in rows:
            row = Adw.ActionRow(title=GLib.markup_escape_text(n.label),
                                activatable=n.kind != "untracked")
            row.network = n
            icon = {"wifi": "network-wireless-symbolic", "wired": "network-wired-symbolic"}.get(
                n.kind, "dialog-question-symbolic")
            row.add_prefix(Gtk.Image(icon_name=icon))
            bits = []
            others = [x for x in n.names if x != n.label]
            if others:
                bits.append("also " + ", ".join(others))
            if n.last_seen:
                bits.append("last used " + datetime.fromtimestamp(n.last_seen).strftime("%d %b %H:%M"))
            if n.kind == "untracked":
                bits.append("traffic from before per-network tracking was installed")
            bits.append(f"{n.total / total * 100:.1f}%")
            row.set_subtitle(GLib.markup_escape_text(" · ".join(bits)))
            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                            valign=Gtk.Align.CENTER)
            right.append(label(nd.human(n.total), ("app-total", "numeric"), xalign=1.0))
            bar = ShareBar(width=160)
            bar.set_value(n.total / peak, OTHER if n.kind == "untracked" else DOWN)
            right.append(bar)
            row.add_suffix(right)
            self.net_list.append(row)
        if not rows:
            self.net_list.append(Gtk.ListBoxRow(
                child=label("No traffic in this period", ("dim-label",), xalign=0.5,
                            margin_top=18, margin_bottom=18), activatable=False))

    def _on_network_row(self, _box, row):
        n = getattr(row, "network", None)
        if n and n.kind != "untracked" and self.db:
            NetworkDetail(self, n).present(self)

    # ---------------------------------------------------------------- history

    def _build_history(self):
        box = page_box()
        box.append(label("History", ("title-2",)))

        c = card(None)
        c.append(label("Billing cycles", ("title-4",)))
        self.cycle_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.cycle_list.add_css_class("flat-list")
        self.cycle_list.connect("row-activated", self._on_cycle_row)
        c.append(self.cycle_list)
        box.append(c)

        c = card(None)
        c.append(label("Every day", ("title-4",)))
        c.append(label("Darker is heavier. Red days went past the daily limit.",
                       ("dim-label", "caption")))
        hs = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                                hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        self.heatmap = Heatmap()
        hs.set_child(self.heatmap)
        hs.set_propagate_natural_height(True)
        c.append(hs)
        box.append(c)

        split = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                            min_children_per_line=1, max_children_per_line=2,
                            column_spacing=14, row_spacing=14)
        c = card(None)
        c.append(label("Busiest days", ("title-4",)))
        self.top_days = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.top_days.add_css_class("flat-list")
        self.top_days.connect("row-activated", self._on_day_row)
        c.append(self.top_days)
        split.append(c)
        c = card(None)
        c.append(label("By month", ("title-4",)))
        c.append(legend())
        self.month_chart = BarChart(height=220)
        self.month_chart.on_activate = lambda b: self._open_month(b)
        c.append(self.month_chart)
        split.append(c)
        box.append(split)

        self.stack.add_titled_with_icon(scrolled(box), "history", "History",
                                        "document-open-recent-symbolic")

    def _open_month(self, bucket):
        if bucket.start is None:
            return
        self.set_kind("month", bucket.start)
        self.stack.set_visible_child_name("overview")

    def _on_cycle_row(self, _box, row):
        start = getattr(row, "cycle_start", None)
        if start:
            self.set_kind("cycle", start)
            self.stack.set_visible_child_name("overview")

    def _on_day_row(self, _box, row):
        day = getattr(row, "day", None)
        if day:
            self.show_day(day)

    def _refresh_history(self, db):
        cycle_day = self.cfg["cycle_day"]
        clear(self.cycle_list)
        entries = nd.cycles(db, cycle_day, 13)
        peak = max((rx + tx for _, rx, tx in entries), default=1) or 1
        current = nd.period_for("cycle", date.today(), cycle_day)
        for p, rx, tx in entries:
            row = Adw.ActionRow(title=p.label, activatable=True)
            row.cycle_start = p.start
            row.set_subtitle(f"↓ {nd.human(rx)}   ↑ {nd.human(tx)}")
            if p == current:
                row.add_suffix(pill("current"))
            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                            valign=Gtk.Align.CENTER)
            right.append(label(nd.human(rx + tx), ("app-total", "numeric"), xalign=1.0))
            bar = ShareBar(width=180)
            bar.set_value((rx + tx) / peak, DOWN)
            right.append(bar)
            row.add_suffix(right)
            self.cycle_list.append(row)

        today = date.today()
        weeks = 53
        start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
        span = nd.Period("custom", start, today + timedelta(days=1))
        days = {d: rx + tx for d, (rx, tx) in nd.daily(db, span).items()}
        self.heatmap.set_data(days, start, weeks, nd.daily_limit(db))

        clear(self.top_days)
        rows = db.execute("SELECT day, SUM(rx), SUM(tx) FROM iface_daily GROUP BY day "
                          "ORDER BY SUM(rx + tx) DESC LIMIT 10").fetchall()
        top = rows[0][1] + rows[0][2] if rows else 1
        for i, (d, rx, tx) in enumerate(rows, 1):
            day = date.fromisoformat(d)
            row = Adw.ActionRow(title=day.strftime("%A %d %B %Y"), activatable=True)
            row.day = day
            row.add_prefix(label(f"{i}", ("dim-label", "numeric"), width_chars=2))
            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                            valign=Gtk.Align.CENTER)
            right.append(label(nd.human(rx + tx), ("app-total", "numeric"), xalign=1.0))
            bar = ShareBar(width=110)
            bar.set_value((rx + tx) / top, DOWN)
            right.append(bar)
            row.add_suffix(right)
            self.top_days.append(row)

        all_p = nd.period_for("all", today, cycle_day, nd.first_day(db))
        self.month_chart.set_data(nd.series(db, all_p))

    # ----------------------------------------------------------------- status

    def _build_status(self):
        page = Adw.PreferencesPage()
        self.st_collector = Adw.PreferencesGroup(title="Collector")
        self.st_rows = {}
        for key, title in (("state", "Service"), ("backend", "Per-app accounting"),
                           ("interfaces", "Counted interfaces"), ("filter", "Local traffic")):
            row = Adw.ActionRow(title=title)
            row.add_css_class("property")
            self.st_rows[key] = row
            self.st_collector.add(row)
        page.add(self.st_collector)

        grp = Adw.PreferencesGroup(title="Data")
        for key, title in (("range", "Recorded"), ("coverage", "Per-app coverage, this cycle"),
                           ("db", "Database"), ("conf", "Collector settings")):
            row = Adw.ActionRow(title=title)
            row.add_css_class("property")
            self.st_rows[key] = row
            grp.add(row)
        page.add(grp)

        grp = Adw.PreferencesGroup(title="Limits")
        for key, title in (("cycle_day", "Billing cycle starts on day"),
                           ("limit", "Daily limit"), ("plan", "Monthly data plan"),
                           ("remind", "Reminders")):
            row = Adw.ActionRow(title=title)
            row.add_css_class("property")
            self.st_rows[key] = row
            grp.add(row)
        page.add(grp)

        grp = Adw.PreferencesGroup(
            title="Commands",
            description="Run these in a terminal when the service needs attention.")
        for cmd, what in (("systemctl status netusage", "is it running"),
                          ("journalctl -u netusage -n 50", "recent log"),
                          ("sudo systemctl restart netusage", "restart it"),
                          ("netusage", "terminal report")):
            row = Adw.ActionRow(title=cmd, subtitle=what)
            row.add_css_class("monospace")
            copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Copy", css_classes=["flat"])
            copy.connect("clicked", self._copy_text, cmd)
            row.add_suffix(copy)
            grp.add(row)
        page.add(grp)
        self.stack.add_titled_with_icon(page, "status", "Status",
                                        "emblem-system-symbolic")

    def _copy_text(self, _btn, text):
        self.get_clipboard().set(text)
        self.toasts.add_toast(Adw.Toast(title="Copied", timeout=1))

    def _refresh_status(self, db):
        h = nd.health(db)
        if h.state == "running":
            state = f"Running · reported {int(h.age)}s ago"
            self.banner.set_revealed(False)
        elif h.state == "stalled":
            mins = int(h.age // 60)
            state = f"Not reporting for {mins} min"
            self.banner.set_title(f"The collector has not reported for {mins} minutes. "
                                  "Usage since then is not being recorded.")
            self.banner.set_revealed(True)
        else:
            state = "Waiting for the first report"
            self.banner.set_revealed(False)
        labels = {
            "probe_filtered.bt": "eBPF, internet traffic only",
            "probe_for.bt": "eBPF (map iteration)",
            "probe_print.bt": "eBPF (print mode)",
            "nethogs": "nethogs (packet capture fallback)",
            "starting": "attaching…",
            "failed": "unavailable (totals unaffected)",
            "off": "disabled",
        }
        self.st_rows["state"].set_subtitle(state)
        self.st_rows["backend"].set_subtitle(labels.get(h.backend, h.backend))
        self.st_rows["interfaces"].set_subtitle(h.interfaces.replace(",", ", "))
        filt = nd.meta(db, "app_filter_since")
        self.st_rows["filter"].set_subtitle(
            f"excluded from per-app figures since {filt}" if filt else
            "counted in per-app figures (update the collector to exclude it)")
        self.st_rows["range"].set_subtitle(
            f"{h.data_from} → {h.data_to} · {h.data_days} days" if h.data_from else "nothing yet")
        cov = nd.coverage(db, nd.period_for("cycle", date.today(), self.cfg["cycle_day"]))
        self.st_rows["coverage"].set_subtitle(f"{cov * 100:.0f}%" if cov else "–")
        self.st_rows["db"].set_subtitle(self.cfg["db"])
        self.st_rows["conf"].set_subtitle(f"{nd.CONF_PATH} (edit with sudo, then restart)")
        self.st_rows["cycle_day"].set_subtitle(str(self.cfg["cycle_day"]))
        limit = nd.daily_limit(db)
        points = nd.meta(db, "notify_at") or ""
        self.st_rows["limit"].set_subtitle(
            f"{nd.human(limit, 1)} · notifies at {points.replace(',', '%, ')}%"
            if limit and points else nd.human(limit, 1) if limit else "off")
        plan = self.plan_bytes(db)
        cpoints = nd.meta(db, "cycle_notify_at") or ""
        self.st_rows["plan"].set_subtitle(
            f"{nd.human(plan, 0)}" + (f" · notifies at {cpoints.replace(',', '%, ')}%"
                                      if cpoints and nd.cycle_limit(db) else "")
            if plan else "not set · Preferences (Ctrl+,)")
        remind = nd.meta_int(db, "remind_minutes")
        self.st_rows["remind"].set_subtitle(f"every {remind} min while over a limit"
                                            if remind else "off")

    # ------------------------------------------------------------------- live

    def _live_tick(self):
        if self.meter is None:
            ifaces = None
            if self.db:
                try:
                    raw = nd.meta(self.db, "interfaces")
                    ifaces = [i for i in (raw or "").split(",") if i]
                except Exception:
                    ifaces = None
            if not ifaces:
                ifaces = [n for n in sorted(os.listdir("/sys/class/net"))
                          if n != "lo" and os.path.exists(f"/sys/class/net/{n}/device")]
            self.meter = nd.LiveMeter(ifaces)
        rx, tx = self.meter.tick()
        self.live_down.set_label(f"↓ {nd.human_rate(rx)}")
        self.live_up.set_label(f"↑ {nd.human_rate(tx)}")
        self.spark.set_samples(self.meter.samples, self.meter.history)
        return GLib.SOURCE_CONTINUE

    # ---------------------------------------------------------------- actions

    def export_csv(self):
        if not self.db:
            return
        dialog = Gtk.FileDialog(title="Export usage")
        slug = self.period.label.lower().replace(" ", "-").replace("–", "to").replace(",", "")
        dialog.set_initial_name(f"netusage-{slug}.csv")

        def done(dlg, result):
            try:
                f = dlg.save_finish(result)
            except GLib.Error:
                return
            path = f.get_path()
            if not path:
                self.toasts.add_toast(Adw.Toast(title="Choose a folder on this computer"))
                return
            try:
                nd.export_csv(self.db, self.period, path)
                self.toasts.add_toast(Adw.Toast(title=f"Saved {os.path.basename(path)}"))
            except Exception as exc:
                self.toasts.add_toast(Adw.Toast(title=f"Could not save: {exc}"))

        dialog.save(self, None, done)

    def copy_summary(self):
        if not self.db:
            return
        rx, tx = nd.totals(self.db, self.period)
        lines = [f"Net usage · {self.period.label}",
                 f"Total {nd.human(rx + tx)} (↓ {nd.human(rx)}, ↑ {nd.human(tx)})"]
        for name, arx, atx in nd.apps(self.db, self.period)[:5]:
            lines.append(f"  {name}: {nd.human(arx + atx)}")
        self.get_clipboard().set("\n".join(lines))
        self.toasts.add_toast(Adw.Toast(title="Summary copied", timeout=2))


# --------------------------------------------------------------------------
# application detail
# --------------------------------------------------------------------------

class AppDetail(Adw.Dialog):
    def __init__(self, win, app):
        super().__init__(title=app, content_width=720, content_height=640)
        db = win.db
        p = win.period
        # a single day has nothing to chart per day, so show the month around it
        span = p if p.kind in ("week", "month", "cycle", "all") else \
            nd.Period("custom", p.start - timedelta(days=29), p.start + timedelta(days=1))

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = page_box()

        head = Gtk.Box(spacing=16)
        head.append(Adw.Avatar(size=64, text=app, show_initials=True))
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                         valign=Gtk.Align.CENTER)
        titles.append(label(app, ("title-1",), wrap=True))
        first, last = nd.app_first_seen(db, app)
        titles.append(label(f"first seen {first} · last seen {last}" if first else "",
                            ("dim-label",)))
        head.append(titles)
        box.append(head)

        rows = {a: (r, t) for a, r, t in nd.apps(db, p)}
        rx, tx = rows.get(app, (0, 0))
        all_total = sum(r + t for r, t in rows.values()) or 1
        stats = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                            min_children_per_line=2, max_children_per_line=4,
                            column_spacing=10, row_spacing=10)
        rank = sorted(rows, key=lambda a: -sum(rows[a])).index(app) + 1 if app in rows else None
        for name, value, css in (("total", nd.human(rx + tx), ()),
                                 ("download", nd.human(rx), ("down",)),
                                 ("upload", nd.human(tx), ("up",)),
                                 ("share", f"{(rx + tx) / all_total * 100:.1f}%"
                                  + (f" · #{rank}" if rank else ""), ())):
            c = card(None, spacing=2)
            c.append(label(name.upper(), ("card-title",), wrap=True))
            c.append(label(value, ("stat-value", "numeric") + css))
            stats.append(c)
        box.append(stats)

        c = card(None, spacing=6)
        c.append(label(f"Daily · {span.label if span.kind != 'custom' else 'last 30 days'}",
                       ("title-4",)))
        c.append(legend())
        chart = BarChart(height=220)
        per_day = nd.app_daily(db, app, span)
        buckets = []
        if span.kind == "all":
            months = {}
            for d, (r, t) in per_day.items():
                key = d.replace(day=1)
                mr, mt = months.get(key, (0, 0))
                months[key] = (mr + r, mt + t)
            for m in sorted(months):
                buckets.append(nd.Bucket(m.strftime("%b"), m.strftime("%B %Y"), *months[m]))
        else:
            d = span.start
            while d < span.end:
                r, t = per_day.get(d, (0, 0))
                buckets.append(nd.Bucket(str(d.day), d.strftime("%A %d %B"), r, t,
                                         current=(d == date.today())))
                d += timedelta(days=1)
        chart.set_data(buckets)
        c.append(chart)
        box.append(c)

        tv.set_content(scrolled(box, 720))
        self.set_child(tv)


class NetworkDetail(Adw.Dialog):
    """One network: its days, the names it has had, and rename / merge / separate."""

    def __init__(self, win, net):
        super().__init__(title=net.label, content_width=720, content_height=680)
        self.win = win
        self.net = net
        db = win.db
        p = win.period
        span = p if p.kind in ("week", "month", "cycle", "all") else \
            nd.Period("custom", p.start - timedelta(days=29), p.start + timedelta(days=1))

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = page_box()
        box.append(label(net.label, ("title-1",), wrap=True))
        if net.names:
            box.append(label("Names seen: " + ", ".join(net.names), ("dim-label",), wrap=True))

        stats = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                            min_children_per_line=2, max_children_per_line=3,
                            column_spacing=10, row_spacing=10)
        for name, value, css in (("total", nd.human(net.total), ()),
                                 ("download", nd.human(net.rx), ("down",)),
                                 ("upload", nd.human(net.tx), ("up",))):
            c = card(None, spacing=2)
            c.append(label(name.upper(), ("card-title",)))
            c.append(label(value, ("stat-value", "numeric") + css))
            stats.append(c)
        box.append(stats)

        c = card(None, spacing=6)
        c.append(label(f"Daily · {span.label if span.kind != 'custom' else 'last 30 days'}",
                       ("title-4",)))
        c.append(legend())
        chart = BarChart(height=220)
        per_day = nd.network_daily(db, net.key, span)
        buckets = []
        if span.kind == "all":
            months = {}
            for d, (r, t) in per_day.items():
                k = d.replace(day=1)
                mr, mt = months.get(k, (0, 0))
                months[k] = (mr + r, mt + t)
            for m in sorted(months):
                buckets.append(nd.Bucket(m.strftime("%b"), m.strftime("%B %Y"), *months[m]))
        else:
            d = span.start
            while d < span.end:
                r, t = per_day.get(d, (0, 0))
                buckets.append(nd.Bucket(str(d.day), d.strftime("%A %d %B"), r, t,
                                         current=(d == date.today())))
                d += timedelta(days=1)
        chart.set_data(buckets)
        c.append(chart)
        box.append(c)

        grp = Adw.PreferencesGroup(title="Manage")
        if not nd.network_prefs_writable():
            grp.set_description("Names and merges are saved to " + nd.NETWORKS_PATH +
                                ". Run sudo ./install.sh once to make it editable.")
        self.name_row = Adw.EntryRow(title="Show as", text=net.label, show_apply_button=True)
        self.name_row.connect("apply", self._rename)
        grp.add(self.name_row)

        all_nets = [n for n in nd.networks(db, nd.period_for("all", date.today(),
                                                             win.cfg["cycle_day"], nd.first_day(db)))
                    if n.kind == net.kind and n.key != net.key]
        self.targets = all_nets
        merge = Adw.ComboRow(title="Merge into",
                             subtitle="Count this network together with another one")
        merge.set_model(Gtk.StringList.new(["—"] + [n.label for n in all_nets]))
        merge.connect("notify::selected", self._merge)
        grp.add(merge)
        if len(net.keys) > 1:
            sep = Adw.ActionRow(title="Merged from " + ", ".join(net.names or net.keys))
            btn = Gtk.Button(label="Separate", valign=Gtk.Align.CENTER)
            btn.connect("clicked", self._separate)
            sep.add_suffix(btn)
            grp.add(sep)
        box.append(grp)

        tv.set_content(scrolled(box, 720))
        self.set_child(tv)

    def _save(self, change):
        prefs = nd.read_network_prefs()
        change(prefs)
        try:
            nd.write_network_prefs(prefs)
        except OSError as exc:
            self.win.toasts.add_toast(Adw.Toast(title=f"Could not save: {exc.strerror}"))
            return False
        self.win.refresh()
        return True

    def _rename(self, row):
        text = row.get_text().strip()

        def change(prefs):
            if text:
                prefs["labels"][self.net.key] = text
            else:
                prefs["labels"].pop(self.net.key, None)
        if self._save(change):
            self.set_title(text or self.net.label)

    def _merge(self, row, _pspec):
        i = row.get_selected()
        if i <= 0:
            return
        target = self.targets[i - 1]

        def change(prefs):
            prefs["merge"][self.net.key] = nd.network_group(target.key, prefs)
        if self._save(change):
            self.close()

    def _separate(self, _btn):
        def change(prefs):
            for k in self.net.keys:
                prefs["merge"].pop(k, None)
            for k, v in list(prefs["merge"].items()):
                if v == self.net.key:
                    prefs["merge"].pop(k)
        if self._save(change):
            self.close()


# --------------------------------------------------------------------------
# preferences: notification limits
# --------------------------------------------------------------------------

REMIND_CHOICES = [(0, "Off"), (15, "Every 15 minutes"), (30, "Every 30 minutes"),
                  (60, "Every hour"), (120, "Every 2 hours"), (240, "Every 4 hours")]
DAILY_POINTS = (50, 80, 90, 100, 150, 200)
CYCLE_POINTS = (50, 75, 90, 100)


def send_notification(title, body, urgency="normal"):
    """Show a desktop notification the same way the collector does."""
    import subprocess
    try:
        subprocess.run(["notify-send", "-a", "netusage", "-u", urgency, "-i", APP_ID,
                        title, body], timeout=10, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


class Preferences(Adw.PreferencesDialog):
    """Limits and reminders, saved to the file the collector watches."""

    def __init__(self, win):
        super().__init__(title="Preferences", search_enabled=False)
        self.win = win
        self.limits = nd.read_limits()
        self.writable = nd.limits_writable()
        if not os.path.exists(nd.LIMITS_PATH):
            # Before the collector update: show what it enforces now, and the plan
            # from where the dashboard used to keep it.
            self.limits["cycle_limit_gb"] = float(win.settings.get("plan_gb") or 0)
            if win.db:
                try:
                    self.limits["daily_limit_gb"] = nd.daily_limit(win.db) / 1024 ** 3
                    points = nd._percents(nd.meta(win.db, "notify_at") or "")
                    self.limits["notify_at"] = points or [80, 100, 150, 200]
                except Exception:
                    pass
        self._save_id = 0

        page = Adw.PreferencesPage(title="Notifications",
                                   icon_name="preferences-system-notifications-symbolic")

        if not self.writable:
            grp = Adw.PreferencesGroup(
                title="One-time setup needed",
                description="The collector sends these notifications as root, so it needs "
                            "a limits file it can read and you can edit. Run this once in "
                            "the netusage folder, then reopen Preferences:")
            row = Adw.ActionRow(title="sudo ./install.sh")
            row.add_css_class("monospace")
            copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                              css_classes=["flat"], tooltip_text="Copy")
            copy.connect("clicked", lambda *_: (self.get_clipboard().set("sudo ./install.sh"),
                                                self.add_toast(Adw.Toast(title="Copied"))))
            row.add_suffix(copy)
            grp.add(row)
            page.add(grp)

        # daily
        grp = Adw.PreferencesGroup(
            title="Daily limit",
            description="Get a notification when today's usage reaches these points.")
        self.daily = Adw.SpinRow.new_with_range(0, 1000, 0.5)
        self.daily.set_digits(1)
        self.daily.set_title("Daily limit")
        self.daily.set_subtitle("GB per day · 0 turns daily notifications off")
        self.daily.set_value(self.limits["daily_limit_gb"])
        grp.add(self.daily)
        self.daily_points, row = self._points_row(DAILY_POINTS, self.limits["notify_at"])
        grp.add(row)
        page.add(grp)

        # billing cycle
        grp = Adw.PreferencesGroup(
            title="Monthly data plan",
            description=f"For the billing cycle that starts on day {win.cfg['cycle_day']}. "
                        "Also drives the budget, safe-per-day figure and pace line on the "
                        "Overview.")
        self.cycle = Adw.SpinRow.new_with_range(0, 100000, 5)
        self.cycle.set_digits(0)
        self.cycle.set_title("Plan allowance")
        self.cycle.set_subtitle("GB per billing cycle · 0 for unlimited")
        self.cycle.set_value(self.limits["cycle_limit_gb"])
        grp.add(self.cycle)
        self.cycle_points, row = self._points_row(CYCLE_POINTS, self.limits["cycle_notify_at"])
        grp.add(row)
        page.add(grp)

        # reminders
        grp = Adw.PreferencesGroup(
            title="Reminders",
            description="Once you are past a limit, keep reminding you until the day or "
                        "cycle ends.")
        self.remind = Adw.ComboRow(title="Remind me again",
                                   model=Gtk.StringList.new([t for _, t in REMIND_CHOICES]))
        minutes = [m for m, _ in REMIND_CHOICES]
        current = self.limits["remind_every_min"]
        self.remind.set_selected(minutes.index(current) if current in minutes else 0)
        grp.add(self.remind)
        page.add(grp)

        grp = Adw.PreferencesGroup()
        row = Adw.ActionRow(title="Send a test notification",
                            subtitle="Check that notifications show up on this desktop")
        btn = Gtk.Button(label="Send", valign=Gtk.Align.CENTER)
        btn.connect("clicked", self._test)
        row.add_suffix(btn)
        row.set_activatable_widget(btn)
        grp.add(row)
        self.status = Adw.ActionRow(title="Saved automatically",
                                    subtitle="The collector applies changes within 10 seconds")
        self.status.add_prefix(Gtk.Image(icon_name="emblem-ok-symbolic"))
        grp.add(self.status)
        page.add(grp)
        self.add(page)

        for widget in (self.daily, self.cycle):
            widget.connect("notify::value", self._changed)
        self.remind.connect("notify::selected", self._changed)
        for btn in list(self.daily_points.values()) + list(self.cycle_points.values()):
            btn.connect("toggled", self._changed)
        if not self.writable:
            for w in (self.daily, self.cycle, self.remind):
                w.set_sensitive(False)
            for btn in list(self.daily_points.values()) + list(self.cycle_points.values()):
                btn.set_sensitive(False)
            self.status.set_visible(False)
        self.connect("closed", lambda *_: self._flush())

    def _points_row(self, points, active):
        row = Adw.ActionRow(title="Notify at", subtitle="percent of the limit")
        box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        buttons = {}
        for p in points:
            b = Gtk.ToggleButton(label=f"{p}%", active=p in active)
            box.append(b)
            buttons[p] = b
        row.add_suffix(box)
        return buttons, row

    def _collect(self):
        return {
            "daily_limit_gb": round(self.daily.get_value(), 1),
            "notify_at": [p for p, b in self.daily_points.items() if b.get_active()],
            "cycle_limit_gb": round(self.cycle.get_value()),
            "cycle_notify_at": [p for p, b in self.cycle_points.items() if b.get_active()],
            "remind_every_min": REMIND_CHOICES[self.remind.get_selected()][0],
        }

    def _changed(self, *_):
        if not self.writable:
            return
        self.status.set_title("Saving…")
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(500, self._flush)

    def _flush(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
            self._save_id = 0
        if not self.writable:
            return False
        limits = self._collect()
        if limits == self.limits:
            self.status.set_title("Saved automatically")
            return False
        try:
            nd.write_limits(limits)
        except OSError as exc:
            self.status.set_title("Could not save")
            self.status.set_subtitle(exc.strerror or str(exc))
            return False
        self.limits = limits
        self.status.set_title("Saved")
        self.status.set_subtitle("The collector applies changes within 10 seconds")
        self.win.refresh()
        return False

    def _test(self, *_):
        lim = self._collect()
        if lim["daily_limit_gb"]:
            body = (f"You will be notified at {', '.join(f'{p}%' for p in lim['notify_at'])} "
                    f"of {lim['daily_limit_gb']:g} GB a day.")
        else:
            body = "Daily notifications are off."
        if lim["cycle_limit_gb"]:
            body += f" Plan: {lim['cycle_limit_gb']:g} GB per cycle."
        if send_notification("Internet Usage notifications work", body):
            self.add_toast(Adw.Toast(title="Test notification sent"))
        else:
            self.add_toast(Adw.Toast(title="Could not send: is notify-send installed?"))


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------

class NetUsageApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS) if hasattr(provider, "load_from_string") \
            else provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for path in (os.path.join(HERE, "icons"), os.path.join(os.path.dirname(HERE), "icons")):
            if os.path.isdir(path):
                theme.add_search_path(path)

        for name, cb, accels in (("quit", lambda *_: self.quit(), ["<Control>q"]),
                                 ("about", self._about, []),
                                 ("preferences", self._preferences, ["<Control>comma"])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def do_activate(self):
        if not self.win:
            self.win = Window(self)
        self.win.present()

    def _about(self, *_):
        about = Adw.AboutDialog(
            application_name="Internet Usage", application_icon=APP_ID, version=VERSION,
            developer_name="AbdelRahman Madboly",
            comments="How much internet this computer uses, by day, week, month and "
                     "billing cycle, and which applications use it.",
            license_type=Gtk.License.MIT_X11)
        about.present(self.win)

    def _preferences(self, *_):
        Preferences(self.win).present(self.win)

def main():
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Internet Usage")
    return NetUsageApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
