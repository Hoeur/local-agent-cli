"""Modal overlays: change approval, file preview and the help sheet."""
from pathlib import Path

from rich.console import Group
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .widgets import clean

LANGUAGES = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx", ".json": "json",
    ".md": "markdown", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".html": "html", ".css": "css",
    ".sh": "bash", ".ps1": "powershell", ".rs": "rust", ".go": "go", ".java": "java", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cs": "csharp", ".sql": "sql", ".xml": "xml", ".ini": "ini", ".cfg": "ini",
}


SCROLL_KEYS = [Binding("up", "scroll_body(-1)", "Scroll", show=False),
               Binding("down", "scroll_body(1)", "Scroll", show=False),
               Binding("pageup", "scroll_body(-10)", "Scroll", show=False),
               Binding("pagedown", "scroll_body(10)", "Scroll", show=False)]


class FitsWindow:
    """Keeps a confirmation dialog inside the window: a long diff or list scrolls, and the title,
    note and buttons always stay on screen."""
    # Rows the dialog needs besides its body: border and padding 4, title 2, body margin 1, note up
    # to 2 when it wraps, buttons 3.
    CHROME = 12

    def fit_body(self):
        body = self.query_one(".dialog-body")
        body.styles.max_height = max(3, int(self.app.size.height * 0.85) - self.CHROME)

    def on_resize(self, event):
        self.fit_body()

    def action_scroll_body(self, lines):
        body = self.query_one(".dialog-body")
        body.scroll_relative(y=lines, animate=False)


class ApprovalScreen(FitsWindow, ModalScreen[bool]):
    """Shows staged memory and skill changes; nothing is saved unless the user approves."""
    BINDINGS = [Binding("y", "decide(True)", "Save"), Binding("n", "decide(False)", "Discard"),
                Binding("escape", "decide(False)", "Discard"), *SCROLL_KEYS]

    def __init__(self, changes):
        super().__init__(classes="modal")
        self.changes = changes

    def compose(self):
        palette, glyphs = self.app.palette, self.app.glyphs
        lines = []
        for (kind, name), value in self.changes.items():
            line = Text()
            if value is None:
                line.append(" − delete ", Style(color=palette["error"], bold=True))
                line.append(f"{kind} ", Style(color=palette["muted"]))
                line.append(clean(name), Style(bold=True))
            elif kind == "memory":
                line.append(" + memory ", Style(color=palette["success"], bold=True))
                line.append(clean(name), Style(bold=True))
                line.append(f": {clean(value)}")
            else:
                line.append(" + skill ", Style(color=palette["success"], bold=True))
                line.append(clean(name), Style(bold=True))
                line.append(f": {clean(value['description'])}")
                for step in str(value["steps"]).splitlines():
                    line.append(f"\n     {clean(step)}", Style(color=palette["muted"]))
            lines.append(line)
        with Vertical(classes="dialog approval"):
            yield Static(Text(f"{glyphs.warn} The agent wants to save these changes",
                              Style(color=palette["warning"], bold=True)), classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                yield Static(Group(*lines))
            yield Static(Text("Nothing is saved unless you approve. Declined changes are discarded.",
                              Style(color=palette["muted"], italic=True)))
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save  y", variant="success", id="save")
                yield Button("Discard  n", variant="error", id="discard")

    def on_mount(self):
        self.fit_body()
        self.query_one("#save", Button).focus()

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "save")

    def action_decide(self, value):
        self.dismiss(value)


class ActionScreen(FitsWindow, ModalScreen[bool]):
    """Asks before a file change or a shell command. Shows the diff or the command in full."""
    BINDINGS = [Binding("y", "decide(True)", "Allow"), Binding("n", "decide(False)", "Decline"),
                Binding("escape", "decide(False)", "Decline"), *SCROLL_KEYS]
    VERBS = {"write_file": "write a file", "patch": "edit a file", "terminal": "run a command"}

    def __init__(self, action):
        super().__init__(classes="modal")
        self.action = action

    def compose(self):
        palette, glyphs = self.app.palette, self.app.glyphs
        action = self.action
        danger = action.get("danger")
        who = f"{clean(action['agent_name'])} ({clean(action['role'])})" if action.get("agent_name") else "The agent"
        title = Text(f"{glyphs.warn} {who} wants to {self.VERBS.get(action['tool'], action['tool'])}",
                     Style(color=palette["error" if danger else "warning"], bold=True))
        detail = clean(str(action.get("detail") or ""))
        language = "bash" if action["tool"] == "terminal" else "diff"
        body = []
        if action.get("agent_name"):
            # Team runs: say exactly who is asking, where, and for which assignment.
            body.append(Text(f"Team run {clean(action['run'])} · project {clean(action['project'])} · "
                             f"{clean(action['model'])} · assignment {clean(action['subtask'])}\n"
                             f"Workspace {clean(action['workspace'])}", Style(color=palette["muted"])))
        body.append(Text(clean(action["summary"]), Style(bold=True)))
        if danger:
            body.append(Text(f"\n Dangerous: this command {danger}. ", Style(color=palette["background"],
                                                                         bgcolor=palette["error"], bold=True)))
        body.append(Text(""))
        body.append(Syntax(detail, language, theme="monokai" if not palette.get("light") else "default",
                           word_wrap=True, background_color="default"))
        with Vertical(classes="dialog approval"):
            yield Static(title, classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                yield Static(Group(*body))
            yield Static(Text("Nothing runs or changes unless you allow it.  ↑ ↓ PgUp PgDn scroll",
                              Style(color=palette["muted"], italic=True)))
            with Horizontal(classes="dialog-buttons"):
                yield Button("Allow  y", variant="error" if danger else "success", id="allow")
                yield Button("Decline  n", variant="default" if danger else "error", id="decline")

    def on_mount(self):
        self.fit_body()
        # A dangerous command starts on Decline, so a stray enter never runs it.
        self.query_one("#decline" if self.action.get("danger") else "#allow", Button).focus()

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "allow")

    def action_decide(self, value):
        self.dismiss(value)


class FilePreview(ModalScreen[None]):
    """Read-only, syntax-highlighted preview of a project file (stays local; nothing is sent)."""
    BINDINGS = [Binding("escape,q", "app.pop_screen", "Close"), Binding("m", "mention", "Mention")]
    LIMIT = 200_000

    def __init__(self, path: Path, label: str, mentionable: bool):
        super().__init__(classes="modal")
        self.path = path
        self.label = label
        self.mentionable = mentionable

    def compose(self):
        palette = self.app.palette
        try:
            with self.path.open(encoding="utf-8", errors="replace") as stream:
                content = stream.read(self.LIMIT + 1)
            truncated = len(content) > self.LIMIT
            content = clean(content[:self.LIMIT])
            body = Syntax(content, LANGUAGES.get(self.path.suffix.lower(), "text"), line_numbers=True,
                          theme="monokai" if not palette.get("light") else "default", word_wrap=False,
                          background_color="default")
            size = f"{self.path.stat().st_size:,} bytes" + (" · showing the first 200 KB" if truncated else "")
        except OSError as error:
            body = Text(f"Could not open this file: {error.strerror or error}", Style(color=palette["error"]))
            size = ""
        title = Text()
        title.append(" ▣ " if self.app.glyphs.orb != "@" else " ", Style(color=palette["primary"]))
        title.append(self.label, Style(bold=True))
        title.append(f"   {size}", Style(color=palette["muted"]))
        hint = Text("esc close", Style(color=palette["muted"]))
        if self.mentionable:
            hint.append("  ·  m mention in message", Style(color=palette["muted"]))
        with Vertical(classes="dialog preview"):
            yield Static(title, classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                yield Static(body)
            yield Static(hint)

    def action_mention(self):
        if self.mentionable:
            self.app.insert_mention(self.label)
            self.app.pop_screen()


HELP = """\
## Keyboard

| Key | Action |
|---|---|
| `enter` | Send the message |
| **Send** / **Stop** | Send the prompt / cancel the turn and keep your next draft |
| `shift+enter` · `ctrl+j` | New line in the composer |
| `up` / `down` | Prompt history (first/last line); down past newest restores your draft |
| `/` · `@` | Command and file autocomplete — `tab` or `enter` to accept |
| `ctrl+p` | Command palette |
| `ctrl+b` · `f2` | Toggle the sidebar |
| `ctrl+o` | Expand or collapse all tool output |
| `pageup` / `pagedown` · `ctrl+end` | Scroll the conversation · jump to the latest |
| Drag text · `ctrl+c` | Copy selected text |
| **Copy reply** · `f4` | Copy the latest reply |
| **Copy code** | Copy a code block with indentation and line breaks |
| `esc` · `ctrl+c` with no selection | Cancel the running turn |
| `ctrl+t` | Next theme |
| `ctrl+l` | Clear the view (history stays saved) |
| `ctrl+n` | New session |
| `f5` | Projects: open, register, rename, unregister |
| `f6` · `f7` | Project home · notes, ideas, decisions and tasks |
| `f8` | Agent teams: choose a team or single-agent mode, edit who runs which model |
| `f9` · **Save…** | Save the latest reply as an idea, note, decision or task (you edit it first) |
| `ctrl+q` · `/exit` | Quit |

After failure or cancellation, **Restore prompt** returns the prompt to an empty composer for editing. \
It does not send automatically or overwrite another draft. Completed file changes and commands are not undone.

## Projects

A project is a registered folder. Opening one (`f5`) makes the folder the workspace and keeps its \
conversations, task lists and generated artifacts apart from other projects. Its knowledge — overview, \
notes, ideas, decisions and tasks — is plain Markdown in `.agent-project/` inside the folder, so you can \
edit it in any editor and commit it.

| Where | Keys |
|---|---|
| Projects | `enter` open · `a` register a folder · `r` rename · `m` folder moved · `d` unregister (deletes nothing) |
| Project home | `c` chat · `b` brainstorm · `n` note · `t` task · `r` continue · `k` notes · `j` tasks · `d` decisions · `e` overview |
| Notes | `enter` open · `e` edit · `n` new · `r` rename · `c` use in this chat · `x` idea → decision/task · `i` implement task · `o` record outcome · `s` status · `l` file path · `/` search · `tab` filter |
| Editor | `ctrl+s` save · `esc` cancel — a file changed outside the app is never overwritten without asking |

Each request includes the project overview when relevant, the notes you selected (`c`, `/context add`) \
and up to three notes whose words match your message. A **context** line above each reply lists them; \
`/context` explains why. `/implement <task>` starts a conversation with the task, its acceptance criteria \
and linked decisions; `/outcome` records what changed, how it was verified and what is unresolved. \
Tasks are completed only when you set the status.

## Agent teams

In a project, `f8` (or `/team NAME`) switches messages from one agent to a team: a coordinator that plans \
and synthesizes, one implementer that may change files, and read-only reviewers or researchers. Each agent \
names a saved profile and model, so a team can use one model or mix providers. Before each run a preflight \
lists every agent's provider/model and any problem. The run panel in the conversation shows each agent's \
role, model, state, action, time and token usage (`~` marks estimates); press enter on a row to see its \
output. Approvals name the agent, project and target. `esc` or **Stop** cancels the run: no new work \
starts, running commands stop, and completed changes stay. `/team off` returns to one agent. In the \
notes browser, `g` on a task starts a team implementation run.

| Teams screen | `enter` use team · `s` single agent · `tab` agents · `e` edit agent · `n` new team · `l` limits · \
`w` parallel writers · `c` check models · `r` run now · `d` delete |
|---|---|

## Commands

{commands}

## Motion and color

`/motion off` stops all animation; `/motion reduced` keeps only status spinners at a low frame rate. \
`AGENT_UI_MOTION`, `TEXTUAL_ANIMATIONS=none` and `NO_COLOR` are honored. `AGENT_UI_GLYPHS=ascii` swaps symbols \
for plain ASCII on fonts without them. `/theme contrast` is a high-contrast palette.
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,f1", "app.pop_screen", "Close")]

    def __init__(self, commands_markdown):
        super().__init__(classes="modal")
        self.commands_markdown = commands_markdown

    def compose(self):
        from textual.widgets import Markdown
        with Vertical(classes="dialog help"):
            yield Static(Text(" Help", Style(color=self.app.palette["primary"], bold=True)), classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                yield Markdown(HELP.format(commands=self.commands_markdown))
            yield Static(Text("esc close", Style(color=self.app.palette["muted"])))
