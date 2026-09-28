"""Chart widgets for the dashboard, drawn with GTK's own renderer.

The drawing code is written against a small cairo-like pen (move_to, arc,
fill, show_text...) that records into a Gtk.Snapshot. Real cairo would need
python3-gi-cairo, which is not installed by default, and GSK paths render on
the GPU anyway. Colours follow the theme.
"""

import math
from types import SimpleNamespace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from netusage_data import human, human_rate  # noqa: E402

# GNOME palette. Download and upload keep the same colour everywhere.
DOWN = (0.208, 0.518, 0.894)       # blue 3
UP = (0.569, 0.255, 0.675)         # purple 3
WARN = (0.902, 0.380, 0.000)       # orange 4
DANGER = (0.878, 0.106, 0.141)     # red 3
OK = (0.180, 0.761, 0.494)         # green 4

APP_COLORS = [
    (0.208, 0.518, 0.894), (0.180, 0.761, 0.494), (0.965, 0.827, 0.176),
    (1.000, 0.471, 0.000), (0.878, 0.106, 0.141), (0.569, 0.255, 0.675),
    (0.129, 0.588, 0.588), (0.596, 0.416, 0.267), (0.384, 0.627, 0.918),
    (0.600, 0.757, 0.945),
]
OTHER = (0.60, 0.60, 0.62)


def app_color(index):
    return APP_COLORS[index] if index < len(APP_COLORS) else OTHER


def rgb_hex(rgb):
    return "#%02x%02x%02x" % tuple(int(c * 255) for c in rgb)


def _fg(widget):
    c = widget.get_color()
    return c.red, c.green, c.blue


def _rounded_top(cr, x, y, w, h, r):
    """Bar with rounded top corners only, so stacked segments sit flush."""
    r = min(r, w / 2, h)
    if h <= 0 or w <= 0:
        return
    cr.move_to(x, y + h)
    cr.line_to(x, y + r)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
    cr.line_to(x + w, y + h)
    cr.close_path()


_UNITS = (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024), ("B", 1))
# multiples whose quarters are round numbers, so four gridlines read cleanly
_NICE = (1, 2, 4, 8, 12, 16, 20, 40, 60, 80, 100, 200, 400, 600, 800, 1000)


def _nice_ceiling(value):
    """Axis maximum in the value's own unit, divisible into four round steps."""
    if value <= 0:
        return 1024 ** 2
    for _name, size in _UNITS:
        if value >= size:
            scaled = value / size
            for step in _NICE:
                if scaled <= step:
                    return step * size
            return scaled * size
    return 1024


def axis_label(value):
    """"2.5 GB", "500 MB": the unit of the value, trailing zeros dropped."""
    if value <= 0:
        return "0"
    for name, size in _UNITS:
        if value >= size:
            text = f"{value / size:.2f}".rstrip("0").rstrip(".")
            return f"{text} {name}"
    return f"{int(value)} B"


class Pen:
    """The subset of the cairo context API the charts use, over GSK paths."""

    _CAPS = {0: Gsk.LineCap.BUTT, 1: Gsk.LineCap.ROUND, 2: Gsk.LineCap.SQUARE}

    def __init__(self, snapshot, widget):
        self.snap = snapshot
        self.widget = widget
        self.color = Gdk.RGBA()
        self.color.red = self.color.green = self.color.blue = 0.0
        self.color.alpha = 1.0
        self.width = 1.0
        self.dash = []
        self.cap = Gsk.LineCap.BUTT
        self.size = 10.0
        self.bold = False
        self._reset()

    def _reset(self):
        self.builder = Gsk.PathBuilder.new()
        self.empty = True
        self.point = None

    # colour and style
    def set_source_rgba(self, r, g, b, a=1.0):
        self.color = Gdk.RGBA()
        self.color.red, self.color.green, self.color.blue, self.color.alpha = r, g, b, a

    def set_source_rgb(self, r, g, b):
        self.set_source_rgba(r, g, b, 1.0)

    def set_line_width(self, width):
        self.width = width

    def set_dash(self, dashes):
        self.dash = list(dashes)

    def set_line_cap(self, cap):
        self.cap = self._CAPS.get(int(cap), Gsk.LineCap.BUTT)

    # path construction
    def new_sub_path(self):
        self.point = None

    def move_to(self, x, y):
        self.builder.move_to(x, y)
        self.point = (x, y)

    def line_to(self, x, y):
        if self.point is None:
            self.move_to(x, y)
            return
        self.builder.line_to(x, y)
        self.point = (x, y)
        self.empty = False

    def arc(self, cx, cy, r, a1, a2):
        while a2 < a1:
            a2 += 2 * math.pi
        start = (cx + r * math.cos(a1), cy + r * math.sin(a1))
        if self.point is None:
            self.move_to(*start)
        else:
            self.line_to(*start)
        sweep = a2 - a1
        if sweep <= 0 or r <= 0:
            return
        pieces = max(1, math.ceil(sweep / (math.pi / 2)))
        for i in range(1, pieces + 1):
            a = a1 + sweep * i / pieces
            end = (cx + r * math.cos(a), cy + r * math.sin(a))
            self.builder.svg_arc_to(r, r, 0, False, True, *end)
        self.point = end
        self.empty = False

    def rectangle(self, x, y, w, h):
        rect = Graphene.Rect()
        rect.init(x, y, max(w, 0), max(h, 0))
        self.builder.add_rect(rect)
        self.point = None
        self.empty = False

    def close_path(self):
        self.builder.close()
        self.point = None

    # painting
    def fill(self):
        if not self.empty:
            self.snap.append_fill(self.builder.to_path(), Gsk.FillRule.WINDING, self.color)
        self._reset()

    def stroke(self):
        if not self.empty:
            stroke = Gsk.Stroke.new(self.width)
            stroke.set_line_cap(self.cap)
            if self.dash:
                stroke.set_dash(self.dash)
            self.snap.append_stroke(self.builder.to_path(), stroke, self.color)
        self._reset()

    # text
    def select_font_face(self, _family, _slant=0, weight=0):
        self.bold = weight == 1

    def set_font_size(self, size):
        self.size = size

    def _layout(self, text):
        layout = self.widget.create_pango_layout(text)
        desc = self.widget.get_pango_context().get_font_description().copy()
        desc.set_absolute_size(self.size * Pango.SCALE)
        desc.set_weight(Pango.Weight.BOLD if self.bold else Pango.Weight.NORMAL)
        layout.set_font_description(desc)
        return layout

    def text_extents(self, text):
        _ink, logical = self._layout(text).get_pixel_extents()
        # cap height, which is what the callers centre on
        return SimpleNamespace(width=logical.width, height=self.size * 0.72, x_bearing=0)

    def show_text(self, text):
        if self.point is None:
            return
        layout = self._layout(text)
        x, y = self.point
        point = Graphene.Point()
        point.init(x, y - layout.get_baseline() / Pango.SCALE)
        self.snap.save()
        self.snap.translate(point)
        self.snap.append_layout(layout, self.color)
        self.snap.restore()
        self.point = None


class _Canvas(Gtk.Widget):
    """Minimal stand-in for Gtk.DrawingArea whose draw function gets a Pen."""

    def __init__(self):
        super().__init__()
        self._draw_func = None
        self._size = [-1, -1]

    def set_draw_func(self, func):
        self._draw_func = func

    def set_content_width(self, width):
        self._size[0] = width
        self.set_size_request(*self._size)

    def set_content_height(self, height):
        self._size[1] = height
        self.set_size_request(*self._size)

    def do_snapshot(self, snapshot):
        if self._draw_func:
            w, h = self.get_width(), self.get_height()
            if w > 0 and h > 0:
                self._draw_func(self, Pen(snapshot, self), w, h)


class BarChart(_Canvas):
    """Stacked download/upload bars with hover highlight and tooltips.

    `set_data(buckets, limit)` takes objects with label/title/rx/tx/current.
    An optional horizontal reference line is drawn at `limit` bytes.
    """

    PAD_L, PAD_R, PAD_T, PAD_B = 56, 8, 12, 24

    def __init__(self, height=220):
        super().__init__()
        self.buckets = []
        self.limit = 0
        self.hover = None
        self.on_activate = None
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", self._leave)
        self.add_controller(motion)
        click = Gtk.GestureClick()
        click.connect("released", self._click)
        self.add_controller(click)

    def set_data(self, buckets, limit=0):
        self.buckets = list(buckets)
        self.limit = limit
        self.hover = None
        self.queue_draw()

    def _geometry(self):
        w, h = self.get_width(), self.get_height()
        plot_w = max(w - self.PAD_L - self.PAD_R, 1)
        n = max(len(self.buckets), 1)
        slot = plot_w / n
        return w, h, plot_w, slot

    def _index_at(self, x):
        if not self.buckets:
            return None
        _, _, _, slot = self._geometry()
        i = int((x - self.PAD_L) // slot)
        return i if 0 <= i < len(self.buckets) else None

    def _motion(self, _ctl, x, _y):
        i = self._index_at(x)
        if i != self.hover:
            self.hover = i
            self.queue_draw()

    def _leave(self, _ctl):
        self.hover = None
        self.queue_draw()

    def _click(self, _gesture, _n, x, _y):
        i = self._index_at(x)
        if i is not None and self.on_activate:
            self.on_activate(self.buckets[i])

    def _tooltip(self, _w, x, _y, _kbd, tooltip):
        i = self._index_at(x)
        if i is None:
            return False
        b = self.buckets[i]
        tooltip.set_markup(
            f"<b>{GLib.markup_escape_text(b.title)}</b>\n"
            f"Total  <b>{human(b.total)}</b>\n"
            f"<span foreground='{rgb_hex(DOWN)}'>●</span> Download  {human(b.rx)}\n"
            f"<span foreground='{rgb_hex(UP)}'>●</span> Upload  {human(b.tx)}")
        return True

    def _draw(self, _area, cr, w, h):
        fg = _fg(self)
        _, _, plot_w, slot = self._geometry()
        plot_h = max(h - self.PAD_T - self.PAD_B, 1)
        base_y = self.PAD_T + plot_h

        peak = max([b.total for b in self.buckets] + [self.limit * 1.08 if self.limit else 0, 1])
        top = _nice_ceiling(peak)

        # grid + y labels
        cr.select_font_face("Sans")
        cr.set_font_size(10)
        for step in range(5):
            val = top * step / 4
            y = base_y - plot_h * step / 4
            cr.set_source_rgba(*fg, 0.08 if step else 0.18)
            cr.set_line_width(1)
            cr.move_to(self.PAD_L, round(y) + 0.5)
            cr.line_to(self.PAD_L + plot_w, round(y) + 0.5)
            cr.stroke()
            text = axis_label(val)
            ext = cr.text_extents(text)
            cr.set_source_rgba(*fg, 0.55)
            cr.move_to(self.PAD_L - 8 - ext.width, y + ext.height / 2)
            cr.show_text(text)

        if not self.buckets:
            return

        bar_w = max(min(slot * 0.62, 38), 2)
        label_every = max(1, math.ceil(len(self.buckets) / max(plot_w / 34, 1)))
        radius = min(4, bar_w / 3)

        for i, b in enumerate(self.buckets):
            x = self.PAD_L + slot * i + (slot - bar_w) / 2
            dim = self.hover is not None and self.hover != i
            alpha = 0.35 if dim else 1.0
            h_rx = plot_h * b.rx / top
            h_tx = plot_h * b.tx / top
            if b.total == 0:
                cr.set_source_rgba(*fg, 0.10)
                _rounded_top(cr, x, base_y - 2, bar_w, 2, 1)
                cr.fill()
            else:
                # download at the bottom, upload stacked on top
                cr.set_source_rgba(*DOWN, alpha)
                if h_tx < 1:
                    _rounded_top(cr, x, base_y - h_rx, bar_w, h_rx, radius)
                else:
                    cr.rectangle(x, base_y - h_rx, bar_w, h_rx)
                cr.fill()
                if h_tx >= 1:
                    cr.set_source_rgba(*UP, alpha)
                    _rounded_top(cr, x, base_y - h_rx - h_tx, bar_w, h_tx, radius)
                    cr.fill()
            if i % label_every == 0 or b.current:
                cr.set_font_size(10)
                ext = cr.text_extents(b.label)
                cr.set_source_rgba(*fg, 0.9 if b.current else 0.55)
                if b.current:
                    cr.select_font_face("Sans", 0, 1)
                cr.move_to(x + bar_w / 2 - ext.width / 2, base_y + 16)
                cr.show_text(b.label)
                cr.select_font_face("Sans")

        if self.limit and self.limit < top:
            y = base_y - plot_h * self.limit / top
            cr.set_source_rgba(*DANGER, 0.75)
            cr.set_dash([5, 4])
            cr.set_line_width(1.2)
            cr.move_to(self.PAD_L, y)
            cr.line_to(self.PAD_L + plot_w, y)
            cr.stroke()
            cr.set_dash([])
            cr.set_font_size(9.5)
            text = f"limit {human(self.limit, 0)}"
            ext = cr.text_extents(text)
            cr.move_to(self.PAD_L + plot_w - ext.width - 2, y - 4)
            cr.show_text(text)


class CumulativeChart(_Canvas):
    """Running total across a period against an even-pace line to a budget.

    Days after today are left empty. With no budget the pace line follows the
    current average instead, which still shows whether usage is speeding up.
    """

    PAD_L, PAD_R, PAD_T, PAD_B = 56, 8, 12, 24

    def __init__(self, height=220):
        super().__init__()
        self.buckets = []
        self.budget = 0
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)

    def set_data(self, buckets, budget=0):
        self.buckets = list(buckets)
        self.budget = budget
        self.queue_draw()

    def _running(self):
        out, acc, seen_current = [], 0, False
        for b in self.buckets:
            if seen_current:
                break
            acc += b.total
            out.append(acc)
            seen_current = b.current
        return out

    def _index_at(self, x):
        n = len(self.buckets)
        if n < 2:
            return None
        plot_w = max(self.get_width() - self.PAD_L - self.PAD_R, 1)
        i = round((x - self.PAD_L) / plot_w * (n - 1))
        return i if 0 <= i < n else None

    def _tooltip(self, _w, x, _y, _kbd, tooltip):
        i = self._index_at(x)
        run = self._running()
        if i is None or i >= len(run):
            return False
        text = f"<b>{GLib.markup_escape_text(self.buckets[i].title)}</b>\nSo far  {human(run[i])}"
        if self.budget:
            pace = self.budget * (i + 1) / len(self.buckets)
            diff = run[i] - pace
            word = "over" if diff > 0 else "under"
            text += f"\n{human(abs(diff))} {word} even pace"
        tooltip.set_markup(text)
        return True

    def _draw(self, _area, cr, w, h):
        fg = _fg(self)
        n = len(self.buckets)
        run = self._running()
        plot_w = max(w - self.PAD_L - self.PAD_R, 1)
        plot_h = max(h - self.PAD_T - self.PAD_B, 1)
        base_y = self.PAD_T + plot_h
        if n < 2:
            return
        if self.budget:
            target = self.budget
        elif run:
            target = run[-1] / len(run) * n
        else:
            target = 1
        top = _nice_ceiling(max([target] + run + [1]))

        cr.select_font_face("Sans")
        cr.set_font_size(10)
        for step in range(5):
            val = top * step / 4
            y = base_y - plot_h * step / 4
            cr.set_source_rgba(*fg, 0.08 if step else 0.18)
            cr.set_line_width(1)
            cr.move_to(self.PAD_L, round(y) + 0.5)
            cr.line_to(self.PAD_L + plot_w, round(y) + 0.5)
            cr.stroke()
            text = axis_label(val)
            ext = cr.text_extents(text)
            cr.set_source_rgba(*fg, 0.55)
            cr.move_to(self.PAD_L - 8 - ext.width, y + ext.height / 2)
            cr.show_text(text)

        def px(i):
            return self.PAD_L + plot_w * i / (n - 1)

        def py(v):
            return base_y - plot_h * v / top

        # even-pace guide
        cr.set_source_rgba(*fg, 0.35)
        cr.set_dash([5, 4])
        cr.set_line_width(1.2)
        cr.move_to(px(0), py(target / n))
        cr.line_to(px(n - 1), py(target))
        cr.stroke()
        cr.set_dash([])

        if self.budget:
            cr.set_source_rgba(*DANGER, 0.75)
            cr.set_line_width(1.2)
            cr.move_to(self.PAD_L, py(self.budget))
            cr.line_to(self.PAD_L + plot_w, py(self.budget))
            cr.stroke()

        if run:
            over = self.budget and run[-1] > self.budget * len(run) / n
            color = WARN if over else DOWN
            cr.move_to(px(0), base_y)
            for i, v in enumerate(run):
                cr.line_to(px(i), py(v))
            cr.line_to(px(len(run) - 1), base_y)
            cr.close_path()
            cr.set_source_rgba(*color, 0.18)
            cr.fill()
            cr.move_to(px(0), py(run[0]))
            for i, v in enumerate(run[1:], 1):
                cr.line_to(px(i), py(v))
            cr.set_source_rgb(*color)
            cr.set_line_width(2.2)
            cr.stroke()
            cr.arc(px(len(run) - 1), py(run[-1]), 4, 0, 2 * math.pi)
            cr.fill()

        label_every = max(1, math.ceil(n / max(plot_w / 34, 1)))
        cr.set_font_size(10)
        for i, b in enumerate(self.buckets):
            if i % label_every and not b.current:
                continue
            ext = cr.text_extents(b.label)
            cr.set_source_rgba(*fg, 0.9 if b.current else 0.55)
            cr.move_to(px(i) - ext.width / 2, base_y + 16)
            cr.show_text(b.label)


class Ring(_Canvas):
    """Progress ring with a centred figure; turns orange at 80%, red past 100%."""

    def __init__(self, size=132, thickness=12):
        super().__init__()
        self.fraction = 0.0
        self.center = ""
        self.caption = ""
        self.thickness = thickness
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def set_value(self, fraction, center, caption=""):
        self.fraction = max(0.0, fraction)
        self.center = center
        self.caption = caption
        self.queue_draw()

    def _draw(self, _area, cr, w, h):
        fg = _fg(self)
        r = min(w, h) / 2 - self.thickness / 2 - 1
        cx, cy = w / 2, h / 2
        cr.set_line_width(self.thickness)
        cr.set_line_cap(1)          # round
        cr.set_source_rgba(*fg, 0.10)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        f = self.fraction
        color = OK if f < 0.8 else WARN if f < 1.0 else DANGER
        if f > 0:
            cr.set_source_rgb(*color)
            start = -math.pi / 2
            cr.arc(cx, cy, r, start, start + 2 * math.pi * min(f, 1.0))
            cr.stroke()
        if f > 1.0:
            # second lap in a darker shade shows how far past the limit
            cr.set_source_rgba(0.55, 0.05, 0.08, 0.9)
            start = -math.pi / 2
            cr.arc(cx, cy, r, start, start + 2 * math.pi * min(f - 1.0, 1.0))
            cr.stroke()
        cr.set_source_rgb(*fg)
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(r * 0.42)
        ext = cr.text_extents(self.center)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + (ext.height / 2 if not self.caption else 2))
        cr.show_text(self.center)
        if self.caption:
            cr.select_font_face("Sans")
            cr.set_font_size(max(r * 0.2, 10))
            ext = cr.text_extents(self.caption)
            cr.set_source_rgba(*fg, 0.6)
            cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + r * 0.40)
            cr.show_text(self.caption)


class Donut(_Canvas):
    """Share of the period's per-app bytes. Segments follow APP_COLORS order."""

    def __init__(self, size=180):
        super().__init__()
        self.values = []
        self.center = ""
        self.caption = ""
        self.hover = None
        self.labels = []
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)

    def set_data(self, values, labels, center, caption):
        self.values = values
        self.labels = labels
        self.center = center
        self.caption = caption
        self.queue_draw()

    def _segment_at(self, x, y):
        w, h = self.get_width(), self.get_height()
        dx, dy = x - w / 2, y - h / 2
        dist = math.hypot(dx, dy)
        r = min(w, h) / 2
        if dist < r * 0.58 or dist > r:
            return None
        angle = (math.atan2(dy, dx) + math.pi / 2) % (2 * math.pi)
        total = sum(self.values) or 1
        acc = 0.0
        for i, v in enumerate(self.values):
            acc += v / total * 2 * math.pi
            if angle <= acc:
                return i
        return None

    def _tooltip(self, _w, x, y, _kbd, tooltip):
        i = self._segment_at(x, y)
        if i is None:
            return False
        total = sum(self.values) or 1
        tooltip.set_markup(f"<b>{GLib.markup_escape_text(self.labels[i])}</b>\n"
                           f"{human(self.values[i])} · {self.values[i] / total * 100:.1f}%")
        return True

    def _draw(self, _area, cr, w, h):
        fg = _fg(self)
        r = min(w, h) / 2 - 2
        thick = r * 0.30
        cx, cy = w / 2, h / 2
        total = sum(self.values)
        cr.set_line_width(thick)
        if not total:
            cr.set_source_rgba(*fg, 0.10)
            cr.arc(cx, cy, r - thick / 2, 0, 2 * math.pi)
            cr.stroke()
        else:
            angle = -math.pi / 2
            gap = 0.018 if len([v for v in self.values if v]) > 1 else 0
            for i, v in enumerate(self.values):
                sweep = v / total * 2 * math.pi
                if sweep <= 0:
                    continue
                color = OTHER if i == len(self.values) - 1 and self.labels[i] == "Other" \
                    else app_color(i)
                cr.set_source_rgb(*color)
                cr.arc(cx, cy, r - thick / 2, angle + gap / 2,
                       angle + max(sweep - gap / 2, gap / 2 + 0.001))
                cr.stroke()
                angle += sweep
        cr.set_source_rgb(*fg)
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(r * 0.2)
        ext = cr.text_extents(self.center)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + 2)
        cr.show_text(self.center)
        cr.select_font_face("Sans")
        cr.set_font_size(max(r * 0.11, 9))
        ext = cr.text_extents(self.caption)
        cr.set_source_rgba(*fg, 0.6)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + r * 0.24)
        cr.show_text(self.caption)


class Sparkline(_Canvas):
    """Rolling download/upload rate, newest at the right."""

    def __init__(self, height=56):
        super().__init__()
        self.samples = []
        self.capacity = 60
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)

    def set_samples(self, samples, capacity):
        self.samples = list(samples)
        self.capacity = capacity
        self.queue_draw()

    def _tooltip(self, _w, x, _y, _kbd, tooltip):
        if not self.samples:
            return False
        w = self.get_width()
        step = w / max(self.capacity - 1, 1)
        offset = self.capacity - len(self.samples)
        i = round(x / step) - offset
        if not 0 <= i < len(self.samples):
            return False
        rx, tx = self.samples[i]
        ago = len(self.samples) - 1 - i
        tooltip.set_text(f"{ago}s ago\n↓ {human_rate(rx)}\n↑ {human_rate(tx)}")
        return True

    def _draw(self, _area, cr, w, h):
        fg = _fg(self)
        cr.set_source_rgba(*fg, 0.08)
        cr.set_line_width(1)
        cr.move_to(0, h - 0.5)
        cr.line_to(w, h - 0.5)
        cr.stroke()
        if len(self.samples) < 2:
            return
        peak = max(max(max(s) for s in self.samples), 16 * 1024)
        step = w / max(self.capacity - 1, 1)
        offset = self.capacity - len(self.samples)
        for idx, color in ((0, DOWN), (1, UP)):
            pts = [((offset + i) * step, h - 2 - (h - 6) * s[idx] / peak)
                   for i, s in enumerate(self.samples)]
            cr.move_to(pts[0][0], h)
            for x, y in pts:
                cr.line_to(x, y)
            cr.line_to(pts[-1][0], h)
            cr.close_path()
            cr.set_source_rgba(*color, 0.16)
            cr.fill()
            cr.move_to(*pts[0])
            for x, y in pts[1:]:
                cr.line_to(x, y)
            cr.set_source_rgb(*color)
            cr.set_line_width(1.6)
            cr.stroke()


class Heatmap(_Canvas):
    """Calendar grid of daily totals, weeks as columns, Monday on top."""

    CELL, GAP = 14, 3

    def __init__(self):
        super().__init__()
        self.days = {}          # date -> bytes
        self.start = None
        self.weeks = 0
        self.limit = 0
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)
        self.set_halign(Gtk.Align.CENTER)

    def set_data(self, days, start, weeks, limit):
        self.days = days
        self.start = start
        self.weeks = weeks
        self.limit = limit
        size = self.CELL + self.GAP
        self.set_content_width(28 + weeks * size)
        self.set_content_height(7 * size + 18)
        self.queue_draw()

    def _cell_date(self, x, y):
        from datetime import timedelta
        size = self.CELL + self.GAP
        col, row = int((x - 28) // size), int((y - 18) // size)
        if not (0 <= col < self.weeks and 0 <= row < 7) or not self.start:
            return None
        return self.start + timedelta(days=col * 7 + row)

    def _tooltip(self, _w, x, y, _kbd, tooltip):
        d = self._cell_date(x, y)
        if d is None:
            return False
        tooltip.set_markup(f"<b>{d.strftime('%a %d %b %Y')}</b>\n{human(self.days.get(d, 0))}")
        return True

    def _draw(self, _area, cr, w, h):
        from datetime import date, timedelta
        if not self.start:
            return
        fg = _fg(self)
        size = self.CELL + self.GAP
        peak = max(list(self.days.values()) + [1])
        today = date.today()
        cr.select_font_face("Sans")
        cr.set_font_size(9)
        for row, name in ((0, "Mon"), (2, "Wed"), (4, "Fri")):
            cr.set_source_rgba(*fg, 0.5)
            cr.move_to(0, 18 + row * size + self.CELL - 3)
            cr.show_text(name)
        last_month, last_label_col = None, -3
        for col in range(self.weeks):
            for row in range(7):
                d = self.start + timedelta(days=col * 7 + row)
                if d > today:
                    continue
                if row == 0 and d.month != last_month and col - last_label_col >= 3:
                    last_month, last_label_col = d.month, col
                    cr.set_source_rgba(*fg, 0.5)
                    cr.move_to(28 + col * size, 11)
                    cr.show_text(d.strftime("%b"))
                v = self.days.get(d, 0)
                x, y = 28 + col * size, 18 + row * size
                if v <= 0:
                    cr.set_source_rgba(*fg, 0.07)
                elif self.limit and v >= self.limit:
                    cr.set_source_rgb(*DANGER)
                else:
                    # square-root scale so light days stay distinguishable
                    level = 0.22 + 0.78 * math.sqrt(v / peak)
                    cr.set_source_rgba(*DOWN, level)
                _rounded_rect(cr, x, y, self.CELL, self.CELL, 3)
                cr.fill()
                if d == today:
                    cr.set_source_rgba(*fg, 0.8)
                    cr.set_line_width(1.2)
                    _rounded_rect(cr, x + 0.6, y + 0.6, self.CELL - 1.2, self.CELL - 1.2, 3)
                    cr.stroke()


def _rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


class ShareBar(_Canvas):
    """Thin horizontal bar used in list rows."""

    def __init__(self, width=120):
        super().__init__()
        self.fraction = 0.0
        self.color = DOWN
        self.set_content_width(width)
        self.set_content_height(6)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def set_value(self, fraction, color):
        self.fraction = max(0.0, min(1.0, fraction))
        self.color = color
        self.queue_draw()

    def _draw(self, _area, cr, w, h):
        fg = _fg(self)
        cr.set_source_rgba(*fg, 0.10)
        _rounded_rect(cr, 0, 0, w, h, h / 2)
        cr.fill()
        if self.fraction > 0:
            cr.set_source_rgb(*self.color)
            _rounded_rect(cr, 0, 0, max(w * self.fraction, h), h, h / 2)
            cr.fill()
