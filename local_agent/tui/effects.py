"""Terminal-native visual effects: gradients, the block logo, a shaded 3D orb and a starfield.

These are pure functions of (size, time, palette) that return Rich Text, so each frame is cheap
to compute and the effects stay testable without a terminal. Textual downgrades the true-color
output automatically on 256- and 16-color terminals.
"""
import math

from rich.style import Style
from rich.text import Text


def hex_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def rgb_hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(channel))):02x}" for channel in rgb)


def blend(first, second, amount):
    a, b = hex_rgb(first), hex_rgb(second)
    return rgb_hex(tuple(x + (y - x) * amount for x, y in zip(a, b)))


def gradient_at(stops, position, cyclic=False):
    """Color at position 0..1 along the stops. Cyclic gradients wrap back to the first stop."""
    stops = list(stops) + ([stops[0]] if cyclic else [])
    position = position % 1.0 if cyclic else max(0.0, min(1.0, position))
    scaled = position * (len(stops) - 1)
    index = min(int(scaled), len(stops) - 2)
    return blend(stops[index], stops[index + 1], scaled - index)


def gradient_text(text, stops, phase=0.0, bold=False, cyclic=True, span=None):
    """Color each character along the gradient. phase shifts it for a flowing shimmer."""
    result = Text()
    span = span or max(len(text), 1)
    for index, char in enumerate(text):
        result.append(char, Style(color=gradient_at(stops, index / span + phase, cyclic), bold=bold))
    return result


# A 5x6 bitmap font, packed two rows per cell with half blocks so the logo stays three rows tall.
FONT = {
    "L": ["#....", "#....", "#....", "#....", "#....", "#####"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", ".###."],
    "C": [".####", "#....", "#....", "#....", "#....", ".####"],
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#"],
    "G": [".####", "#....", "#....", "#..##", "#...#", ".###."],
    "E": ["#####", "#....", "####.", "#....", "#....", "#####"],
    "N": ["#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"],
    "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#.."],
    " ": [".." for _ in range(6)],
}
HALF = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}


def logo_rows(word="LOCAL AGENT", ascii_only=False):
    pixels = ["" for _ in range(6)]
    for letter in word:
        for row, bits in enumerate(FONT[letter]):
            pixels[row] += bits + "."
    if ascii_only:
        return ["".join("#" if bit == "#" else " " for bit in pixels[row]) for row in (0, 2, 3, 5)]
    return [
        "".join(HALF[(top == "#", bottom == "#")] for top, bottom in zip(pixels[row], pixels[row + 1]))
        for row in (0, 2, 4)
    ]


def render_logo(t, palette, motion=True, ascii_only=False):
    """The block logo with a flowing gradient and a soft highlight sweeping across it."""
    rows = logo_rows(ascii_only=ascii_only)
    width = len(rows[0])
    phase = (t * 0.08) if motion else 0.0
    sweep = ((t * 0.35) % 1.6 - 0.3) * width if motion else -100
    lines = []
    for row_index, row in enumerate(rows):
        line = Text()
        for col, char in enumerate(row):
            if char == " ":
                line.append(" ")
                continue
            color = gradient_at(palette["gradient"], col / width * 0.9 + phase + row_index * 0.02, True)
            glow = max(0.0, 1 - abs(col - sweep - row_index * 2) / 6)
            if glow:
                color = blend(color, "#ffffff", glow * 0.65)
            line.append(char, Style(color=color, bold=True))
        lines.append(line)
    return lines


SHADES = " .:-=+*#%@"


def orb_cells(t, palette, rows=9, cols=19, motion=True):
    """A lit, slowly rotating sphere drawn with shade characters (ASCII ray casting).

    Returns rows of (char, color, bold) cells; None marks empty space."""
    angle = t * 0.9 if motion else 0.8
    light = (math.cos(angle), -0.45, math.sin(angle) * 0.9 + 0.4)
    norm = math.sqrt(sum(value * value for value in light))
    light = tuple(value / norm for value in light)
    spin = t * 0.6 if motion else 0.0
    dark = palette["background"]
    core, rim, hot = palette["gradient"][1], palette["gradient"][-1], palette["primary"]
    grid = []
    for row in range(rows):
        cells = []
        y = (row + 0.5) / rows * 2 - 1
        for col in range(cols):
            x = (col + 0.5) / cols * 2 - 1
            distance = x * x + y * y
            if distance > 1:
                cells.append(None)
                continue
            z = math.sqrt(1 - distance)
            diffuse = max(0.0, x * light[0] + y * light[1] + z * light[2])
            longitude = math.atan2(x, z) + spin
            band = 0.5 + 0.5 * math.sin(longitude * 5 + y * 2.5)
            level = min(0.999, 0.12 + diffuse * 0.78 + band * 0.12)
            char = SHADES[int(level * len(SHADES))]
            if char == " ":
                char = "."
            color = blend(dark, core, min(1, level * 1.6))
            color = blend(color, hot, max(0, level - 0.45) * 1.4)
            color = blend(color, "#ffffff", max(0, level - 0.82) * 2.5)
            edge = 1 - z
            if edge > 0.55:
                color = blend(color, rim, (edge - 0.55) * 1.8)
            cells.append((char, color, level > 0.7))
        grid.append(cells)
    return grid


def cells_to_text(grid):
    lines = []
    for cells in grid:
        line = Text(no_wrap=True)
        blank = 0
        for cell in cells:
            if cell is None:
                blank += 1
                continue
            if blank:
                line.append(" " * blank)
                blank = 0
            line.append(cell[0], Style(color=cell[1], bold=cell[2]))
        if blank:
            line.append(" " * blank)
        lines.append(line)
    return lines


def render_orb(t, palette, rows=9, cols=19, motion=True):
    return cells_to_text(orb_cells(t, palette, rows, cols, motion))


def star_field(width, height, t, palette, count=None, glyphs="·∙✦⋆+", motion=True):
    """Sparse drifting, twinkling stars. Returns {(x, y): (char, color)}."""
    count = count if count is not None else max(6, width * height // 70)
    stars = {}
    for index in range(count):
        seed = (index * 2654435761) & 0xFFFFFFFF
        speed = 0.3 + (seed % 7) / 10
        x = (seed % 997) / 997 * width - (t * speed if motion else 0)
        y = (seed // 997 % 991) / 991 * height
        twinkle = 0.5 + 0.5 * math.sin(t * (1.5 + seed % 5) + index) if motion else 0.6
        char = glyphs[seed % len(glyphs)] if twinkle > 0.35 else glyphs[0]
        color = blend(palette["background"], palette["gradient"][seed % len(palette["gradient"])],
                      0.25 + twinkle * 0.55)
        stars[(int(x) % max(width, 1), int(y) % max(height, 1))] = (char, color)
    return stars


def pulse(t, period=1.6):
    """0..1 smooth breathing curve."""
    return 0.5 - 0.5 * math.cos(2 * math.pi * t / period)


def wave_dots(t, palette, count=3, dot="●", motion=True):
    """Three dots that light up in a travelling wave: the thinking indicator."""
    result = Text()
    for index in range(count):
        level = pulse(t - index * 0.18, 1.0) if motion else 0.6
        color = blend(palette["muted"], palette["primary"], level)
        result.append(dot, Style(color=color, bold=level > 0.6))
        result.append(" ")
    return result


_RULE_CACHE = {}


def _rule_colors(width, palette, busy):
    key = (width, tuple(palette["gradient"]), palette["background"], busy)
    if key not in _RULE_CACHE:
        if len(_RULE_CACHE) > 32:
            _RULE_CACHE.clear()
        _RULE_CACHE[key] = [
            blend(palette["background"], gradient_at(palette["gradient"], col / max(width, 1), False),
                  0.55 if busy else 0.35)
            for col in range(width)
        ]
    return _RULE_CACHE[key]


def glow_line(width, t, palette, busy, motion=True, char="━"):
    """A thin gradient rule; while busy a bright comet sweeps across it."""
    head = (t * 0.7 % 1.3 - 0.15) * width if (busy and motion) else -1000
    colors = _rule_colors(width, palette, busy)
    if head < -2 or head - 18 > width:
        line = Text()
        for col in range(width):
            line.append(char, Style(color=colors[col]))
        return line
    line = Text()
    for col in range(width):
        color = colors[col]
        distance = head - col
        if 0 <= distance < 18:
            color = blend(color, "#ffffff", (1 - distance / 18) ** 2 * 0.9)
        elif -2 < distance < 0:
            color = blend(color, "#ffffff", 0.9)
        line.append(char, Style(color=color))
    return line


def meter(fraction, width, palette, full="▰", empty="▱"):
    """A small segmented gauge that shifts from primary to warning to error as it fills."""
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    color = palette["primary"] if fraction < 0.7 else palette["warning"] if fraction < 0.9 else palette["error"]
    result = Text()
    result.append(full * filled, Style(color=color))
    result.append(empty * (width - filled), Style(color=palette["muted"]))
    return result


def human_count(number):
    if number < 1000:
        return str(number)
    if number < 10_000:
        return f"{number / 1000:.1f}k"
    return f"{number // 1000}k"
