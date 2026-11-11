"""Project screens: the project switcher, the project home, the notes browser, the note editor and
the small dialogs they use.

Screens read and write notes through projects.py. Anything that changes the conversation (opening
a project, starting a chat, filling the composer) goes back through the app, so the rules for busy
turns, drafts and attachments live in one place (app.py).
"""
import hashlib
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Markdown, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from ..projects import KINDS, TASK_STATUSES, TEMPLATES, NoteConflict, ProjectError, convert_note, parse_note
from .widgets import clean

KIND_LABELS = {None: "All", "overview": "Overview", "note": "Notes", "idea": "Ideas", "decision": "Decisions",
               "task": "Tasks"}
FILTERS = [None, "task", "decision", "idea", "note", "overview"]
STATUS_GLYPHS = {"pending": "○", "in-progress": "◐", "completed": "●"}
STATUS_ASCII = {"pending": "[ ]", "in-progress": "[>]", "completed": "[x]"}
NARROW = 100


def status_mark(app, status):
    return (STATUS_GLYPHS if app.glyphs.orb != "@" else STATUS_ASCII).get(status, "")


def key_hints(app, pairs):
    """A one-line row of key hints in the app's style."""
    palette = app.palette
    line = Text()
    for keys, label in pairs:
        line.append(f" {keys} ", Style(color=palette["background"], bgcolor=palette["secondary"], bold=True))
        line.append(f" {label}  ", Style(color=palette["muted"]))
    return line


def empty_option(text):
    return Option(Text(text, Style(italic=True)), disabled=True)


class Narrow:
    """Screens that rearrange themselves below 100 columns instead of squeezing panels together."""

    def on_resize(self, event):
        self.set_class(event.size.width < NARROW, "-narrow")

    def on_mount(self):
        self.set_class(self.app.size.width < NARROW, "-narrow")


# ── Small dialogs ────────────────────────────────────────────────────────────────────────

class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("y", "decide(True)", "Yes"), Binding("n,escape", "decide(False)", "No")]

    def __init__(self, title, message, yes="Yes", no="Cancel", danger=False):
        super().__init__(classes="modal")
        self.title_text, self.message, self.yes, self.no, self.danger = title, message, yes, no, danger

    def compose(self):
        palette = self.app.palette
        with Vertical(classes="dialog confirm"):
            yield Static(Text(self.title_text, Style(color=palette["warning"], bold=True)), classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                yield Static(Text(clean(self.message)))
            with Horizontal(classes="dialog-buttons"):
                yield Button(f"{self.yes}  y", variant="error" if self.danger else "success", id="yes")
                yield Button(f"{self.no}  n", id="no")

    def on_mount(self):
        self.query_one("#no" if self.danger else "#yes", Button).focus()

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "yes")

    def action_decide(self, value):
        self.dismiss(value)


class ChoiceScreen(Narrow, ModalScreen):
    """A few labelled choices, each with its own key. Returns the chosen value, or None."""
    BINDINGS = [Binding("escape", "pick(None)", "Cancel")]

    def __init__(self, title, message, choices):
        super().__init__(classes="modal")
        self.title_text, self.message, self.choices = title, message, choices  # [(key, label, value)]

    def compose(self):
        palette = self.app.palette
        with Vertical(classes="dialog confirm"):
            yield Static(Text(self.title_text, Style(color=palette["primary"], bold=True)), classes="dialog-title")
            if self.message:
                yield Static(Text(clean(self.message), Style(color=palette["muted"])), classes="dialog-body")
            with Horizontal(classes="dialog-buttons choice-buttons"):
                for index, (key, label, _) in enumerate(self.choices):
                    yield Button(f"{label}  {key}", id=f"choice-{index}", compact=True)

    def on_mount(self):
        super().on_mount()
        self.query_one("#choice-0", Button).focus()

    def on_key(self, event):
        for index, (key, _, _) in enumerate(self.choices):
            if event.key == key:
                event.stop()
                self.dismiss(self.choices[index][2])
                return

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss(self.choices[int(event.button.id.split("-")[1])][2])

    def action_pick(self, index):
        self.dismiss(None if index is None else self.choices[index][2])


class PromptScreen(ModalScreen):
    """One or more text fields. Returns {name: value}, or None when cancelled."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title, fields, note=""):
        super().__init__(classes="modal")
        self.title_text, self.fields, self.note = title, fields, note  # [(name, label, value, placeholder)]

    def compose(self):
        palette = self.app.palette
        with Vertical(classes="dialog prompt"):
            yield Static(Text(self.title_text, Style(color=palette["primary"], bold=True)), classes="dialog-title")
            for name, label, value, placeholder in self.fields:
                yield Static(Text(label, Style(color=palette["muted"])), classes="field-label")
                yield Input(value=value, placeholder=placeholder, id=f"field-{name}")
            if self.note:
                yield Static(Text(self.note, Style(color=palette["muted"], italic=True)), classes="field-note")
            with Horizontal(classes="dialog-buttons"):
                yield Button("OK  enter", variant="primary", id="ok")
                yield Button("Cancel  esc", id="cancel")

    def on_mount(self):
        self.query_one(Input).focus()

    def values(self):
        return {name: self.query_one(f"#field-{name}", Input).value.strip() for name, *_ in self.fields}

    def on_input_submitted(self, event):
        inputs = list(self.query(Input))
        index = inputs.index(event.input)
        if index + 1 < len(inputs):
            inputs[index + 1].focus()
        else:
            self.dismiss(self.values())

    def on_button_pressed(self, event):
        self.dismiss(self.values() if event.button.id == "ok" else None)

    def action_cancel(self):
        self.dismiss(None)


class OutcomeScreen(ModalScreen):
    """Record what an implementation achieved. The status only changes to what the user picks."""
    BINDINGS = [Binding("ctrl+s", "save", "Save", priority=True), Binding("escape", "cancel", "Cancel")]

    def __init__(self, task, outcome=""):
        super().__init__(classes="modal")
        self.task_note = task
        self.outcome = outcome

    def compose(self):
        palette = self.app.palette
        statuses = [(f"keep {self.task_note.status}", "")] + [(status, status) for status in TASK_STATUSES
                                                          if status != self.task_note.status]
        with Vertical(classes="dialog outcome"):
            yield Static(Text(f"Record the outcome · {clean(self.task_note.title)}",
                              Style(color=palette["primary"], bold=True)), classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                for name, label, text in (("outcome", "Outcome — what changed", self.outcome),
                                          ("verification", "Verification performed (tests run, checks made)", ""),
                                          ("unresolved", "Unresolved issues", "")):
                    yield Static(Text(label, Style(color=palette["muted"])), classes="field-label")
                    yield TextArea(text, id=f"outcome-{name}", soft_wrap=True, tab_behavior="focus",
                                   classes="outcome-field")
                yield Static(Text("Status", Style(color=palette["muted"])), classes="field-label")
                yield Select(statuses, value="", allow_blank=False, id="outcome-status")
            yield Static(Text("A model's claim of success doesn't complete a task: choose the status yourself.",
                              Style(color=palette["muted"], italic=True)))
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save  ctrl+s", variant="success", id="save")
                yield Button("Cancel  esc", id="cancel")

    def on_mount(self):
        self.query_one("#outcome-outcome", TextArea).focus()

    def on_button_pressed(self, event):
        if event.button.id == "save":
            self.action_save()
        else:
            self.dismiss(None)

    def action_save(self):
        outcome = self.query_one("#outcome-outcome", TextArea).text.strip()
        if not outcome:
            self.app.notify("Describe the outcome in a sentence or two.", severity="warning")
            return
        status = self.query_one("#outcome-status", Select).value
        self.dismiss({"outcome": outcome, "verification": self.query_one("#outcome-verification", TextArea).text,
                      "unresolved": self.query_one("#outcome-unresolved", TextArea).text,
                      "status": status or None})

    def action_cancel(self):
        self.dismiss(None)

    def action_scroll_body(self, lines):
        self.query_one(".dialog-body").scroll_relative(y=lines, animate=False)


# ── Projects ─────────────────────────────────────────────────────────────────────────────

class ProjectsScreen(Narrow, Screen):
    """Every registered project, plus the classic sessions outside projects."""
    BINDINGS = [Binding("escape", "back", "Back"), Binding("a", "add", "Add"), Binding("r", "rename", "Rename"),
                Binding("m", "move", "Move"), Binding("delete,d", "unregister", "Unregister"),
                Binding("h", "home", "Home")]

    def compose(self):
        yield Static("", id="projects-title", classes="screen-title")
        yield OptionList(id="project-list", classes="screen-list")
        yield Static("", id="projects-detail", classes="screen-detail")
        yield Static("", id="projects-keys", classes="screen-keys")

    def on_mount(self):
        super().on_mount()
        self.refresh_list()
        self.query_one("#project-list").focus()

    def refresh_list(self, select=None):
        app, palette = self.app, self.app.palette
        title = Text(" Projects", Style(color=palette["primary"], bold=True))
        title.append(f"   registry: {app.registry.file}", Style(color=palette["muted"]))
        self.query_one("#projects-title", Static).update(title)
        listing = self.query_one("#project-list", OptionList)
        listing.clear_options()
        try:
            projects = app.registry.projects()
        except ProjectError as error:
            projects = []
            app.notify(clean(str(error)), severity="error", timeout=6)
        current = app.project.id if app.project else ""
        options = []
        for project in projects:
            prompt = Text()
            here = project.id == current
            prompt.append(f"{app.glyphs.dot_on if here else app.glyphs.dot_off} ",
                          Style(color=palette["primary"] if here else palette["muted"]))
            prompt.append(project.name[:40], Style(bold=True))
            prompt.append(f"  {project.path}", Style(color=palette["muted"]))
            if not project.available:
                prompt.append("  folder missing", Style(color=palette["error"]))
            options.append(Option(prompt, id=f"project:{project.id}"))
        classic = Text()
        classic.append(f"{app.glyphs.dot_on if not current else app.glyphs.dot_off} ",
                       Style(color=palette["primary"] if not current else palette["muted"]))
        classic.append("Classic sessions", Style(bold=True))
        classic.append("  conversations outside any project", Style(color=palette["muted"]))
        options.append(Option(classic, id="project:"))
        listing.add_options(options)
        ids = [option.id for option in options]
        wanted = f"project:{select if select is not None else current}"
        listing.highlighted = ids.index(wanted) if wanted in ids else 0
        if not projects:
            self.query_one("#projects-detail", Static).update(Text(
                "No projects yet. Press a to register an existing folder: its conversations, notes, decisions "
                "and tasks are then kept together. Nothing in the folder changes until you save a note.",
                Style(color=palette["muted"], italic=True)))
        else:
            self.query_one("#projects-detail", Static).update("")
        self.query_one("#projects-keys", Static).update(key_hints(app, [
            ("enter", "open"), ("a", "add folder"), ("r", "rename"), ("m", "moved folder"), ("d", "unregister"),
            ("h", "home"), ("esc", "back")]))

    def selected(self):
        listing = self.query_one("#project-list", OptionList)
        option = listing.highlighted_option if listing.highlighted is not None else None
        if option is None:
            return None, False
        identifier = option.id.split(":", 1)[1]
        if not identifier:
            return None, True
        try:
            return self.app.registry.find(identifier), True
        except ProjectError as error:
            self.app.notify(clean(str(error)), severity="error")
            return None, False

    async def on_option_list_option_selected(self, event):
        event.stop()
        project, chosen = self.selected()
        if chosen:
            await self.app.open_project(project, show_home=project is not None, origin=self)

    def action_back(self):
        self.app.pop_screen()

    async def action_home(self):
        if self.app.project is None:
            self.app.notify("Open a project first.", timeout=2)
            return
        self.app.pop_screen()
        self.app.action_project_home()

    def action_add(self):
        start = str(self.app.workspace or Path.cwd())

        async def added(values):
            if not values or not values["folder"]:
                return
            try:
                project = self.app.registry.add(values["folder"], values["name"] or None)
            except (OSError, ProjectError) as error:
                self.app.notify(clean(str(error)), severity="error", timeout=6)
                return
            self.refresh_list(project.id)
            self.app.notify(f"Registered {project.name}. Press enter to open it.", title="Projects", timeout=4)

        self.app.push_screen(PromptScreen("Register a project folder", [
            ("folder", "Existing folder", start, "C:\\path\\to\\project"),
            ("name", "Name (optional; defaults to the folder name)", "", "My project")],
            "Registering changes nothing in the folder. Notes go in .agent-project/ when you save one."), added)

    def action_rename(self):
        project, _ = self.selected()
        if project is None:
            return

        def renamed(values):
            if not values or not values["name"]:
                return
            try:
                updated = self.app.registry.rename(project.id, values["name"])
            except ProjectError as error:
                self.app.notify(clean(str(error)), severity="error")
                return
            if self.app.project and self.app.project.id == updated.id:
                self.app.project = updated
                self.app.refresh_project_labels()
                self.app.refresh_sidebar()
            self.refresh_list(updated.id)

        self.app.push_screen(PromptScreen(f"Rename {project.name}", [("name", "New name", project.name, "")],
                                          "Only the display name changes; conversations and notes stay linked."),
                             renamed)

    def action_move(self):
        project, _ = self.selected()
        if project is None:
            return

        def moved(values):
            if not values or not values["folder"]:
                return
            try:
                updated = self.app.registry.move(project.id, values["folder"])
            except (OSError, ProjectError) as error:
                self.app.notify(clean(str(error)), severity="error", timeout=6)
                return
            self.refresh_list(updated.id)
            self.app.notify(f"{updated.name} now points at {updated.path}.", timeout=4)

        self.app.push_screen(PromptScreen(f"Where is {project.name} now?",
                                          [("folder", "New folder", str(project.path), "")],
                                          "Use this after moving or renaming the project folder."), moved)

    def action_unregister(self):
        project, _ = self.selected()
        if project is None:
            return

        async def decided(value):
            if not value:
                return
            try:
                self.app.registry.remove(project.id)
            except ProjectError as error:
                self.app.notify(clean(str(error)), severity="error")
                return
            if self.app.project and self.app.project.id == project.id:
                await self.app.open_project(None, quiet=True)
            self.refresh_list()
            self.app.notify(f"Unregistered {project.name}. No files were deleted.", title="Projects", timeout=4)

        self.app.push_screen(ConfirmScreen(
            f"Unregister {project.name}?",
            f"The folder {project.path} and its notes stay exactly as they are. Its conversations stay in the "
            "data directory and come back if you register the folder again.", "Unregister", "Keep"), decided)


class ProjectHome(Narrow, Screen):
    """Overview, open tasks, recent notes and conversations, and the main actions."""
    BINDINGS = [Binding("escape", "back", "Back"), Binding("c", "chat", "Chat"),
                Binding("b", "brainstorm", "Brainstorm"), Binding("n", "new_note", "Note"),
                Binding("t", "new_task", "Task"), Binding("r", "resume", "Continue"),
                Binding("k", "notes", "Notes"), Binding("j", "tasks", "Tasks"), Binding("d", "decisions", "Decisions"),
                Binding("e", "edit_overview", "Overview"), Binding("p", "projects", "Projects")]

    def compose(self):
        yield Static("", id="home-title", classes="screen-title")
        with Horizontal(id="home-actions"):
            for key, label, action in (("c", "Chat", "chat"), ("b", "Brainstorm", "brainstorm"),
                                       ("n", "New note", "new_note"), ("t", "New task", "new_task"),
                                       ("r", "Continue", "resume"), ("k", "Notes", "notes")):
                yield Button(f"{label} {key}", id=f"action-{action}", compact=True, flat=True)
        with VerticalScroll(id="home-scroll"):
            with Horizontal(id="home-columns"):
                with Vertical(id="home-left", classes="home-column"):
                    yield Static("", id="home-overview-title", classes="home-heading")
                    yield Markdown("", id="home-overview")
                    yield Static("", id="home-tasks-title", classes="home-heading")
                    yield OptionList(id="home-tasks", classes="home-list")
                with Vertical(id="home-right", classes="home-column"):
                    yield Static("", id="home-notes-title", classes="home-heading")
                    yield OptionList(id="home-notes", classes="home-list")
                    yield Static("", id="home-chats-title", classes="home-heading")
                    yield OptionList(id="home-chats", classes="home-list")
        yield Static("", id="home-keys", classes="screen-keys")

    def on_mount(self):
        super().on_mount()
        self.refresh_home()

    def on_screen_resume(self):
        self.refresh_home()

    def heading(self, widget_id, text, count=None):
        palette = self.app.palette
        line = Text(text, Style(color=palette["secondary"], bold=True))
        if count is not None:
            line.append(f"  {count}", Style(color=palette["muted"]))
        self.query_one(widget_id, Static).update(line)

    def refresh_home(self):
        app = self.app
        project, palette = app.project, app.palette
        if project is None:
            return
        title = Text()
        title.append(f" {project.name}", Style(color=palette["primary"], bold=True))
        title.append(f"   {project.path}", Style(color=palette["muted"]))
        self.query_one("#home-title", Static).update(title)
        knowledge = project.knowledge
        try:
            project.require_folder()
            notes = knowledge.notes()
        except (OSError, ProjectError) as error:
            notes = []
            app.notify(clean(str(error)), severity="error", timeout=8)
        overview = next((note for note in notes if note.kind == "overview"), None)
        self.heading("#home-overview-title", "Overview & goals")
        if overview is None:
            text = ("_No overview yet._ Press **e** to write what this project is and what it should achieve. "
                    "It is shared with the agent at the start of each conversation.")
        else:
            body = overview.body.strip()
            if body.splitlines() and body.splitlines()[0].strip() == f"# {overview.title}":
                body = "\n".join(body.splitlines()[1:]).strip()  # The title is already at the top.
            text = body[:1200] + ("\n\n…" if len(body) > 1200 else "") or "_The overview is empty — press **e**._"
        self.query_one("#home-overview", Markdown).update(text)
        tasks = sorted((note for note in notes if note.kind == "task" and note.status != "completed"),
                       key=lambda note: (note.status != "in-progress", note.updated))
        self.heading("#home-tasks-title", "Open tasks", len(tasks))
        task_list = self.query_one("#home-tasks", OptionList)
        task_list.clear_options()
        task_list.add_options([Option(Text(f"{status_mark(app, note.status)} {note.title}"), id=f"note:{note.id}")
                               for note in tasks[:12]] or
                              [empty_option("No open tasks — press t to create one, or j to see all tasks.")])
        recent = sorted((note for note in notes if note.kind in {"note", "idea", "decision"}),
                        key=lambda note: note.updated, reverse=True)[:10]
        self.heading("#home-notes-title", "Recent notes & decisions")
        note_list = self.query_one("#home-notes", OptionList)
        note_list.clear_options()
        rows = []
        for note in recent:
            line = Text()
            line.append(f"{note.kind:<9}", Style(color=palette["muted"]))
            line.append(note.title)
            rows.append(Option(line, id=f"note:{note.id}"))
        note_list.add_options(rows or [empty_option("No notes yet — press n to write one, or b to brainstorm.")])
        chats = app.state.recent_conversations(8)
        self.heading("#home-chats-title", "Recent conversations")
        chat_list = self.query_one("#home-chats", OptionList)
        chat_list.clear_options()
        rows = []
        for item in chats:
            line = Text()
            line.append(item["session"][:28], Style(bold=item["session"] == app.session))
            line.append(f"  {item['kind']} · {item['turns']}", Style(color=palette["muted"]))
            if item["opener"]:
                line.append(f"  {item['opener'][:60]}", Style(color=palette["muted"], italic=True))
            rows.append(Option(line, id=f"session:{item['session']}"))
        chat_list.add_options(rows or [empty_option("No conversations yet — press c to chat or b to brainstorm.")])
        self.query_one("#home-keys", Static).update(key_hints(app, [
            ("c", "chat"), ("b", "brainstorm"), ("n", "note"), ("t", "task"), ("r", "continue"), ("k", "notes"),
            ("j", "tasks"), ("d", "decisions"), ("e", "overview"), ("p", "projects"), ("esc", "back")]))

    async def on_option_list_option_selected(self, event):
        event.stop()
        kind, _, value = (event.option.id or "").partition(":")
        if kind == "note":
            self.app.push_screen(NotesScreen(select=value, view=True))
        elif kind == "session":
            self.app.pop_screen()
            await self.app.switch_session(value)

    async def on_button_pressed(self, event):
        event.stop()
        await self.app.run_action(event.button.id.removeprefix("action-"), self)

    def action_back(self):
        self.app.pop_screen()

    async def action_chat(self):
        self.app.pop_screen()
        await self.app.start_conversation("chat")

    async def action_brainstorm(self):
        self.app.pop_screen()
        await self.app.start_conversation("brainstorm")

    async def action_resume(self):
        chats = self.app.state.recent_conversations(1)
        if not chats:
            self.app.notify("No conversations yet. Press c to start one.", timeout=3)
            return
        self.app.pop_screen()
        await self.app.switch_session(chats[0]["session"])

    def action_new_note(self):
        self.app.push_screen(ChoiceScreen("New note", "What kind of entry?", [
            ("n", "Note", "note"), ("i", "Idea", "idea"), ("d", "Decision", "decision"), ("t", "Task", "task")]),
            lambda kind: kind and self.app.edit_new_note(kind))

    def action_new_task(self):
        self.app.edit_new_note("task")

    def action_notes(self):
        self.app.push_screen(NotesScreen())

    def action_tasks(self):
        self.app.push_screen(NotesScreen(kind="task"))

    def action_decisions(self):
        self.app.push_screen(NotesScreen(kind="decision"))

    def action_edit_overview(self):
        overview = self.app.project.knowledge.overview() if self.app.project else None
        if overview is None:
            self.app.edit_new_note("overview", self.app.project.name)
        else:
            self.app.push_screen(NoteEditor(note=overview))

    def action_projects(self):
        self.app.pop_screen()
        self.app.action_projects()

    def action_scroll_body(self, lines):
        self.query_one("#home-scroll").scroll_relative(y=lines, animate=False)


# ── Notes ────────────────────────────────────────────────────────────────────────────────

def note_markdown(app, note, knowledge):
    """A note as shown in the preview: a metadata line, warnings, links, then its text."""
    parts = [f"`{note.id}` · {note.kind}" + (f" · **{note.status}**" if note.kind == "task" else "")]
    if note.path is not None:
        try:
            parts.append(f"`{note.path.relative_to(knowledge.root).as_posix()}`")
        except ValueError:
            parts.append(f"`{note.path}`")
    if note.problems:
        parts.append("\n".join(f"> ⚠ {clean(problem)}" for problem in note.problems))
    known = {item.id for item in knowledge.notes()}
    links = note.links()
    if links:
        parts.append("Links: " + ", ".join(f"`{item}`" + ("" if item in known else " (missing)") for item in links))
    backlinks = [item.id for item in knowledge.backlinks(note.id)]
    if backlinks:
        parts.append("Linked from: " + ", ".join(f"`{item}`" for item in backlinks))
    if note.conversations:
        parts.append("Conversations: " + ", ".join(f"`{item}`" for item in note.conversations))
    selected = app.state.conversation(app.session)["notes"] if app.project else []
    if note.id in selected:
        parts.append("_Selected as context for this conversation._")
    return "\n\n".join(parts) + "\n\n---\n\n" + (clean(note.body.strip()) or "_Empty._")


NOTE_KEYS = [("enter", "open"), ("e", "edit"), ("n", "new"), ("r", "rename"), ("c", "use in chat"),
             ("x", "convert"), ("i", "implement"), ("g", "team implement"), ("o", "outcome"), ("s", "status"),
             ("l", "locate"),
             ("/", "search"), ("tab", "filter"), ("esc", "back")]


class NoteActions:
    """Actions on one note, shared by the notes browser and the note view."""

    def note_action(self, action, note):
        app = self.app
        if note is None:
            app.notify("Choose a note first.", timeout=2)
            return
        if action == "edit":
            app.push_screen(NoteEditor(note=note), self.after_change)
        elif action == "rename":
            def renamed(values):
                if not values or not values["title"]:
                    return
                try:
                    updated = app.project.knowledge.rename(app.project.knowledge.get(note.id), values["title"])
                except (OSError, ProjectError) as error:
                    app.notify(clean(str(error)), severity="error", timeout=6)
                    return
                app.notify(f"Renamed to {updated.path.name}. Its id {updated.id} and links are unchanged.", timeout=4)
                self.after_change(updated)
            app.push_screen(PromptScreen(f"Rename {note.title}", [("title", "Title", note.title, "")],
                                         "The file name follows the title; the id stays the same."), renamed)
        elif action == "context":
            added = app.toggle_context_note(note.id)
            app.notify(f"{note.title} {'will be' if added else 'is no longer'} sent with this conversation.",
                       title="Context", timeout=3)
            self.after_change(note)
        elif action == "convert":
            if note.kind not in {"idea", "note", "decision"}:
                app.notify("Convert ideas and notes into decisions or tasks, and decisions into tasks.", timeout=3)
                return
            choices = [("t", "Task", "task")] if note.kind == "decision" else \
                [("d", "Decision", "decision"), ("t", "Task", "task")]
            def chosen(kind):
                if kind:
                    title, body = convert_note(note, kind)
                    app.push_screen(NoteEditor(kind=kind, title=title, body=body, related=[note.id],
                                               conversations=note.conversations), self.after_change)
            app.push_screen(ChoiceScreen(f"Turn {note.title} into…", "You review the draft before it is saved.",
                                         choices), chosen)
        elif action == "implement":
            if note.kind != "task":
                app.notify("Only tasks can be implemented. Convert this note to a task with x.", timeout=3)
                return
            app.run_worker(app.start_implementation(note.id), exclusive=True, group="project")
        elif action == "team":
            if note.kind != "task":
                app.notify("Team implementation runs start from tasks.", timeout=3)
                return
            from ..projects import handoff_prompt
            mode = app.team_mode or "implement"
            while len(app.screen_stack) > 1:
                app.pop_screen()
            app.confirm_team_run(mode, handoff_prompt(note), task=note.id)
        elif action == "outcome":
            if note.kind != "task":
                app.notify("Outcomes are recorded on tasks.", timeout=3)
                return
            app.record_outcome(note.id, self.after_change)
        elif action == "status":
            if note.kind != "task":
                app.notify("Only tasks have a status.", timeout=2)
                return
            nxt = TASK_STATUSES[(TASK_STATUSES.index(note.status) + 1) % len(TASK_STATUSES)]
            if nxt == "completed" and "**Outcome:**" not in note.section("Outcome"):
                def confirmed(value):
                    if value:
                        self.set_status(note, nxt)
                app.push_screen(ConfirmScreen("Complete without an outcome?",
                                              "No outcome is recorded for this task yet. Press o to record what was "
                                              "done and how it was verified, or complete it anyway.",
                                              "Complete", "Cancel"), confirmed)
                return
            self.set_status(note, nxt)
        elif action == "locate":
            app.copy_to_clipboard(str(note.path))
            app.notify(f"{note.path}\n(path copied — open it in any editor)", title="File", timeout=6)

    def set_status(self, note, status):
        app = self.app
        try:
            fresh = app.project.knowledge.get(note.id)
            if fresh.digest != note.digest:
                raise NoteConflict(f"{note.path.name} changed outside the app; reload it first.")
            fresh.status = status
            updated = app.project.knowledge.save(fresh)
        except (OSError, ProjectError) as error:
            app.notify(clean(str(error)), severity="error", timeout=6)
            return
        app.notify(f"{updated.title}: {updated.status}", title="Task", timeout=2)
        self.after_change(updated)

    def after_change(self, note=None):
        """Called after a note was saved or changed; screens refresh what they show."""


class NotesScreen(Narrow, NoteActions, Screen):
    """Browse, filter and search the project's notes, ideas, decisions and tasks."""
    ACTIONS = (("e", "edit", "Edit"), ("r", "rename", "Rename"), ("c", "context", "Context"),
               ("x", "convert", "Convert"), ("i", "implement", "Implement"), ("g", "team", "Team"),
               ("o", "outcome", "Outcome"), ("s", "status", "Status"), ("l", "locate", "Locate"))
    BINDINGS = [Binding("escape", "back", "Back"), Binding("slash", "search", "Search"),
                Binding("tab", "next_filter", "Filter"), Binding("shift+tab", "previous_filter", "Filter"),
                Binding("n", "new", "New"),
                *[Binding(key, f"act('{action}')", label, show=False) for key, action, label in ACTIONS]]

    def __init__(self, kind=None, query="", select=None, view=False):
        super().__init__()
        self.kind = kind if kind in KINDS else None
        self.query_text = query
        self.select = select
        self.open_view = view
        self.notes = []

    def compose(self):
        yield Static("", id="notes-title", classes="screen-title")
        yield Static("", id="notes-filters")
        yield Input(value=self.query_text, placeholder="Search notes — every word must match", id="notes-search")
        with Horizontal(id="notes-body"):
            yield OptionList(id="notes-list", classes="screen-list")
            with VerticalScroll(id="notes-preview"):
                yield Markdown("", id="notes-preview-text")
        yield Static("", id="notes-keys", classes="screen-keys")

    def on_mount(self):
        super().on_mount()
        self.refresh_notes()
        self.query_one("#notes-list").focus()
        if self.open_view and self.select:
            note = self.current()
            if note is not None and note.id == self.select:
                self.view(note)

    def on_screen_resume(self):
        self.refresh_notes(keep=True)

    def refresh_notes(self, keep=False):
        app = self.app
        palette = app.palette
        if app.project is None:
            return
        knowledge = app.project.knowledge
        current = self.current() if keep else None
        try:
            app.project.require_folder()
            if self.query_text.strip():
                notes = knowledge.search(self.query_text, [self.kind] if self.kind else None, limit=200)
            else:
                notes = knowledge.notes([self.kind] if self.kind else None)
        except (OSError, ProjectError) as error:
            notes = []
            app.notify(clean(str(error)), severity="error", timeout=8)
        if self.kind == "task":
            notes.sort(key=lambda note: (TASK_STATUSES.index(note.status), note.title.casefold()))
        self.notes = notes
        title = Text(f" {KIND_LABELS[self.kind]}", Style(color=palette["primary"], bold=True))
        title.append(f" · {app.project.name}", Style(color=palette["foreground"]))
        title.append(f"   {knowledge.directory}", Style(color=palette["muted"]))
        self.query_one("#notes-title", Static).update(title)
        filters = Text(" ")
        for kind in FILTERS:
            active = kind == self.kind
            filters.append(f" {KIND_LABELS[kind]} ", Style(color=palette["background"] if active else palette["muted"],
                                                           bgcolor=palette["primary"] if active else None,
                                                           bold=active))
            filters.append(" ")
        self.query_one("#notes-filters", Static).update(filters)
        selected = set(app.state.conversation(app.session)["notes"])
        listing = self.query_one("#notes-list", OptionList)
        listing.clear_options()
        options = []
        for note in notes:
            line = Text()
            line.append("● " if note.id in selected else "  ", Style(color=palette["accent"]))
            if note.kind == "task":
                line.append(f"{status_mark(app, note.status)} ", Style(color=palette["secondary"]))
            elif not self.kind:
                line.append(f"{note.kind:<9}", Style(color=palette["muted"]))
            line.append(note.title[:60])
            if note.problems:
                line.append(" ⚠" if app.glyphs.orb != "@" else " !", Style(color=palette["warning"]))
            options.append(Option(line, id=f"note:{note.id}"))
        for path, message in knowledge.skipped:
            options.append(empty_option(f"skipped {path.name}: {message}"))
        if not options:
            what = KIND_LABELS[self.kind].lower() if self.kind else "notes"
            options.append(empty_option(f"No {what} match “{self.query_text}”." if self.query_text.strip() else
                                        f"No {what} yet — press n to create one."))
        listing.add_options(options)
        wanted = (current.id if current else None) or self.select
        ids = [option.id for option in options]
        if wanted and f"note:{wanted}" in ids:
            listing.highlighted = ids.index(f"note:{wanted}")
        elif notes:
            listing.highlighted = 0
        self.show_preview()
        self.query_one("#notes-keys", Static).update(key_hints(app, NOTE_KEYS))

    def current(self):
        listing = self.query_one("#notes-list", OptionList)
        if listing.highlighted is None or not self.notes:
            return None
        option = listing.get_option_at_index(listing.highlighted)
        identifier = (option.id or "").removeprefix("note:")
        return next((note for note in self.notes if note.id == identifier), None)

    def show_preview(self):
        note = self.current()
        text = note_markdown(self.app, note, self.app.project.knowledge) if note else \
            "_Choose a note on the left._"
        self.query_one("#notes-preview-text", Markdown).update(text)

    def on_option_list_option_highlighted(self, event):
        if event.option_list.id == "notes-list":
            self.show_preview()

    def on_option_list_option_selected(self, event):
        event.stop()
        note = self.current()
        if note is not None:
            self.view(note)

    def view(self, note):
        def done(action):
            if action:
                self.note_action(action, self.current() if self.current() and self.current().id == note.id
                                 else note)
        self.app.push_screen(NoteView(note), done)

    def on_input_changed(self, event):
        if event.input.id == "notes-search":
            self.query_text = event.value
            self.refresh_notes()

    def on_input_submitted(self, event):
        self.query_one("#notes-list").focus()

    def after_change(self, note=None):
        if note is not None:
            self.select = note.id
        self.refresh_notes(keep=note is None)

    def action_back(self):
        if self.focused is not None and self.focused.id == "notes-search":
            self.query_one("#notes-list").focus()
            return
        self.app.pop_screen()

    def action_search(self):
        self.query_one("#notes-search", Input).focus()

    def action_next_filter(self, step=1):
        self.kind = FILTERS[(FILTERS.index(self.kind) + step) % len(FILTERS)]
        self.refresh_notes()

    def action_previous_filter(self):
        self.action_next_filter(-1)

    def action_new(self):
        if self.kind:
            self.app.edit_new_note(self.kind, callback=self.after_change)
            return
        self.app.push_screen(ChoiceScreen("New note", "What kind of entry?", [
            ("n", "Note", "note"), ("i", "Idea", "idea"), ("d", "Decision", "decision"), ("t", "Task", "task")]),
            lambda kind: kind and self.app.edit_new_note(kind, callback=self.after_change))

    def action_act(self, action):
        if self.focused is not None and self.focused.id == "notes-search":
            return
        self.note_action(action, self.current())

    def action_scroll_body(self, lines):
        self.query_one("#notes-preview").scroll_relative(y=lines, animate=False)


class NoteView(ModalScreen):
    """One note in full. Its keys return an action for the notes browser to carry out."""
    BINDINGS = [Binding("escape,q", "close", "Close"),
                *[Binding(key, f"pick('{action}')", label, show=False) for key, action, label in NotesScreen.ACTIONS]]

    def __init__(self, note):
        super().__init__(classes="modal")
        self.note = note

    def compose(self):
        app = self.app
        palette = app.palette
        with Vertical(classes="dialog preview note-view"):
            yield Static(Text(f" {self.note.title}", Style(color=palette["primary"], bold=True)),
                         classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                yield Markdown(note_markdown(app, self.note, app.project.knowledge))
            yield Static(key_hints(app, [pair for pair in NOTE_KEYS if pair[0] not in {"/", "tab", "n", "enter"}]))

    def action_close(self):
        self.dismiss(None)

    def action_pick(self, action):
        self.dismiss(action)

    def action_scroll_body(self, lines):
        self.query_one(".dialog-body").scroll_relative(y=lines, animate=False)


class NoteEditor(Screen):
    """Edit a note's title, status and Markdown text, or review a new one before it is saved.

    Saving checks that the file hasn't changed since it was opened; if it has, the user chooses to
    overwrite it, reload it (dropping these edits) or keep editing."""
    BINDINGS = [Binding("ctrl+s", "save", "Save", priority=True), Binding("escape", "cancel", "Cancel")]

    def __init__(self, note=None, *, kind=None, title="", body=None, related=(), conversations=(), status=None):
        super().__init__()
        self.note = note
        self.kind = note.kind if note else kind
        self.initial = (note.title if note else title, note.body if note else body,
                        note.status if note else (status or "pending"))
        self.related = list(related)
        self.conversations = list(conversations)

    def compose(self):
        app = self.app
        palette = app.palette
        title, body, status = self.initial
        heading = Text(f" {'Edit' if self.note else 'New'} {self.kind}", Style(color=palette["primary"], bold=True))
        if self.note is not None and self.note.path is not None:
            heading.append(f"   {self.note.path}", Style(color=palette["muted"]))
        elif self.conversations:
            heading.append(f"   from conversation {self.conversations[-1]}", Style(color=palette["muted"]))
        yield Static(heading, id="editor-title", classes="screen-title")
        with Horizontal(id="editor-meta"):
            yield Input(value=title, placeholder="Title", id="editor-name")
            if self.kind == "task":
                yield Select([(item, item) for item in TASK_STATUSES], value=status, allow_blank=False,
                             id="editor-status")
        if body is None:
            body = f"# {title}\n\n{TEMPLATES[self.kind]}" if title else TEMPLATES[self.kind]
        yield TextArea(body, id="editor-body", soft_wrap=True, tab_behavior="focus", show_line_numbers=False)
        links = ", ".join(self.related or (self.note.related if self.note else []))
        hint = [("ctrl+s", "save"), ("esc", "cancel")]
        yield Static(key_hints(app, hint + ([("", f"links: {links}")] if links else [])), classes="screen-keys")

    def on_mount(self):
        self.initial = self.values()  # What "unchanged" means for the cancel check.
        self.query_one("#editor-name" if not self.initial[0] else "#editor-body").focus()

    def values(self):
        status = self.query_one("#editor-status", Select).value if self.kind == "task" else ""
        return (self.query_one("#editor-name", Input).value, self.query_one("#editor-body", TextArea).text, status)

    def modified(self):
        return self.values() != self.initial

    def action_save(self, expected=...):
        app = self.app
        knowledge = app.project.knowledge
        title, body, status = self.values()
        try:
            if self.note is None:
                note = knowledge.create(self.kind, title, body, related=self.related,
                                        conversations=self.conversations, status=status or None)
            else:
                note = self.note
                renamed = " ".join(title.split()) != note.title
                note.body, note.status = body, status or note.status
                note = knowledge.save(note, expected)
                if renamed:
                    note = knowledge.rename(note, title)
        except NoteConflict as conflict:
            self.conflict(conflict)
            return
        except (OSError, ProjectError) as error:
            app.notify(clean(str(error)), severity="error", timeout=6)
            return
        app.notify(f"Saved {note.id} · {note.path.name}", title="Notes", timeout=3)
        self.dismiss(note)

    def conflict(self, conflict):
        def chosen(choice):
            if choice == "overwrite":
                current = self.note.path.read_bytes() if self.note.path.exists() else None
                self.action_save(hashlib.sha256(current).hexdigest() if current is not None else None)
            elif choice == "reload":
                fresh = self.note.path.read_bytes() if self.note.path.exists() else None
                if fresh is None:
                    self.app.notify("The file is gone. Save to create it again, or cancel.", severity="warning")
                    return
                self.note = parse_note(fresh, self.note.path, self.kind)
                self.query_one("#editor-name", Input).value = self.note.title
                self.query_one("#editor-body", TextArea).load_text(self.note.body)
                if self.kind == "task":
                    self.query_one("#editor-status", Select).value = self.note.status
                self.initial = (self.note.title, self.note.body, self.note.status if self.kind == "task" else "")
                self.app.notify("Reloaded the version on disk.", timeout=3)
        self.app.push_screen(ChoiceScreen("This note changed outside the app", str(conflict), [
            ("k", "Keep editing", None), ("r", "Reload theirs", "reload"), ("o", "Overwrite with mine", "overwrite")]),
            chosen)

    def action_cancel(self):
        if not self.modified():
            self.dismiss(None)
            return
        self.app.push_screen(ConfirmScreen("Discard your changes?", "Nothing has been saved yet.", "Discard",
                                           "Keep editing", danger=True),
                             lambda discard: discard and self.dismiss(None))
