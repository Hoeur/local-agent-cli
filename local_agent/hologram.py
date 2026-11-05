"""Deterministic, dependency-free 3D scene and terminal capability policy."""
import math
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path


GRAPHICS_DIR = Path(__file__).with_name("graphics")


@dataclass(frozen=True)
class Capabilities:
    interactive: bool
    unicode: bool
    color: bool
    remote: bool
    reduced_motion: bool
    bun: str | None

    @classmethod
    def detect(cls, environ=None, stream=None):
        env = os.environ if environ is None else environ
        out = sys.stdout if stream is None else stream
        interactive = bool(out.isatty()) and env.get("TERM") != "dumb"
        try:
            "⠿─●".encode(out.encoding or "ascii")
            unicode = True
        except (UnicodeError, LookupError):
            unicode = False
        local_bun = GRAPHICS_DIR / "node_modules" / "bun" / "bin" / ("bun.exe" if os.name == "nt" else "bun")
        bun = shutil.which("bun") or (str(local_bun) if local_bun.is_file() else None)
        installed = (GRAPHICS_DIR / "node_modules" / "@opentui" / "three" / "package.json").is_file()
        return cls(interactive, unicode, "NO_COLOR" not in env and interactive,
                   bool(env.get("SSH_CONNECTION") or env.get("SSH_TTY") or env.get("TMUX") or env.get("STY")),
                   env.get("AGENT_NO_ANIMATION", "").lower() in {"1", "true", "yes"},
                   bun if installed else None)


class HologramScene:
    """Time-based motion; only real runtime events change the semantic state."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.state = "idle"
        self.changed = clock()
        self.last = self.changed
        self.phase = 0.0
        self.speed = 0.2
        self.energy = 0.25
        self.activity = 0.0
        self.audio = 0.0
        self.audio_at = -math.inf
        self.progress = (0, 0)
        self.yaw = 0.0
        self.pitch = 0.25

    def event(self, event):
        kind = event.kind
        if kind == "token":
            self.activity = min(1.0, self.activity + 0.1 + len(event.text) / 160)
            kind = "generating"
        elif kind in {"tool_start", "tool_end"}:
            self.progress = (event.completed, event.total)
            kind = "tool"
        elif kind == "tool_error":
            kind = "error"
        elif kind == "audio":
            if event.level is None or not math.isfinite(event.level):
                return
            self.audio = min(1.0, max(0.0, event.level))
            self.audio_at = self.clock()
            kind = "voice"
        if kind in {"idle", "thinking", "generating", "tool", "success", "error", "voice"}:
            if kind != self.state:
                self.changed = self.clock()
            self.state = kind

    def advance(self, animate=True):
        now = self.clock()
        dt = min(0.1, max(0.0, now - self.last))
        self.last = now
        if self.state in {"success", "error"} and now - self.changed > 1.2:
            self.state = "idle"
        if self.state == "voice" and now - self.audio_at > 0.4:
            self.audio *= math.exp(-dt * 10)
            if now - self.audio_at > 1.2:
                self.state = "idle"
        target = {"idle": (0.2, 0.25), "thinking": (1.3, 0.9), "generating": (0.7, 0.6 + self.activity * 0.4),
                  "tool": (1.0, 0.8), "voice": (0.5 + self.audio, 0.4 + self.audio * 0.6),
                  "success": (0.4, 1.0), "error": (0.3, 0.8)}[self.state]
        mix = 1 - math.exp(-dt * 6)
        self.speed += (target[0] - self.speed) * mix
        self.energy += (target[1] - self.energy) * mix
        self.activity *= math.exp(-dt * 2.5)
        if animate:
            self.phase += dt * self.speed

    @property
    def color(self):
        return (70, 255, 161) if self.state == "success" else (255, 75, 110) if self.state == "error" else (62, 218, 255)

    def snapshot(self):
        return dict(state=self.state, phase=self.phase, energy=self.energy, activity=self.activity,
                    audio=self.audio, yaw=self.yaw, pitch=self.pitch,
                    pulse=min(1.0, max(0.0, (self.clock() - self.changed) / 1.2)), color=self.color)

    def points(self):
        p = self.phase
        points = []
        # Latitude and longitude curves, sampled independently of terminal size.
        for latitude in range(-3, 4):
            a = latitude * math.pi / 8
            for i in range(100):
                b = i * math.tau / 100
                points.append((math.cos(a) * math.cos(b), math.sin(a), math.cos(a) * math.sin(b), 0.58))
        for meridian in range(10):
            a = meridian * math.pi / 5
            for i in range(72):
                b = i * math.tau / 72
                points.append((math.cos(b) * math.cos(a), math.sin(b), math.cos(b) * math.sin(a), 0.55))
        rotated = [(*self.rotate(x, y, z, p + self.yaw, self.pitch), light) for x, y, z, light in points]
        for ring in range(3):
            for i in range(150):
                a = i * math.tau / 150
                radius = 1.22 + ring * 0.14
                x, y, z = self.rotate(radius * math.cos(a), 0, radius * math.sin(a),
                                      p * (0.4 + ring * 0.3) + self.yaw, 0.4 + ring * 0.75 + p * (ring - 1) * 0.3)
                rotated.append((x, y, z, 0.8 if (i + int(p * 25)) % 24 < 18 else 0.25))
        for i in range(26):
            a = i * 2.39996 + p * (1 + i % 3 * 0.4)
            radius = 1.65 - (0.65 * (0.5 + 0.5 * math.sin(p * 3 + i)) if self.state == "thinking" else 0)
            for tail in range(5):
                angle = a - tail * 0.035
                x, y, z = self.rotate(radius * math.cos(angle), 0.25 * math.sin(angle * 3 + i), radius * math.sin(angle),
                                      self.yaw + i, self.pitch + i * 0.6)
                rotated.append((x, y, z, (1 - tail / 6) * 0.95))
        core = 0.13 + self.energy * 0.07 + math.sin(p * 4) * 0.025
        for i in range(100):
            a = i * 2.39996
            r = core * math.sqrt(i / 100)
            rotated.append((r * math.cos(a), r * math.sin(a), 0, 1.0))
        if self.state in {"generating", "success", "error"}:
            age = (self.clock() - self.changed) / 1.2
            radius = 0.25 + (p * 1.8 % 1.5 if self.state == "generating" else min(1, age) * 1.7)
            for i in range(120):
                a = i * math.tau / 120
                rotated.append((radius * math.cos(a), radius * math.sin(a), 0, 0.7))
        if self.state == "voice":
            for i in range(160):
                x = (i / 159 - 0.5) * 3.3
                rotated.append((x, math.sin(x * 12 + p * 8) * self.audio * 0.4, 0, 1))
        return rotated

    @staticmethod
    def rotate(x, y, z, yaw, pitch):
        x, z = x * math.cos(yaw) + z * math.sin(yaw), z * math.cos(yaw) - x * math.sin(yaw)
        return x, y * math.cos(pitch) - z * math.sin(pitch), z * math.cos(pitch) + y * math.sin(pitch)

    def project(self, width, height):
        scale = min(width, height) / 4.4
        for x, y, z, light in self.points():
            perspective = 4.5 / (4.5 - z)
            if self.state == "error":
                x += math.sin(y * 35 + self.phase * 40) * 0.035
            px, py = round(width / 2 + x * perspective * scale), round(height / 2 - y * perspective * scale)
            if 0 <= px < width and 0 <= py < height:
                yield px, py, min(1.0, light * (0.55 + (z + 1.8) / 7) * (0.65 + self.energy * 0.4))

    def cells(self, width, height, unicode=True):
        """Return (character, intensity bucket) cells, no terminal escape codes."""
        width, height = max(1, min(120, width)), max(1, min(40, height))
        masks = [[0] * width for _ in range(height)]
        lights = [[0.0] * width for _ in range(height)]
        sx, sy = (2, 4) if unicode else (1, 2)
        bits = ((1, 8), (2, 16), (4, 32), (64, 128))
        for x, y, light in self.project(width * sx, height * sy):
            col, row = x // sx, y // sy
            masks[row][col] |= bits[y % sy][x % sx] if unicode else 1
            lights[row][col] = max(lights[row][col], light)
        return [[(chr(0x2800 + masks[y][x]) if unicode and masks[y][x] else
                  " .:+*#@"[min(6, max(1, round(lights[y][x] * 6)))] if masks[y][x] else " ",
                  min(5, int(lights[y][x] * 6))) for x in range(width)] for y in range(height)]

    def image(self, width, height):
        from PIL import Image, ImageDraw, ImageFilter, ImageChops
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        for x, y, light in self.project(width, height):
            color = tuple(int(c * light) for c in self.color)
            draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=color)
        return ImageChops.add(canvas, canvas.filter(ImageFilter.GaussianBlur(3)))
