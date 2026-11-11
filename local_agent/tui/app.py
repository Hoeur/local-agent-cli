"""The Textual application: layout, keyboard handling, and the bridge to the agent.

Turns run on a daemon thread so input and animation stay responsive. The agent reports progress
through Agent(on_event=...) and those events are posted to the UI thread as messages. Cancelling
marks the turn's token; the next event raised from the agent stops it before anything is saved.
"""
import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from rich.console import Group
from rich.markdown import Markdown as RichMarkdown
from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.theme import Theme
from textual.widgets import Button, Collapsible, DirectoryTree, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .. import project_commands, providers, search
from .. import tools as toolkit
from ..agent import MAX_TOOL_ROUNDS, TOOLS, Agent, TurnCancelled, tool_names
from ..attachments import message_text
from ..config import load_config, read_settings, save_config, set_active
from ..file_commands import FILE_COMMANDS, FileCommands
from ..projects import KINDS, NoteConflict, ProjectContext, ProjectError, ProjectRegistry, draft_from_reply
from ..projects import record_outcome as record_task_outcome
from ..orchestrator import RunStore, TeamRun, preflight, result_text
from ..teams import TeamStore
from . import clipboard, commands, effects, hologram
from .holoview import HoloHud, HoloPanel, ViewControl, make_view
from .project_screens import (ChoiceScreen, ConfirmScreen, NoteEditor, NotesScreen, OutcomeScreen, ProjectHome,
                              ProjectsScreen)
from .screens import ActionScreen, ApprovalScreen, FilePreview, HelpScreen
from .team_screens import TeamRunPanel, TeamScreen
from .theme import (DEFAULT_PREFS, HOLOGRAM_WIDTHS, MOTION_FPS, MOTION_LEVELS, PALETTES, TEXTUAL_LEVEL, detect_glyphs,
                    detect_motion, save_prefs)
from .widgets import (ArtifactCard, AssistantMessage, AttachmentBar, ChatLog, Composer, ErrorCard, NoteCard, Sidebar, StatusBar, Suggestions,
                      TopBar, UserMessage, Welcome, clean, divider, split_reasoning, stamp)

HISTORY_TURNS = 40


class Clock:
    """One shared timer drives every animation, and it only runs while something is animating.

    fps is the cap. The timer itself runs at the rate the fastest subscriber asks for, with a floor of
    15 fps, so the 30 fps hologram costs nothing extra once it settles into its idle rate."""

    def __init__(self, app):
        self.app = app
        self.subscribers = set()
        self.timer = None
        self.fps = 15
        self.rate = 0
        self.start = time.monotonic()

    def now(self):
        return time.monotonic() - self.start

    def subscribe(self, widget):
        self.subscribers.add(widget)
        widget.frame(self.now())
        self.ensure()

    def unsubscribe(self, widget):
        self.subscribers.discard(widget)
        if not self.subscribers:
            self.stop()

    def desired_rate(self):
        fastest = min((widget.frame_interval() for widget in self.subscribers), default=1.0)
        wanted = round(1 / max(fastest, 1e-3))
        return max(min(self.fps, 15), min(self.fps, wanted))

    def ensure(self):
        if self.fps and self.timer is None and self.subscribers:
            self.rate = self.desired_rate()
            self.timer = self.app.set_interval(1 / self.rate, self.tick, name="animation-clock")

    def stop(self):
        if self.timer is not None:
            self.timer.stop()
            self.timer = None

    def set_fps(self, fps):
        self.stop()
        self.fps = fps
        self.ensure()
        self.tick()

    def tick(self):
        t = self.now()
        for widget in list(self.subscribers):
            if not widget.is_attached:
                self.subscribers.discard(widget)
                continue
            # Each widget sets its own pace; idle decorations update far less often than spinners.
            interval = widget.frame_interval()
            if self.fps == 0 or t - widget.last_frame >= interval - 0.01:
                widget.last_frame = t
                widget.frame(t)
        if self.timer is not None and self.desired_rate() != self.rate:
            self.stop()
            self.ensure()


class TurnToken:
    def __init__(self, project=""):
        self.project = project  # Events from a turn of another project are ignored.
        self.prompt = ""
        self.cancelled = False
        self.started = time.monotonic()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.estimated = False
        self.changes = 0
        self.compressed = False


class AgentEvent(Message):
    def __init__(self, token, kind, data):
        super().__init__()
        self.token, self.kind, self.data = token, kind, data


class TeamEvent(Message):
    """Something a team run reported, posted from its threads to the UI thread."""

    def __init__(self, kind, data):
        super().__init__()
        self.kind, self.data = kind, data


class TurnFinished(Message):
    def __init__(self, token, content, error):
        super().__init__()
        self.token, self.content, self.error = token, content, error


class AudioLevel(Message):
    """A voice input or output level for the hologram, posted from any thread."""

    def __init__(self, level):
        super().__init__()
        self.level = level


class ConnectionChecked(Message):
    def __init__(self, status, detail, announce):
        super().__init__()
        self.status, self.detail, self.announce = status, detail, announce


class ModelsListed(Message):
    def __init__(self, body):
        super().__init__()
        self.body = body


def git_branch(start: Path):
    """Read the current branch from .git/HEAD without running git."""
    for folder in (start, *start.parents):
        dot_git = folder / ".git"
        try:
            if dot_git.is_file():
                text = dot_git.read_text(encoding="utf-8").strip()
                if not text.startswith("gitdir:"):
                    return None
                dot_git = (folder / text[7:].strip()).resolve()
            if dot_git.is_dir():
                head = (dot_git / "HEAD").read_text(encoding="utf-8").strip()
                return head.rsplit("/", 1)[-1] if head.startswith("ref:") else head[:8]
        except OSError:
            return None
    return None


def error_hint(message):
    message = str(message)
    if "Could not reach" in message or "interrupted" in message:
        return "Is the model server running? Try /doctor, or `ollama serve` for a local model."
    if "HTTP 404" in message:
        return "Check the model name and base URL: /model lists models, or run setup again."
    if "No API key" in message:
        return "Quit, run the command above, then start the chat again — or /provider to switch."
    if "HTTP 401" in message or "HTTP 403" in message:
        return "Check the API key: `python -m local_agent key set <profile>`, or the provider's key variable."
    if "HTTP 402" in message or "quota" in message.lower() or "balance" in message.lower():
        return "The account is out of credit. Top up with the provider, or /provider to switch."
    if "HTTP 429" in message:
        return "Rate limited. Wait a moment and send again."
    if "context budget" in message:
        return "Start a /new session, or raise the budget with setup --context-chars."
    if "another process" in message:
        return "Send the message again."
    if "tool-call limit" in message or "empty reply" in message:
        return "Small models can loop on tools; try rephrasing more directly."
    return ""


class AgentApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "Local Agent"
    COMMANDS = App.COMMANDS | {commands.AgentCommands}
    COMMAND_PALETTE_BINDING = "ctrl+p"
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Copy selection / cancel", priority=True, show=False),
        Binding("escape", "escape", "Cancel / close", show=False),
        Binding("ctrl+b,f2", "toggle_sidebar", "Sidebar", priority=True),
        Binding("ctrl+o", "expand_all", "Tool output", priority=True),
        Binding("ctrl+t", "next_theme", "Theme", priority=True),
        Binding("ctrl+l", "clear_view", "Clear", priority=True),
        Binding("ctrl+n", "new_session", "New session", priority=True),
        Binding("f1", "help", "Help", priority=True),
        Binding("f3", "toggle_hologram", "Hologram", priority=True),
        Binding("f4", "copy_reply", "Copy reply", priority=True),
        Binding("f5", "projects", "Projects", priority=True),
        Binding("f6", "project_home", "Project home", priority=True),
        Binding("f7", "notes", "Notes", priority=True),
        Binding("f8", "teams", "Teams", priority=True),
        Binding("f9", "save_reply", "Save reply to project", priority=True),
        Binding("alt+left", "rotate_hologram(-0.25, 0)", "Rotate", priority=True, show=False),
        Binding("alt+right", "rotate_hologram(0.25, 0)", "Rotate", priority=True, show=False),
        Binding("alt+up", "rotate_hologram(0, -0.18)", "Tilt", priority=True, show=False),
        Binding("alt+down", "rotate_hologram(0, 0.18)", "Tilt", priority=True, show=False),
        Binding("pageup", "scroll_chat(-1)", "Scroll up", priority=True, show=False),
        Binding("pagedown", "scroll_chat(1)", "Scroll down", priority=True, show=False),
        Binding("ctrl+end", "scroll_bottom", "Latest", priority=True, show=False),
        Binding("ctrl+home", "scroll_top", "Oldest", priority=True, show=False),
        Binding("ctrl+d", "eof", "Quit", show=False),
    ]

    def __init__(self, provider, state, data_dir: Path, config, session, workspace=None, learn=False,
                 auto_approve=False, prefs=None, environ=None, image_support=None, write=False, shell=False,
                 web=False, max_rounds=MAX_TOOL_ROUNDS, attachments=(), project=None, draft=None):
        super().__init__()
        # Projects: the registry, the open project (None for the classic sessions) and what each
        # project left behind when the user switched away (draft, attachments, session).
        self.registry = ProjectRegistry(data_dir)
        self.project = project
        self.project_generation = 0
        self.stash = {}
        self.context_sources = []
        self.initial_draft = draft
        self.base_state = state
        self.classic_workspace = workspace.resolve() if workspace is not None and project is None else None
        self.launch_flags = {"write": write, "shell": shell, "web": web}
        # Team mode: None is the usual single agent. A running team run blocks new turns like a turn does.
        self.team_mode = None
        self.team_run = None
        self.team_approval_mount = None
        self.team_panel = None
        self.team_prompt = ""
        self.team_provider_factory = None  # Tests swap in scripted models; None builds real providers.
        self.file_commands = FileCommands(state, attachments)
        environ = os.environ if environ is None else environ
        self.environ = environ
        self.provider = provider
        self.state = state
        self.data_dir = data_dir
        self.config = config
        self.session = session
        self.workspace = workspace.resolve() if workspace is not None else None
        self.learn = learn
        self.auto_approve = auto_approve
        self.write = write and self.workspace is not None
        self.shell = shell and self.workspace is not None
        self.web = web
        self.max_rounds = max_rounds
        self.prefs = {**DEFAULT_PREFS, **(prefs or {})}
        self.palettes = PALETTES
        self.theme_name = self.prefs.get("theme", "neon")
        self.motion = detect_motion(environ, self.prefs.get("motion"))
        self.glyphs = detect_glyphs(environ)
        self.animation_level = TEXTUAL_LEVEL[self.motion]
        self.clock = Clock(self)
        self.clock.fps = MOTION_FPS[self.motion]
        # The holographic core: one state machine, scene and rotation shared by every view.
        self.holo = hologram.Director(lambda: self.clock.now())
        self.holo_scene = hologram.Scene()
        self.holo_control = ViewControl()
        self.image_support = image_support
        self.hologram_visible = self.prefs["hologram"]
        self.hologram_mode = self.prefs["hologram_mode"]
        self.hologram_width = self.prefs["hologram_width"]
        self.hologram_animate = self.prefs["hologram_animate"]
        self.hologram_renderer, self.hologram_reason = hologram.choose_renderer(
            self.hologram_mode, environ, self.glyphs.orb != "@", image_support)
        self.focused_terminal = True
        self.token = None
        self.current = None
        self.refs = []
        self.file_index = None
        self.file_index_time = 0.0
        self.sidebar_visible = self.prefs.get("sidebar", True)
        self.sidebar_width = self.prefs.get("sidebar_width", 34)
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.estimated = False
        self.connection = "checking"
        self.last_reply = ""
        self.recoverable_prompt = ""
        self.path_agent = Agent(None, state, self.workspace) if self.workspace is not None else None
        for name, palette in PALETTES.items():
            self.register_theme(Theme(
                name=f"agent-{name}", primary=palette["primary"], secondary=palette["secondary"],
                accent=palette["accent"], background=palette["background"], surface=palette["surface"],
                panel=palette["panel"], foreground=palette["foreground"], success=palette["success"],
                warning=palette["warning"], error=palette["error"], dark=not palette.get("light"),
                variables={
                    "user-color": palette["user"], "assistant-color": palette["assistant"],
                    "muted-color": palette["muted"], "block-cursor-background": palette["primary"],
                    "footer-key-foreground": palette["primary"], "input-selection-background":
                        effects.blend(palette["background"], palette["secondary"], 0.45),
                },
            ))
        self.theme = f"agent-{self.theme_name}"

    @property
    def palette(self):
        return PALETTES[self.theme_name]

    @property
    def busy(self):
        return self.token is not None or self.team_run is not None

    # ── Layout ────────────────────────────────────────────────────────────────────────────

    def compose(self):
        yield TopBar(id="topbar")
        with Horizontal(id="main"):
            yield Sidebar(self.workspace or Path.cwd(), id="sidebar")
            with Vertical(id="center"):
                yield ChatLog(id="chat")
                with Vertical(id="composer-area"):
                    with Horizontal(id="copy-tools"):
                        yield Button("Send", variant="primary", id="send-prompt", compact=True, flat=True,
                                     action="app.send_prompt", tooltip="Send the current prompt (Enter)")
                        yield Button("Stop", variant="error", id="stop-turn", compact=True, flat=True,
                                     action="app.stop_reply", tooltip="Cancel the current turn (Esc)")
                        yield Button("Restore prompt", id="restore-prompt", compact=True, flat=True,
                                     action="app.restore_prompt", tooltip="Restore the failed or cancelled prompt for editing")
                        yield Button("Copy reply", id="copy-reply", compact=True, flat=True,
                                     action="app.copy_reply", tooltip="Copy the latest reply (F4)")
                        yield Button("Save…", id="save-reply", compact=True, flat=True,
                                     action="app.save_reply",
                                     tooltip="Save the latest reply as an idea, note, decision or task (F9)")
                        yield Static("", id="copy-hint")
                    yield Suggestions(id="suggestions")
                    yield AttachmentBar(id="attachment-bar")
                    yield Composer(id="composer", placeholder="Ask anything  ·  / commands  ·  @ files"
                                   if self.workspace is not None else "Ask anything  ·  / for commands")
            yield HoloPanel(id="holo-panel")
        yield StatusBar(id="status")

    async def on_mount(self):
        top = self.query_one(TopBar)
        top.model = self.config["model"]
        top.session = self.session
        self.refresh_project_labels()
        status = self.query_one(StatusBar)
        status.learn = self.learn
        status.budget = self.config["context_chars"]
        self.query_one("#suggestions").display = False
        sidebar = self.query_one(Sidebar)
        sidebar.styles.width = self.sidebar_width
        self.apply_layout()
        self.refresh_sidebar()
        await self.load_session(fade=False)
        await self.refresh_attachments()
        if self.project is not None and self.state.conversation(self.session)["kind"] == "brainstorm" \
                and not self.state.turn_count(self.session):
            await self.note("Describe the problem or goal. The agent will restate it, explore alternatives, "
                            "compare tradeoffs, list open questions and suggest next steps. Press **f9** to save "
                            "an idea, note, decision or task from a reply; you edit it before it is saved.",
                            "Brainstorm")
        self.load_team_mode()
        if self.initial_draft:
            composer = self.query_one(Composer)
            composer.load_text(self.initial_draft)
            composer.move_cursor(composer.document.end)
        self.refresh_composer_controls()
        self.query_one(Composer).focus()
        self.check_connection(False)

    def on_resize(self, event):
        self.apply_layout(event.size)

    def apply_layout(self, size=None):
        width, height = size or self.size
        screen = self.screen
        screen.set_class(width < 100, "-narrow")
        screen.set_class(width < 64 or height < 18, "-tiny")
        sidebar = self.query_one(Sidebar)
        if width >= 100:
            # Wide terminals follow the saved preference; a narrow-screen overlay is temporary.
            sidebar.remove_class("-overlay")
            self.sidebar_visible = self.prefs["sidebar"]
            sidebar.display = self.sidebar_visible
        elif not sidebar.has_class("-overlay"):
            self.sidebar_visible = False
            sidebar.display = False
        self.layout_hologram(width, height)

    def layout_hologram(self, width=None, height=None):
        """Show the compact hologram beside the chat when it fits, shrinking it before hiding it.
        The welcome screen has its own, larger hologram, so the panel waits until it is gone."""
        if width is None:
            width, height = self.size
        panel = next(iter(self.query(HoloPanel)), None)
        sidebar = next(iter(self.query(Sidebar)), None)
        if panel is None or sidebar is None:
            return  # A renderer change can finish while the screen is unmounting.
        # A docked sidebar takes layout space whether it is pinned or opened as an overlay.
        docked = self.sidebar_width if self.sidebar_visible or sidebar.display else 0
        room = width - docked - 58  # Keep at least 58 columns for the conversation.
        panel_width = min(self.hologram_width, room)
        show = (self.hologram_visible and not self.query(Welcome) and height >= 16
                and panel_width >= HOLOGRAM_WIDTHS[0])
        if show:
            panel.styles.width = panel_width + 1  # Plus the left border.
            view = next(iter(panel.query("#side-holo")), None)
            if view is not None:
                view.styles.height = max(7, min(height - 9, round(panel_width / 2.15)))
        panel.display = show
        for welcome in self.query(Welcome):
            welcome.layout_hero()
        self.refresh_hologram()

    # ── Sessions and history ──────────────────────────────────────────────────────────────

    async def load_session(self, fade=True, limit=HISTORY_TURNS):
        chat = self.query_one(ChatLog)
        await chat.remove_children()
        self.last_reply = ""
        self.refs.clear()
        self.query_one(TopBar).session = self.session
        turns = []
        for message in self.state.transcript(self.session):
            if message.get("role") == "user":
                turns.append([message])
            elif turns:
                turns[-1].append(message)
        if not turns:
            await chat.mount(Welcome(self.welcome_info(), id="welcome"))
        else:
            hidden = max(0, len(turns) - limit) if limit else 0
            note = f"session {self.session} · {len(turns)} turn{'s' * (len(turns) != 1)}"
            if hidden:
                note += f" · {hidden} earlier hidden — /history shows all"
            await chat.mount(NoteCard(divider(note, self), "divider"))
            for turn in turns[hidden:]:
                await self.render_turn(chat, turn)
        self.layout_hologram()
        self.refresh_composer_controls()
        chat.anchor()
        if fade and self.motion != "off":
            chat.styles.opacity = 0.0
            chat.styles.animate("opacity", 1.0, duration=0.3)

    async def render_turn(self, chat, turn):
        await chat.mount(UserMessage(message_text(turn[0])))
        reply = AssistantMessage(when="", live=False)
        await chat.mount(reply)
        for message in turn[1:]:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    await reply.start_tool(call["id"], call["function"]["name"], call["function"]["arguments"])
                if message.get("content") and not message.get("tool_calls"):
                    await reply.set_content(message["content"])
                    self.last_reply = message["content"]
                    if message.get("workspace_writes"):
                        await reply.body.mount(NoteCard(Text(clean(message["workspace_writes"]))))
            elif message.get("role") == "tool":
                content = message.get("content", "")
                error = None
                try:
                    parsed = json.loads(content)
                    error = parsed.get("error") if isinstance(parsed, dict) else None
                except ValueError:
                    pass
                reply.end_tool(message.get("tool_call_id"), content, error)
        await reply.finish()

    def welcome_info(self):
        sessions = dict(self.state.sessions())
        info = [("project", self.project.name)] if self.project is not None else []
        info += [("model", self.config["model"]),
                ("provider", self.provider_label()),
                ("endpoint", urlsplit(self.config["base_url"]).netloc or self.config["base_url"]),
                ("session", f"{self.session} (new)" if self.session not in sessions else self.session),
                ("workspace", str(self.workspace) if self.workspace else "off — pass --workspace"),
                ("learning", "on — changes need approval" if self.learn and not self.auto_approve
                 else "on — auto-save" if self.learn else "off — /learn on"),
                ("actions", self.actions_label())]
        return info

    def actions_label(self):
        enabled = [name for name, on in (("files", self.write), ("shell", self.shell), ("web", self.web)) if on]
        if not enabled:
            return "read-only — /write · /shell · /web"
        return " · ".join(enabled) + (" — you approve each change" if not self.auto_approve
                                      else " — auto, dangerous commands ask")

    async def switch_session(self, name):
        name = name.strip()
        if not name or len(name) > 100:
            self.notify("Session names must contain between 1 and 100 characters.", severity="error")
            return
        if self.busy:
            self.notify("Wait for the current turn, or press esc to cancel it.", severity="warning")
            return
        if name != self.session:
            self.recoverable_prompt = ""
        self.session = name
        self.remember_session()
        await self.load_session()
        self.refresh_sidebar()
        kind = self.state.conversation(name)["kind"] if self.project is not None else "chat"
        self.notify(f"Session {name}" + (f" · {kind}" if kind != "chat" else ""), title="Switched", timeout=2)

    # ── Sidebar ───────────────────────────────────────────────────────────────────────────

    def refresh_sidebar(self):
        palette, glyphs = self.palette, self.glyphs
        sidebar = self.query_one(Sidebar)
        title = Text(" ")
        title.append_text(effects.gradient_text("NAVIGATOR", palette["gradient"], 0, True, False))
        sidebar.query_one("#sidebar-title", Static).update(title)
        session_list = sidebar.query_one("#session-list", OptionList)
        sessions = self.state.sessions()
        options = []
        names = [name for name, _ in sessions]
        if self.session not in names:
            sessions = [(self.session, 0), *sessions]
        for name, count in sessions:
            prompt = Text()
            current = name == self.session
            prompt.append(f"{glyphs.dot_on if current else glyphs.dot_off} ",
                          Style(color=palette["primary"] if current else palette["muted"]))
            prompt.append(name[:22], Style(bold=current))
            prompt.append(f"  {count}", Style(color=palette["muted"]))
            options.append(Option(prompt, id=f"session:{name}"))
        session_list.clear_options()
        session_list.add_options(options)
        self.refresh_tools()
        model = Text()
        for label, value in (("model", self.config["model"]),
                             ("provider", self.config.get("profile") or self.config.get("provider") or "?"),
                             ("endpoint", urlsplit(self.config["base_url"]).netloc or "?"),
                             ("budget", f"{self.config['context_chars']:,} chars"),
                             ("stream", "on" if getattr(self.provider, "streaming", False) else "off"),
                             ("status", self.connection)):
            model.append(f"{label:<9}", Style(color=palette["muted"]))
            model.append(f"{value}\n")
        sidebar.query_one("#model-info", Static).update(model)
        library = Text()
        try:
            memories = self.state.library("memory")
            skills = self.state.library("skill")
        except sqlite3.Error:
            memories, skills = {}, {}
        library.append(f"memories {len(memories)}/20\n", Style(color=palette["primary"]))
        for name in list(memories)[:8]:
            library.append(f"  {glyphs.arrow} {clean(name)}\n", Style(color=palette["muted"]))
        library.append(f"skills   {len(skills)}/20\n", Style(color=palette["secondary"]))
        for name in list(skills)[:8]:
            library.append(f"  {glyphs.arrow} {clean(name)}\n", Style(color=palette["muted"]))
        sidebar.query_one("#library-info", Static).update(library)
        self.refresh_project_info()

    def refresh_tools(self, running=None):
        palette, glyphs = self.palette, self.glyphs
        enabled = set()
        if self.max_rounds:
            enabled.update(tool_names(self.learn, self.workspace is not None, self.write, self.shell, self.web,
                                      bool(self.config.get("image_model"))))
        text = Text()
        for name in TOOLS:
            if name == running:
                text.append(f"{glyphs.tool} {name}\n", Style(color=palette["accent"], bold=True))
            elif name in enabled:
                text.append(f"{glyphs.dot_on} ", Style(color=palette["success"]))
                text.append(f"{name}\n")
            else:
                text.append(f"{glyphs.dot_off} {name}\n", Style(color=palette["muted"]))
        self.query_one("#tool-list", Static).update(text)

    def on_option_list_option_selected(self, event):
        if event.option_list.id == "session-list" and event.option.id:
            self.run_worker(self.switch_session(event.option.id.split(":", 1)[1]), exclusive=True)
        elif event.option_list.id == "suggestions":
            self.accept_suggestion(submit=False)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected):
        self.preview(event.path)

    def preview(self, path: Path):
        root = self.workspace or Path.cwd()
        try:
            label = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            label = path.name
        self.push_screen(FilePreview(path, label, self.workspace is not None))

    def register_ref(self, path):
        self.refs.append(path)
        return len(self.refs) - 1

    def action_open_ref(self, index):
        if not (0 <= index < len(self.refs)) or self.path_agent is None:
            return
        try:
            target = self.path_agent.workspace_path(self.refs[index])
        except (OSError, ValueError) as error:
            self.notify(f"Can't open {self.refs[index]}: {error}", severity="warning")
            return
        if target.is_file():
            self.preview(target)
        else:
            self.notify(f"{self.refs[index]} is a directory.", timeout=2)

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked):
        href = event.href
        if href.startswith(("http://", "https://")):
            self.open_url(href)
        elif self.path_agent is not None:
            self.action_open_ref(self.register_ref(href.split("#")[0]))

    def insert_mention(self, label):
        composer = self.query_one(Composer)
        composer.insert(f"@{label} ")
        composer.focus()

    def refresh_project_info(self):
        palette, glyphs = self.palette, self.glyphs
        info = Text()
        muted = Style(color=palette["muted"])
        if self.project is None:
            info.append("No project open.\n", Style(color=palette["foreground"]))
            info.append("f5 open or register one\n", muted)
        else:
            info.append(f"{self.project.name}\n", Style(color=palette["primary"], bold=True))
            info.append(f"{self.state.conversation(self.session)['kind']} conversation\n", muted)
            try:
                notes = self.project.knowledge.notes()
            except (OSError, ProjectError):
                notes = []
            open_tasks = sum(note.kind == "task" and note.status != "completed" for note in notes)
            info.append(f"{len(notes)} notes · {open_tasks} open tasks\n", muted)
            selected = self.state.conversation(self.session)["notes"]
            if selected:
                info.append("selected\n", Style(color=palette["secondary"]))
                for identifier in selected[:6]:
                    info.append(f"  {glyphs.arrow} {clean(identifier)[:26]}\n", muted)
            included = [source for source in self.context_sources if source.get("included")]
            if included:
                info.append("last context\n", Style(color=palette["secondary"]))
                for source in included[:6]:
                    info.append(f"  {glyphs.arrow} {clean(source['id'])[:26]}\n", muted)
            info.append("f6 home · f7 notes · f9 save", muted)
        self.query_one("#project-info", Static).update(info)
        self.query_one("#project-section").title = f"Project · {self.project.name[:18]}" if self.project else "Project"

    # ── Composer, suggestions and slash commands ──────────────────────────────────────────

    def workspace_files(self):
        if self.workspace is None:
            return []
        if self.file_index is None or time.monotonic() - self.file_index_time > 30:
            files = []
            for folder, directories, names in os.walk(self.workspace):
                directories[:] = sorted(d for d in directories if not d.startswith(".")
                                        and d not in {"__pycache__", "node_modules"})
                relative = Path(folder).relative_to(self.workspace)
                if len(relative.parts) > 5:
                    directories[:] = []
                files.extend((relative / name).as_posix() for name in sorted(names) if not name.startswith("."))
                if len(files) > 3000:
                    break
            self.file_index, self.file_index_time = files, time.monotonic()
        return self.file_index

    def suggestion_items(self):
        composer = self.query_one(Composer)
        text = composer.text
        token, _, _ = composer.current_token()
        if text.startswith("/") and "\n" not in text:
            name, space, argument = text[1:].partition(" ")
            if not space:
                return [(f"/{command.name}", command.usage, command.help) for command in commands.COMMANDS
                        if command.name.startswith(name.lower())]
            choices = {
                "theme": list(PALETTES), "motion": list(MOTION_LEVELS), "learn": ["on", "off"],
                "sidebar": ["on", "off"], "session": [name for name, _ in self.state.sessions()],
                "hologram": ["on", "off", *hologram.MODES, *hologram.SIZES, "still", "live", "reset", "status"],
                "skill": list(self.state.library("skill")),
                "todo": list(toolkit.TODO_ACTIONS),
                **self.project_choices(name.lower()),
            }.get(name.lower(), [])
            return [(f"/{name} {choice}", choice, "") for choice in choices if choice.startswith(argument)
                    and choice != argument]
        if token.startswith("@") and self.workspace is not None:
            needle = token[1:].lower()
            matches = [path for path in self.workspace_files() if needle in path.lower()]
            matches.sort(key=lambda path: (not path.lower().startswith(needle), len(path)))
            return [(f"@{path}", path, "file") for path in matches[:12]]
        return []

    def project_choices(self, name):
        """Autocomplete values for project commands (only computed for the command being typed)."""
        if name == "project":
            try:
                return {name: [project.id for project in self.registry.projects()] + ["none"]}
            except ProjectError:
                return {name: ["none"]}
        if name in {"save", "create"}:
            return {name: ["idea", "note", "decision", "task"] + (["overview"] if name == "create" else [])}
        if name == "notes":
            return {name: ["tasks", "decisions", "ideas", "notes", "overview"]}
        if name == "context":
            return {name: ["add", "remove", "clear"]}
        if name == "team":
            try:
                names = list(TeamStore(self.data_dir, self.project.id).teams()) if self.project else []
            except ProjectError:
                names = []
            return {name: [*names, "off", "runs", "result", "run"]}
        if self.project is None or name not in {"note", "implement", "outcome"}:
            return {}
        try:
            kinds = ["task"] if name != "note" else None
            notes = self.project.knowledge.notes(kinds)
        except (OSError, ProjectError):
            return {}
        return {name: [note.id for note in notes if name == "note" or note.status != "completed"]}

    def on_text_area_changed(self, event):
        if event.text_area.id != "composer":
            return
        self.update_suggestions()
        self.refresh_composer_controls()

    def refresh_composer_controls(self):
        composer = self.query_one(Composer)
        send = self.query_one("#send-prompt", Button)
        send.display = not self.busy
        send.disabled = not composer.text.strip()
        self.query_one("#stop-turn").display = self.busy
        restore = self.query_one("#restore-prompt", Button)
        restore.display = bool(self.recoverable_prompt) and not self.busy
        restore.disabled = bool(composer.text)
        self.query_one("#copy-reply", Button).disabled = not self.last_reply
        save = self.query_one("#save-reply", Button)
        save.display = self.project is not None
        save.disabled = not self.last_reply
        if self.team_run is not None:
            hint = "Team working · draft your next message · Esc stop"
        elif self.busy:
            hint = "Working · draft your next message · Esc stop"
        elif self.recoverable_prompt:
            hint = ("Clear draft to restore previous prompt" if composer.text else
                    "Restore to edit · completed actions may remain")
        elif self.team_mode and self.project is not None:
            hint = f"Enter starts team {self.team_mode} · /team off for one agent"
        else:
            hint = "Enter send · Ctrl+J newline"
        self.query_one("#copy-hint", Static).update(hint)

    def action_send_prompt(self):
        composer = self.query_one(Composer)
        composer.focus()
        if not self.busy:
            composer.submit()

    async def action_stop_reply(self):
        await self.cancel_turn()
        self.query_one(Composer).focus()

    def action_restore_prompt(self):
        composer = self.query_one(Composer)
        if self.busy or not self.recoverable_prompt:
            return
        if composer.text:
            self.notify("Keep or clear your draft before restoring the previous prompt.", severity="warning")
            return
        composer.load_text(self.recoverable_prompt)
        composer.move_cursor(composer.document.end)
        self.recoverable_prompt = ""
        self.refresh_composer_controls()
        composer.focus()
        self.notify("Prompt restored for editing. Review completed actions before sending again.", timeout=5)

    def update_suggestions(self):
        composer = self.query_one(Composer)
        box = self.query_one("#suggestions", Suggestions)
        items = self.suggestion_items()
        palette = self.palette
        self.suggestion_values = [value for value, _, _ in items]
        if not items:
            box.display = False
            composer.suggesting = False
            return
        options = []
        for value, label, help_text in items:
            prompt = Text()
            prompt.append(label, Style(color=palette["primary"], bold=True))
            if help_text:
                prompt.append(f"   {help_text}", Style(color=palette["muted"]))
            options.append(Option(prompt))
        box.clear_options()
        box.add_options(options)
        box.highlighted = 0
        box.display = True
        composer.suggesting = True

    def hide_suggestions(self):
        self.query_one("#suggestions").display = False
        self.query_one(Composer).suggesting = False

    def on_composer_navigate(self, message: Composer.Navigate):
        box = self.query_one("#suggestions", Suggestions)
        if message.key == "escape":
            self.hide_suggestions()
        elif message.key == "up":
            box.action_cursor_up()
        elif message.key == "down":
            box.action_cursor_down()
        else:
            self.accept_suggestion(submit=message.key == "enter")

    def accept_suggestion(self, submit):
        composer = self.query_one(Composer)
        box = self.query_one("#suggestions", Suggestions)
        index = box.highlighted or 0
        if not self.suggestion_values:
            return
        value = self.suggestion_values[min(index, len(self.suggestion_values) - 1)]
        if value.startswith("@"):
            _, start, end = composer.current_token()
            composer.replace(value + " ", start, end)
        else:
            parsed = commands.parse(value)
            needs_argument = parsed and parsed[1] is not None and not parsed[1] and \
                "<" in commands.BY_NAME[parsed[0]].usage
            composer.load_text(value + (" " if needs_argument else ""))
            composer.move_cursor(composer.document.end)
            if submit and not needs_argument:
                self.hide_suggestions()
                composer.post_message(Composer.Submitted(value))
                return
        self.hide_suggestions()

    async def on_composer_submitted(self, message: Composer.Submitted):
        composer = self.query_one(Composer)
        text = message.text
        if text.startswith("/") and commands.parse(text.split("\n")[0]) is not None:
            composer.load_text("")
            self.hide_suggestions()
            await self.run_slash(text.strip())
            return
        if self.busy:
            self.notify("A turn is still running. Press esc to cancel it.", severity="warning", timeout=3)
            return
        if len(text) > self.config["context_chars"]:
            self.notify(f"Prompts can be at most {self.config['context_chars']:,} characters.", severity="error")
            return
        if self.team_mode and self.project is not None:
            self.confirm_team_run(self.team_mode, text, clear=True)
            return
        composer.load_text("")
        self.hide_suggestions()
        await self.start_turn(text)

    async def run_slash(self, text):
        name, argument = commands.parse(text) or (None, None)
        chat = self.query_one(ChatLog)
        if argument is None:
            self.notify(f"Unknown command /{name}. Try /help.", severity="error", timeout=3)
            return
        if name in FILE_COMMANDS:
            if self.busy and name in {"attach", "detach"}:
                self.notify("Wait for the current turn before changing attachments.", severity="warning")
                return
            if name == "artifacts" and not argument:
                await self.show_artifacts()
                return
            try:
                if name == "open":
                    body = await asyncio.to_thread(self.file_commands.handle, f"/{name} {argument}")
                else:
                    body = self.file_commands.handle(f"/{name} {argument}")
            except (OSError, RuntimeError, ValueError) as error:
                self.notify(clean(str(error)), severity="error")
                return
            await self.refresh_attachments()
            await self.note(body, "Files", literal=True)
        elif name == "help":
            self.action_help()
        elif name == "exit":
            await self.action_quit()
        elif name == "new":
            await self.switch_session(argument or datetime.now().strftime("chat-%m%d-%H%M%S"))
        elif name == "session":
            if not argument:
                self.notify("Usage: /session <name>", severity="warning")
            else:
                await self.switch_session(argument)
        elif name == "sessions":
            rows = ["| | Session | Turns |", "|---|---|---|"]
            for session, count in self.state.sessions():
                rows.append(f"| {'●' if session == self.session else ''} | {session} | {count} |")
            await self.note("\n".join(rows) if len(rows) > 2 else "_No saved sessions yet._", "Sessions")
        elif name == "history":
            if self.busy:
                self.notify("Wait for the current turn first.", severity="warning")
            else:
                await self.load_session(limit=0)
        elif name == "clear":
            await self.action_clear_view()
        elif name == "memory":
            memory = self.state.library("memory")
            body = "\n".join(f"- **{clean(key)}** — {clean(value)}" for key, value in memory.items())
            await self.note(body or "_No saved memories. Turn on /learn and ask the agent to remember._",
                            f"Memory · {len(memory)}/20")
        elif name == "skills":
            skills = self.state.library("skill")
            body = "\n".join(f"- **{clean(key)}** — {clean(entry['description'])}" for key, entry in skills.items())
            await self.note(body or "_No saved skills yet._", f"Skills · {len(skills)}/20")
        elif name == "skill":
            skills = self.state.library("skill")
            if argument not in skills:
                self.notify(f"Unknown skill {argument!r}.", severity="error")
            else:
                await self.note(f"_{clean(skills[argument]['description'])}_\n\n```text\n"
                                f"{clean(skills[argument]['steps'])}\n```", f"Skill · {argument}")
        elif name == "todo":
            await self.todo_command(argument)
        elif name == "search":
            if not argument:
                self.notify("Usage: /search <words>", severity="warning")
                return
            results = self.state.search(argument)
            body = "\n".join(f"- **{clean(r['session'])}** #{r['turn']} _{r['role']}_ — {clean(r['excerpt'])}"
                             for r in results)
            await self.note(body or "_No matches._", f"Search · {clean(argument)}")
        elif name == "forget":
            if self.busy:
                self.notify("Wait for the current turn first.", severity="warning")
            elif argument not in self.state.library("memory"):
                self.notify(f"Unknown memory {argument!r}.", severity="error")
            else:
                self.state.update_library({("memory", argument): None})
                self.refresh_sidebar()
                self.notify(f"Deleted memory {argument}.", timeout=2)
        elif name in {"write", "shell", "web"}:
            if self.busy:
                self.notify("Wait for the current turn first.", severity="warning")
                return
            if name != "web" and self.workspace is None:
                self.notify(f"/{name} needs a workspace. Restart with --workspace <folder>.", severity="error")
                return
            value = {"on": True, "off": False}.get(argument.lower(), not getattr(self, name))
            setattr(self, name, value)
            if self.project is not None:
                try:
                    self.registry.update_settings(self.project.id, tools={key: getattr(self, key)
                                                                          for key in ("write", "shell", "web")})
                except ProjectError:
                    pass
            self.refresh_tools()
            label = {"write": "File changes", "shell": "Shell commands", "web": "Web access"}[name]
            detail = ("You approve each one." if name != "web" else
                      f"Search with {self.search_label()} and read public pages.") if value else ""
            self.notify(detail or "Turned off.", title=f"{label} {'on' if value else 'off'}", timeout=3)
        elif name == "learn":
            if self.busy:
                self.notify("Wait for the current turn first.", severity="warning")
                return
            self.learn = {"on": True, "off": False}.get(argument.lower(), not self.learn)
            self.query_one(StatusBar).learn = self.learn
            self.refresh_tools()
            self.notify("The agent may propose memory and skill changes; you approve each one." if self.learn
                        else "Memory and skill changes are off.", title=f"Learning {'on' if self.learn else 'off'}",
                        timeout=3)
        elif name == "theme":
            if not argument:
                await self.note("\n".join(f"- `{key}` — {value['label']}" + (" ✓" if key == self.theme_name else "")
                                          for key, value in PALETTES.items()), "Themes")
            elif argument in PALETTES:
                self.set_theme(argument)
            else:
                self.notify(f"Unknown theme {argument!r}. Try /theme.", severity="error")
        elif name == "motion":
            level = argument.lower() or {"full": "reduced", "reduced": "off", "off": "full"}[self.motion]
            if level not in MOTION_LEVELS:
                self.notify("Use /motion full, reduced or off.", severity="error")
            else:
                self.set_motion(level)
        elif name == "sidebar":
            if argument.isdigit():
                self.resize_sidebar(int(argument) - self.sidebar_width)
            elif argument in {"on", "off"}:
                if (argument == "on") != self.sidebar_visible:
                    self.action_toggle_sidebar()
            else:
                self.action_toggle_sidebar()
        elif name == "hologram":
            await self.hologram_command(argument)
        elif name == "expand":
            self.action_expand_all()
        elif name == "doctor":
            self.notify("Checking the model endpoint…", timeout=2)
            self.check_connection(True)
        elif name == "provider":
            await self.provider_command(argument)
        elif name == "model":
            await self.model_command(argument)
        elif name == "copy":
            self.action_copy_reply()
        elif name in project_commands.PROJECT_COMMANDS or name in {"create", "team"}:
            await self.project_slash(name, argument)
        chat.anchor()

    async def project_slash(self, name, argument):
        """/projects, /project, /notes, /note, /create, /tasks, /decisions, /find, /save, /context,
        /brainstorm, /implement and /outcome."""
        if name == "projects":
            self.action_projects()
            return
        if name == "project":
            if not argument:
                self.action_project_home()
            elif argument.lower() in {"none", "classic", "off"}:
                await self.open_project(None)
            else:
                try:
                    project = self.registry.find(argument)
                except ProjectError as error:
                    self.notify(clean(str(error)), severity="error", timeout=6)
                    return
                await self.open_project(project, show_home=True)
            return
        if self.project is None:
            self.notify("Open a project first: f5, or /project NAME.", severity="warning", timeout=4)
            return
        if name == "team":
            await self.team_command(argument)
            return
        if name in {"notes", "tasks", "decisions"}:
            kind = {"tasks": "task", "decisions": "decision"}.get(name)
            if name == "notes" and argument:
                try:
                    kind = project_commands.kind_of(argument)
                except ProjectError:
                    self.push_screen(NotesScreen(query=argument))
                    return
            self.push_screen(NotesScreen(kind=kind))
        elif name == "find":
            if not argument:
                self.notify("Usage: /find <words>", severity="warning")
            else:
                self.push_screen(NotesScreen(query=argument))
        elif name == "note":
            if not argument:
                self.push_screen(NotesScreen())
                return
            try:
                note = self.project.knowledge.find(argument)
            except ProjectError as error:
                self.notify(clean(str(error)), severity="error", timeout=5)
                return
            self.push_screen(NotesScreen(kind=note.kind, select=note.id, view=True))
        elif name == "create":
            kind, _, title = argument.partition(" ")
            try:
                kind = project_commands.kind_of(kind or "note")
            except ProjectError as error:
                self.notify(clean(str(error)), severity="error")
                return
            if kind == "overview" and self.project.knowledge.overview() is not None:
                self.push_screen(NoteEditor(note=self.project.knowledge.overview()))
                return
            self.edit_new_note(kind, title.strip() or (self.project.name if kind == "overview" else ""))
        elif name == "save":
            kind = argument.split(" ", 1)[0] if argument else None
            if kind:
                try:
                    kind = project_commands.kind_of(kind)
                except ProjectError as error:
                    self.notify(clean(str(error)), severity="error")
                    return
            self.save_reply(kind, argument.partition(" ")[2].strip() or None)
        elif name == "context":
            await self.context_command(argument)
        elif name == "brainstorm":
            await self.start_conversation("brainstorm", argument or None)
        elif name == "implement":
            if not argument:
                self.push_screen(NotesScreen(kind="task"))
                self.notify("Choose a task and press i to implement it.", timeout=3)
            else:
                await self.start_implementation(argument)
        elif name == "outcome":
            reference = argument or self.state.conversation(self.session)["task"]
            if not reference:
                self.notify("Usage: /outcome <task> (or run it in an implementation conversation)", severity="warning")
            else:
                self.record_outcome(reference)

    async def context_command(self, argument):
        action, _, rest = argument.partition(" ")
        if action in {"add", "remove"} and rest.strip():
            try:
                note = self.project.knowledge.find(rest.strip())
            except ProjectError as error:
                self.notify(clean(str(error)), severity="error")
                return
            selected = self.state.conversation(self.session)["notes"]
            if (note.id in selected) != (action == "add"):
                self.toggle_context_note(note.id)
        elif action == "clear":
            self.state.set_conversation(self.session, notes=[])
            self.refresh_project_info()
        elif action:
            self.notify("Usage: /context [add <note> | remove <note> | clear]", severity="warning")
            return
        selected = self.state.conversation(self.session)["notes"]
        body = ["**Selected for this conversation:** " + (", ".join(f"`{item}`" for item in selected) or "none"),
                "", "The overview and up to three notes whose words match your message are added automatically, "
                "within a quarter of the context budget. Notes are sent as reference material, never as "
                "instructions. Select notes with **c** in the notes browser (f7) or `/context add <note>`."]
        if self.context_sources:
            body += ["", "**Last request:**", ""]
            body += [f"- `{clean(source['id'])}` — {clean(source['reason'])}"
                     + (" _(shortened)_" if source.get("truncated") else "")
                     + ("" if source.get("included") else f" — **not included:** {clean(source.get('note', ''))}")
                     for source in self.context_sources]
        await self.note("\n".join(body), f"Context · {self.session}")

    async def todo_command(self, argument):
        """/todo shows the session's task list; /todo add|edit|move|start|done|undo|remove changes it.
        Edits are saved at once and work during a turn: the turn then keeps the user's version."""
        message = ""
        if argument:
            items, revision = self.state.todo_revision(self.session)
            try:
                items, message = toolkit.edit_todo(items, argument)
            except ValueError as error:
                self.notify(clean(str(error)), severity="error", timeout=4)
                return
            if self.state.save_todo(self.session, items, revision) is None:
                message = "List changed. Check the task numbers and retry the edit."
                self.notify(message, title="Tasks", severity="warning", timeout=4)
            else:
                self.notify(message, title="Tasks", timeout=2)
        items = self.state.todo(self.session)
        body = "\n".join(f"{number}. `{clean(item[:3])}` {clean(item[4:])}" for number, item in enumerate(items, 1))
        if not body:
            body = "_No task list for this session. The agent keeps one for multi-step work, or add one with_ " \
                   "`/todo add TEXT`."
        elif not argument:
            body += f"\n\n_{toolkit.TODO_USAGE}_"
        await self.note(body, f"Tasks · {self.session}" + (f" · {message}" if message else ""))

    # ── Projects ──────────────────────────────────────────────────────────────────────────

    @property
    def project_key(self):
        return self.project.id if self.project is not None else ""

    def refresh_project_labels(self):
        top = self.query_one(TopBar)
        root = self.workspace or Path.cwd()
        top.project = self.project.name if self.project is not None else root.name
        top.branch = git_branch(root) or ""

    def remember_session(self):
        if self.project is not None:
            try:
                self.registry.update_settings(self.project.id, last_session=self.session)
            except (OSError, ProjectError):
                pass

    async def open_project(self, project, session=None, *, show_home=False, origin=None, quiet=False, force=False):
        """Switch to a project (or to the classic sessions with None). The draft, queued attachments and
        session of the project being left are kept and come back when the user returns to it. A running
        turn is cancelled only after the user agrees; its late events are then ignored."""
        if project is not None:
            try:
                project.require_folder()
            except ProjectError as error:
                self.notify(clean(str(error)), severity="error", timeout=8)
                return False
        if self.busy and not force:
            async def decided(cancel):
                if cancel:
                    await self.cancel_turn()
                    await self.open_project(project, session, show_home=show_home, origin=origin, quiet=quiet,
                                            force=True)
            where = project.name if project is not None else "the classic sessions"
            self.push_screen(ConfirmScreen("A turn is still running",
                                           f"Switching to {where} cancels it. Nothing from the turn is saved; file "
                                           "changes and commands it already made stay.", "Cancel turn and switch",
                                           "Stay", danger=True), decided)
            return False
        composer = self.query_one(Composer)
        self.stash[self.project_key] = {"draft": composer.text, "attachments": list(self.file_commands.pending),
                                        "recoverable": self.recoverable_prompt, "session": self.session}
        self.remember_session()
        self.project = project
        self.project_generation += 1
        self.context_sources = []
        self.state = self.base_state.for_project(self.project_key)
        self.workspace = project.path.resolve() if project is not None else self.classic_workspace
        tools = project.settings.get("tools") if project is not None else None
        if isinstance(tools, dict):
            flags = {key: bool(tools.get(key)) for key in ("write", "shell", "web")}
        elif project is None:
            flags = dict(self.launch_flags)
        else:
            flags = {"write": False, "shell": False, "web": self.launch_flags["web"]}
        self.write = flags["write"] and self.workspace is not None
        self.shell = flags["shell"] and self.workspace is not None
        self.web = flags["web"]
        self.path_agent = Agent(None, self.state, self.workspace) if self.workspace is not None else None
        self.file_index = None
        self.refs.clear()
        stash = self.stash.pop(self.project_key, {})
        composer.load_text(stash.get("draft", ""))
        composer.move_cursor(composer.document.end)
        self.file_commands.pending = list(stash.get("attachments", []))
        self.recoverable_prompt = stash.get("recoverable", "")
        last = project.settings.get("last_session") if project is not None else None
        self.session = session or stash.get("session") or (last if isinstance(last, str) and last else "default")
        try:
            if project is not None:
                self.project = self.registry.mark_opened(project.id)
            else:
                self.registry.forget_last()
        except (OSError, ProjectError) as error:
            self.notify(clean(str(error)), severity="warning", timeout=5)
        try:
            from .widgets import Explorer
            self.query_one(Explorer).path = self.workspace or Path.cwd()
        except Exception:  # noqa: BLE001 - the explorer is a convenience; never block a switch on it
            pass
        self.refresh_project_labels()
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.load_team_mode()
        await self.load_session()
        await self.refresh_attachments()
        self.refresh_sidebar()
        self.refresh_composer_controls()
        composer.focus()
        if show_home and project is not None:
            self.push_screen(ProjectHome())
        if not quiet:
            label = project.name if project is not None else "Classic sessions"
            enabled = [name for name in ("write", "shell", "web") if getattr(self, name)]
            detail = f"{self.workspace}" if project is not None else "Conversations outside projects"
            self.notify(f"{detail}\nTools: {', '.join(enabled) or 'read-only'}", title=label, timeout=4)
        return True

    def action_projects(self):
        if not isinstance(self.screen, ProjectsScreen):
            self.push_screen(ProjectsScreen())

    def action_project_home(self):
        if self.project is None:
            self.notify("No project is open. Choose or register one.", timeout=3)
            self.action_projects()
        elif not isinstance(self.screen, ProjectHome):
            self.push_screen(ProjectHome())

    def action_notes(self):
        if self.project is None:
            self.action_project_home()
        elif not isinstance(self.screen, NotesScreen):
            self.push_screen(NotesScreen())

    def action_save_reply(self):
        self.save_reply(None)

    async def start_conversation(self, kind, topic=None):
        if self.busy:
            self.notify("Wait for the current turn, or press esc to cancel it.", severity="warning")
            return
        if kind == "brainstorm" and self.project is None:
            self.notify("Brainstorms belong to a project. Open one with f5.", severity="warning", timeout=4)
            return
        if kind == "brainstorm":
            session = project_commands.brainstorm_session(self.state)
        else:
            session = datetime.now().strftime("chat-%m%d-%H%M%S")
        await self.switch_session(session)
        if kind == "brainstorm":
            await self.note("Describe the problem or goal. The agent will restate it, explore alternatives, "
                            "compare tradeoffs, list open questions and suggest next steps.\n\n"
                            "When something is worth keeping, press **f9** (or `/save idea|note|decision|task`): "
                            "you review and edit the draft before it is saved, linked to this conversation.",
                            "Brainstorm")
            if topic:
                await self.start_turn(f"Let's brainstorm: {topic}")

    async def start_implementation(self, reference):
        if self.project is None:
            self.notify("Open a project first.", severity="warning")
            return
        if self.busy:
            self.notify("Wait for the current turn, or press esc to cancel it.", severity="warning")
            return
        try:
            session, task, prompt = project_commands.start_implementation(self.project, self.state, reference)
        except NoteConflict as error:
            self.notify(clean(str(error)) + " Reload the task and try again.", severity="error", timeout=6)
            return
        except (OSError, ProjectError) as error:
            self.notify(clean(str(error)), severity="error", timeout=6)
            return
        while len(self.screen_stack) > 1:
            self.pop_screen()
        await self.switch_session(session)
        composer = self.query_one(Composer)
        if not composer.text.strip():
            composer.load_text(prompt)
            composer.move_cursor(composer.document.end)
            hint = "Review the first message, then press enter."
        else:
            hint = "Your draft was kept; clear it and run /implement again for the suggested first message."
        missing = [label for flag, label in ((self.write, "/write on"), (self.shell, "/shell on")) if not flag]
        if missing:
            hint += f" File changes or commands are off: {' and '.join(missing)} to allow them (you approve each)."
        await self.note(f"**{clean(task.title)}** · `{task.id}` · {task.status}\n\n{hint}\n\nWhen it's done, "
                        "record the outcome with `/outcome` — the task stays open until you complete it.",
                        "Implementation")
        composer.focus()
        self.refresh_composer_controls()

    def edit_new_note(self, kind, title="", body=None, *, related=(), conversations=(), callback=None):
        if self.project is None:
            self.notify("Open a project first.", severity="warning")
            return
        def saved(note):
            if note is not None:
                self.refresh_project_info()
            if callback is not None:
                callback(note)
        self.push_screen(NoteEditor(kind=kind, title=title, body=body, related=related,
                                    conversations=conversations), saved)

    def save_reply(self, kind=None, title=None):
        """Turn the latest reply into a draft idea, note, decision or task for the user to review."""
        if self.project is None:
            self.notify("Open a project to save replies as notes (f5).", severity="warning", timeout=3)
            return
        if not self.last_reply:
            self.notify("There is no reply to save yet.", timeout=2)
            return
        if kind is None:
            self.push_screen(ChoiceScreen("Save the latest reply as…", "You can edit the draft before it is saved.", [
                ("i", "Idea", "idea"), ("n", "Note", "note"), ("d", "Decision", "decision"), ("t", "Task", "task")]),
                lambda chosen: chosen and self.save_reply(chosen, title))
            return
        title, body = draft_from_reply(kind, self.last_reply, self.session, title)
        self.edit_new_note(kind, title, body, conversations=[self.session])

    def record_outcome(self, reference, callback=None):
        try:
            task = self.project.knowledge.find(reference, ["task"])
        except (OSError, ProjectError) as error:
            self.notify(clean(str(error)), severity="error", timeout=5)
            return
        details = self.state.conversation(self.session)
        session = self.session if details["task"] == task.id else None
        draft = ""
        if session and self.last_reply:
            draft = next((part.strip() for part in self.last_reply.split("\n\n") if part.strip()), "")[:600]

        def recorded(values):
            if not values:
                return
            try:
                fresh = self.project.knowledge.get(task.id)
                updated = record_task_outcome(self.project.knowledge, fresh, values["outcome"],
                                              values["verification"], values["unresolved"], session or None,
                                              values["status"])
            except (OSError, ProjectError) as error:
                self.notify(clean(str(error)), severity="error", timeout=6)
                return
            self.notify(f"Recorded the outcome for {updated.title} · status {updated.status}.", title="Task",
                        timeout=4)
            self.refresh_project_info()
            if callback is not None:
                callback(updated)
        self.push_screen(OutcomeScreen(task, draft), recorded)

    def toggle_context_note(self, identifier):
        selected = self.state.conversation(self.session)["notes"]
        added = identifier not in selected
        selected = [*selected, identifier] if added else [item for item in selected if item != identifier]
        self.state.set_conversation(self.session, notes=selected)
        self.refresh_project_info()
        return added

    def context_card(self, sources):
        """One line above the reply: which project notes went with the request, and why."""
        if not sources:
            return None
        palette = self.palette
        line = Text()
        line.append(f"{self.glyphs.arrow} context ", Style(color=palette["secondary"], bold=True))
        parts = []
        for source in sources:
            part = Text()
            if source.get("included"):
                part.append(clean(source["id"]), Style(color=palette["foreground"]))
                part.append(f" ({clean(source['reason'])}{', shortened' if source.get('truncated') else ''})",
                            Style(color=palette["muted"]))
            else:
                part.append(f"{clean(source['id'])} skipped", Style(color=palette["warning"]))
            parts.append(part)
        line.append_text(Text(" · ", Style(color=palette["muted"])).join(parts))
        line.append("   /context for details", Style(color=palette["muted"], italic=True))
        return NoteCard(line, "context-sources")

    # ── Teams ─────────────────────────────────────────────────────────────────────────────

    def load_team_mode(self):
        mode = self.project.settings.get("team_mode") if self.project is not None else None
        self.team_mode = mode if isinstance(mode, str) and mode else None
        self.show_team_mode()

    def show_team_mode(self):
        self.query_one(StatusBar).team = self.team_mode or ""
        self.query_one(Composer).placeholder = (
            f"Team {self.team_mode}: describe the task  ·  /team off for one agent" if self.team_mode else
            "Ask anything  ·  / commands  ·  @ files" if self.workspace is not None else "Ask anything  ·  / for commands")
        self.refresh_composer_controls()

    def set_team_mode(self, name):
        if self.project is None:
            self.notify("Teams belong to a project. Open one with f5.", severity="warning")
            return
        if name is not None:
            try:
                TeamStore(self.data_dir, self.project.id).get(name)
            except ProjectError as error:
                self.notify(clean(str(error)), severity="error", timeout=5)
                return
        self.team_mode = name
        try:
            self.project = self.registry.update_settings(self.project.id, team_mode=name or "")
        except (OSError, ProjectError):
            pass
        self.show_team_mode()
        self.notify(f"Messages start a team run with {name}. Each run is checked first." if name else
                    "Messages go to one agent, as before.", title=f"Team {name}" if name else "Single agent", timeout=3)

    def action_teams(self):
        if self.project is None:
            self.notify("Teams belong to a project. Open one with f5.", severity="warning", timeout=3)
        elif not isinstance(self.screen, TeamScreen):
            self.push_screen(TeamScreen())

    async def team_command(self, argument):
        word, _, rest = argument.partition(" ")
        if not word:
            self.action_teams()
        elif word in {"off", "single", "none"}:
            self.set_team_mode(None)
        elif word == "runs":
            runs = RunStore(self.data_dir, self.project.id).runs(10)
            rows = [f"- `{record['id']}` **{record['state']}** · {clean(record['team'].get('name', '?'))} · "
                    f"{clean(record['prompt'])[:70]}" for record in runs]
            await self.note("\n".join(rows) or "_No team runs yet._", "Team runs")
        elif word == "result":
            try:
                record = RunStore(self.data_dir, self.project.id).load(rest.strip() or "")
            except ProjectError as error:
                self.notify(clean(str(error)), severity="error", timeout=5)
                return
            await self.note(f"```text\n{clean(result_text(record))}\n```", f"Team run {record['id']}")
        elif word == "run":
            if not self.team_mode or not rest.strip():
                self.notify("Usage: /team run <request> (choose a team first with /team NAME)", severity="warning")
            else:
                self.confirm_team_run(self.team_mode, rest.strip())
        else:
            self.set_team_mode(word)

    def confirm_team_run(self, name, prompt, task=None, clear=False):
        """Check the team, then show the preflight summary; the run starts only when the user agrees."""
        if self.busy:
            self.notify("Wait for the current turn or run, or press esc to cancel it.", severity="warning")
            return
        try:
            team = TeamStore(self.data_dir, self.project.id).get(name)
            ok, report, _ = preflight(team, self.data_dir, self.project)
        except (OSError, ProjectError) as error:
            self.notify(clean(str(error)), severity="error", timeout=6)
            return
        if not ok:
            self.run_worker(self.note(f"```text\n{clean(report)}\n```\nFix these in the team screen (f8) or with "
                                      "`python -m local_agent team assign`. Single-agent chat still works: "
                                      "`/team off`.", f"Team {name} can't run"))
            self.notify(f"Team {name} can't run yet; see the problems in the conversation.", severity="error",
                        timeout=5)
            return

        async def decided(start):
            if start:
                if clear:
                    self.query_one(Composer).load_text("")
                    self.hide_suggestions()
                await self.start_team_run(team, prompt, task)
        self.push_screen(ConfirmScreen(f"Start team {name}?", report + "\n\nRequest: " + prompt
                                       + ("\nTask: " + task if task else ""),
                                       "Start team run", "Cancel"), decided)

    async def start_team_run(self, team, prompt, task=None):
        if self.busy or self.project is None:
            return
        try:
            run = TeamRun(self.project, self.base_state, self.data_dir, team, prompt, task=task, session=self.session,
                          selected=self.state.conversation(self.session)["notes"], approve=self.team_approval,
                          auto_approve=self.auto_approve, on_event=self.team_event,
                          **({"provider_factory": self.team_provider_factory} if self.team_provider_factory else {}))
        except (OSError, ProjectError) as error:
            self.notify(clean(str(error)), severity="error", timeout=6)
            return
        await self.hide_welcome()
        chat = self.query_one(ChatLog)
        await chat.mount(UserMessage(f"[team {team.get('name')}] {prompt}", stamp()))
        self.team_panel = TeamRunPanel(run.id, team.get("name"))
        await chat.mount(self.team_panel)
        await self.team_panel.update_snapshot(run.snapshot())
        chat.anchor()
        self.team_run, self.team_prompt = run, prompt
        self.recoverable_prompt = ""
        self.set_state("thinking", f"team {team.get('name')}")
        threading.Thread(target=run.run, name="team-run", daemon=True).start()

    def team_event(self, kind, data):
        """Runs on team threads; the UI thread decides whether the event is still current."""
        self.post_message(TeamEvent(kind, data))

    def team_approval(self, request, stop):
        """Runs on an agent's thread. The orchestrator sends one request at a time; this waits for the
        dialog and gives up (declining) as soon as the run is stopped."""
        decided = threading.Event()
        result = {}

        def done(value):
            result["value"] = bool(value)
            decided.set()
        self.post_message(TeamEvent("approval", {"run": request["run"], "project": request["project_id"],
                                                 "request": request, "done": done}))
        while not decided.wait(0.1):
            if stop.is_set():
                return False
        return result.get("value", False) and not stop.is_set()

    def team_event_current(self, data):
        run = self.team_run
        return run is not None and data.get("run") == run.id and data.get("project") == self.project_key

    async def on_team_event(self, event: TeamEvent):
        data = event.data
        if not self.team_event_current(data):
            if event.kind == "approval":
                data["done"](False)  # A late request from a cancelled run or another project.
            return
        if event.kind == "update":
            if self.team_panel is not None:
                await self.team_panel.update_snapshot(data)
            state = data["state"]
            running = [agent["id"] for agent in data["agents"] if agent["state"] in {"running", "waiting_approval"}]
            self.set_state("approval" if state == "waiting_approval" else "tool" if running else "thinking",
                           f"team · {', '.join(running)}" if running else "team")
        elif event.kind == "note":
            text = data["text"]
            if text.startswith(("Fallback", "Limit", "Integration")):
                self.notify(clean(text)[:200], title="Team run", severity="warning", timeout=6)
        elif event.kind == "approval":
            request = data["request"]
            self.set_state("approval", f"{request['agent']} · {request['tool'].replace('_', ' ')}")
            if self.team_run.stop.is_set():
                data["done"](False)
                return
            self.team_approval_mount = self.push_screen(ActionScreen(request), data["done"])
        elif event.kind == "done":
            await self.finish_team_run(data)

    async def close_team_approval(self, run):
        dialog = self.screen
        if isinstance(dialog, ActionScreen) and dialog.action.get("run") == run.id:
            await self.team_approval_mount
            if self.screen is dialog:
                dialog.dismiss(False)
        self.team_approval_mount = None

    async def finish_team_run(self, record):
        run = self.team_run
        await self.close_team_approval(run)
        chat = self.query_one(ChatLog)
        if self.team_panel is not None:
            await self.team_panel.update_snapshot(run.snapshot())
        markdown = run.markdown()
        reply = AssistantMessage(when=stamp(), live=False)
        await chat.mount(reply)
        await reply.set_content(markdown)
        await reply.finish(f"team run {record['id']}")
        self.last_reply = markdown
        self.team_run = None
        state = record["state"]
        if state == "completed":
            self.holo.flash("success")
            self.set_state("idle", "")
        else:
            self.recoverable_prompt = self.team_prompt
            self.holo.flash("error")
            self.set_state("error" if state == "failed" else "cancelled", f"team {state}")
        if record.get("outcome"):
            self.notify(clean(record["outcome"]), title="Task", timeout=4)
        self.team_panel = None
        self.refresh_sidebar()
        self.refresh_composer_controls()
        chat.anchor()

    async def cancel_team_run(self):
        run = self.team_run
        if run is None:
            return
        run.cancel()
        self.team_run = None  # From here on, the run's late events are ignored.
        self.recoverable_prompt = self.team_prompt
        await self.close_team_approval(run)
        if self.team_panel is not None:
            await self.team_panel.update_snapshot({**run.snapshot(), "state": "cancelled"})
            self.team_panel = None
        chat = self.query_one(ChatLog)
        await chat.mount(NoteCard(divider("team run cancelled · no new work starts · completed file changes and "
                                          "commands stay · the record is kept", self), "divider"))
        self.set_state("cancelled", "")
        self.refresh_tools()
        chat.anchor()
        self.notify("Team run cancelled.", severity="warning", timeout=2)

    # ── Attachments and artifacts ─────────────────────────────────────────────────────────

    async def refresh_attachments(self):
        await self.query_one(AttachmentBar).show(self.file_commands.pending)

    async def on_attachment_bar_remove(self, message: AttachmentBar.Remove):
        if self.busy:
            self.notify("Wait for the current turn before changing attachments.", severity="warning")
            return
        if 0 <= message.index < len(self.file_commands.pending):
            path = self.file_commands.pending.pop(message.index)
            self.notify(f"Removed {clean(path.name)}; it won't be sent.", title="Attachments", timeout=2)
        await self.refresh_attachments()
        self.query_one(Composer).focus()

    async def show_artifacts(self, limit=10):
        listing = self.state.artifacts.list()
        items = listing["artifacts"]
        if not items:
            await self.note("_No artifacts yet. Ask the agent to create a file._", "Artifacts")
            return
        more = len(items) - limit if len(items) > limit else 0
        await self.note(f"Latest {min(limit, len(items))} · **Preview** opens a sandboxed browser page"
                        + (f" · {more}{'+' if listing['truncated'] else ''} older — `/artifact ID` reads any"
                           if more else ""), "Artifacts")
        chat = self.query_one(ChatLog)
        await chat.mount_all(ArtifactCard(item) for item in items[:limit])
        chat.anchor()

    @work(thread=True, exclusive=True, group="preview")
    def preview_artifact(self, identifier):
        try:
            path = self.state.artifacts.preview(identifier, open_browser=True)
        except (OSError, RuntimeError, ValueError) as error:
            self.call_from_thread(self.notify, clean(str(error)), severity="error", timeout=4)
        else:
            self.call_from_thread(self.notify, f"Opened a sandboxed preview: {clean(path)}",
                                  title="Artifact", timeout=3)

    def search_label(self):
        try:
            return search.label(search.configured(self.data_dir))
        except ValueError as error:
            return clean(str(error))

    # ── Providers ─────────────────────────────────────────────────────────────────────────

    def provider_label(self):
        preset = providers.PRESETS.get(self.config.get("provider"))
        name = self.config.get("profile") or self.config.get("provider") or "custom"
        return f"{name} — {preset.label}" if preset and preset.label.lower() != name else name

    async def provider_command(self, argument):
        """/provider lists profiles and providers; /provider <name> switches to one."""
        try:
            settings = read_settings(self.data_dir)
        except (OSError, ValueError) as error:
            self.notify(clean(str(error))[:200], severity="error")
            return
        if not argument:
            active = self.config.get("profile")
            rows = ["| | Profile | Provider | Model |", "|---|---|---|---|"]
            for name, profile in settings["profiles"].items():
                rows.append(f"| {'●' if name == active else ''} | {clean(name)} | {clean(profile.get('provider', '?'))} "
                            f"| `{clean(profile.get('model', '?'))}` |")
            ready = []
            for name, preset in providers.PRESETS.items():
                if name in settings["profiles"] or not preset.base_url or not preset.model:
                    continue
                key, _ = providers.resolve_key({"profile": name, "provider": name})
                if key or not preset.key_env:
                    ready.append(f"`{name}`")
            body = "\n".join(rows) if len(rows) > 2 else "_No saved profiles yet._"
            body += "\n\nSwitch with `/provider <name>`."
            if ready:
                body += " Also ready (key found): " + ", ".join(ready) + "."
            body += "\n\nAdd a cloud model in a terminal: `python -m local_agent setup --provider deepseek` " \
                    "then `python -m local_agent key set deepseek`. Providers: " + \
                    ", ".join(f"`{name}`" for name in providers.PRESETS) + "."
            await self.note(body, "Providers")
            return
        if self.busy:
            self.notify("Wait for the current turn, or press esc to cancel it.", severity="warning")
            return
        name = argument.split()[0].lower()
        if name not in settings["profiles"] and providers.preset_name(name) in settings["profiles"]:
            name = providers.preset_name(name)
        try:
            if name not in settings["profiles"]:
                provider = providers.preset_name(name)
                preset = providers.PRESETS.get(provider)
                if not preset or not preset.base_url or not preset.model:
                    raise ValueError(f"No saved profile {name!r}. Create it with "
                                     f"`python -m local_agent setup --provider {provider or name}`.")
                profile = {"profile": provider, "provider": provider, "base_url": preset.base_url,
                           "model": preset.model}
                providers.make_provider(profile)  # Fails early when the key is missing.
                save_config(self.data_dir, preset.base_url, preset.model, preset.context_chars,
                            provider=provider, name=provider)
                name = provider
            config = load_config(self.data_dir, name)
            new_provider = providers.make_provider(config)
            set_active(self.data_dir, name)
        except (OSError, ValueError) as error:
            self.notify(clean(str(error))[:240], title="Can't switch", severity="error", timeout=6)
            return
        self.use_provider(config, new_provider)
        self.notify(f"{clean(name)} · {clean(config['model'])}", title="Provider", timeout=3)

    async def model_command(self, argument):
        """/model lists the endpoint's models; /model <name> switches this profile to one."""
        if not argument:
            current = self.config["model"]
            await self.note(f"Current model: `{clean(current)}` on **{clean(self.provider_label())}**.\n\n"
                            "Fetching the models this endpoint offers…", "Model")
            provider = self.provider

            def run():
                try:
                    found = sorted(provider.models())
                    body = "\n".join(f"- `{clean(model)}`{'  ●' if model == current else ''}" for model in found[:80])
                    if len(found) > 80:
                        body += f"\n- …and {len(found) - 80} more"
                    body = (body or "_The endpoint listed no models._") + "\n\nSwitch with `/model <name>`."
                except Exception as error:  # noqa: BLE001 - shown to the user
                    body = f"Couldn't list models: {clean(str(error))[:240]}\n\nYou can still try `/model <name>`."
                self.post_message(ModelsListed(body))

            threading.Thread(target=run, name="model-list", daemon=True).start()
            return
        if self.busy:
            self.notify("Wait for the current turn, or press esc to cancel it.", severity="warning")
            return
        model = argument.split()[0]
        if len(model) > 200:
            self.notify("That model name is too long.", severity="error")
            return
        config = {**self.config, "model": model}
        try:
            new_provider = providers.make_provider(config)
            if config.get("profile") in read_settings(self.data_dir)["profiles"]:
                set_active(self.data_dir, config["profile"], model)
        except (OSError, ValueError) as error:
            self.notify(clean(str(error))[:240], severity="error", timeout=6)
            return
        self.use_provider(config, new_provider)
        self.notify(clean(model), title="Model", timeout=3)

    async def on_models_listed(self, message):
        await self.note(message.body, "Models")

    def use_provider(self, config, new_provider):
        new_provider.streaming = getattr(self.provider, "streaming", True)
        self.provider = new_provider
        self.config = config
        self.query_one(TopBar).model = config["model"]
        self.query_one(StatusBar).budget = config["context_chars"]
        self.refresh_sidebar()
        self.check_connection(False)

    async def note(self, markdown, title, *, literal=False):
        await self.hide_welcome()
        chat = self.query_one(ChatLog)
        palette = self.palette
        header = Text()
        header.append(f"{self.glyphs.arrow} {title}", Style(color=palette["secondary"], bold=True))
        body = Text(clean(markdown)) if literal else RichMarkdown(
            markdown, code_theme="monokai" if not palette.get("light") else "default"
        )
        await chat.mount(NoteCard(Group(header, body), "card"))
        chat.anchor()

    async def hide_welcome(self):
        removed = False
        for welcome in self.query(Welcome):
            await welcome.remove()
            removed = True
        if removed:
            self.layout_hologram()

    # ── Turns ─────────────────────────────────────────────────────────────────────────────

    async def start_turn(self, prompt):
        await self.hide_welcome()
        chat = self.query_one(ChatLog)
        attachments = tuple(self.file_commands.pending)
        display = prompt
        if attachments:
            display += "\n[Attached files: " + ", ".join(path.name for path in attachments) + "]"
        await chat.mount(UserMessage(display, stamp()))
        reply = AssistantMessage(live=True)
        await chat.mount(reply)
        chat.anchor()
        token = TurnToken(self.project_key)
        token.prompt = prompt
        self.recoverable_prompt = ""
        self.token, self.current = token, reply
        self.set_state("thinking", "")
        scope = {"state": self.state, "workspace": self.workspace, "write": self.write, "shell": self.shell,
                 "web": self.web, "project": self.project}
        threading.Thread(target=self.turn_thread, args=(token, prompt, self.session, self.learn, attachments, scope),
                         name="agent-turn", daemon=True).start()

    def turn_thread(self, token, prompt, session, learn, attachments, scope=None):
        lock = threading.Lock()
        batch = None

        def on_event(kind, data):
            nonlocal batch
            if token.cancelled and kind not in {"todo_kept", "done"}:
                raise TurnCancelled("Cancelled.")
            if kind == "tool_wait":
                return  # A command is still running; only the cancellation check above matters.
            if kind == "text":
                # Coalesce fragments: one message per batch, however fast the model streams, so
                # keystrokes never queue behind hundreds of tiny updates.
                with lock:
                    if batch is None or batch["closed"]:
                        batch = {"closed": False, "parts": [], "lock": lock}
                        self.post_message(AgentEvent(token, "text_batch", batch))
                    batch["parts"].append((data["kind"], data["text"]))
                return
            with lock:
                batch = None  # Text after this event belongs after it on screen.
            self.post_message(AgentEvent(token, kind, data))

        def approve(changes):
            return self.ask_from_thread(token, "approval", {"changes": changes})

        def confirm(action):
            if self.auto_approve and not action["danger"]:
                return True  # --yes: only commands flagged as dangerous still ask.
            return self.ask_from_thread(token, "confirm", {"action": action})

        content, error = None, None
        scope = scope or {"state": self.state, "workspace": self.workspace, "write": self.write,
                          "shell": self.shell, "web": self.web, "project": self.project}
        try:
            state, project = scope["state"], scope["project"]
            agent = Agent(self.provider, state, scope["workspace"], self.config["context_chars"],
                          (lambda changes: True) if self.auto_approve else approve, on_event,
                          confirm=confirm, write=scope["write"], shell=scope["shell"], web=scope["web"],
                          max_rounds=self.max_rounds, image_provider=providers.make_image_provider(self.config),
                          project_context=ProjectContext(project, state) if project is not None else None)
            content = agent.turn(session, prompt, learn, attachments=attachments)
        except Exception as caught:  # Shown in the UI; the agent has already discarded the turn.
            error = caught
        self.post_message(TurnFinished(token, content, error))

    def ask_from_thread(self, token, kind, payload):
        """Runs on the turn thread. The request goes through the same ordered message queue as the
        turn's events, so the dialog opens only after everything streamed before it is shown."""
        decided = threading.Event()
        result = {}

        def done(value):
            result["value"] = bool(value)
            decided.set()

        self.post_message(AgentEvent(token, kind, {**payload, "done": done}))
        while not decided.wait(0.1):
            if token.cancelled:
                raise TurnCancelled("Cancelled.")
        if token.cancelled:
            raise TurnCancelled("Cancelled.")
        return result.get("value", False)

    async def on_agent_event(self, event: AgentEvent):
        token, reply = event.token, self.current
        if token is not self.token or reply is None or token.project != self.project_key:
            if event.kind in {"approval", "confirm"}:
                event.data["done"](False)
            return
        data = event.data
        status = self.query_one(StatusBar)
        if event.kind == "request":
            status.used, status.budget = data["used"], data["budget"]
            round_label = f"round {data['round'] + 1}" if data["round"] else ""
            self.set_state("thinking", "final answer" if data["final"] else round_label)
        elif event.kind == "text_batch":
            with data["lock"]:
                data["closed"] = True
                parts = data["parts"]
            merged = []
            for kind, fragment in parts:
                if merged and merged[-1][0] == kind:
                    merged[-1][1].append(fragment)
                else:
                    merged.append((kind, [fragment]))
            for kind, fragments in merged:
                await reply.add_text(kind, "".join(fragments))
            if split_reasoning(reply.raw)[1]:
                self.set_state("streaming", "")
                chars = sum(len(fragment) for kind, fragments in merged if kind == "content"
                            for fragment in fragments)
                if chars:
                    self.holo.wave(chars / 80)
        elif event.kind == "reply":
            usage = data.get("usage") or {}
            if usage:
                token.prompt_tokens += usage.get("prompt_tokens", 0)
                token.completion_tokens += usage.get("completion_tokens", 0)
            else:
                token.estimated = True
                token.completion_tokens += len(data.get("content") or "") // 4
            if data.get("content") and not reply.raw:
                await reply.set_content(data["content"])  # Provider did not stream.
                self.holo.wave(1.0)
            if data.get("calls"):
                self.holo.tool_round(len(data["calls"]))
        elif event.kind == "tool_start":
            await reply.start_tool(data["id"], data["name"], data["arguments"])
            self.set_state("tool", data["name"])
            self.refresh_tools(running=data["name"])
        elif event.kind == "tool_end":
            reply.end_tool(data["id"], data["result"], data["error"])
            self.holo.tool_finished(error=bool(data["error"]))
            self.refresh_tools()
            self.set_state("thinking", "")
        elif event.kind == "approval":
            changes = data["changes"]
            self.set_state("approval", f"{len(changes)} change{'s' * (len(changes) != 1)}")

            def decided(value):
                if self.token is token:
                    self.set_state("thinking", "saving" if value else "")
                data["done"](value)

            self.push_screen(ApprovalScreen(changes), decided)
        elif event.kind == "confirm":
            action = data["action"]
            self.set_state("approval", action["tool"].replace("_", " "))

            def allowed(value):
                if self.token is token:
                    if value:
                        self.set_state("tool", action["tool"])
                    else:
                        self.set_state("thinking", "declined")
                data["done"](value)

            self.push_screen(ActionScreen(action), allowed)
        elif event.kind == "compress":
            if not token.compressed:
                token.compressed = True
                dropped = f", dropped {data['dropped']} old turn{'s' * (data['dropped'] != 1)}" if data["dropped"] else ""
                self.notify(f"Trimmed tool output to fit the {data['budget']:,}-character budget{dropped}.",
                            title="Context compressed", timeout=3)
        elif event.kind == "todo_kept":
            self.notify("You edited the task list during this turn, so your version was kept.",
                        title="Tasks", timeout=4)
        elif event.kind == "commit":
            token.changes = data["changes"]
        elif event.kind == "context":
            self.context_sources = data["sources"]
            self.refresh_project_info()
            card = self.context_card(data["sources"])
            if card is not None:
                await self.query_one(ChatLog).mount(card, before=reply)
        elif event.kind == "done":
            await reply.set_content(data["content"])
            if data["workspace_writes"]:
                await reply.body.mount(NoteCard(Text(data["workspace_writes"])))

    async def on_turn_finished(self, message: TurnFinished):
        token, reply = message.token, self.current
        if token is not self.token or token.project != self.project_key:
            return
        self.token = self.current = None
        elapsed = time.monotonic() - token.started
        self.prompt_tokens += token.prompt_tokens
        self.completion_tokens += token.completion_tokens
        self.estimated = self.estimated or token.estimated
        status = self.query_one(StatusBar)
        status.prompt_tokens, status.completion_tokens = self.prompt_tokens, self.completion_tokens
        status.estimated = self.estimated
        chat = self.query_one(ChatLog)
        if message.error is None:
            self.file_commands.pending.clear()
            await self.refresh_attachments()
            tokens = token.completion_tokens
            meta = f"{elapsed:.1f}s" + (f" · {'~' if token.estimated else ''}{tokens} tok" if tokens else "")
            await reply.finish(meta)
            self.last_reply = message.content or ""
            self.holo.flash("success")
            self.set_state("idle", "")
            if token.changes:
                self.notify(f"Saved {token.changes} memory or skill change{'s' * (token.changes != 1)}.",
                            title="Learned", severity="information", timeout=3)
            if self.connection != "online":
                self.set_connection("online")
        elif isinstance(message.error, TurnCancelled):
            self.recoverable_prompt = token.prompt
            await self.finish_cancelled(reply)
        else:
            self.recoverable_prompt = token.prompt
            for card in reply.artifacts:
                card.mark_removed()
            await reply.finish("failed")
            error = message.error
            title = "Something went wrong" if not isinstance(error, (RuntimeError, ValueError, OSError,
                                                                     sqlite3.Error)) else "The turn failed"
            if "reach the model" in str(error) or "interrupted" in str(error):
                title = "Couldn't reach the model"
                self.set_connection("offline")
            await chat.mount(ErrorCard(title, str(error) or type(error).__name__, error_hint(error)))
            self.holo.flash("error")
            self.set_state("error", "")
            self.notify(clean(str(error))[:120] or "The turn failed.", severity="error", timeout=4)
        self.refresh_sidebar()
        chat.anchor()

    async def finish_cancelled(self, reply):
        if reply is not None:
            for card in reply.artifacts:
                card.mark_removed()
            await reply.finish("cancelled")
        chat = self.query_one(ChatLog)
        await chat.mount(NoteCard(divider("turn cancelled · conversation not saved", self), "divider"))
        self.set_state("cancelled", "")
        chat.anchor()

    async def cancel_turn(self):
        if self.team_run is not None:
            await self.cancel_team_run()
            return
        token, reply = self.token, self.current
        if token is None:
            return
        token.cancelled = True
        self.token = self.current = None
        self.recoverable_prompt = token.prompt
        if isinstance(self.screen, (ApprovalScreen, ActionScreen)):
            self.screen.dismiss(False)
        await self.finish_cancelled(reply)
        self.refresh_tools()
        self.notify("Turn cancelled.", severity="warning", timeout=2)

    def set_state(self, state, detail):
        status = self.query_one(StatusBar)
        status.state, status.detail = state, detail
        self.refresh_composer_controls()
        self.query_one(TopBar).busy = state in {"thinking", "streaming", "tool", "approval"}
        holo_state = {"thinking": "thinking", "streaming": "generating", "tool": "tool",
                      "approval": "waiting"}.get(state, "idle")
        if holo_state == "thinking" and self.holo.state == "tool" and \
                self.holo.tools_done < self.holo.tools_total:
            holo_state, detail = "tool", self.holo.detail  # More calls in this round: keep the arc.
        self.holo.set_state(holo_state, detail)
        self.refresh_hologram()

    # ── Hologram ──────────────────────────────────────────────────────────────────────────

    def make_hologram(self, **kwargs):
        return make_view(self, **kwargs)

    def refresh_hologram(self):
        """Let every hologram view re-decide between animating and a single static frame."""
        for view in self.query(".hologram"):
            view.sync()
        for hud in self.query(HoloHud):
            hud.refresh()
        for orb in self.query("HeaderOrb"):
            orb.refresh()

    def hologram_color(self):
        palette = self.palette
        flash, _ = self.holo.flash_age()
        if flash == "success":
            return palette["success"]
        if flash in {"error", "glitch"}:
            return palette["error"]
        return {"thinking": palette["primary"], "generating": palette["accent"], "tool": palette["secondary"],
                "waiting": palette["warning"], "voice": palette["accent"]}.get(self.holo.effective_state(),
                                                                            palette["primary"])

    def set_audio_level(self, level):
        """Feed a real microphone or speech-output level (0..1) to the hologram's voice mode.

        Safe to call from any thread and never blocks, so an audio callback can call it directly.
        Nothing in this app produces audio yet; voice mode stays dormant until something does."""
        self.post_message(AudioLevel(level))

    def on_audio_level(self, message):
        was_live = self.holo.voice_live()
        self.holo.set_audio_level(message.level)
        if not was_live:
            self.refresh_hologram()

    async def remount_holograms(self):
        """Rebuild the hologram widgets after a renderer change (text and image views differ)."""
        for container in [*self.query(HoloPanel), *self.query(Welcome)]:
            await container.recompose()
        self.layout_hologram()

    def hologram_failed(self, reason):
        """A renderer broke at run time: fall back to text and keep going."""
        if self.hologram_renderer == "image":
            self.hologram_renderer = "braille" if self.glyphs.orb != "@" else "ascii"
            self.hologram_reason = reason
            self.run_worker(self.remount_holograms(), exclusive=False)
            self.notify(f"{reason}. Using the {self.hologram_renderer} renderer.", title="Hologram",
                        severity="warning", timeout=4)

    def save_hologram_prefs(self):
        self.prefs.update(hologram=self.hologram_visible, hologram_mode=self.hologram_mode,
                          hologram_width=self.hologram_width, hologram_animate=self.hologram_animate)
        save_prefs(self.data_dir, self.prefs)

    def hologram_status(self):
        lines = [f"- **renderer** `{self.hologram_renderer}` — {self.hologram_reason}",
                 f"- **requested** `{self.hologram_mode}` · visible {'yes' if self.hologram_visible else 'no'}"
                 f" · width {self.hologram_width} · animation {'live' if self.hologram_animate else 'still'}"
                 f" · motion `{self.motion}`"]
        depth = getattr(self.console, "color_system", None) or "unknown"
        image = self.image_support
        probe = ("not probed (choose `/hologram image` and restart)" if image is None else
                 image.get("protocol") or image.get("error") or "no graphics protocol")
        lines.append(f"- **terminal** colors `{depth}` · glyphs `{'unicode' if self.glyphs.orb != '@' else 'ascii'}`"
                     f" · image protocol: {probe}")
        for view in self.query(".hologram"):
            stats = view.stats()
            where = "welcome" if view.id == "hero-holo" else "side panel"
            if view.suspended:
                lines.append(f"- **{where}** hidden (suspended)")
            elif stats["animating"]:
                lines.append(f"- **{where}** {view.size.width}x{view.size.height} cells · target "
                             f"{stats['target']:.0f} fps · measured {stats['fps']:.1f} fps · "
                             f"{stats['cost_ms']:.1f} ms/frame")
            else:
                lines.append(f"- **{where}** {view.size.width}x{view.size.height} cells · static frame "
                             f"({stats['cost_ms']:.1f} ms)")
        lines.append(f"- **core** state `{self.holo.effective_state()}` · energy {self.holo.params['energy']:.2f}")
        return "\n".join(lines)

    async def hologram_command(self, argument):
        words = argument.lower().split()
        word = words[0] if words else ""
        if not word or word == "toggle":
            self.action_toggle_hologram()
        elif word in {"on", "show"}:
            self.show_hologram(True)
        elif word in {"off", "hide"}:
            self.show_hologram(False)
        elif word in hologram.MODES:
            self.hologram_mode = word
            renderer, reason = hologram.choose_renderer(word, self.environ, self.glyphs.orb != "@",
                                                        self.image_support)
            changed = renderer != self.hologram_renderer
            self.hologram_renderer, self.hologram_reason = renderer, reason
            self.save_hologram_prefs()
            if changed:
                await self.remount_holograms()
            self.notify(reason, title=f"Hologram {renderer}", timeout=3,
                        severity="information" if renderer == word or word == "auto" else "warning")
        elif word in hologram.SIZES or word.isdigit():
            width = hologram.SIZES.get(word) or int(word)
            if not HOLOGRAM_WIDTHS[0] <= width <= HOLOGRAM_WIDTHS[1]:
                self.notify(f"Hologram width must be {HOLOGRAM_WIDTHS[0]}–{HOLOGRAM_WIDTHS[1]} columns.",
                            severity="error")
                return
            self.hologram_width = width
            self.hologram_visible = True
            self.save_hologram_prefs()
            self.layout_hologram()
            panel = self.query_one(HoloPanel)
            if not panel.display and not self.query(Welcome):
                self.notify("Saved. The terminal is too small to show it right now.", title="Hologram",
                            timeout=3)
        elif word in {"still", "live"}:
            self.hologram_animate = word == "live"
            self.save_hologram_prefs()
            self.holo.version += 1
            self.refresh_hologram()
            self.notify("Hologram animation on." if self.hologram_animate else "Hologram holds still; it "
                        "still changes color with the agent's state.", title="Hologram", timeout=3)
        elif word == "reset":
            self.holo_control.reset()
            self.holo.version += 1
            self.refresh_hologram()
        elif word == "status":
            await self.note(self.hologram_status(), "Hologram")
        else:
            self.notify("Try /hologram on, off, auto, braille, ascii, image, small, medium, large, a width, "
                        "still, live, reset or status.", severity="error", timeout=5)

    def show_hologram(self, visible):
        self.hologram_visible = visible
        self.save_hologram_prefs()
        self.layout_hologram()
        if visible and not self.query_one(HoloPanel).display and not self.query(Welcome):
            self.notify("The terminal is too narrow for the hologram; widen it or hide the sidebar.",
                        title="Hologram", timeout=3)

    def action_toggle_hologram(self):
        self.show_hologram(not self.hologram_visible)

    def action_rotate_hologram(self, yaw, pitch):
        self.holo_control.nudge(float(yaw), float(pitch))
        self.holo.version += 1
        self.refresh_hologram()

    def on_app_blur(self):
        self.focused_terminal = False
        self.refresh_hologram()

    def on_app_focus(self):
        self.focused_terminal = True
        self.refresh_hologram()

    # ── Connection ────────────────────────────────────────────────────────────────────────

    def check_connection(self, announce):
        model = self.config["model"]

        provider = self.provider

        def run():
            try:
                if hasattr(provider, "check"):  # Also tries a tiny message when the model isn't listed.
                    status, detail = provider.check()
                else:
                    models = provider.models()
                    status = "online" if model in models else "missing"
                    detail = (f"Model {model!r} is available." if status == "online" else
                              f"Model {model!r} isn't served here. Available: {', '.join(models[:6]) or 'none'}.")
            except Exception as error:  # noqa: BLE001 - shown to the user
                status, detail = "offline", str(error)
            self.post_message(ConnectionChecked(status, detail, announce))

        threading.Thread(target=run, name="connection-check", daemon=True).start()

    def on_connection_checked(self, message: ConnectionChecked):
        self.set_connection(message.status)
        if message.announce or message.status != "online":
            severity = {"online": "information", "missing": "warning"}.get(message.status, "error")
            self.notify(clean(message.detail)[:200], title={"online": "Connected", "missing": "Model missing"}
                        .get(message.status, "Offline"), severity=severity, timeout=4)

    def set_connection(self, status):
        self.connection = status
        self.query_one(TopBar).connection = status
        self.refresh_sidebar()

    # ── Appearance ────────────────────────────────────────────────────────────────────────

    def set_theme(self, name):
        self.theme_name = name
        self.theme = f"agent-{name}"
        self.prefs["theme"] = name
        save_prefs(self.data_dir, self.prefs)
        self.refresh_sidebar()
        for widget in self.query("AssistantMessage"):
            widget.set_meta(widget.meta)
        for widget in self.query("ToolCall"):
            widget.query_one(".tool-row", Static).update(widget.row())
        self.holo.version += 1
        self.refresh_hologram()
        self.screen.refresh()
        self.notify(PALETTES[name]["label"], title="Theme", timeout=2)

    def action_next_theme(self):
        names = list(PALETTES)
        self.set_theme(names[(names.index(self.theme_name) + 1) % len(names)])

    def set_motion(self, level):
        self.motion = level
        self.animation_level = TEXTUAL_LEVEL[level]
        self.prefs["motion"] = level
        save_prefs(self.data_dir, self.prefs)
        self.clock.set_fps(MOTION_FPS[level])
        self.holo.version += 1
        self.refresh_hologram()
        self.screen.refresh()
        self.notify({"full": "All animations on.", "reduced": "Only status spinners animate, slowly.",
                     "off": "Animations off."}[level], title=f"Motion {level}", timeout=2)

    def resize_sidebar(self, delta):
        self.sidebar_width = max(24, min(60, self.sidebar_width + delta))
        self.query_one(Sidebar).styles.width = self.sidebar_width
        self.prefs["sidebar_width"] = self.sidebar_width
        save_prefs(self.data_dir, self.prefs)
        self.layout_hologram()

    # ── Actions ───────────────────────────────────────────────────────────────────────────

    def action_toggle_sidebar(self):
        sidebar = self.query_one(Sidebar)
        narrow = self.size.width < 100
        showing = not sidebar.display
        self.sidebar_visible = showing
        if not narrow:
            self.prefs["sidebar"] = showing
            save_prefs(self.data_dir, self.prefs)
        sidebar.set_class(narrow and showing, "-overlay")
        self.layout_hologram()
        animate = self.motion == "full"
        if showing:
            sidebar.styles.width = 0 if animate else self.sidebar_width
            sidebar.display = True
            if animate:
                sidebar.styles.animate("width", self.sidebar_width, duration=0.22, easing="out_cubic")
        elif animate:
            def hide():
                sidebar.display = False
                sidebar.styles.width = self.sidebar_width
            sidebar.styles.animate("width", 0, duration=0.18, easing="in_cubic", on_complete=hide)
        else:
            sidebar.display = False

    def action_expand_all(self):
        outputs = [widget for widget in self.query(".tool-output") if isinstance(widget, Collapsible)
                   and widget.display]
        if not outputs:
            self.notify("No tool output in view.", timeout=2)
            return
        collapse = any(not widget.collapsed for widget in outputs)
        for widget in outputs:
            widget.collapsed = collapse

    async def action_clear_view(self):
        chat = self.query_one(ChatLog)
        if self.busy:
            self.notify("Wait for the current turn first.", severity="warning")
            return
        await chat.remove_children()
        await chat.mount(NoteCard(divider(f"view cleared · session {self.session} is still saved", self),
                                  "divider"))

    async def action_new_session(self):
        await self.run_slash("/new")

    def action_help(self):
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen(commands.markdown_table()))

    def action_scroll_chat(self, direction):
        if hasattr(self.screen, "action_scroll_body"):  # PageUp/PageDown scroll an open dialog.
            self.screen.action_scroll_body(direction * 10)
            return
        chat = self.query_one(ChatLog)
        if direction < 0:
            chat.scroll_page_up()
        else:
            chat.scroll_page_down()

    def action_scroll_bottom(self):
        self.query_one(ChatLog).anchor()

    def action_scroll_top(self):
        chat = self.query_one(ChatLog)
        chat.release_anchor()
        chat.scroll_home()

    async def action_escape(self):
        composer = self.query_one(Composer)
        if composer.suggesting:
            self.hide_suggestions()
        elif self.busy:
            await self.cancel_turn()
        elif self.size.width < 100 and self.query_one(Sidebar).display:
            self.action_toggle_sidebar()
        else:
            composer.focus()

    def action_copy_reply(self):
        if self.last_reply:
            self.copy_to_clipboard(self.last_reply)
            self.notify("Copied the last reply.", timeout=2)
        else:
            self.notify("Nothing to copy yet.", timeout=2)
        if self.focused is not None and self.focused.id == "copy-reply":
            self.query_one(Composer).focus()

    def use_system_clipboard(self):
        """Local Windows consoles get the system clipboard directly (Unicode, CRLF line breaks).
        Elsewhere, and over SSH, Textual asks the terminal to copy with OSC 52."""
        return os.name == "nt" and not self.is_headless and not os.environ.get("SSH_CONNECTION")

    def copy_to_clipboard(self, text):
        if self.use_system_clipboard() and clipboard.copy(text):
            self._clipboard = text
            return
        super().copy_to_clipboard(text)

    async def action_interrupt(self):
        focused = self.focused
        selection = self.screen.get_selected_text()
        if not selection and isinstance(focused, TextArea):
            selection = focused.selected_text
        if selection:
            self.copy_to_clipboard(selection)
            self.notify("Copied selection.", timeout=2)
        elif self.busy:
            await self.cancel_turn()
        elif isinstance(focused, Composer) and focused.text:
            focused.load_text("")
        else:
            self.notify("Press ctrl+q or type /exit to quit.", timeout=2)

    async def action_eof(self):
        if not self.query_one(Composer).text:
            await self.action_quit()

    def get_system_commands(self, screen):
        for command in super().get_system_commands(screen):
            if command.title not in {"Theme", "Keys"}:  # Themes are offered by our own provider.
                yield command

    def stop_turn(self):
        """Cancel a running turn without waiting for it, so nothing from it is saved."""
        if self.team_run is not None:
            self.team_run.cancel("The app was closed.")
            self.team_run = None
        if self.token is not None:
            self.token.cancelled = True
            self.token = self.current = None

    async def action_quit(self):
        self.stop_turn()  # Quitting mid-turn cancels it, like esc; a late reply is never saved.
        self.exit()

    def on_unmount(self):
        self.clock.stop()
        self.stop_turn()
