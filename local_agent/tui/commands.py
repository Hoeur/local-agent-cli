"""Slash commands and the command-palette provider. Both call the same app methods."""
from dataclasses import dataclass
from functools import partial

from textual.command import DiscoveryHit, Hit, Provider


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    help: str
    palette: bool = True  # Listed in the ctrl+p palette (commands that need an argument are not).


COMMANDS = [
    Command("help", "/help", "Keyboard shortcuts and commands"),
    Command("new", "/new [name]", "Start a new session"),
    Command("session", "/session <name>", "Switch to a session (created on first message)", False),
    Command("sessions", "/sessions", "List saved sessions"),
    Command("history", "/history", "Reload this session's full transcript"),
    Command("clear", "/clear", "Clear the view (history stays saved)"),
    Command("memory", "/memory", "Show saved memories"),
    Command("skills", "/skills", "List saved skills"),
    Command("skill", "/skill <name>", "Show one saved skill", False),
    Command("forget", "/forget <name>", "Delete a saved memory", False),
    Command("todo", "/todo", "Show this session's task list"),
    Command("search", "/search <words>", "Search past conversations", False),
    Command("learn", "/learn [on|off]", "Allow the agent to propose memory and skill changes"),
    Command("write", "/write [on|off]", "Allow file changes in the workspace (you approve each)"),
    Command("shell", "/shell [on|off]", "Allow shell commands in the workspace (you approve each)"),
    Command("web", "/web [on|off]", "Allow web search and reading pages"),
    Command("theme", "/theme [name]", "Switch color theme"),
    Command("motion", "/motion [full|reduced|off]", "Animation level"),
    Command("sidebar", "/sidebar [on|off|width]", "Toggle or resize the sidebar"),
    Command("hologram", "/hologram [on|off|auto|braille|ascii|image|small|medium|large|width|still|live|reset|status]",
            "Show, hide, resize or switch the holographic core"),
    Command("expand", "/expand", "Expand or collapse all tool output"),
    Command("doctor", "/doctor", "Check the model endpoint"),
    Command("provider", "/provider [name]", "List model profiles, or switch to one (Claude, DeepSeek, Alibaba...)"),
    Command("model", "/model [name]", "List this endpoint's models, or switch model"),
    Command("copy", "/copy", "Copy the last reply to the clipboard"),
    Command("exit", "/exit", "Quit"),
]
BY_NAME = {command.name: command for command in COMMANDS}
ALIASES = {"quit": "exit", "q": "exit", "h": "help", "?": "help", "cls": "clear"}


def parse(text):
    """Return (command, argument) for '/name arg', or None when text is not a known command."""
    if not text.startswith("/"):
        return None
    name, _, argument = text[1:].partition(" ")
    name = ALIASES.get(name.lower(), name.lower())
    if name not in BY_NAME:
        return name, None
    return name, argument.strip()


def markdown_table():
    rows = ["| Command | Description |", "|---|---|"]
    rows += [f"| `{command.usage}` | {command.help} |" for command in COMMANDS]
    return "\n".join(rows)


class AgentCommands(Provider):
    """Palette entries: every command, plus session switching and themes."""

    def entries(self):
        app = self.app
        for command in COMMANDS:
            if command.palette:
                yield f"{command.usage.split()[0]}  {command.help}", partial(app.run_slash, f"/{command.name}"), \
                    command.help
        for name, count in app.state.sessions():
            if name != app.session:
                yield f"Switch session: {name}", partial(app.run_slash, f"/session {name}"), f"{count} turns"
        try:
            from ..config import read_settings

            profiles = read_settings(app.data_dir)["profiles"]
        except (OSError, ValueError):
            profiles = {}
        for name, profile in profiles.items():
            if name != app.config.get("profile"):
                yield f"Provider: {name}", partial(app.run_slash, f"/provider {name}"), \
                    str(profile.get("model", ""))
        for theme in app.palettes:
            yield f"Theme: {app.palettes[theme]['label']}", partial(app.run_slash, f"/theme {theme}"), theme
        for level in ("full", "reduced", "off"):
            yield f"Motion: {level}", partial(app.run_slash, f"/motion {level}"), "animation level"
        yield "Sidebar: wider", partial(app.resize_sidebar, 4), "resize panel"
        yield "Sidebar: narrower", partial(app.resize_sidebar, -4), "resize panel"
        yield "Hologram: show or hide", partial(app.run_slash, "/hologram"), "f3"
        for word, help_text in (("auto", "best renderer for this terminal"), ("braille", "enhanced 3D, 2x4 dots per cell"),
                                ("ascii", "universal fallback"), ("image", "Sixel / Kitty graphics, after a restart"),
                                ("small", "22 columns"), ("medium", "30 columns"), ("large", "40 columns"),
                                ("still", "hold the hologram still"), ("live", "animate the hologram"),
                                ("reset", "reset rotation and zoom"), ("status", "renderer, fps and capabilities")):
            yield f"Hologram: {word}", partial(app.run_slash, f"/hologram {word}"), help_text

    async def discover(self):
        for title, callback, help_text in self.entries():
            yield DiscoveryHit(title, callback, help=help_text)

    async def search(self, query):
        matcher = self.matcher(query)
        for title, callback, help_text in self.entries():
            score = matcher.match(title)
            if score > 0:
                yield Hit(score, matcher.highlight(title), callback, help=help_text)
