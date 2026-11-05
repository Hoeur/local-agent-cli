"""The holographic AI core: a 3D wireframe sphere, energy rings, orbiting particles and a pulsing core.

This module is pure Python with no Textual imports, so every piece is testable without a terminal.

    Director    the animation state machine. Agent events set its state; it eases every visual
                parameter toward that state's profile, so transitions are smooth, and it plays
                one-shot effects (success pulse, error distortion, streamed-text waves).
    Scene       projects the 3D model for one moment into screen-space samples (perspective,
                depth cues, back-face dimming). It knows nothing about cells or pixels.
    rasterizers turn a Scene frame into rows of styled runs:
                  braille  2x4 dots per cell (square sub-pixels), the enhanced 3D renderer
                  ascii    one sample per cell with pure ASCII characters, the universal fallback
                  image    a Pillow bitmap for terminal image protocols (Sixel / Kitty), optional
    Governor    adapts the frame rate to measured frame cost and event-loop lag.
"""
import math

from . import effects

# ── States ────────────────────────────────────────────────────────────────────────────────

STATES = ("idle", "thinking", "generating", "tool", "waiting", "voice")
ACTIVE_STATES = frozenset({"thinking", "generating", "tool", "waiting", "voice"})
FLASHES = {"success": 1.1, "error": 1.0, "glitch": 0.4}  # One-shot effects and their duration (s).

# Visual targets per state. The Director eases toward them, so changing state never jumps.
#   spin      sphere rotation (rad/s)           energy    brightness, core size and glow (0..1)
#   converge  particles pulled into the core    flow      ring rotation and dash speed
#   orbit     particle orbit speed              agitation particle jitter
#   ripple    wireframe surface waves           arc       visibility of the tool progress arc
PROFILES = {
    "idle":       {"spin": 0.30, "energy": 0.40, "converge": 0.00, "flow": 0.45, "orbit": 0.30,
                   "agitation": 0.00, "ripple": 0.00, "arc": 0.0},
    "thinking":   {"spin": 1.30, "energy": 0.85, "converge": 0.80, "flow": 1.50, "orbit": 1.25,
                   "agitation": 0.20, "ripple": 0.00, "arc": 0.0},
    "generating": {"spin": 0.70, "energy": 0.90, "converge": 0.10, "flow": 1.00, "orbit": 0.75,
                   "agitation": 0.10, "ripple": 1.00, "arc": 0.0},
    "tool":       {"spin": 0.55, "energy": 0.75, "converge": 0.35, "flow": 2.80, "orbit": 0.60,
                   "agitation": 0.05, "ripple": 0.00, "arc": 1.0},
    "waiting":    {"spin": 0.15, "energy": 0.55, "converge": 0.00, "flow": 0.25, "orbit": 0.20,
                   "agitation": 0.00, "ripple": 0.00, "arc": 0.0},
    "voice":      {"spin": 0.45, "energy": 0.70, "converge": 0.00, "flow": 0.80, "orbit": 0.50,
                   "agitation": 0.00, "ripple": 0.00, "arc": 0.0},
}
# Each state tints the whole hologram toward one palette color by some amount.
TINTS = {"idle": ("primary", 0.0), "thinking": ("#ffffff", 0.14), "generating": ("accent", 0.22),
         "tool": ("secondary", 0.28), "waiting": ("warning", 0.50), "voice": ("accent", 0.30)}
FLASH_TINT = {"success": "success", "error": "error", "glitch": "error"}
LABELS = {"idle": "standing by", "thinking": "thinking", "generating": "generating", "tool": "running tool",
          "waiting": "awaiting approval", "voice": "listening", "success": "complete", "error": "error"}

EASE = 3.2          # Parameter easing rate (1/s): about 0.3 s to cover most of a transition.
WAVE_LIFE = 1.5     # Seconds an output wave takes to expand and fade.
AUDIO_STALE = 0.6   # Voice mode ends this long after the last audio level arrives.


def resolve(palette, key):
    return key if key.startswith("#") else palette[key]


class Director:
    """Owns animation state. Call the event methods from agent events and step(t) once per frame.

    Every method takes an optional time so tests can drive it; otherwise the injected clock is used.
    """

    def __init__(self, clock):
        self.clock = clock
        self.state = "idle"
        self.detail = ""
        self.params = dict(PROFILES["idle"])
        self.phase = {"spin": 0.0, "flow": 0.0, "orbit": 0.0, "breath": 0.0}
        self.tint_rgb = None
        self.tint_amount = 0.0
        self.flash_kind = None
        self.flash_start = -100.0
        self.waves = []                 # [birth time, strength]
        self.tools_total = 0
        self.tools_done = 0
        self.tool_started = 0.0
        self.audio = 0.0
        self.audio_time = -100.0
        self.last = None
        self.version = 0                # Bumped on every event so static views know to redraw.

    # ── Events ──
    def set_state(self, state, detail=""):
        if state not in PROFILES:
            raise ValueError(f"Unknown hologram state {state!r}")
        if state != self.state or detail != self.detail:
            if state == "tool" and self.state != "tool":
                self.tool_started = self.clock()
            if state == "idle":
                self.tools_total = self.tools_done = 0
            self.state, self.detail = state, detail
            self.version += 1

    def flash(self, kind):
        if kind not in FLASHES:
            raise ValueError(f"Unknown hologram flash {kind!r}")
        self.flash_kind, self.flash_start = kind, self.clock()
        self.version += 1

    def wave(self, strength=1.0):
        """One pulse of streamed output. Bursts closer than 120 ms merge, so the view never floods."""
        now = self.clock()
        strength = max(0.15, min(1.0, strength))
        if self.waves and now - self.waves[-1][0] < 0.12:
            self.waves[-1][1] = min(1.4, self.waves[-1][1] + strength * 0.3)
        else:
            self.waves.append([now, strength])
            del self.waves[:-6]

    def tool_round(self, total):
        self.tools_total, self.tools_done = max(0, total), 0
        self.version += 1

    def tool_started_event(self):
        self.tool_started = self.clock()

    def tool_finished(self, error=False):
        self.tools_done = min(self.tools_total or self.tools_done + 1, self.tools_done + 1)
        if error:
            self.flash("glitch")
        self.version += 1

    def set_audio_level(self, level):
        """Feed real audio input levels (0..1). Voice mode lasts while they keep arriving."""
        self.audio = max(0.0, min(1.0, float(level)))
        self.audio_time = self.clock()
        self.version += 1

    # ── Queries ──
    def voice_live(self, now=None):
        now = self.clock() if now is None else now
        return now - self.audio_time < AUDIO_STALE

    def effective_state(self, now=None):
        if self.state == "idle" and self.voice_live(now):
            return "voice"
        return self.state

    def flash_age(self, now=None):
        """(kind, 0..1 progress) of the running flash, or (None, 1)."""
        now = self.clock() if now is None else now
        if self.flash_kind is None:
            return None, 1.0
        progress = (now - self.flash_start) / FLASHES[self.flash_kind]
        if progress >= 1:
            self.flash_kind = None
            return None, 1.0
        return self.flash_kind, max(0.0, progress)

    def active(self, now=None):
        """True while anything is happening: a busy agent, a live flash, waves or voice input."""
        now = self.clock() if now is None else now
        return (self.effective_state(now) in ACTIVE_STATES or self.flash_age(now)[0] is not None
                or any(now - birth < WAVE_LIFE for birth, _ in self.waves))

    def progress(self, now=None):
        """Tool progress 0..1: finished calls plus an easing estimate for the running one."""
        if not self.tools_total:
            return 0.0
        now = self.clock() if now is None else now
        running = 0.0
        if self.tools_done < self.tools_total:
            running = 0.85 * (1 - math.exp(-(now - self.tool_started) / 1.2))
        return min(1.0, (self.tools_done + running) / self.tools_total)

    def label(self, now=None):
        kind, _ = self.flash_age(now)
        if kind in {"success", "error"}:
            return LABELS[kind]
        return LABELS[self.effective_state(now)]

    # ── Integration ──
    def step(self, t, palette, motion=True):
        """Advance to time t. Without motion, jump straight to the target (static frames)."""
        dt = 0.0 if self.last is None else max(0.0, min(0.1, t - self.last))
        self.last = t
        state = self.effective_state(t)
        target = PROFILES[state]
        ease = 1.0 if not motion else 1 - math.exp(-dt * EASE)
        for key, goal in target.items():
            self.params[key] += (goal - self.params[key]) * ease
        if motion:
            p = self.params
            self.phase["spin"] += p["spin"] * dt
            self.phase["flow"] += p["flow"] * dt
            self.phase["orbit"] += p["orbit"] * dt
            self.phase["breath"] += dt * (0.35 + p["energy"] * 0.55)
        key, amount = TINTS[state]
        kind, progress = self.flash_age(t)
        if kind is not None and motion:
            flash_amount = (1 - progress) ** 1.4 * (0.9 if kind != "glitch" else 0.55)
            if flash_amount > amount:
                key, amount = FLASH_TINT[kind], flash_amount
        goal_rgb = effects.hex_rgb(resolve(palette, key))
        if self.tint_rgb is None or not motion:
            self.tint_rgb = goal_rgb
        else:
            fast = 1 - math.exp(-dt * 9)
            self.tint_rgb = tuple(a + (b - a) * fast for a, b in zip(self.tint_rgb, goal_rgb))
        self.tint_amount += (amount - self.tint_amount) * (1 - math.exp(-dt * 9) if motion else 1.0)
        self.waves = [wave for wave in self.waves if t - wave[0] < WAVE_LIFE]


# ── Scene ─────────────────────────────────────────────────────────────────────────────────

WIRE, RING, SPARK, CORE, FX = range(5)       # Color roles.
LINE, HEAD, TRAIL = range(3)                 # Sample kinds (ASCII picks characters by kind).
EXTENT = 1.9                                 # Scene half-size that must fit the canvas.
CAMERA = 5.0                                 # Camera distance for the perspective divide.
BASE_PITCH = 0.36                            # Look slightly down on the sphere.
# Wireframe detail by canvas density (sub-pixels per unit): small views get fewer lines so the
# sphere still reads as a wireframe instead of a filled ball.
WIREFRAMES = (  # (minimum density, latitude circles in radians, great circles through the poles)
    (24, (-0.66, -0.33, 0.0, 0.33, 0.66), 6),
    (17, (-0.6, -0.3, 0.0, 0.3, 0.6), 4),
    (0, (-0.55, 0.0, 0.55), 3),
)
RINGS = (  # radius, tilt about x, precession speed, dashes, duty cycle, phase offset
    (1.26, 1.20, 1.0, 14, 0.60, 0.0),
    (1.46, 1.48, -0.7, 22, 0.42, 2.1),
    (1.66, 0.34, 0.45, 9, 0.74, 4.0),
)
SPARSE = 0.42   # Below this depth (the far side), lines are drawn dotted.


def rotation(yaw, pitch):
    return math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)


def _seeded(index, salt):
    value = (index * 2654435761 + salt * 40503) & 0xFFFFFFFF
    value ^= value >> 15
    value = (value * 2246822519) & 0xFFFFFFFF
    return (value ^ (value >> 13)) / 0xFFFFFFFF


class Frame:
    """Screen-space description of one moment. Coordinates are scene units, y up, origin centered."""
    __slots__ = ("points", "core", "glow", "distort", "scan", "flicker", "colors", "hot", "background",
                 "light", "t")

    def __init__(self):
        self.points = []   # (x, y, depth 0..1, intensity 0..1, role, kind)
        self.core = (0.2, 1.0)
        self.glow = 0.5
        self.distort = 0.0
        self.scan = 0.5
        self.flicker = 1.0
        self.colors = {}
        self.hot = "#ffffff"
        self.background = "#000000"
        self.light = False
        self.t = 0.0


class Scene:
    def __init__(self):
        self._wire_cache = {}

    def wire_points(self, density):
        """Model-space sphere wireframe, sampled for the given pixels-per-unit density."""
        bucket = max(4, int(density))
        cached = self._wire_cache.get(bucket)
        if cached is not None:
            return cached
        _, latitudes, meridians = next(spec for spec in WIREFRAMES if bucket >= spec[0])
        points = []
        for lat in latitudes:
            radius = math.cos(lat)
            count = max(16, min(600, int(2 * math.pi * radius * bucket * 1.35)))
            y = math.sin(lat)
            for step in range(count):
                angle = 2 * math.pi * step / count
                points.append((math.cos(angle) * radius, y, math.sin(angle) * radius, lat, angle, step % 3))
        count = max(16, min(600, int(math.pi * bucket * 1.35)))
        for meridian in range(meridians * 2):
            lon = math.pi * meridian / meridians
            for step in range(1, count):  # Skip the poles; every meridian would repeat them.
                lat = -math.pi / 2 + math.pi * step / count
                radius = math.cos(lat)
                points.append((math.cos(lon) * radius, math.sin(lat), math.sin(lon) * radius, lat, lon, step % 3))
        if len(self._wire_cache) > 8:
            self._wire_cache.clear()
        self._wire_cache[bucket] = points
        return points

    def frame(self, director, t, palette, density, view=(0.0, 0.0, 1.0), detail=1.0):
        """Project everything for time t. view is (user yaw, user pitch, zoom)."""
        p, ph = director.params, director.phase
        user_yaw, user_pitch, zoom = view
        pitch = BASE_PITCH + user_pitch
        energy = p["energy"]
        frame = Frame()
        frame.t = t
        light = bool(palette.get("light"))
        frame.light, frame.background = light, palette["background"]
        tint = effects.rgb_hex(director.tint_rgb) if director.tint_rgb else palette["primary"]
        amount = director.tint_amount
        base = {WIRE: palette["gradient"][1], RING: palette["primary"], SPARK: palette["primary"],
                CORE: palette["primary"], FX: palette["accent"]}
        kind, progress = director.flash_age(t)
        if kind is not None:
            base[FX] = palette[FLASH_TINT[kind]]
        frame.colors = {role: effects.blend(color, tint, amount) for role, color in base.items()}
        frame.hot = "#ffffff" if not light else effects.blend(palette["foreground"], tint, 0.3)
        breath = 0.5 + 0.5 * math.sin(ph["breath"] * 2 * math.pi)
        audio = director.audio if director.voice_live(t) else 0.0
        out = frame.points
        add = out.append
        level = 0.55 + 0.45 * energy

        # Sphere wireframe: rotate, ripple, project; the far side is dimmer than the near side.
        cy, sy, cp, sp = rotation(ph["spin"] + user_yaw, pitch)
        ripple = p["ripple"]
        wave_front = [(t - birth) / WAVE_LIFE for birth, _ in director.waves]
        scan_y = math.sin(ph["breath"] * 1.7) * 0.9  # A bright scanning latitude sweeps the sphere.
        for x, y, z, lat, lon, parity in self.wire_points(density * detail):
            radius = 1.0
            if ripple > 0.01:
                radius += 0.045 * ripple * math.sin(lat * 7 - t * 6.5)
            if audio:
                radius += audio * 0.13 * math.sin(lon * 6 + t * 9) * math.cos(lat)
            if radius != 1.0:
                x, y, z = x * radius, y * radius, z * radius
            x1 = cy * x + sy * z
            z1 = cy * z - sy * x
            y2 = cp * y - sp * z1
            z2 = sp * y + cp * z1
            depth = (z2 + 1) * 0.5
            if depth < SPARSE and parity:
                continue  # Dotted far side: depth reads at a glance, even without color.
            f = zoom / (1 - z2 / CAMERA)
            intensity = level * (0.14 + 0.7 * depth * depth)
            band = abs(y - scan_y)
            if band < 0.09:
                intensity += (0.09 - band) * 6 * (0.4 + energy)
            add((x1 * f, y2 * f, depth, intensity, WIRE, LINE))

        # Energy rings: each precesses on its own axis, with dashes flowing along it.
        arc = p["arc"]
        tool_progress = director.progress(t)
        flow = ph["flow"]
        for index, (ring_r, tilt, speed, dashes, duty, offset) in enumerate(RINGS):
            ct, st = math.cos(tilt), math.sin(tilt)
            ry = offset + flow * speed * 0.55 + user_yaw
            rcy, rsy, rcp, rsp = rotation(ry, pitch)
            count = max(24, min(720, int(2 * math.pi * ring_r * density * detail * 1.25)))
            dash_phase = flow * speed * 1.6
            ring_level = 0.55 + 0.45 * energy
            for step in range(count):
                u = step / count
                angle = u * 2 * math.pi
                lx, lz = math.cos(angle) * ring_r, math.sin(angle) * ring_r
                ly = lz * st                      # Tilt about x.
                lz = lz * ct
                x1 = rcy * lx + rsy * lz
                z1 = rcy * lz - rsy * lx
                y2 = rcp * ly - rsp * z1
                z2 = rsp * ly + rcp * z1
                f = zoom / (1 - z2 / CAMERA)
                depth = (z2 / ring_r + 1) * 0.5
                cycle = (u * dashes + dash_phase) % 1.0
                lit = cycle < duty
                if not lit and step % 3:
                    continue  # Between energy dashes the ring is a faint dotted rail.
                intensity = ring_level * (0.3 + 0.7 * depth) * (1.0 if lit else 0.22)
                role = RING
                if index == 0 and arc > 0.02:  # The inner ring doubles as the tool progress arc.
                    sweep = (u - 0.25) % 1.0
                    head = (flow * 0.9) % 1.0
                    if sweep <= tool_progress:
                        intensity = max(intensity, arc * (0.65 + 0.35 * depth))
                        role = FX
                    elif abs(sweep - head) < 0.05:
                        intensity = max(intensity, arc * 0.8)
                        role = FX
                if intensity > 0.04:
                    add((x1 * f, y2 * f, depth, intensity, role, LINE))

        # Particles on inclined orbits, with fading trails; thinking pulls them into the core.
        converge, agitation = p["converge"], p["agitation"]
        orbit = ph["orbit"]
        count = max(12, min(44, int(density * 1.5 * detail)))
        trail_len = 4 if density * detail >= 14 else 3
        core_r = (0.16 + 0.07 * energy + 0.035 * breath + 0.09 * audio) * zoom
        for index in range(count):
            u1, u2, u3, u4, u5 = (_seeded(index, salt) for salt in range(1, 6))
            orbit_r = 1.12 + 0.7 * u1
            inclination = (u2 - 0.5) * 2.4
            node = u3 * 2 * math.pi
            speed = (0.55 + 0.9 * u4) * (1 if u5 > 0.25 else -1)
            ci, si = math.cos(inclination), math.sin(inclination)
            pcy, psy, pcp, psp = rotation(node + user_yaw, pitch)
            for trail in range(trail_len):
                angle = u5 * 6.283 + orbit * speed * (1 + converge) - trail * 0.085 * (1 + converge) * \
                    (1 if speed > 0 else -1)
                pull = converge * (0.55 + 0.45 * math.sin(angle * 2 + index))
                radius = orbit_r + (0.28 + 0.22 * u1 - orbit_r) * pull
                if agitation:
                    radius += agitation * 0.07 * math.sin(t * 13 + index * 7)
                if audio:
                    radius += audio * 0.25 * math.sin(t * 11 + index * 3)
                lx, lz = math.cos(angle) * radius, math.sin(angle) * radius
                ly, lz = lz * si, lz * ci
                x1 = pcy * lx + psy * lz
                z1 = pcy * lz - psy * lx
                y2 = pcp * ly - psp * z1
                z2 = psp * ly + pcp * z1
                f = zoom / (1 - z2 / CAMERA)
                sx, syy = x1 * f, y2 * f
                if z2 < 0 and sx * sx + syy * syy < core_r * core_r * 1.4:
                    continue  # Hidden behind the core.
                depth = max(0.0, min(1.0, (z2 / 2 + 1) * 0.5))
                intensity = (0.95 if trail == 0 else 0.62 ** trail) * (0.45 + 0.55 * depth) * level
                add((sx, syy, depth, intensity, SPARK, HEAD if trail == 0 else TRAIL))

        # Output waves expanding from the core while text streams, and the success/error pulse.
        for (birth, strength), front in zip(director.waves, wave_front):
            radius = (0.3 + front * 1.75) * zoom
            intensity = strength * (1 - front) ** 1.3 * 0.95
            if intensity > 0.05:
                self._circle(add, radius, intensity, RING, density * detail, t * 2 + birth, 0.7)
        if kind in {"success", "error"}:
            radius = (0.9 + progress * 1.3) * zoom
            self._circle(add, radius, (1 - progress) ** 1.2, FX, density * detail, t, 1.0)
            self._circle(add, radius * 0.82, (1 - progress) ** 1.6 * 0.6, FX, density * detail, t + 1, 0.5)

        frame.core = (core_r, 0.8 + 0.2 * breath)
        frame.glow = 0.35 + 0.65 * energy + (0.4 * (1 - progress) if kind == "success" else 0.0)
        frame.distort = 0.0
        if kind == "error":
            frame.distort = (1 - progress) ** 1.2
        elif kind == "glitch":
            frame.distort = 0.45 * (1 - progress)
        frame.scan = (t * 0.32) % 1.5 - 0.25
        frame.flicker = 0.965 + 0.035 * math.sin(t * 31.0) * math.sin(t * 17.3)
        return frame

    @staticmethod
    def _circle(add, radius, intensity, role, density, seed, duty):
        count = max(20, min(720, int(2 * math.pi * radius * density * 1.25)))
        for step in range(count):
            u = step / count
            if duty < 1.0 and (u * 18 + seed * 0.2) % 1.0 > duty:
                continue
            angle = u * 2 * math.pi
            add((math.cos(angle) * radius, math.sin(angle) * radius, 0.6, intensity, role, LINE))


# ── Rasterizers ───────────────────────────────────────────────────────────────────────────

LEVELS = 8        # Brightness steps per color role. Fewer steps mean longer same-style runs.
GLOW_LEVELS = 4   # Background glow steps behind the core.
_RAMPS = {}


def ramp(color, background, hot, light):
    """LEVELS colors from the background through the role color to the hot highlight."""
    key = (color, background, hot)
    cached = _RAMPS.get(key)
    if cached is None:
        if len(_RAMPS) > 256:
            _RAMPS.clear()
        cached = []
        for level in range(LEVELS):
            i = level / (LEVELS - 1)
            if i < 0.62:
                cached.append(effects.blend(background, color, 0.18 + i / 0.62 * 0.82))
            else:
                cached.append(effects.blend(color, hot, (i - 0.62) / 0.38 * (0.75 if not light else 0.55)))
        _RAMPS[key] = cached
    return cached


# Braille dot bits by (row, column) inside a cell.
BRAILLE = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))
ASCII_RAMP = " .,:-=+*#%@"


def _runs(cells_row):
    """Merge (char, fg, bg) cells into [text, fg, bg] runs.

    Terminals receive a color escape per run, which costs far more bytes than the characters, so
    runs are made as long as possible: a blank cell looks the same in any foreground color, so it
    joins its neighbor's run whenever the backgrounds match."""
    runs = []
    last = None
    for char, fg, bg in cells_row:
        if last is not None and last[2] == bg:
            if last[1] == fg or char == " ":
                last[0] += char
                continue
            if not last[0].strip():  # A blank run adopts the next color instead of ending.
                last[0] += char
                last[1] = fg
                continue
        last = [char, fg, bg]
        runs.append(last)
    return runs


def _prepare(frame):
    ramps = {role: ramp(color, frame.background, frame.hot, frame.light) for role, color in frame.colors.items()}
    glow_ramp = [effects.blend(frame.background, frame.colors[CORE], 0.2 * level / (GLOW_LEVELS - 1))
                 for level in range(GLOW_LEVELS)]
    return ramps, glow_ramp


def _distort_offset(row, frame, unit):
    if not frame.distort:
        return 0
    # Glitch bands: a few rows tear sideways, the rest shimmer slightly.
    band = int(row / (3 * unit) + frame.t * 23) % 5 == 0
    amount = frame.distort * (4.0 if band else 0.8) * unit
    return int(round(math.sin(row * 0.9 + frame.t * 41) * amount))


def rasterize_braille(frame, cols, rows):
    """Enhanced renderer: each cell holds a 2x4 grid of Braille dots, i.e. square sub-pixels."""
    if cols < 2 or rows < 2:
        return [[[" " * max(cols, 0), None, None]] for _ in range(max(rows, 0))]
    pw, ph = cols * 2, rows * 4
    scale = min(pw, ph) / 2 / EXTENT
    cx, cy = pw / 2, ph / 2
    size = cols * rows
    mask = [0] * size
    value = [0.0] * size
    total = [0.0] * size
    role = [0] * size
    distort = frame.distort
    offsets = {} if distort else None
    for x, y, depth, intensity, kind_role, _ in frame.points:
        px = int(cx + x * scale)
        py = int(cy - y * scale)
        if distort:
            shift = offsets.get(py)
            if shift is None:
                shift = offsets[py] = _distort_offset(py, frame, 1)
            px += shift
        if 0 <= px < pw and 0 <= py < ph:
            index = (py >> 2) * cols + (px >> 1)
            mask[index] |= BRAILLE[py & 3][px & 1]
            total[index] += intensity
            if intensity > value[index]:
                value[index] = intensity
                role[index] = kind_role
    # The core: a filled, shimmering disk with a hot center.
    core_r, core_i = frame.core
    radius = core_r * scale
    reach = int(radius) + 1
    shimmer = int(frame.t * 18)
    for dy in range(-reach, reach + 1):
        py = int(cy) + dy
        if not 0 <= py < ph:
            continue
        for dx in range(-reach, reach + 1):
            px = int(cx) + dx
            d = math.sqrt(dx * dx + dy * dy) / max(radius, 0.5)
            if d > 1 or not 0 <= px < pw:
                continue
            if d > 0.7 and (dx * 7 + dy * 13 + shimmer) % 3 == 0:
                continue  # Sparkling edge.
            index = (py >> 2) * cols + (px >> 1)
            mask[index] |= BRAILLE[py & 3][px & 1]
            intensity = core_i * (1.05 - 0.4 * d * d)
            total[index] += intensity
            if intensity >= value[index]:
                value[index] = intensity
                role[index] = CORE
    return _compose(frame, cols, rows, mask, value, total, role, scale, cx, cy, 2, 4, braille=True)


def rasterize_ascii(frame, cols, rows):
    """Universal fallback: one sample per cell, ASCII characters only. Cells are about twice as
    tall as they are wide, so vertical positions are halved."""
    if cols < 2 or rows < 2:
        return [[[" " * max(cols, 0), None, None]] for _ in range(max(rows, 0))]
    scale = min(cols, rows * 2) / 2 / EXTENT
    cx, cy = cols / 2, rows / 2
    size = cols * rows
    value = [0.0] * size
    total = [0.0] * size
    role = [0] * size
    kinds = [LINE] * size
    distort = frame.distort
    for x, y, depth, intensity, kind_role, kind in frame.points:
        px = int(cx + x * scale)
        py = int(cy - y * scale / 2)
        if distort:
            px += _distort_offset(py, frame, 0.5)
        if 0 <= px < cols and 0 <= py < rows:
            index = py * cols + px
            total[index] += intensity
            if intensity > value[index] or (kind == HEAD and kinds[index] != HEAD):
                value[index] = max(value[index], intensity)
                role[index] = kind_role
                kinds[index] = kind
    core_r, core_i = frame.core
    radius = core_r * scale
    for py in range(int(cy - radius / 2) - 1, int(cy + radius / 2) + 2):
        for px in range(int(cx - radius) - 1, int(cx + radius) + 2):
            if not (0 <= px < cols and 0 <= py < rows):
                continue
            dx, dy = px + 0.5 - cx, (py + 0.5 - cy) * 2
            d = math.sqrt(dx * dx + dy * dy) / max(radius, 0.5)
            if d <= 1:
                index = py * cols + px
                value[index] = max(value[index], core_i * (1.05 - 0.4 * d * d))
                role[index] = CORE
                kinds[index] = HEAD if d < 0.45 else LINE
    return _compose(frame, cols, rows, kinds, value, total, role, scale, cx, cy, 1, 0.5, braille=False)


def _compose(frame, cols, rows, marks, value, total, role, scale, cx, cy, unit_x, unit_y, braille):
    ramps, glow_ramp = _prepare(frame)
    glow = frame.glow
    scan, flicker = frame.scan, frame.flicker
    top = LEVELS - 1
    glow_top = GLOW_LEVELS - 1
    out = []
    for row in range(rows):
        # Scanline: a soft bright band drifts down the hologram.
        band = abs(row / max(rows - 1, 1) - scan)
        boost = flicker * (1.0 + max(0.0, 0.12 - band) * 2.2)
        gy = (cy - (row + 0.5) * (4 if braille else 1)) / scale * (1 if braille else 2)
        cells = []
        for col in range(cols):
            index = row * cols + col
            gx = ((col + 0.5) * unit_x - cx) / scale
            d2 = gx * gx + gy * gy
            halo = min(1.0, glow) * math.exp(-d2 * 4.5)
            level = min(glow_top, int(halo * glow_top + 0.35))
            bg = glow_ramp[level] if level else None
            v = value[index]
            if braille:
                m = marks[index]
                if not m:
                    cells.append((" ", None, bg))
                    continue
                char = chr(0x2800 + m)
            else:
                if v <= 0.04:
                    cells.append((" ", None, bg))
                    continue
                kind = marks[index]
                if role[index] == CORE:
                    char = "@" if kind == HEAD else "%"
                elif kind == HEAD:
                    char = "*"
                elif kind == TRAIL:
                    char = "." if v < 0.45 else "+"
                else:
                    char = ASCII_RAMP[1 + min(len(ASCII_RAMP) - 2, int(v * (len(ASCII_RAMP) - 2)))]
            lit = min(1.0, (v * 0.8 + min(0.3, (total[index] - v) * 0.07)) * boost)
            cells.append((char, ramps[role[index]][min(top, int(lit * top + 0.5))], bg))
        out.append(_runs(cells))
    return out


def render_image(frame, width, height):
    """High-fidelity bitmap for terminal image protocols. Needs Pillow; raises ImportError without it.

    Light is drawn on black, bloomed with a blur, then screened over the theme background, so the
    background color stays exact."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    light = Image.new("RGB", (width, height), "#000000")
    draw = ImageDraw.Draw(light)
    scale = min(width, height) / 2 / EXTENT
    cx, cy = width / 2, height / 2
    black = "#000000"
    ramps = {role: ramp(color, black, frame.hot, frame.light) for role, color in frame.colors.items()}
    top = LEVELS - 1
    point, ellipse = draw.point, draw.ellipse
    for x, y, depth, intensity, kind_role, kind in frame.points:
        px, py = cx + x * scale, cy - y * scale
        if frame.distort:
            px += _distort_offset(int(py), frame, 2)
        color = ramps[kind_role][min(top, int(intensity * frame.flicker * top + 0.5))]
        if kind == HEAD:
            ellipse((px - 1.2, py - 1.2, px + 1.2, py + 1.2), fill=color)
        else:
            point((px, py), fill=color)
    core_r, core_i = frame.core
    radius = core_r * scale
    for step in range(5, 0, -1):
        r = radius * step / 5
        level = min(top, int(core_i * (1.1 - 0.5 * (step / 5) ** 2) * top))
        ellipse((cx - r, cy - r, cx + r, cy + r), fill=ramps[CORE][level])
    bloom = light.filter(ImageFilter.BoxBlur(max(1, width // 90)))
    halo = Image.new("RGB", (width, height), black)
    ImageDraw.Draw(halo).ellipse((cx - radius * 3.5, cy - radius * 3.5, cx + radius * 3.5, cy + radius * 3.5),
                                 fill=effects.blend(black, frame.colors[CORE], 0.3 * min(1.0, frame.glow)))
    halo = halo.filter(ImageFilter.GaussianBlur(radius * 1.4 + 1))
    glow = ImageChops.screen(ImageChops.screen(light, bloom), halo)
    if frame.light:
        return ImageChops.subtract(Image.new("RGB", (width, height), frame.background), glow)
    return ImageChops.screen(glow, Image.new("RGB", (width, height), frame.background))


RASTERIZERS = {"braille": rasterize_braille, "ascii": rasterize_ascii}


# ── Frame-rate governor ───────────────────────────────────────────────────────────────────

class Governor:
    """Chooses frames per second from what the machine and terminal can sustain.

    The shared clock ticks at most 30 times a second, so the achievable rates are 30 divided by a
    whole number. Frames that cost more than a third of their slot, or arrive late because the
    event loop is busy (a long streamed reply re-rendering Markdown, say), step the rate down at
    once; two calm seconds step it back up toward the ceiling. Raising the ceiling (the agent just
    became active) jumps straight to it: the governor backs off within three frames if it must."""
    DIVISORS = (1, 2, 3, 4, 5, 6, 10)
    BASE = 30

    def __init__(self, ceiling=30):
        self.divisor = 1
        self.ceiling_divisor = 1
        self.cost = 0.0
        self.lateness = 0.0
        self.strain = 0
        self.calm = 0
        self.last = None
        self.frames = 0
        self.window_start = None
        self.measured = 0.0
        self.set_ceiling(ceiling)

    @property
    def fps(self):
        return self.BASE / self.divisor

    def interval(self):
        return self.divisor / self.BASE

    def set_ceiling(self, fps):
        fps = max(1.0, min(self.BASE, fps))
        ceiling = min((d for d in self.DIVISORS if self.BASE / d <= fps + 1e-6), default=self.DIVISORS[-1])
        if ceiling < getattr(self, "ceiling_divisor", ceiling):
            self.divisor = ceiling  # Raised: be optimistic.
            self.strain = self.calm = 0
        self.ceiling_divisor = ceiling
        self.divisor = max(self.divisor, self.ceiling_divisor)

    def restart(self):
        self.last = None
        self.lateness = 0.0
        self.window_start = None
        self.frames = 0

    def record(self, cost, now):
        self.cost = cost if not self.cost else self.cost * 0.8 + cost * 0.2
        slot = self.interval()
        late = 0.0 if self.last is None else max(0.0, (now - self.last) - slot)
        self.lateness = self.lateness * 0.7 + late * 0.3  # One missed timer tick is not a trend.
        self.last = now
        if self.window_start is None:
            self.window_start, self.frames = now, 0
        self.frames += 1
        if now - self.window_start >= 1.0:
            self.measured = self.frames / (now - self.window_start)
            self.window_start, self.frames = now, 0
        if self.cost > slot / 3 or self.lateness > slot * 0.6:
            self.strain += 1
            self.calm = 0
        else:
            self.strain = max(0, self.strain - 1)
            if self.cost < slot / 8 and self.lateness < slot * 0.25:
                self.calm += 1
        if self.strain >= 3:
            self.step(+1)
        elif self.calm >= self.fps * 2 and self.divisor > self.ceiling_divisor:
            self.step(-1)

    def step(self, direction):
        index = self.DIVISORS.index(self.divisor) if self.divisor in self.DIVISORS else 0
        index = max(self.DIVISORS.index(self.ceiling_divisor), min(len(self.DIVISORS) - 1, index + direction))
        self.divisor = self.DIVISORS[index]
        self.strain = self.calm = 0


# ── Capabilities ──────────────────────────────────────────────────────────────────────────

MODES = ("auto", "braille", "ascii", "image")
SIZES = {"small": 22, "medium": 30, "large": 40}


def choose_renderer(requested, environ, unicode_glyphs, image_support):
    """Pick the renderer that will actually work here. Returns (mode, reason).

    image_support is None when the terminal was not probed, else {"protocol": "sixel"|"tgp"|None,
    "error": str|None} from the start-up probe (image protocols can only be queried before Textual
    owns the terminal)."""
    override = environ.get("AGENT_HOLOGRAM", "").lower()
    if override in MODES:
        requested = override
    term = environ.get("TERM", "").lower()
    text_ok = unicode_glyphs and term not in {"linux", "dumb", "vt100", "vt220"}
    text_mode = "braille" if text_ok else "ascii"
    if requested == "ascii":
        return "ascii", "ASCII renderer selected"
    if requested == "image":
        if image_support is None:
            reason = "image mode needs a start-up terminal probe; restart to enable it"
        elif image_support.get("protocol"):
            return "image", f"{image_support['protocol'].upper()} graphics detected"
        else:
            reason = image_support.get("error") or "this terminal reported no Sixel or Kitty graphics support"
        return text_mode, f"{reason} — using {text_mode}"
    if requested == "braille" and not text_ok:
        return "ascii", "this terminal lacks Braille glyphs — using ascii"
    if text_ok:
        return "braille", "Braille sub-cell 3D renderer"
    return "ascii", "Unicode glyphs unavailable — using ascii"
