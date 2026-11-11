"""Team mode: choosing and editing team presets, the preflight summary, and the run panel.

The orchestration itself (orchestrator.py) knows nothing about Textual; the app starts runs and
feeds their snapshots to TeamRunPanel."""
from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Collapsible, OptionList, Static
from textual.widgets.option_list import Option

from ..config import read_settings
from ..orchestrator import preflight
from ..teams import LIMITS, ROLES, TOOL_GROUPS, TeamError, TeamStore, assign, preset, validate
from .project_screens import ChoiceScreen, ConfirmScreen, Narrow, PromptScreen, key_hints
from .widgets import clean

STATE_COLORS = {"pending": "muted", "running": "primary", "waiting_approval": "warning", "completed": "success",
                "idle": "success", "failed": "error", "cancelled": "warning", "interrupted": "warning",
                "skipped": "muted"}
TEAM_KEYS = [("enter", "use team"), ("s", "single agent"), ("tab", "agents"), ("e", "edit agent"), ("n", "new team"),
             ("l", "limits"), ("w", "parallel"), ("c", "check models"), ("r", "run now"), ("d", "delete"),
             ("esc", "back")]


def usage_text(usage):
    measured = usage.get("prompt", 0) + usage.get("completion", 0)
    parts = []
    if measured:
        parts.append(f"↑{usage['prompt']:,} ↓{usage['completion']:,}")
    if usage.get("unattributed"):
        parts.append(f"{usage['unattributed']:,} unsplit")
    if usage.get("estimated"):
        parts.append(f"~{usage['estimated']:,} est")
    return " ".join(parts) or "no usage yet"


class TeamScreen(Narrow, Screen):
    """Team presets of the open project: who runs which model, with which tools and limits."""
    BINDINGS = [Binding("escape", "back", "Back"), Binding("s", "single", "Single agent"),
                Binding("e", "edit", "Edit agent"), Binding("n", "new", "New team"), Binding("l", "limits", "Limits"),
                Binding("w", "parallel", "Parallel"), Binding("c", "check", "Check"), Binding("r", "run", "Run"),
                Binding("d", "delete", "Delete")]

    def __init__(self, select=None):
        super().__init__()
        self.select = select
        self.teams = {}
        self.generation = 0

    @property
    def store(self):
        return TeamStore(self.app.data_dir, self.app.project.id)

    def compose(self):
        yield Static("", id="team-title", classes="screen-title")
        with Horizontal(id="team-body"):
            with Vertical(id="team-lists"):
                yield OptionList(id="team-list", classes="screen-list")
                yield OptionList(id="team-agents", classes="screen-list")
            with VerticalScroll(id="team-detail"):
                yield Static("", id="team-detail-text")
        yield Static("", id="team-keys", classes="screen-keys")

    def on_mount(self):
        super().on_mount()
        self.refresh_teams()
        self.query_one("#team-list").focus()

    def refresh_teams(self, select=None):
        app, palette = self.app, self.app.palette
        title = Text(" Teams", Style(color=palette["primary"], bold=True))
        title.append(f" · {app.project.name}   mode: ", Style(color=palette["muted"]))
        title.append(f"team {app.team_mode}" if app.team_mode else "single agent", Style(color=palette["accent"]))
        self.query_one("#team-title", Static).update(title)
        try:
            self.teams = self.store.teams()
        except TeamError as error:
            self.teams = {}
            app.notify(clean(str(error)), severity="error", timeout=8)
        listing = self.query_one("#team-list", OptionList)
        listing.clear_options()
        options = []
        for name, team in self.teams.items():
            errors = [problem for problem in validate(team, app.data_dir) if problem.level == "error"]
            line = Text()
            line.append(f"{app.glyphs.dot_on if name == app.team_mode else app.glyphs.dot_off} ",
                        Style(color=palette["primary"]))
            line.append(name, Style(bold=True))
            line.append(f"  {team['workflow']} · {len(team['agents'])} agents", Style(color=palette["muted"]))
            if errors:
                line.append(f"  {len(errors)} problem{'s' * (len(errors) != 1)}", Style(color=palette["error"]))
            options.append(Option(line, id=f"team:{name}"))
        listing.add_options(options or [Option(Text("No teams", Style(italic=True)), disabled=True)])
        wanted = select or self.select or app.team_mode
        ids = [option.id for option in options]
        listing.highlighted = ids.index(f"team:{wanted}") if f"team:{wanted}" in ids else 0
        self.show_team()
        self.query_one("#team-keys", Static).update(key_hints(app, TEAM_KEYS))

    def current(self):
        listing = self.query_one("#team-list", OptionList)
        if listing.highlighted is None or not self.teams:
            return None, None
        name = (listing.get_option_at_index(listing.highlighted).id or "").removeprefix("team:")
        return name, self.teams.get(name)

    def current_agent(self):
        _, team = self.current()
        agents = self.query_one("#team-agents", OptionList)
        if team is None or agents.highlighted is None:
            return None
        identifier = (agents.get_option_at_index(agents.highlighted).id or "").removeprefix("agent:")
        return next((agent for agent in team["agents"] if agent["id"] == identifier), None)

    def show_team(self, extra=None):
        app, palette = self.app, self.app.palette
        name, team = self.current()
        agents = self.query_one("#team-agents", OptionList)
        keep = agents.highlighted or 0
        agents.clear_options()
        detail = Text()
        if team is None:
            self.query_one("#team-detail-text", Static).update("")
            return
        settings = read_settings(app.data_dir)
        for agent in team["agents"]:
            saved = settings["profiles"].get(agent.get("profile"), {})
            line = Text()
            line.append(f"{agent['id']:<14}", Style(bold=True))
            line.append(f"{agent.get('profile')}/{agent.get('model') or saved.get('model') or '?'}",
                        Style(color=palette["secondary"]))
            agents.add_option(Option(line, id=f"agent:{agent['id']}"))
        agents.highlighted = min(keep, len(team["agents"]) - 1)
        detail.append(f"{team.get('title') or team['workflow']}\n", Style(bold=True))
        if team.get("builtin"):
            detail.append("Built-in preset: editing it saves a copy for this project.\n", Style(color=palette["muted"]))
        if team.get("parallel"):
            detail.append("Parallel writers in Git worktrees.\n", Style(color=palette["accent"]))
        detail.append("\n")
        for agent in team["agents"]:
            saved = settings["profiles"].get(agent.get("profile"), {})
            detail.append(f"{agent['id']}", Style(bold=True))
            detail.append(f"  {agent['role']}\n", Style(color=palette["muted"]))
            detail.append(f"  {agent.get('profile')}/{agent.get('model') or saved.get('model') or '?'}",
                          Style(color=palette["secondary"]))
            detail.append(f" · tools {', '.join(agent.get('tools') or []) or 'none'}", Style(color=palette["muted"]))
            fallback = agent.get("fallback")
            if fallback:
                detail.append(f" · fallback {fallback.get('profile')}/{fallback.get('model') or '(profile model)'}",
                              Style(color=palette["warning"]))
            detail.append("\n")
        limits = team.get("limits", {})
        detail.append("\nlimits  " + " · ".join(f"{key} {limits.get(key, value[0])}" for key, value in LIMITS.items()
                                               if key != "delegation_depth") + "\n\n", Style(color=palette["muted"]))
        problems = validate(team, app.data_dir)
        for problem in problems:
            detail.append(f"{problem}\n", Style(color=palette["error" if problem.level == "error" else "warning"]))
        if not problems:
            detail.append("No problems found.\n", Style(color=palette["success"]))
        agent = self.current_agent()
        if agent is not None:
            detail.append(f"\n{agent['id']} · {ROLES[agent['role']].label}: {ROLES[agent['role']].summary}\n",
                          Style(color=palette["secondary"], bold=True))
            detail.append(clean(agent.get("instructions") or "") + "\n", Style(color=palette["muted"]))
        if extra:
            detail.append("\n" + clean(extra) + "\n", Style(color=palette["foreground"]))
        self.query_one("#team-detail-text", Static).update(detail)

    def on_option_list_option_highlighted(self, event):
        if event.option_list.id == "team-list":
            self.show_team()
        elif event.option_list.id == "team-agents":
            self.show_team()

    def on_option_list_option_selected(self, event):
        event.stop()
        if event.option_list.id == "team-list":
            name, team = self.current()
            if team is not None:
                self.app.set_team_mode(name)
                self.refresh_teams(name)
        else:
            self.action_edit()

    def save(self, name, team, message):
        try:
            self.store.save_team(name, team)
        except TeamError as error:
            self.app.notify(clean(str(error)), severity="error", timeout=6)
            return False
        self.app.notify(message, title="Teams", timeout=3)
        self.refresh_teams(name)
        return True

    def action_back(self):
        if self.focused is not None and self.focused.id == "team-agents":
            self.query_one("#team-list").focus()
            return
        self.app.pop_screen()

    def action_single(self):
        self.app.set_team_mode(None)
        self.refresh_teams()

    def action_edit(self):
        name, team = self.current()
        agent = self.current_agent()
        if team is None or agent is None:
            return
        profiles = ", ".join(read_settings(self.app.data_dir)["profiles"]) or "none — run setup first"
        fallback = agent.get("fallback") or {}

        def edited(values):
            if not values:
                return
            fallback_value = values["fallback"].strip()
            if fallback_value.lower() in {"", "none"}:
                new_fallback = None
            else:
                profile, _, model = fallback_value.partition(":")
                new_fallback = {"profile": profile.strip(), "model": model.strip() or None}
            try:
                assign(team, agent["id"], profile=values["profile"] or agent["profile"], model=values["model"] or None,
                       tools=[item.strip() for item in values["tools"].split(",") if item.strip()],
                       instructions=values["instructions"] or agent["instructions"], fallback=new_fallback)
            except TeamError as error:
                self.app.notify(clean(str(error)), severity="error", timeout=6)
                return
            self.save(name, team, f"Saved {agent['id']} in team {name}.")

        self.app.push_screen(PromptScreen(f"{name} · {agent['id']} ({agent['role']})", [
            ("profile", f"Profile (saved: {profiles})", agent.get("profile") or "", ""),
            ("model", "Model (empty: the profile's model)", agent.get("model") or "", ""),
            ("tools", f"Tool groups ({', '.join(TOOL_GROUPS)})", ",".join(agent.get("tools") or []), "read"),
            ("fallback", "Fallback PROFILE[:MODEL], used only if this agent's provider fails (empty: none)",
             f"{fallback.get('profile')}" + (f":{fallback['model']}" if fallback.get("model") else "") if fallback
             else "", ""),
            ("instructions", "Instructions", agent.get("instructions") or "", "")],
            "API keys stay in the key store; teams only name profiles. Read-only roles can't get write or shell."),
            edited)

    def action_new(self):
        def chosen(workflow):
            if not workflow:
                return

            def named(values):
                if not values or not values["name"]:
                    return
                team = preset(workflow, values["profile"] or self.store.default_profile(), values["model"] or None)
                self.save(values["name"].strip().lower(), team, f"Saved team {values['name']}.")
            self.app.push_screen(PromptScreen(f"New {workflow} team", [
                ("name", "Name", workflow if workflow not in self.teams or self.teams[workflow].get("builtin")
                 else f"{workflow}-2", ""),
                ("profile", "Profile for every agent (edit agents later for a mixed team)", self.store.default_profile(),
                 ""), ("model", "Model for every agent (empty: the profile's)", "", "")]), named)
        self.app.push_screen(ChoiceScreen("New team from a preset", "Every agent starts on the same model.", [
            ("b", "Brainstorm", "brainstorm"), ("i", "Implement", "implement"), ("r", "Review", "review")]), chosen)

    def action_limits(self):
        name, team = self.current()
        if team is None:
            return
        limits = team.get("limits", {})
        keys = [key for key in LIMITS if key != "delegation_depth"]

        def edited(values):
            if not values:
                return
            try:
                team.setdefault("limits", {}).update({key: int(values[key]) for key in keys})
            except ValueError:
                self.app.notify("Limits are whole numbers.", severity="error")
                return
            errors = [problem for problem in validate(team, self.app.data_dir) if problem.level == "error"
                      and "Limit" in problem.message]
            if errors:
                self.app.notify(clean(str(errors[0])), severity="error", timeout=6)
                return
            self.save(name, team, f"Saved the limits of {name}.")
        self.app.push_screen(PromptScreen(f"Limits · {name}", [
            (key, f"{key} — {LIMITS[key][3]} ({LIMITS[key][1]}–{LIMITS[key][2]:,})", str(limits.get(key, LIMITS[key][0])),
             "") for key in keys]), edited)

    def action_parallel(self):
        name, team = self.current()
        if team is None:
            return
        if team["workflow"] != "implement":
            self.app.notify("Parallel writers are only for implement teams.", timeout=3)
            return
        team["parallel"] = not team.get("parallel")
        if team["parallel"]:
            from ..worktrees import suitability
            reason = suitability(self.app.project.path)
            if reason:
                self.app.notify(f"Parallel writers are unavailable: {reason} One writer is still available.",
                                severity="warning", timeout=6)
                return
            self.app.notify(f"Add an implementer with: python -m local_agent team add-writer {name} writer-b "
                            f"--project {self.app.project.id}", timeout=8)
        self.save(name, team, f"Parallel writers {'on' if team['parallel'] else 'off'} for {name}.")

    def action_check(self):
        name, team = self.current()
        if team is None:
            return
        self.generation += 1
        generation, project = self.generation, self.app.project
        self.show_team("Asking each model…")

        def work():
            ok, text, _ = preflight(team, self.app.data_dir, project, online=True)
            self.app.call_from_thread(self.checked, generation, project.id, text, ok)
        self.run_worker(work, thread=True, exclusive=True, group="team-check")

    def checked(self, generation, project_id, text, ok):
        if generation != self.generation or self.app.project is None or self.app.project.id != project_id:
            return  # A newer check, or another project: this result is stale.
        self.show_team(("Ready to run.\n" if ok else "Not ready.\n") + text)

    def action_run(self):
        name, team = self.current()
        if team is None:
            return

        def asked(values):
            if values and values["prompt"]:
                self.app.pop_screen()
                self.app.confirm_team_run(name, values["prompt"])
        self.app.push_screen(PromptScreen(f"Run {name}", [("prompt", "What should the team do?", "", "")]), asked)

    def action_delete(self):
        name, team = self.current()
        if team is None:
            return
        if team.get("builtin"):
            self.app.notify("Built-in presets can't be deleted; edit them to save your own version.", timeout=4)
            return

        def decided(value):
            if value:
                try:
                    self.store.delete(name)
                except TeamError as error:
                    self.app.notify(clean(str(error)), severity="error")
                    return
                if self.app.team_mode == name:
                    self.app.set_team_mode(None)
                self.refresh_teams()
        self.app.push_screen(ConfirmScreen(f"Delete team {name}?", "Only this preset is deleted; runs and their "
                                           "records stay.", "Delete", "Keep", danger=True), decided)

    def action_scroll_body(self, lines):
        self.query_one("#team-detail").scroll_relative(y=lines, animate=False)


class TeamRunPanel(Vertical):
    """A run in the conversation: one row per agent (role, model, state, action, elapsed time,
    usage), each expandable to its latest output and tool activity. Raw agent output never goes
    into the main conversation; the combined result is added once the run ends."""
    DEFAULT_CSS = "TeamRunPanel { height: auto; margin: 0 0 1 2; }"

    def __init__(self, run_id, team, **kwargs):
        super().__init__(classes="team-run", **kwargs)
        self.run_id = run_id
        self.team = team
        self.rows = {}

    def compose(self):
        yield Static(Text(f"team run {self.run_id} · {self.team} · starting"), classes="team-run-header")
        yield Static("", classes="team-run-subtasks")

    async def update_snapshot(self, snapshot):
        app = self.app
        palette = app.palette
        header = Text()
        header.append(f"{app.glyphs.tool} team {snapshot['team']} ", Style(color=palette["secondary"], bold=True))
        state = snapshot["state"]
        header.append(state.replace("_", " "), Style(color=palette[STATE_COLORS.get(state, "muted")], bold=True))
        header.append(f"  {snapshot['elapsed']:.0f}s · {snapshot['usage']['requests']} requests · "
                      f"{usage_text(snapshot['usage'])}  run {self.run_id}", Style(color=palette["muted"]))
        self.query_one(".team-run-header", Static).update(header)
        marks = {"completed": "✓", "running": "◐", "waiting_approval": "?", "failed": "✗", "skipped": "–",
                 "cancelled": "■", "pending": "○", "interrupted": "!"}
        if app.glyphs.orb == "@":
            marks = {key: {"completed": "+", "running": ">", "failed": "x"}.get(key, ".") for key in marks}
        steps = Text("  ")
        for item in snapshot["subtasks"]:
            steps.append(f"{marks.get(item['state'], '?')} {item['id']}  ",
                         Style(color=palette[STATE_COLORS.get(item["state"], "muted")]))
        self.query_one(".team-run-subtasks", Static).update(steps)
        for agent in snapshot["agents"]:
            title = (f"{agent['name']} · {agent['role']} · {agent['model']} · {agent['state'].replace('_', ' ')}"
                     f" · {agent['elapsed']:.0f}s · {usage_text(agent['usage'])}"
                     + (f" · {agent['action']}" if agent["action"] else "")
                     + (f" · FALLBACK from {agent['fallback']['from']}" if agent.get("fallback") else ""))
            row = self.rows.get(agent["id"])
            body = Text()
            events = [event for event in snapshot["events"] if event["agent"] == agent["id"]][-6:]
            for event in events:
                body.append(f"{event['kind']}: {clean(event['detail'])}\n", Style(color=palette["muted"]))
            if agent["output"]:
                body.append(clean(agent["output"][-900:]))
            if not body.plain:
                body.append("No output yet.", Style(color=palette["muted"], italic=True))
            if row is None:
                content = Static(body, classes="team-agent-output")
                row = Collapsible(content, title=clean(title)[:160], collapsed=True,
                                  classes="tool-output team-agent")
                self.rows[agent["id"]] = row
                await self.mount(row)
            else:
                row.title = clean(title)[:160]
                row.query_one(".team-agent-output", Static).update(body)
