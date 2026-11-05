import json
import sqlite3
import time

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, Static
from rich.text import Text

from .events import AgentEvent
from .attachments import message_text
from .file_commands import FILE_HELP, FileCommands
from .hologram import Capabilities
from .hologram_widget import HologramRenderer
from .monitor_widget import SystemMonitor


CYAN = "#62edff"
VIOLET = "#d6a5ff"
MUTED = "#93a9bf"
AMBER = "#ffca80"
CONTROL_CODES = dict.fromkeys(code for code in (*range(32), *range(127, 160)) if code not in (9, 10))
TOOL_LABELS = {
    "list_files": "List files",
    "read_file": "Read",
    "read_skill": "Load skill",
    "remember": "Stage memory",
    "forget": "Stage memory removal",
    "save_skill": "Stage skill",
    "delete_skill": "Stage skill removal",
    "create_artifact": "Create artifact",
    "read_artifact": "Read artifact",
    "list_artifacts": "List artifacts",
    "generate_image": "Generate image",
}
TURN_LABELS = {
    "user": ("YOU", CYAN),
    "assistant": ("AGENT", VIOLET),
    "error": ("TURN FAILED", AMBER),
    "system": ("SYSTEM", MUTED),
}


def safe_text(value):
    return value.translate(CONTROL_CODES)


class ToolFinished(Message):
    def __init__(self, name, target, succeeded):
        super().__init__()
        self.name = name
        self.target = target
        self.succeeded = succeeded


class TurnFinished(Message):
    def __init__(self, prompt, response, *, failed=False):
        super().__init__()
        self.prompt = prompt
        self.response = response
        self.failed = failed


class RuntimeEvent(Message):
    def __init__(self, event):
        super().__init__()
        self.event = event


class ChatApp(App[int]):
    TITLE = "Local Agent"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+c", "close_chat(130)", show=False, priority=True),
        Binding("ctrl+q", "close_chat(0)", show=False, priority=True),
        Binding("pageup", "scroll_chat(-1)", show=False),
        Binding("pagedown", "scroll_chat(1)", show=False),
        Binding("ctrl+h", "toggle_hologram", show=False),
        Binding("ctrl+left", "rotate_hologram('left')", show=False),
        Binding("ctrl+right", "rotate_hologram('right')", show=False),
    ]
    CSS = """
    Screen {
        background: #080f18;
        color: #e5eef8;
        padding: 0 1;
    }
    #brand {
        height: 3;
        padding: 0 1;
        border: round #62edff;
        background: #102235;
    }
    #context, #shortcuts {
        height: 1;
        padding: 0 1;
        text-overflow: ellipsis;
    }
    #main { height: 1fr; }
    #conversation { width: 1fr; height: 1fr; }
    #welcome { height: 17; }
    #welcome-core { height: 14; }
    #welcome-title { height: 2; text-align: center; color: #62edff; }
    #transcript {
        width: 1fr;
        height: 1fr;
        border: round #32748d;
        background: #0c1623;
        padding: 0 1;
        scrollbar-color: #32748d;
        scrollbar-background: #0c1623;
    }
    #transcript Static { height: auto; }
    .turn { margin: 1 0; }
    #status {
        width: 29;
        margin-left: 1;
        padding: 0 1;
        border: round #32748d;
        background: #101b2b;
        overflow-y: auto;
    }
    #status-title { height: 1; margin: 0 0 1 0; }
    #status-lines { height: auto; }
    #orb { height: 12; }
    #prompt {
        height: 3;
        border: round #32748d;
        background: #102235;
        color: #e5eef8;
    }
    #prompt:focus { border: round #62edff; }
    """

    def __init__(self, agent, session, learn, *, hologram="auto", animated=True, capabilities=None,
                 image_factory=None, attachments=()):
        super().__init__()
        self.agent = agent
        self.session = session
        self.learn = learn
        self.files = FileCommands(agent.state, attachments)
        self.busy = False
        _, self.history = agent.state.history(session)
        self.welcome = not bool(self.history)
        self.hologram_visible = hologram != "off"
        self.hologram_size = "medium"
        self.graphics_options = dict(mode="auto" if hologram == "off" else hologram,
                                     animated=animated, capabilities=capabilities or Capabilities.detect(),
                                     image_factory=image_factory)
        self.streaming_row = None
        self.streaming_text = ""

    def compose(self) -> ComposeResult:
        yield Static(Text("❯_  LOCAL AGENT", style=f"bold {CYAN}"), id="brand")
        context = Text.assemble(
            ("SESSION  ", CYAN), (safe_text(self.session) + "    ", "white"),
            ("MODEL  ", VIOLET), (safe_text(self.agent.provider.model), "white"),
        )
        yield Static(context, id="context")
        with Horizontal(id="main"):
            with Vertical(id="conversation"):
                with Vertical(id="welcome"):
                    yield HologramRenderer(id="welcome-core", **self.graphics_options)
                    yield Static("N E U R A L   C O R E\nReady for your next idea", id="welcome-title")
                yield VerticalScroll(id="transcript")
            with VerticalScroll(id="status"):
                yield Static(Text("SYSTEM MONITOR", style=f"bold {CYAN}"), id="status-title")
                yield SystemMonitor(self.agent.workspace or self.agent.state.path.parent,
                                    unicode=self.graphics_options["capabilities"].unicode,
                                    color=self.graphics_options["capabilities"].color, id="system-monitor")
                yield Static(id="status-lines")
                yield HologramRenderer(id="orb", **self.graphics_options)
        yield Input(placeholder="❯ Ask a follow-up…", id="prompt")
        yield Static(id="shortcuts")

    def on_mount(self):
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.anchor()
        for message in self.history:
            if message["role"] in {"user", "assistant"} and message.get("content"):
                self.write_turn(message["role"], message_text(message))
        self.history.clear()
        self.query_one(Input).focus()
        self.layout_hologram()
        self.update_status()

    def on_resize(self, event):
        self.layout_hologram(event.size)

    def layout_hologram(self, size=None):
        size = size or self.size
        status = self.query_one("#status")
        status.display = size.width >= 80 and size.height >= 18
        panel_width, core_height = {"small": (25, 9), "medium": (31, 12), "large": (43, 17)}[self.hologram_size]
        status.styles.width = min(panel_width, max(25, size.width // 3))
        welcome = self.welcome and self.hologram_visible and size.height >= 25 and size.width >= 50
        self.query_one("#welcome").display = welcome
        self.query_one("#welcome-core", HologramRenderer).set_active(welcome)
        orb = self.query_one("#orb", HologramRenderer)
        available = max(0, size.height - 25)
        orb.styles.height = min(core_height, available)
        orb.set_active(self.hologram_visible and status.display and not welcome and available >= 4)

    def update_status(self):
        files = "READ ONLY" if self.agent.workspace is not None else "OFF"
        learning = "ON" if self.learn else "OFF"
        state = "WAITING FOR MODEL" if self.busy else "READY"
        color = VIOLET if self.busy else CYAN
        self.query_one("#status-lines", Static).update(Text.assemble(
            ("WORKSPACE  ", MUTED), files, ("\nLEARNING   ", MUTED), learning,
            ("\nSTATE      ", MUTED), ("WORKING" if self.busy else state, color),
            ("\nATTACHED   ", MUTED), str(len(self.files.pending)),
        ))
        self.query_one("#shortcuts", Static).update(Text.assemble(
            (state, color), ("   /help  /hologram  /sessions  /exit", MUTED),
        ))

    def write_turn(self, role, content):
        label, color = TURN_LABELS[role]
        self.write_line(Text.assemble((label + "\n", f"bold {color}"), safe_text(content)), "turn")

    def write_line(self, text, classes=None):
        if self.welcome:
            self.welcome = False
            self.layout_hologram()
        self.query_one("#transcript", VerticalScroll).mount(Static(text, classes=classes))

    def on_input_submitted(self, event: Input.Submitted):
        prompt = event.value.strip()
        if not prompt:
            return
        if prompt.split()[0] == "/hologram":
            event.input.clear()
            self.hologram_command(prompt.split()[1:])
            return
        if self.busy and prompt not in {"/help", "/sessions", "/exit"}:
            return
        event.input.clear()
        try:
            output = self.files.handle(prompt)
        except (OSError, ValueError, RuntimeError) as error:
            self.write_turn("system", str(error))
            return
        if output is not None:
            self.write_turn("system", output)
            self.update_status()
            return
        if prompt == "/exit":
            self.action_close_chat(0)
        elif prompt == "/help":
            self.write_turn("system", "Enter sends a prompt. Page Up and Page Down scroll the chat.\n"
                            "/sessions lists saved chats. /exit closes this view.\n"
                            "/hologram controls the AI core. Ctrl+H hides it; Ctrl+Left/Right rotates it.\n"
                            + FILE_HELP)
        elif prompt == "/sessions":
            try:
                sessions = self.agent.state.sessions()
            except sqlite3.Error as error:
                self.write_turn("system", f"Could not list sessions. {error}")
                return
            self.write_turn("system", "\n".join(f"{name}  ({count} turns)" for name, count in sessions)
                            or "No saved sessions yet.")
        else:
            self.welcome = False
            self.layout_hologram()
            self.streaming_row = None
            self.streaming_text = ""
            self.write_turn("user", prompt)
            if self.files.pending:
                self.write_turn("system", "Attached: " + ", ".join(path.name for path in self.files.pending))
            self.busy = True
            self.update_status()
            self.run_turn(prompt, tuple(self.files.pending))

    @work(thread=True)
    def run_turn(self, prompt, attachments=()):
        pending = []
        last = time.monotonic()

        def observe(event):
            nonlocal last
            if event.kind == "token":
                pending.append(event.text)
                if time.monotonic() - last < 0.05:
                    return
            if pending:
                self.post_message(RuntimeEvent(AgentEvent("token", text="".join(pending))))
                pending.clear()
                last = time.monotonic()
            if event.kind != "token":
                self.post_message(RuntimeEvent(event))

        try:
            response = self.agent.turn(
                self.session, prompt, self.learn,
                attachments=attachments,
                on_tool=lambda *args: self.post_message(ToolFinished(*args)),
                on_event=observe,
            )
        except Exception as error:
            self.post_message(TurnFinished(prompt, str(error) or type(error).__name__, failed=True))
        else:
            self.post_message(TurnFinished(prompt, response))

    def on_runtime_event(self, message: RuntimeEvent):
        event = message.event
        for core in self.query(HologramRenderer):
            core.handle_event(event)
        if event.kind == "artifact":
            item = json.loads(event.text)
            self.write_turn("system", f"Artifact: {item['name']}\nID: {item['id']}\n"
                            f"Path: {item['path']}\nUse /open {item['id']} to preview.")
        elif event.kind == "thinking":
            self.streaming_row = None
            self.streaming_text = ""
        elif event.kind == "token":
            if self.streaming_row is None:
                self.streaming_row = Static(classes="turn")
                self.query_one("#transcript", VerticalScroll).mount(self.streaming_row)
            self.streaming_text += safe_text(event.text)
            self.streaming_row.update(Text.assemble(("AGENT\n", f"bold {VIOLET}"), self.streaming_text))

    def on_tool_finished(self, event: ToolFinished):
        label = TOOL_LABELS.get(event.name, "Unavailable tool")
        target = " " + " ".join(safe_text(event.target).split()) if event.target else ""
        marker, color = ("✓", CYAN) if event.succeeded else ("!", AMBER)
        self.write_line(Text.assemble((f"  {marker} ", color), (label + target, MUTED)))

    def on_turn_finished(self, event: TurnFinished):
        self.busy = False
        if not event.failed:
            self.files.pending.clear()
        self.update_status()
        if self.streaming_row is not None and not event.failed:
            self.streaming_row.update(Text.assemble(("AGENT\n", f"bold {VIOLET}"), safe_text(event.response)))
        else:
            if self.streaming_row is not None:
                self.streaming_row.update(Text.assemble(("INCOMPLETE RESPONSE\n", f"bold {AMBER}"), self.streaming_text))
            self.write_turn("error" if event.failed else "assistant", event.response)
        if event.failed:
            field = self.query_one(Input)
            if not field.value:
                field.value = event.prompt
                field.cursor_position = len(event.prompt)

    def hologram_command(self, args):
        usage = ("/hologram show|hide|small|medium|large|pause|resume\n"
                 "/hologram auto|ascii|unicode|enhanced|image\n"
                 "/hologram left|right|up|down|status")
        if len(args) != 1:
            self.write_turn("system", usage)
            return
        value = args[0].lower()
        if value in {"show", "hide"}:
            self.hologram_visible = value == "show"
        elif value in {"small", "medium", "large"}:
            self.hologram_size = value
        elif value in {"auto", "ascii", "unicode", "enhanced", "image", "pause", "resume", "left", "right", "up", "down"}:
            for core in self.query(HologramRenderer):
                core.configure(value)
        elif value != "status":
            self.write_turn("system", usage)
            return
        self.layout_hologram()
        core = self.query_one("#welcome-core" if self.query_one("#welcome").display else "#orb", HologramRenderer)
        self.write_turn("system", f"Hologram: {'visible' if core.active else 'hidden / terminal too small'}; "
                        f"{core.requested_mode} → {core.mode}; {'animated' if core.animated else 'paused'}. " + core.reason)

    def action_toggle_hologram(self):
        self.hologram_visible = not self.hologram_visible
        self.layout_hologram()

    def action_rotate_hologram(self, direction):
        for core in self.query(HologramRenderer):
            core.configure(direction)

    def action_scroll_chat(self, direction):
        transcript = self.query_one("#transcript", VerticalScroll)
        if direction < 0:
            transcript.scroll_page_up()
        else:
            transcript.scroll_page_down()

    def action_close_chat(self, code):
        if self.busy:
            self.query_one("#shortcuts", Static).update(Text("Turn in progress. Exit when it finishes.", style=AMBER))
        else:
            self.exit(code)
