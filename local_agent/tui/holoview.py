"""Textual widgets for the holographic core.

    HologramView   the reusable renderer widget (Braille or ASCII), drawn through Textual's line API
    HologramImage  the same scene as a bitmap through a terminal image protocol (optional extra)
    HoloHud        a few lines of live telemetry under the side-panel hologram
    HoloPanel      the compact hologram shown beside the conversation

Every view shares the app's Director (state), Scene (geometry) and ViewControl (user rotation), so
the welcome hologram and the side panel always agree. Views animate only while they are shown and
something calls for motion; otherwise they draw one static frame when the state changes.
"""
import math
import time

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.containers import Vertical
from textual.geometry import Region
from textual.strip import Strip
from textual.widget import Widget

from . import effects, hologram
from .widgets import Animated

_STYLES = {}


def style_for(fg, bg):
    key = (fg, bg)
    style = _STYLES.get(key)
    if style is None:
        if len(_STYLES) > 4096:
            _STYLES.clear()
        style = _STYLES[key] = Style(color=fg, bgcolor=bg)
    return style


class ViewControl:
    """User rotation and zoom, shared by every hologram view. Drags leave a little inertia."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.yaw = 0.0
        self.pitch = 0.0
        self.zoom = 1.0
        self.velocity = 0.0
        self.last = None

    def drag(self, dx, dy, now=None):
        now = time.monotonic() if now is None else now
        self.yaw += dx
        self.pitch = max(-1.1, min(1.1, self.pitch + dy))
        elapsed = max(0.016, now - getattr(self, "dragged", now - 0.05))
        self.dragged = now
        self.velocity = max(-3.0, min(3.0, dx / elapsed))  # Released drags coast, then settle.

    def nudge(self, dyaw, dpitch):
        self.yaw += dyaw
        self.pitch = max(-1.1, min(1.1, self.pitch + dpitch))

    def zoom_by(self, factor):
        self.zoom = max(0.6, min(1.5, self.zoom * factor))

    def step(self, t):
        dt = 0.0 if self.last is None else max(0.0, min(0.1, t - self.last))
        self.last = t
        if abs(self.velocity) > 0.01:
            self.yaw += self.velocity * dt
            self.velocity *= math.exp(-dt * 3.5)
        else:
            self.velocity = 0.0

    def coasting(self):
        return abs(self.velocity) > 0.01

    def view(self):
        return self.yaw, self.pitch, self.zoom


class HoloBase(Animated):
    """Shared scheduling for both renderers: when to animate, at what rate, and suspension."""
    max_fps = 30

    def setup(self):
        self.add_class("hologram")  # Queries match on this; a mixin is not a CSS type.
        self.governor = hologram.Governor(self.max_fps)
        self.drawn_version = None
        self.last_cost = 0.0

    def wants_motion(self):
        app = self.app
        if not app.hologram_animate or app.motion == "off":
            return False
        if app.motion == "reduced":  # Reduced motion: animate only as a status indicator.
            return app.holo.active()
        return True

    def ceiling(self):
        app = self.app
        if app.motion == "reduced":
            return 4
        if not app.focused_terminal:
            return 5  # The terminal window is in the background.
        if app.holo.active() or app.holo_control.coasting():
            return self.max_fps
        # Idle motion is slow (under one dot per frame at 10 fps), so a lower rate looks the same
        # and halves the terminal traffic of a screen nobody is interacting with.
        return min(self.max_fps, 10)

    @property
    def suspended(self):
        """True when this view or any container above it is hidden. Worked out from the widget tree
        each time rather than from Show/Hide events, which can arrive after a quick hide-and-show."""
        if not self.is_attached:
            return True
        return any(isinstance(node, Widget) and not node.display for node in self.ancestors_with_self)

    def sync(self):
        """Re-decide between animating and a static frame. Cheap; call after any state change."""
        if not self.is_attached:
            return
        if self.suspended:
            self.stop_animation()
            return
        if self.wants_motion():
            self.governor.set_ceiling(self.ceiling())
            if not self.animating:
                self.governor.restart()
            self.start_animation()
        else:
            self.stop_animation()
            if self.drawn_version != self.app.holo.version:
                self.draw(static=True)

    def frame_interval(self):
        return self.governor.interval()

    def frame(self, t):
        if self.suspended or not self.wants_motion():
            self.sync()
            return
        self.governor.set_ceiling(self.ceiling())
        self.draw(t)

    def on_show(self):
        self.drawn_version = None
        self.sync()

    def on_hide(self):
        self.sync()

    def on_resize(self):
        if not self.animating:
            self.drawn_version = None
            self.sync()

    # Pointer and wheel control. The view never takes focus, so typing stays in the composer.
    def on_mouse_down(self, event):
        self.capture_mouse()
        self.dragging = True
        self.drag_from = (event.screen_x, event.screen_y)
        event.stop()

    def on_mouse_move(self, event):
        if getattr(self, "dragging", False):
            # Positions, not event deltas: some terminals and drivers leave the deltas at zero.
            x, y = event.screen_x, event.screen_y
            dx, dy = x - self.drag_from[0], y - self.drag_from[1]
            self.drag_from = (x, y)
            if dx or dy:
                self.app.holo_control.drag(dx * 0.09, dy * 0.18)
                self.app.holo.version += 1
                self.app.refresh_hologram()

    def on_mouse_up(self, event):
        if getattr(self, "dragging", False):
            self.dragging = False
            self.release_mouse()
            self.app.refresh_hologram()

    def on_mouse_scroll_up(self, event):
        self.app.holo_control.zoom_by(1.08)
        self.app.refresh_hologram()
        event.stop()

    def on_mouse_scroll_down(self, event):
        self.app.holo_control.zoom_by(1 / 1.08)
        self.app.refresh_hologram()
        event.stop()

    def scene_frame(self, t, static, density):
        app = self.app
        app.holo.step(t, app.palette, motion=not static)
        app.holo_control.step(t)
        detail = 1.0 if self.governor.divisor <= 2 else 0.8  # Thin the geometry if the rate drops.
        return app.holo_scene.frame(app.holo, t, app.palette, density, app.holo_control.view(), detail)

    def stats(self):
        return {"fps": self.governor.measured if self.animating else 0.0, "target": self.governor.fps,
                "cost_ms": self.last_cost * 1000, "animating": self.animating}


class HologramView(HoloBase, Widget):
    """The reusable hologram renderer (Braille or ASCII). Size it with CSS; it fills its region."""
    DEFAULT_CSS = "HologramView { width: 30; height: 14; }"

    def __init__(self, renderer=None, **kwargs):
        super().__init__(**kwargs)
        self.renderer = renderer
        self.strips = []
        self.rows = []
        self.rows_width = 0
        self.rows_base = None
        self.setup()

    def on_mount(self):
        self.sync()

    @property
    def mode(self):
        mode = self.renderer or self.app.hologram_renderer
        return mode if mode in hologram.RASTERIZERS else "braille" if self.app.glyphs.orb != "@" else "ascii"

    def draw(self, t=None, static=False):
        width, height = self.size.width, self.size.height
        if width < 2 or height < 2:
            return
        started = time.perf_counter()
        app = self.app
        t = app.clock.now() if t is None else t
        mode = self.mode
        density = (min(width * 2, height * 4) if mode == "braille" else min(width, height * 2)) / 2 / \
            hologram.EXTENT
        frame = self.scene_frame(t, static, density)
        rows = hologram.RASTERIZERS[mode](frame, width, height)
        base = self.rich_style  # Line-API segments must carry the widget's own background.
        resized = len(self.strips) != height or self.rows_width != width or self.rows_base != base
        changed = []
        for y, row in enumerate(rows):
            if resized or row != self.rows[y]:
                changed.append(y)
        self.strips = [Strip([Segment(text, base + style_for(fg, bg)) for text, fg, bg in row], width)
                       for row in rows] if resized else [
            Strip([Segment(text, base + style_for(fg, bg)) for text, fg, bg in row], width)
            if y in changed else self.strips[y] for y, row in enumerate(rows)]
        self.rows, self.rows_width, self.rows_base = rows, width, base
        self.drawn_version = app.holo.version
        # Only rows that changed are sent to the terminal; the empty space around the sphere and
        # anything that happens to hold still cost nothing.
        if resized or len(changed) > height // 2:
            self.refresh()
        elif changed:
            self.refresh(*(Region(0, y, width, 1) for y in changed))
        self.last_cost = time.perf_counter() - started
        if not static:
            self.governor.record(self.last_cost, time.monotonic())

    def render_line(self, y):
        if y < len(self.strips):
            return self.strips[y]
        return Strip.blank(self.size.width, self.rich_style)


class HologramImage(HoloBase, Widget):
    """The scene as a bitmap through Sixel or the Kitty graphics protocol, via textual-image.

    Each frame re-encodes and re-sends an image, which costs far more than text, so it is capped at
    12 fps and the governor lowers that further on slow machines or links."""
    DEFAULT_CSS = "HologramImage { width: 30; height: 14; } HologramImage > * { width: 100%; height: 100%; }"
    max_fps = 12

    def __init__(self, image_class, cell_size, **kwargs):
        super().__init__(**kwargs)
        self.image_class = image_class
        self.cell_size = cell_size
        self.picture = None
        self.setup()

    def compose(self):
        self.picture = self.image_class()
        yield self.picture

    def on_mount(self):
        self.sync()

    def draw(self, t=None, static=False):
        width, height = self.size.width, self.size.height
        if width < 2 or height < 2 or self.picture is None:
            return
        started = time.perf_counter()
        app = self.app
        t = app.clock.now() if t is None else t
        cell_w, cell_h = self.cell_size
        px_w, px_h = width * cell_w, height * cell_h
        shrink = min(1.0, 220 / max(px_w, px_h))  # Bound the per-frame encoding cost.
        px_w, px_h = max(8, int(px_w * shrink)), max(8, int(px_h * shrink))
        frame = self.scene_frame(t, static, min(px_w, px_h) / 2 / hologram.EXTENT * 0.8)
        try:
            self.picture.image = hologram.render_image(frame, px_w, px_h)
        except Exception as error:  # noqa: BLE001 - graphics must never take the chat down
            app.hologram_failed(f"image renderer failed: {error}")
            return
        self.drawn_version = app.holo.version
        self.last_cost = time.perf_counter() - started
        if not static:
            self.governor.record(self.last_cost, time.monotonic())


def make_view(app, **kwargs):
    """The best hologram widget for this terminal: image protocol when probed and chosen, else text."""
    if app.hologram_renderer == "image" and app.image_support and app.image_support.get("widget"):
        return HologramImage(app.image_support["widget"], app.image_support["cell_size"], **kwargs)
    return HologramView(**kwargs)


class HoloHud(Animated, Widget):
    """State, energy and renderer telemetry beside the compact hologram. Repaints 4 times a second
    while the core is active and otherwise only when something changes."""
    DEFAULT_CSS = "HoloHud { height: 4; }"

    def __init__(self, view_id, **kwargs):
        super().__init__(**kwargs)
        self.view_id = view_id

    def on_mount(self):
        self.start_animation()

    def frame_interval(self):
        return 0.25 if self.app.holo.active() and self.app.motion != "off" else 3600.0

    def render(self):
        app = self.app
        palette, glyphs = app.palette, app.glyphs
        director = app.holo
        width = self.size.width
        muted = Style(color=palette["muted"])
        state = director.effective_state()
        flash, _ = director.flash_age()
        color = {"thinking": palette["primary"], "generating": palette["accent"], "tool": palette["secondary"],
                 "waiting": palette["warning"], "voice": palette["accent"]}.get(state, palette["primary"])
        if flash == "success":
            color = palette["success"]
        elif flash in {"error", "glitch"}:
            color = palette["error"]
        head = Text(no_wrap=True, overflow="ellipsis")
        head.append_text(effects.gradient_text("AI CORE", palette["gradient"], 0, True, False))
        head.append(f" {glyphs.sep} ", muted)
        head.append(director.label().upper(), Style(color=color, bold=True))
        energy = Text(no_wrap=True)
        energy.append("energy ", muted)
        cells = max(4, min(14, width - 9))
        filled = round(director.params["energy"] * cells)
        for index in range(cells):
            lit = index < filled
            color = effects.gradient_at(palette["gradient"], index / cells) if lit else palette["muted"]
            energy.append(glyphs.bar_full if lit else glyphs.bar_empty, Style(color=color))
        detail = Text(no_wrap=True, overflow="ellipsis")
        if director.state == "tool" and director.tools_total:
            detail.append(f"{director.detail or 'tool'} ", Style(color=palette["secondary"]))
            detail.append(f"{director.tools_done}/{director.tools_total} ", muted)
            detail.append(f"{director.progress():.0%}", Style(color=palette["foreground"]))
        elif director.detail:
            detail.append(director.detail, muted)
        stats = Text(no_wrap=True, overflow="ellipsis")
        try:
            view = app.query_one(f"#{self.view_id}", HoloBase)
        except Exception:  # noqa: BLE001 - the view may be mid-remount
            view = None
        stats.append(f"{app.hologram_renderer}", Style(color=palette["muted"]))
        if view is not None and view.animating:
            stats.append(f" {glyphs.sep} {view.governor.fps:.0f} fps", muted)
        elif not app.hologram_animate or app.motion == "off":
            stats.append(f" {glyphs.sep} still", muted)
        lines = [line for line in (head, energy, detail, stats) if line.plain]
        for line in lines:
            line.truncate(width)
        return Text("\n").join(lines)


class HoloPanel(Vertical):
    """The compact hologram docked beside the conversation."""

    def compose(self):
        yield make_view(self.app, id="side-holo")
        yield HoloHud("side-holo", id="holo-hud")
