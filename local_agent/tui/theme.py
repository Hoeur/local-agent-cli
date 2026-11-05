"""Design tokens for the terminal UI: themes, glyph sets, motion levels and saved preferences.

Everything here is plain data so it can be imported (and tested) without starting the UI.
"""
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Each palette drives both the Textual theme and the hand-drawn effects (logo, orb, glow line).
# "gradient" runs left to right across the logo and the header glow.
PALETTES = {
    "neon": {
        "label": "Neon Core",
        "primary": "#22d3ee", "secondary": "#818cf8", "accent": "#c084fc",
        "background": "#07090f", "surface": "#0c111b", "panel": "#111827",
        "foreground": "#dbe7f5", "success": "#34d399", "warning": "#fbbf24", "error": "#fb7185",
        "gradient": ["#22d3ee", "#3b82f6", "#8b5cf6", "#c084fc"],
        "user": "#a78bfa", "assistant": "#22d3ee", "muted": "#5b6b82",
    },
    "aurora": {
        "label": "Aurora",
        "primary": "#5eead4", "secondary": "#60a5fa", "accent": "#e879f9",
        "background": "#060b10", "surface": "#0b1520", "panel": "#0f1d2b",
        "foreground": "#d8f3ee", "success": "#4ade80", "warning": "#facc15", "error": "#f87171",
        "gradient": ["#5eead4", "#38bdf8", "#818cf8", "#e879f9"],
        "user": "#e879f9", "assistant": "#5eead4", "muted": "#557083",
    },
    "synthwave": {
        "label": "Synthwave",
        "primary": "#f472b6", "secondary": "#a855f7", "accent": "#38bdf8",
        "background": "#0d0618", "surface": "#140a24", "panel": "#1c0f31",
        "foreground": "#f3e8ff", "success": "#4ade80", "warning": "#fb923c", "error": "#f43f5e",
        "gradient": ["#f472b6", "#c026d3", "#7c3aed", "#38bdf8"],
        "user": "#38bdf8", "assistant": "#f472b6", "muted": "#74608f",
    },
    "contrast": {
        "label": "High Contrast",
        "primary": "#00e5ff", "secondary": "#7aa2ff", "accent": "#ff8cff",
        "background": "#000000", "surface": "#000000", "panel": "#101010",
        "foreground": "#ffffff", "success": "#00ff87", "warning": "#ffd000", "error": "#ff5f5f",
        "gradient": ["#00e5ff", "#7aa2ff", "#ff8cff", "#ffffff"],
        "user": "#ff8cff", "assistant": "#00e5ff", "muted": "#b0b0b0",
    },
    "daylight": {
        "label": "Daylight",
        "primary": "#0369a1", "secondary": "#4f46e5", "accent": "#9333ea",
        "background": "#f6f8fb", "surface": "#ffffff", "panel": "#e9eef5",
        "foreground": "#0f172a", "success": "#15803d", "warning": "#b45309", "error": "#be123c",
        "gradient": ["#0891b2", "#2563eb", "#7c3aed", "#a21caf"],
        "user": "#7c3aed", "assistant": "#0369a1", "muted": "#64748b", "light": True,
    },
}
DEFAULT_THEME = "neon"

MOTION_LEVELS = ("full", "reduced", "off")
# Frame-rate cap for the shared animation clock at each motion level. Most elements pace themselves
# at 15 fps or less; only the hologram uses the full 30 while the agent is active.
MOTION_FPS = {"full": 30, "reduced": 4, "off": 0}
TEXTUAL_LEVEL = {"full": "full", "reduced": "basic", "off": "none"}


@dataclass(frozen=True)
class Glyphs:
    orb: str
    spinner: tuple
    ok: str
    fail: str
    warn: str
    user: str
    agent: str
    tool: str
    branch: str
    session: str
    folder: str
    dot_on: str
    dot_off: str
    bar_full: str
    bar_empty: str
    sep: str
    cursor: str
    arrow: str
    stars: str


UNICODE = Glyphs(
    orb="◉", spinner=("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
    ok="✓", fail="✗", warn="▲", user="❯", agent="◆", tool="⚙", branch="⎇", session="◇", folder="▣",
    dot_on="●", dot_off="○", bar_full="▰", bar_empty="▱", sep="│", cursor="▍", arrow="›",
    stars="·∙✦⋆+",
)
ASCII = Glyphs(
    orb="@", spinner=("|", "/", "-", "\\"),
    ok="+", fail="x", warn="!", user=">", agent="*", tool="#", branch="git:", session="#", folder="dir:",
    dot_on="*", dot_off="o", bar_full="#", bar_empty="-", sep="|", cursor="_", arrow=">",
    stars=".:+*",
)


def detect_glyphs(environ=None, platform=None):
    """Use Unicode glyphs unless the terminal is likely a legacy console without braille or symbols."""
    environ = os.environ if environ is None else environ
    choice = environ.get("AGENT_UI_GLYPHS", "").lower()
    if choice in {"ascii", "unicode"}:
        return ASCII if choice == "ascii" else UNICODE
    platform = sys.platform if platform is None else platform
    if platform == "win32" and not any(
        environ.get(name) for name in ("WT_SESSION", "TERM_PROGRAM", "ConEmuANSI", "ALACRITTY_LOG", "WEZTERM_PANE")
    ):
        return ASCII  # Classic conhost fonts lack braille and many symbols.
    return UNICODE


def detect_motion(environ=None, saved=None):
    """AGENT_UI_MOTION wins, then TEXTUAL_ANIMATIONS, then the saved choice. With no choice anywhere,
    SSH sessions get reduced motion (animation output adds up on slow links) and others get full."""
    environ = os.environ if environ is None else environ
    choice = environ.get("AGENT_UI_MOTION", "").lower()
    if choice in MOTION_LEVELS:
        return choice
    textual = environ.get("TEXTUAL_ANIMATIONS", "").lower()
    if textual == "none":
        return "off"
    if textual == "basic":
        return "reduced"
    if saved in MOTION_LEVELS:
        return saved
    return "reduced" if environ.get("SSH_CONNECTION") or environ.get("SSH_TTY") else "full"


# motion None means "not chosen yet": see detect_motion.
DEFAULT_PREFS = {"theme": DEFAULT_THEME, "motion": None, "sidebar": True, "sidebar_width": 34,
                 "hologram": True, "hologram_mode": "auto", "hologram_width": 30, "hologram_animate": True}
HOLOGRAM_MODES = ("auto", "braille", "ascii", "image")
HOLOGRAM_WIDTHS = (18, 48)


def load_prefs(directory: Path):
    """UI preferences live beside config.json so `setup` never overwrites them."""
    prefs = dict(DEFAULT_PREFS)
    try:
        saved = json.loads((directory / "ui.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return prefs
    if isinstance(saved, dict):
        if saved.get("theme") in PALETTES:
            prefs["theme"] = saved["theme"]
        if saved.get("motion") in MOTION_LEVELS:
            prefs["motion"] = saved["motion"]
        if isinstance(saved.get("sidebar"), bool):
            prefs["sidebar"] = saved["sidebar"]
        if isinstance(saved.get("sidebar_width"), int) and 24 <= saved["sidebar_width"] <= 60:
            prefs["sidebar_width"] = saved["sidebar_width"]
        for key in ("hologram", "hologram_animate"):
            if isinstance(saved.get(key), bool):
                prefs[key] = saved[key]
        if saved.get("hologram_mode") in HOLOGRAM_MODES:
            prefs["hologram_mode"] = saved["hologram_mode"]
        width = saved.get("hologram_width")
        if isinstance(width, int) and not isinstance(width, bool) and \
                HOLOGRAM_WIDTHS[0] <= width <= HOLOGRAM_WIDTHS[1]:
            prefs["hologram_width"] = width
    return prefs


def save_prefs(directory: Path, prefs):
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({key: prefs.get(key, DEFAULT_PREFS[key]) for key in DEFAULT_PREFS}, stream, indent=2)
        try:
            os.replace(temporary, directory / "ui.json")
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        pass  # Preferences are a convenience; never fail the UI over them.
