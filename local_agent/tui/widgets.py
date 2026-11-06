"""Visual components. They only render state handed to them; agent logic lives in app.py."""
import json
import time
from datetime import datetime
from pathlib import Path

from rich.console import Group
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from textual import events
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Collapsible, DirectoryTree, Markdown, OptionList, Static, TextArea
from textual.widgets.markdown import MarkdownFence

from . import effects


def clean(text):
    """Strip terminal control characters from model or tool text, keeping newlines and tabs."""
    return "".join(char if char.isprintable() or char in "\n\t" else "?" for char in str(text or ""))


def split_reasoning(raw):
    """Split Qwen-style '<think>...</think>answer' text. Returns (reasoning, answer, still_thinking)."""
    stripped = raw.lstrip()
    if not stripped.startswith("<think>"):
        if "<think>".startswith(stripped) and stripped:
            return "", "", True  # The opening tag is still arriving.
        return "", raw, False
    body = stripped[len("<think>"):]
    end = body.find("</think>")
    if end < 0:
        return body.strip(), "", True
    return body[:end].strip(), body[end + len("</think>"):].lstrip(), False


class Animated:
    """Mixin for widgets driven by the app's shared animation clock (one timer for the whole UI)."""
    animating = False
    last_frame = -1.0

    def frame_interval(self):
        """Seconds between frames for this widget; the shared clock caps the rate. The clock can
        tick at 30 fps for the hologram, so everything else defaults to 15."""
        return 1 / 15

    def start_animation(self):
        if not self.animating:
            self.animating = True
            self.app.clock.subscribe(self)

    def stop_animation(self):
        if self.animating:
            self.animating = False
            self.app.clock.unsubscribe(self)

    def on_unmount(self):
        self.stop_animation()

    def frame(self, t):
        self.refresh()


# ── Header ────────────────────────────────────────────────────────────────────────────────
# Split into small widgets so each animation repaints only its own cells: the 3-cell orb breathes,
# the info line redraws only when its data changes, and the glow rule animates only while busy.

class HeaderOrb(Animated, Widget):
    DEFAULT_CSS = "HeaderOrb { width: 3; height: 1; }"

    def __init__(self, bar, **kwargs):
        super().__init__(**kwargs)
        self.bar = bar
        self._t = 0.0

    def on_mount(self):
        self.start_animation()

    def frame_interval(self):
        if self.bar.busy:
            return 1 / 15
        return 0.25 if self.app.motion == "full" else 3600.0  # Idle: a slow breath, or nothing.

    def frame(self, t):
        self._t = t
        self.refresh()

    def render(self):
        palette, glyphs = self.app.palette, self.app.glyphs
        busy = self.bar.busy
        level = effects.pulse(self._t, 0.7 if busy else 2.8) if self.app.motion != "off" else 0.7
        # The header orb mirrors the hologram's state color, so state stays visible when the
        # hologram itself is hidden or the terminal is too small for it.
        state_color = self.app.hologram_color()
        color = effects.blend(palette["secondary"], "#ffffff" if busy else state_color, level)
        if busy:
            color = effects.blend(state_color, color, 0.5)
        return Text(f" {glyphs.orb} ", Style(color=color, bold=True))


class HeaderInfo(Animated, Widget):
    DEFAULT_CSS = "HeaderInfo { width: 1fr; height: 1; }"

    def __init__(self, bar, **kwargs):
        super().__init__(**kwargs)
        self.bar = bar
        self._t = 0.0

    def on_mount(self):
        self.start_animation()

    def frame_interval(self):
        return 0.1 if self.bar.connection == "checking" and self.app.motion != "off" else 3600.0

    def frame(self, t):
        self._t = t
        if self.bar.connection == "checking":
            self.refresh()

    def render(self):
        bar, app = self.bar, self.app
        palette, glyphs = app.palette, app.glyphs
        width = self.size.width
        title = "LOCAL·AGENT" if glyphs.orb != "@" else "LOCAL AGENT"
        top = effects.gradient_text(title, palette["gradient"], 0.0, True, cyclic=False)
        muted = Style(color=palette["muted"])
        segments = []
        if bar.model:
            segments.append(("model ", bar.model, palette["primary"]))
        if bar.session:
            segments.append((glyphs.session + " ", bar.session, palette["user"]))
        if bar.project and width >= 85:
            segments.append((glyphs.folder + " ", bar.project, palette["foreground"]))
        if bar.branch and width >= 105:
            segments.append((glyphs.branch + " ", bar.branch, palette["secondary"]))
        for label, value, color in segments:
            top.append(f"  {glyphs.sep} ", muted)
            top.append(label, muted)
            top.append(value[:32], Style(color=color))
        status_color, status_text = {
            "online": (palette["success"], "online"),
            "missing": (palette["warning"], "model missing"),
            "offline": (palette["error"], "offline"),
        }.get(bar.connection, (palette["muted"], "connecting"))
        right = Text()
        dot = glyphs.dot_on if bar.connection == "online" else glyphs.dot_off
        if bar.connection == "checking" and app.motion != "off":
            dot = glyphs.spinner[int(self._t * 10) % len(glyphs.spinner)]
        right.append(dot + " ", Style(color=status_color))
        right.append(status_text + " ", Style(color=status_color))
        gap = width - top.cell_len - right.cell_len
        if gap < 1:
            top.truncate(max(0, width - right.cell_len - 1))
            gap = max(1, width - top.cell_len - right.cell_len)
        top.append(" " * gap)
        top.append_text(right)
        return top


class GlowRule(Animated, Widget):
    """A gradient rule under the header. While the agent works, a bright comet sweeps across it."""
    DEFAULT_CSS = "GlowRule { width: 1fr; height: 1; }"

    def __init__(self, bar, **kwargs):
        super().__init__(**kwargs)
        self.bar = bar
        self._t = 0.0

    def on_mount(self):
        self.start_animation()

    def frame_interval(self):
        return 0.066 if self.bar.busy and self.app.motion != "off" else 3600.0

    def frame(self, t):
        self._t = t
        self.refresh()

    def render(self):
        app = self.app
        return effects.glow_line(self.size.width, self._t, app.palette, self.bar.busy, app.motion != "off",
                                 "━" if app.glyphs.orb != "@" else "=")


class TopBar(Widget):
    """Orb, product name, model, session, project, git branch and connection status, over a glow rule."""
    DEFAULT_CSS = "TopBar { height: 2; layout: vertical; } TopBar > Horizontal { height: 1; }"
    model = reactive("")
    session = reactive("")
    project = reactive("")
    branch = reactive("")
    connection = reactive("checking")
    busy = reactive(False)

    def compose(self):
        with Horizontal():
            yield HeaderOrb(self)
            yield HeaderInfo(self)
        yield GlowRule(self)

    def _changed(self, *_):
        for child in self.query("HeaderOrb, HeaderInfo, GlowRule"):
            child.refresh()

    watch_model = watch_session = watch_project = watch_branch = watch_connection = watch_busy = _changed


# ── Welcome ───────────────────────────────────────────────────────────────────────────────
# A static star field fills the background layer; only the logo and the orb repaint each frame.

class StarField(Widget):
    def render(self):
        app = self.app
        width, height = self.size.width, self.size.height
        stars = effects.star_field(width, height, 0.0, app.palette, glyphs=app.glyphs.stars, motion=False)
        by_row = {}
        for (x, y), star in stars.items():
            by_row.setdefault(y, []).append((x, star))
        lines = []
        for row in range(height):
            line = Text(no_wrap=True, overflow="crop")
            col = 0
            for x, (char, color) in sorted(by_row.get(row, ())):
                if x > col:
                    line.append(" " * (x - col))
                line.append(char, Style(color=color))
                col = x + 1
            if col < width:
                line.append(" " * (width - col))
            lines.append(line)
        return Group(*lines)


class Logo(Animated, Widget):
    """The block logo with a flowing gradient and a light sweep. Compact text when space is short."""
    DEFAULT_CSS = "Logo { width: auto; height: auto; }"
    compact = reactive(False, layout=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._t = 0.0

    def on_mount(self):
        self.start_animation()

    def frame_interval(self):
        return 0.125 if self.app.motion == "full" else 3600.0

    def frame(self, t):
        self._t = t
        self.refresh()

    def lines(self):
        app = self.app
        ascii_only = app.glyphs.orb == "@"
        motion = app.motion == "full"
        if self.compact:
            return [effects.gradient_text("◆ LOCAL·AGENT" if not ascii_only else "* LOCAL AGENT",
                                          app.palette["gradient"], self._t * 0.15 if motion else 0, True)]
        return effects.render_logo(self._t, app.palette, motion, ascii_only)

    def get_content_width(self, container, viewport):
        return max(line.cell_len for line in self.lines())

    def get_content_height(self, container, viewport, width):
        return len(self.lines())

    def render(self):
        return Group(*self.lines())


class Welcome(Widget):
    """Empty state: animated logo, the large holographic core, session details and key shortcuts."""

    def __init__(self, info, **kwargs):
        super().__init__(**kwargs)
        self.info = info  # list of (label, value)

    def compose(self):
        palette = self.app.palette
        muted = Style(color=palette["muted"])
        card = Text()
        for label, value in self.info:
            card.append(f"{label:<10}", muted)
            card.append(str(value)[:30] + "\n", Style(color=palette["foreground"]))
        card.append("\n")
        for keys, action in (("enter", "send"), ("shift+enter", "new line"), ("/", "commands"),
                             ("@", "mention a file"), ("ctrl+p", "command palette"), ("ctrl+b", "sidebar"),
                             ("f1", "help")):
            card.append(f" {keys} ", Style(color=palette["background"], bgcolor=palette["secondary"], bold=True))
            card.append(f" {action}\n", muted)
        card.rstrip()
        self.card_size = (max(line.cell_len for line in card.split()), len(card.split()))
        yield StarField(id="stars")
        with Vertical(id="welcome-content"):
            yield Logo(id="logo")
            yield Static(Text("a local, tool-using agent · memory · skills · workspace", muted), id="tagline")
            with Horizontal(id="welcome-row"):
                yield self.app.make_hologram(id="hero-holo", classes="hero")
                yield Static(card, id="welcome-card")

    def on_resize(self, event):
        self.layout_hero(*event.size)

    def layout_hero(self, width=None, height=None):
        """Give the hologram the largest square-looking area that fits beside (or above) the card.
        Cells are about twice as tall as wide, so a round sphere needs roughly 2.15 columns per row."""
        if width is None:
            width, height = self.size
        compact = width < 68 or height < 12
        self.query_one(Logo).compact = compact
        card = self.query_one("#welcome-card")
        card.display = height >= 18
        row = self.query_one("#welcome-row")
        heroes = self.query(".hero")
        if not heroes:
            return
        hero = heroes.first()
        card_w, card_h = self.card_size if card.display else (0, 0)
        header = (1 if compact else 3) + 2 + 1      # Logo, tagline with margins, slack.
        side = min(22, height - header, int((width - 6 - (card_w + 3 if card_w else 0)) / 2.15))
        stacked = min(22, height - header - card_h - 1, int((width - 6) / 2.15)) if card_w else -1
        rows = max(side, stacked)
        if rows < 7 or not self.app.hologram_visible:
            hero.display = False
            return
        row.styles.layout = "vertical" if stacked > side else "horizontal"
        card.styles.margin = (1, 0, 0, 0) if stacked > side else (0, 0, 0, 3)
        hero.styles.width = int(rows * 2.15)
        hero.styles.height = rows
        hero.display = True


# ── Messages ──────────────────────────────────────────────────────────────────────────────

def stamp():
    return datetime.now().strftime("%H:%M")


class UserMessage(Static):
    def __init__(self, text, when=None):
        super().__init__(classes="message user")
        self.text = clean(text)
        self.when = when

    def render(self):
        palette, glyphs = self.app.palette, self.app.glyphs
        head = Text()
        head.append(f"{glyphs.user} you", Style(color=palette["user"], bold=True))
        if self.when:
            head.append(f"  {self.when}", Style(color=palette["muted"]))
        body = Text()
        for word in self.text.split(" "):
            if body.plain:
                body.append(" ")
            mention = word.startswith("@") and len(word) > 1
            body.append(word, Style(color=palette["accent"], underline=True) if mention else Style())
        return Group(head, body)


class Thinking(Animated, Static):
    """The pulsing 'thinking' or 'streaming' line shown while the assistant works."""
    label = reactive("thinking")

    def on_mount(self):
        self.started = time.monotonic()
        self.start_animation()

    def frame(self, t):
        self._t = t
        self.refresh()

    def render(self):
        palette, glyphs = self.app.palette, self.app.glyphs
        motion = self.app.motion != "off"
        t = getattr(self, "_t", 0.0)
        line = Text()
        line.append_text(effects.wave_dots(t, palette, 3, "●" if glyphs.orb != "@" else "o", motion))
        line.append_text(effects.gradient_text(self.label, palette["gradient"], t * 0.4 if motion else 0, False))
        elapsed = time.monotonic() - getattr(self, "started", time.monotonic())
        line.append(f"  {elapsed:4.1f}s", Style(color=palette["muted"]))
        return line


class ToolCall(Animated, Vertical):
    """One tool execution: spinner while running, then a result you can expand."""

    def __init__(self, call_id, name, arguments):
        super().__init__(classes="tool")
        self.call_id = call_id
        self.tool_name = name
        try:
            self.arguments = json.loads(arguments)
        except ValueError:
            self.arguments = {}
        self.state = "running"
        self.error = None
        self.result = None
        self.started = time.monotonic()
        self.elapsed = 0.0

    def compose(self):
        yield Static(self.row(), classes="tool-row")
        self.details = Collapsible(Static(""), title="output", collapsed=True, classes="tool-output")
        self.details.display = False
        yield self.details

    def on_mount(self):
        if self.state == "running":
            self.start_animation()

    def frame(self, t):
        self._t = t
        rows = self.query(".tool-row")
        if rows:
            rows.first(Static).update(self.row())

    def row(self):
        app = self.app
        palette, glyphs = app.palette, app.glyphs
        line = Text()
        if self.state == "running":
            t = getattr(self, "_t", 0.0)
            frames = glyphs.spinner
            icon = frames[int(t * 12) % len(frames)] if app.motion != "off" else glyphs.tool
            line.append(f"{icon} ", Style(color=palette["primary"], bold=True))
        elif self.state == "ok":
            line.append(f"{glyphs.ok} ", Style(color=palette["success"], bold=True))
        else:
            line.append(f"{glyphs.fail} ", Style(color=palette["error"], bold=True))
        line.append(self.tool_name, Style(color=palette["secondary"], bold=True))
        for key, value in self.arguments.items():
            value = clean(value).replace("\n", " ")
            line.append(f"  {key}=", Style(color=palette["muted"]))
            if key == "path" and self.tool_name in {"read_file", "list_files", "write_file", "patch", "search_files"}:
                ref = app.register_ref(value)
                line.append(value[:60], Style(color=palette["accent"], underline=True,
                                              meta={"@click": f"app.open_ref({ref})"}))
            else:
                line.append(value[:60] + ("…" if len(value) > 60 else ""), Style(color=palette["foreground"]))
        if self.state == "running":
            line.append("  running", Style(color=palette["muted"], italic=True))
        else:
            line.append(f"  {self.elapsed * 1000:.0f} ms", Style(color=palette["muted"]))
            if self.error:
                line.append(f"  {clean(self.error)[:90]}", Style(color=palette["error"]))
        return line

    def finish(self, result, error):
        self.stop_animation()
        self.elapsed = time.monotonic() - self.started
        self.state = "error" if error else "ok"
        self.error = error
        self.result = result
        self.query_one(".tool-row", Static).update(self.row())
        try:
            pretty = json.dumps(json.loads(result), indent=2, ensure_ascii=False)
        except ValueError:
            pretty = str(result)
        pretty = clean(pretty)
        if len(pretty) > 6000:
            pretty = pretty[:6000] + "\n… (truncated in view)"
        dark = not self.app.palette.get("light")
        self.details.query_one(Static).update(
            Syntax(pretty, "json", theme="monokai" if dark else "default", word_wrap=True,
                   background_color="default")
        )
        self.details.title = f"output · {len(result):,} chars"
        self.details.display = True


class CopyableCodeBlock(MarkdownFence):
    def __init__(self, markdown, token, _code):
        super().__init__(markdown, token, token.content)

    def compose(self):
        yield Button("Copy code", classes="copy-code", compact=True, flat=True)
        yield from super().compose()

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        self.app.copy_to_clipboard(self.code)
        self.app.notify("Copied code.", timeout=2)


class ReplyMarkdown(Markdown):
    BLOCKS = {**Markdown.BLOCKS, "fence": CopyableCodeBlock, "code_block": CopyableCodeBlock}


def file_size(count):
    for unit in ("bytes", "KB", "MB"):
        if count < 1024 or unit == "MB":
            return f"{count:,} {unit}" if unit == "bytes" else f"{count:,.1f} {unit}"
        count /= 1024


class ArtifactCard(Horizontal):
    """A generated artifact with Preview (a sandboxed browser page) and Copy path actions."""

    def __init__(self, artifact):
        super().__init__(classes="artifact-card")
        self.artifact = {key: artifact.get(key) for key in ("id", "name", "path", "mime", "bytes")}
        self.removed = False

    def compose(self):
        yield Static(self.label(), classes="artifact-label")
        yield Button("Preview", classes="artifact-preview", compact=True, flat=True, disabled=self.removed,
                     tooltip="Open a sandboxed browser preview (scripts and network blocked)")
        yield Button("Copy path", classes="artifact-copy", compact=True, flat=True, disabled=self.removed,
                     tooltip=clean(self.artifact["path"]))

    def label(self):
        palette, glyphs = self.app.palette, self.app.glyphs
        line = Text()
        line.append(f"{glyphs.folder} ", Style(color=palette["accent"]))
        line.append(clean(self.artifact["name"]), Style(bold=not self.removed, strike=self.removed))
        if self.removed:
            line.append("  removed with the turn", Style(color=palette["muted"], italic=True))
        else:
            line.append(f"  {file_size(self.artifact['bytes'] or 0)} · {clean(self.artifact['mime'])}",
                        Style(color=palette["muted"]))
        return line

    def mark_removed(self):
        """A failed or cancelled turn deletes the artifacts it created."""
        self.removed = True
        for button in self.query(Button):
            button.disabled = True
        for label in self.query(".artifact-label"):  # Not there yet if the card is still mounting.
            label.update(self.label())

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if self.removed:
            return
        if event.button.has_class("artifact-copy"):
            self.app.copy_to_clipboard(self.artifact["path"])
            self.app.notify(f"Copied the path of {clean(self.artifact['name'])}.", timeout=2)
        else:
            self.app.preview_artifact(self.artifact["id"])
        self.app.query_one(Composer).focus()  # Keep typing where you were.


ARTIFACT_TOOLS = {"create_artifact", "generate_image"}


def artifact_result(name, result, error):
    """The artifact a successful create_artifact or generate_image call returned, or None."""
    if name not in ARTIFACT_TOOLS or error:
        return None
    try:
        value = json.loads(result)
    except (TypeError, ValueError):
        return None
    if isinstance(value, dict) and isinstance(value.get("id"), str) and isinstance(value.get("path"), str):
        return value
    return None


class AttachmentBar(Horizontal):
    """Files queued for the next message, beside the composer. Each chip's ✕ removes that file."""

    class Remove(Message):
        def __init__(self, index):
            super().__init__()
            self.index = index

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.paths = []

    async def show(self, paths):
        self.paths = list(paths)
        await self.remove_children()
        if self.paths:
            await self.mount(Static(f"{self.app.glyphs.folder} attached", classes="attachment-title"))
            await self.mount_all(
                Button(f"{clean(path.name)[:28]} ✕" if self.app.glyphs.orb != "@" else f"{clean(path.name)[:28]} x",
                       classes="attachment-chip", compact=True, flat=True, name=str(index),
                       tooltip=f"Remove {clean(path)} (/detach {index + 1})")
                for index, path in enumerate(self.paths))
        self.display = bool(self.paths)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.has_class("attachment-chip"):
            event.stop()
            self.post_message(self.Remove(int(event.button.name)))


class AssistantMessage(Vertical):
    """A full assistant turn: thinking indicator, optional reasoning, text segments and tool calls."""

    def __init__(self, when=None, live=True):
        super().__init__(classes="message assistant")
        self.when = when or stamp()
        self.live = live
        self.raw = ""
        self.sent = 0
        self.stream = None
        self.segment = None
        self.tools = {}
        self.artifacts = []
        self.reasoning_text = ""
        self.meta = ""
        self.started = time.monotonic()
        self.first_token = None
        self.content_chars = 0

    def compose(self):
        yield Static(self.head(), classes="assistant-head")
        self.reasoning = Collapsible(Static("", classes="reasoning-body"), title="reasoning",
                                     collapsed=True, classes="reasoning")
        self.reasoning.display = False
        yield self.reasoning
        self.body = Vertical(classes="assistant-body")
        yield self.body
        if self.live:
            self.thinking = Thinking(classes="thinking")
            yield self.thinking
        else:
            self.thinking = None

    def head(self):
        palette, glyphs = self.app.palette, self.app.glyphs
        line = Text()
        line.append(f"{glyphs.agent} agent", Style(color=palette["assistant"], bold=True))
        line.append(f"  {self.when}", Style(color=palette["muted"]))
        if self.meta:
            line.append(f"  {glyphs.sep} {self.meta}", Style(color=palette["muted"]))
        return line

    def set_meta(self, meta):
        self.meta = meta
        self.query_one(".assistant-head", Static).update(self.head())

    async def new_segment(self):
        await self.close_stream()
        self.raw = ""
        self.sent = 0
        self.segment = ReplyMarkdown("", classes="segment")
        await self.body.mount(self.segment)
        if self.thinking is not None:
            self.thinking.label = "thinking"

    async def close_stream(self):
        if self.stream is not None:
            await self.stream.stop()
            self.stream = None

    async def add_text(self, kind, text):
        text = clean(text)
        if kind == "reasoning":
            self.show_reasoning(self.reasoning_text + text)
            if self.thinking is not None:
                self.thinking.label = "reasoning"
            return
        if self.segment is None:
            await self.new_segment()
        if self.first_token is None:
            self.first_token = time.monotonic()
        self.raw += text
        self.content_chars += len(text)
        reasoning, answer, thinking = split_reasoning(self.raw)
        if reasoning:
            self.show_reasoning(reasoning)
        if thinking:
            if self.thinking is not None:
                self.thinking.label = "reasoning"
            return
        if self.thinking is not None:
            rate = self.content_chars / 4 / max(0.25, time.monotonic() - self.first_token)
            self.thinking.label = f"streaming · {rate:,.0f} tok/s"
        delta = answer[self.sent:]
        if delta:
            if self.stream is None:
                self.stream = Markdown.get_stream(self.segment)
            await self.stream.write(delta)
            self.sent = len(answer)

    def show_reasoning(self, text):
        self.reasoning_text = text
        if text.strip():
            self.reasoning.display = True
            self.reasoning.query_one(".reasoning-body", Static).update(Text(text, Style(italic=True)))
            self.reasoning.title = f"reasoning · {len(text.split())} words"

    async def set_content(self, content):
        """Render a complete reply (non-streaming providers, history, or the final answer)."""
        content = clean(content)
        if self.segment is not None and content == self.raw and self.sent == len(split_reasoning(content)[1]):
            await self.close_stream()  # Already on screen from the stream; skip a full re-render.
            return
        if self.segment is None:
            await self.new_segment()
        await self.close_stream()
        reasoning, answer, _ = split_reasoning(content)
        if reasoning:
            self.show_reasoning(reasoning)
        self.raw = content
        self.sent = len(answer)
        await self.segment.update(answer)

    async def start_tool(self, call_id, name, arguments):
        await self.close_stream()
        if self.segment is not None and not split_reasoning(self.raw)[1].strip():
            await self.segment.remove()  # Drop an empty text segment before the tool row.
        self.segment = None
        tool = ToolCall(call_id, name, arguments)
        self.tools[call_id] = tool
        await self.body.mount(tool)
        if self.thinking is not None:
            self.thinking.label = f"running {name}"

    def end_tool(self, call_id, result, error):
        tool = self.tools.get(call_id)
        if tool is not None:
            tool.finish(result, error)
            artifact = artifact_result(tool.tool_name, result, error)
            if artifact is not None:
                card = ArtifactCard(artifact)
                self.artifacts.append(card)
                self.body.mount(card, after=tool)
        if self.thinking is not None:
            self.thinking.label = "thinking"

    async def finish(self, meta=""):
        await self.close_stream()
        if self.thinking is not None:
            await self.thinking.remove()
            self.thinking = None
        if meta:
            self.set_meta(meta)


class NoteCard(Static):
    """System output: command results, session dividers and hints."""

    def __init__(self, renderable, kind="info"):
        super().__init__(renderable, classes=f"note {kind}")


class ErrorCard(Static):
    def __init__(self, title, detail, hint=""):
        super().__init__(classes="message error-card")
        self.title, self.detail, self.hint = title, clean(detail), hint

    def render(self):
        palette, glyphs = self.app.palette, self.app.glyphs
        lines = [Text(f"{glyphs.fail} {self.title}", Style(color=palette["error"], bold=True)),
                 Text(self.detail, Style(color=palette["foreground"]))]
        if self.hint:
            lines.append(Text(f"{glyphs.arrow} {self.hint}", Style(color=palette["muted"], italic=True)))
        return Group(*lines)


def divider(text, app):
    palette = app.palette
    line = Text(justify="center")
    line.append("── ", Style(color=palette["muted"]))
    line.append(text, Style(color=palette["muted"], italic=True))
    line.append(" ──", Style(color=palette["muted"]))
    return line


class ChatLog(VerticalScroll):
    """Scrollable conversation that follows new output until the user scrolls up."""
    can_focus = True


# ── Composer ──────────────────────────────────────────────────────────────────────────────

class Composer(TextArea):
    """Multiline input. Enter sends; shift+enter, alt+enter or ctrl+j inserts a newline."""

    class Submitted(Message):
        def __init__(self, text):
            super().__init__()
            self.text = text

    class Navigate(Message):
        def __init__(self, key):
            super().__init__()
            self.key = key

    NEWLINE_KEYS = {"shift+enter", "alt+enter", "ctrl+j", "ctrl+enter"}

    def __init__(self, **kwargs):
        super().__init__(soft_wrap=True, tab_behavior="focus", show_line_numbers=False,
                         highlight_cursor_line=False, **kwargs)
        self.recall = []
        self.recall_index = None
        self.suggesting = False

    async def _on_key(self, event: events.Key):
        key = event.key
        if key in self.NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self._replace_via_keyboard("\n", start, end)
            return
        if self.suggesting and key in {"up", "down", "tab", "enter", "escape"}:
            event.stop()
            event.prevent_default()
            self.post_message(self.Navigate(key))
            return
        if key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text.strip()
            if text:
                if not self.recall or self.recall[-1] != text:
                    self.recall.append(text)
                self.recall_index = None
                self.post_message(self.Submitted(text))
            return
        row, _ = self.cursor_location
        if key == "up" and row == 0 and self.recall:
            event.stop()
            event.prevent_default()
            self.recall_index = len(self.recall) - 1 if self.recall_index is None else max(0, self.recall_index - 1)
            self.load_text(self.recall[self.recall_index])
            self.move_cursor(self.document.end)
            return
        if key == "down" and self.recall_index is not None and row == self.document.line_count - 1:
            event.stop()
            event.prevent_default()
            self.recall_index += 1
            if self.recall_index >= len(self.recall):
                self.recall_index = None
                self.load_text("")
            else:
                self.load_text(self.recall[self.recall_index])
            self.move_cursor(self.document.end)
            return
        await super()._on_key(event)

    def current_token(self):
        """The whitespace-delimited word before the cursor and where it starts."""
        row, col = self.cursor_location
        line = self.document.get_line(row)[:col]
        start = max(line.rfind(" "), line.rfind("\t")) + 1
        return line[start:], (row, start), (row, col)


class Suggestions(OptionList):
    """Slash-command and @file autocomplete shown just above the composer."""
    can_focus = False


# ── Sidebar ───────────────────────────────────────────────────────────────────────────────

class Explorer(DirectoryTree):
    def filter_paths(self, paths):
        return [path for path in paths if not path.name.startswith(".") and path.name != "__pycache__"]


class SidebarList(OptionList):
    pass


class Sidebar(Vertical):
    def __init__(self, root: Path, **kwargs):
        super().__init__(**kwargs)
        self.root = root

    def compose(self):
        from .system_monitor import SystemMonitor

        yield Static("", id="sidebar-title")
        yield SystemMonitor(self.root, unicode=self.app.glyphs.orb != "@",
                            color="NO_COLOR" not in self.app.environ, id="system-monitor")
        with VerticalScroll(id="sidebar-scroll"):
            with Collapsible(title="Sessions", collapsed=False, id="sessions-section"):
                yield SidebarList(id="session-list")
            with Collapsible(title="Explorer", collapsed=False, id="explorer-section"):
                yield Explorer(self.root, id="explorer")
            with Collapsible(title="Tools", collapsed=False, id="tools-section"):
                yield Static("", id="tool-list")
            with Collapsible(title="Model", collapsed=True, id="model-section"):
                yield Static("", id="model-info")
            with Collapsible(title="Library", collapsed=True, id="library-section"):
                yield Static("", id="library-info")


# ── Status bar ────────────────────────────────────────────────────────────────────────────

class StatusBar(Animated, Widget):
    DEFAULT_CSS = "StatusBar { height: 1; }"
    state = reactive("idle")
    detail = reactive("")
    used = reactive(0)
    budget = reactive(0)
    prompt_tokens = reactive(0)
    completion_tokens = reactive(0)
    estimated = reactive(False)
    learn = reactive(False)

    def on_mount(self):
        self.start_animation()

    def frame(self, t):
        if self.state in {"thinking", "streaming", "tool", "approval"}:
            self._t = t
            self.refresh()

    def render(self):
        app = self.app
        palette, glyphs = app.palette, app.glyphs
        width = self.size.width
        t = getattr(self, "_t", 0.0)
        muted = Style(color=palette["muted"])
        colors = {"idle": palette["success"], "thinking": palette["primary"], "streaming": palette["accent"],
                  "tool": palette["secondary"], "approval": palette["warning"], "error": palette["error"],
                  "cancelled": palette["warning"]}
        color = colors.get(self.state, palette["muted"])
        line = Text(" ")
        busy = self.state in {"thinking", "streaming", "tool", "approval"}
        icon = glyphs.spinner[int(t * 12) % len(glyphs.spinner)] if busy and app.motion != "off" else glyphs.dot_on
        line.append(f"{icon} {self.state}", Style(color=color, bold=True))
        if self.detail:
            line.append(f"  {clean(self.detail)[:40]}", muted)
        parts = []
        if self.budget:
            fraction = self.used / self.budget
            ctx = Text()
            ctx.append("ctx ", muted)
            ctx.append_text(effects.meter(fraction, 8 if width >= 100 else 5, palette,
                                          glyphs.bar_full, glyphs.bar_empty))
            ctx.append(f" {fraction:4.0%}", muted)
            parts.append(ctx)
        if self.prompt_tokens or self.completion_tokens:
            tok = Text()
            approx = "~" if self.estimated else ""
            tok.append(f"tok {approx}↑{effects.human_count(self.prompt_tokens)} "
                       f"↓{effects.human_count(self.completion_tokens)}", muted)
            parts.append(tok)
        learn = Text()
        learn.append("learn ", muted)
        learn.append("on" if self.learn else "off",
                     Style(color=palette["success"] if self.learn else palette["muted"]))
        parts.append(learn)
        hints = Text()
        for key, label in (("^P", "palette"), ("^B", "sidebar"), ("esc", "cancel" if busy else "close"),
                           ("f1", "help")):
            hints.append(f" {key}", Style(color=palette["primary"], bold=True))
            hints.append(f" {label}", muted)
        right = Text()
        for part in parts:
            right.append(f" {glyphs.sep} ", muted)
            right.append_text(part)
        if line.cell_len + right.cell_len + hints.cell_len + 2 <= width:
            right.append(f" {glyphs.sep}", muted)
            right.append_text(hints)
        while parts and line.cell_len + right.cell_len + 1 > width:
            parts.pop()
            right = Text()
            for part in parts:
                right.append(f" {glyphs.sep} ", muted)
                right.append_text(part)
        gap = max(1, width - line.cell_len - right.cell_len - 1)
        line.append(" " * gap)
        line.append_text(right)
        line.truncate(width)
        return line
