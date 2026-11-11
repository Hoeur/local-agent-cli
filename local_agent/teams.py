"""Agent teams: roles, tool permissions, presets and validated, project-scoped configurations.

A team is a small, fixed group of agents that run one project task together (see orchestrator.py).
Each agent has a role, instructions, a saved provider profile and model, the tool groups it may use
and its own context and output limits. Configurations are saved per project in
``<data-dir>/projects/<project-id>/teams.json`` and never contain API keys: agents name a profile,
and keys are looked up in the existing key store or environment when a run starts.

Standard library only.
"""
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import providers
from .config import MAX_CONTEXT_CHARS, MIN_CONTEXT_CHARS, load_config, read_settings
from .projects import ProjectError, atomic_write
from .tools import is_reserved_name

TEAMS_FILE = "teams.json"
AGENT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")
TEAM_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
WORKFLOWS = ("brainstorm", "implement", "review")


@dataclass(frozen=True)
class Role:
    label: str
    summary: str
    may_write: bool
    tools: tuple


ROLES = {
    "coordinator": Role("Coordinator", "plans the work, assigns bounded subtasks, tracks progress and synthesizes "
                        "the results", False, ("read",)),
    "implementer": Role("Implementer", "makes approved changes in the project workspace", True,
                        ("read", "write", "shell", "todo")),
    "reviewer": Role("Reviewer", "inspects changes, identifies problems and evaluates the acceptance criteria",
                     False, ("read",)),
    "researcher": Role("Researcher", "investigates the project and proposes or critiques approaches", False,
                       ("read",)),
}
# Tool groups an agent may be given. Only implementers may get "write" or "shell": a shell can
# change files, so a read-only agent never gets one.
TOOL_GROUPS = {
    "read": ("list_files", "read_file", "search_files"),
    "write": ("write_file", "patch"),
    "shell": ("terminal",),
    "web": ("web_search", "web_extract"),
    "todo": ("todo",),
}
WRITE_GROUPS = {"write", "shell"}

LIMITS = {
    # name: (default, low, high, meaning)
    "concurrency": (2, 1, 8, "agents working at the same time"),
    "local_concurrency": (1, 1, 8, "agents at once on local model servers (Ollama, localhost)"),
    "max_subtasks": (6, 1, 12, "subtasks in a plan"),
    "delegation_depth": (1, 1, 1, "levels of delegation; agents can't create agents"),
    "agent_rounds": (8, 1, 30, "tool rounds per agent per subtask"),
    "revision_rounds": (1, 0, 3, "implement-review cycles after the first review"),
    "request_timeout": (180, 10, 1800, "seconds to wait for one model reply"),
    "run_minutes": (30, 1, 480, "minutes for the whole run"),
    "max_requests": (80, 1, 1000, "model requests in the whole run"),
    "max_tokens": (0, 0, 50_000_000, "reported tokens in the whole run (0: no limit)"),
}

INSTRUCTIONS = {
    "coordinator": "Break the task into a few bounded subtasks with clear expected outputs, give each to the "
                   "right team member, and keep dependencies explicit. When synthesizing, report only what the "
                   "records show: verification means commands and checks that actually ran, not an agent saying "
                   "it is done. Name disagreements instead of smoothing them over.",
    "implementer": "Make the smallest correct change for your assignment. Read files before editing them. Run the "
                   "project's tests or another relevant check when you can, and report the exact commands and "
                   "results. Say plainly what you did not finish.",
    "reviewer": "Review independently and critically. Check the changes and evidence against each acceptance "
                "criterion; cite files and lines. Approve only what the evidence supports.",
    "researcher": "Investigate on your own and propose a concrete approach with tradeoffs and risks. When asked to "
                  "critique, look for flaws and missing cases rather than agreeing.",
}


class TeamError(ProjectError):
    """A team configuration problem; ordinary chat is unaffected."""


def member(identifier, role, profile, *, name=None, tools=None, model=None, instructions=None):
    return {"id": identifier, "name": name or ROLES[role].label, "role": role, "profile": profile, "model": model,
            "instructions": instructions or INSTRUCTIONS[role], "tools": list(tools or ROLES[role].tools),
            "context_chars": None, "max_tokens": None, "fallback": None}


def preset(workflow, profile, model=None):
    """A built-in team where every agent uses one profile (and model): a same-model team."""
    if workflow == "brainstorm":
        agents = [member("coordinator", "coordinator", profile, model=model),
                  member("proposer-a", "researcher", profile, name="Proposer A", model=model),
                  member("proposer-b", "researcher", profile, name="Proposer B", model=model)]
        title = "Brainstorm: independent proposals and critique, then synthesis"
    elif workflow == "implement":
        agents = [member("coordinator", "coordinator", profile, model=model),
                  member("implementer", "implementer", profile, model=model),
                  member("reviewer", "reviewer", profile, model=model)]
        title = "Implement: plan, implementation, review and verification"
    elif workflow == "review":
        agents = [member("coordinator", "coordinator", profile, model=model),
                  member("reviewer-a", "reviewer", profile, name="Reviewer A", model=model),
                  member("reviewer-b", "reviewer", profile, name="Reviewer B", model=model)]
        title = "Review: independent read-only inspections, then consolidated findings"
    else:
        raise TeamError(f"Choose a workflow: {', '.join(WORKFLOWS)}.")
    return {"name": workflow, "title": title, "workflow": workflow, "parallel": False, "agents": agents,
            "limits": {key: value[0] for key, value in LIMITS.items()}}


class TeamStore:
    """Team presets of one project. Missing presets come from the built-in ones."""

    def __init__(self, data_dir: Path, project_id: str):
        self.data_dir = Path(data_dir)
        self.project_id = project_id
        self.file = self.data_dir / "projects" / project_id / TEAMS_FILE

    def default_profile(self):
        settings = read_settings(self.data_dir)
        return settings.get("active") or next(iter(settings["profiles"]), None) or "ollama"

    def load(self):
        if not self.file.exists():
            return {}
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise TeamError(f"Could not read {self.file}: {error}. Fix or move that file; chat still works.") from None
        if not isinstance(data, dict) or not isinstance(data.get("teams"), dict):
            raise TeamError(f"{self.file} is not in the expected format. Fix or move it; chat still works.")
        return {name: team for name, team in data["teams"].items() if isinstance(team, dict)}

    def teams(self):
        """Saved teams, plus the built-in brainstorm, implement and review presets not overridden."""
        saved = self.load()
        profile = None
        result = {}
        for workflow in WORKFLOWS:
            if workflow not in saved:
                profile = profile or self.default_profile()
                result[workflow] = {**preset(workflow, profile), "builtin": True}
        result.update(saved)
        return dict(sorted(result.items(), key=lambda item: (item[0] not in WORKFLOWS, item[0])))

    def get(self, name):
        teams = self.teams()
        if name not in teams:
            raise TeamError(f"No team named {name!r}. Teams: {', '.join(teams)}.")
        return json.loads(json.dumps(teams[name]))

    def save_team(self, name, team):
        if not TEAM_NAME.fullmatch(name):
            raise TeamError("Team names use lowercase letters, numbers and hyphens (up to 32).")
        check_shape(team)
        saved = self.load()
        team = {key: value for key, value in team.items() if key != "builtin"}
        team["name"] = name
        saved[name] = team
        atomic_write(self.file, (json.dumps({"version": 1, "teams": saved}, indent=2, ensure_ascii=False)
                                 + "\n").encode("utf-8"))
        return team

    def delete(self, name):
        saved = self.load()
        if name not in saved:
            raise TeamError(f"{name!r} is not a saved team." + (" Built-in presets can't be deleted; "
                                                                  "edit them instead." if name in WORKFLOWS else ""))
        del saved[name]
        atomic_write(self.file, (json.dumps({"version": 1, "teams": saved}, indent=2, ensure_ascii=False)
                                 + "\n").encode("utf-8"))


def check_shape(team):
    if not isinstance(team.get("agents"), list) or not all(isinstance(agent, dict) for agent in team["agents"]):
        raise TeamError("A team needs a list of agents.")
    if not isinstance(team.get("limits", {}), dict):
        raise TeamError("Team limits must be an object.")


def add_writer(team, identifier, profile=None, model=None):
    if team.get("workflow") != "implement" or not team.get("parallel"):
        raise TeamError("Enable parallel writers on an implementation team with 'team parallel NAME on' first.")
    if not AGENT_ID.fullmatch(identifier) or identifier == "integration" or is_reserved_name(identifier):
        raise TeamError("Choose a non-reserved writer ID of up to 24 lowercase letters, numbers or hyphens.")
    if any(agent["id"] == identifier for agent in team["agents"]):
        raise TeamError(f"Agent {identifier!r} already exists.")
    existing = next((agent for agent in team["agents"] if agent["role"] == "implementer"), None)
    if existing is None:
        raise TeamError("The implementation team needs a primary implementer.")
    profile = profile or existing["profile"]
    if model is None and profile == existing["profile"]:
        model = existing.get("model")
    team["agents"].append(member(identifier, "implementer", profile, name=identifier, model=model))


def assign(team, agent_id, *, profile=None, model=..., tools=None, instructions=None, name=None,
           context_chars=..., max_tokens=..., fallback=...):
    """Change one agent of a team in place. Pass model=None to use the profile's model."""
    agent = next((item for item in team["agents"] if item.get("id") == agent_id), None)
    if agent is None:
        raise TeamError(f"No agent {agent_id!r} in this team. Agents: "
                        + ", ".join(item.get("id", "?") for item in team["agents"]) + ".")
    if profile is not None:
        agent["profile"] = profile
    if model is not ...:
        agent["model"] = model or None
    if tools is not None:
        unknown = [tool for tool in tools if tool not in TOOL_GROUPS]
        if unknown:
            raise TeamError(f"Unknown tool groups {', '.join(unknown)}; choose {', '.join(TOOL_GROUPS)}.")
        if WRITE_GROUPS & set(tools) and not ROLES.get(agent.get("role"), ROLES["reviewer"]).may_write:
            raise TeamError(f"{agent_id} is a {agent.get('role')}, a read-only role: it can't have write or shell "
                            "tools. Only implementers may change files.")
        agent["tools"] = list(dict.fromkeys(tools))
    if instructions is not None:
        agent["instructions"] = instructions
    if name is not None:
        agent["name"] = name
    if context_chars is not ...:
        agent["context_chars"] = context_chars
    if max_tokens is not ...:
        agent["max_tokens"] = max_tokens
    if fallback is not ...:
        agent["fallback"] = fallback
    return agent


# ── Validation and resolution ──────────────────────────────────────────────────────────

@dataclass
class Problem:
    level: str      # "error" stops a run; "warning" is shown in the preflight
    agent: str      # agent id, or "" for the team
    message: str

    def __str__(self):
        return f"{self.level}: " + (f"{self.agent}: " if self.agent else "") + self.message


def limits_of(team):
    raw = team.get("limits") if isinstance(team.get("limits"), dict) else {}
    result = {}
    for key, (default, _, _, _) in LIMITS.items():
        result[key] = raw.get(key, default)
    return result


def validate(team, data_dir, *, check_keys=True):
    """Every problem that would stop this team from running, as Problem records. Nothing here
    contacts a model; see orchestrator.preflight for the online check."""
    problems = []

    def error(message, agent=""):
        problems.append(Problem("error", agent, message))

    def warn(message, agent=""):
        problems.append(Problem("warning", agent, message))

    try:
        settings = read_settings(Path(data_dir))
    except (OSError, ValueError) as caught:
        settings = {"profiles": {}}
        error(f"Could not read the saved profiles: {caught}")
    profiles = settings.get("profiles", {})
    workflow = team.get("workflow")
    if workflow not in WORKFLOWS:
        error(f"Unknown workflow {workflow!r}; choose {', '.join(WORKFLOWS)}.")
    agents = team.get("agents") if isinstance(team.get("agents"), list) else []
    if not agents:
        error("The team has no agents.")
    seen, roles, writers = set(), {}, []
    for agent in agents:
        identifier = str(agent.get("id", ""))
        if not AGENT_ID.fullmatch(identifier):
            error(f"Agent id {identifier!r} must be lowercase letters, numbers and hyphens (up to 24).")
            continue
        if identifier in seen:
            error(f"Agent id {identifier!r} is used twice.")
        seen.add(identifier)
        role = agent.get("role")
        if role not in ROLES:
            error(f"Unknown role {role!r}; choose {', '.join(ROLES)}.", identifier)
            continue
        roles.setdefault(role, []).append(identifier)
        tools = agent.get("tools") if isinstance(agent.get("tools"), list) else []
        unknown = [tool for tool in tools if tool not in TOOL_GROUPS]
        if unknown:
            error(f"Unknown tool groups {', '.join(unknown)}; choose {', '.join(TOOL_GROUPS)}.", identifier)
        if WRITE_GROUPS & set(tools):
            if not ROLES[role].may_write:
                error(f"The {role} role is read-only; it can't have write or shell tools.", identifier)
            else:
                writers.append(identifier)
        if not str(agent.get("instructions") or "").strip():
            warn("No instructions; the role's defaults are used.", identifier)
        for key, low, high in (("context_chars", MIN_CONTEXT_CHARS, MAX_CONTEXT_CHARS), ("max_tokens", 16, 1_000_000)):
            value = agent.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high):
                error(f"{key} must be a whole number from {low:,} to {high:,}, or unset.", identifier)
        check_model(agent, profiles, identifier, error, check_keys)
        fallback = agent.get("fallback")
        if fallback is not None:
            if not isinstance(fallback, dict):
                error("fallback must name a profile and optionally a model.", identifier)
            else:
                check_model(fallback, profiles, identifier, error, check_keys, "fallback ")
    if len(roles.get("coordinator", [])) != 1:
        error("A team needs exactly one coordinator.")
    if workflow == "implement":
        if not roles.get("implementer"):
            error("The implement workflow needs an implementer.")
        if not roles.get("reviewer"):
            error("The implement workflow needs a reviewer.")
        if not writers:
            error("No implementer may change files: give one the write tools.")
    elif workflow in {"brainstorm", "review"}:
        if writers:
            error(f"The {workflow} workflow is read-only; remove write and shell tools.")
        helpers = [identifier for role in ("researcher", "reviewer") for identifier in roles.get(role, [])]
        if not helpers:
            error(f"The {workflow} workflow needs at least one researcher or reviewer.")
        elif len(helpers) == 1:
            warn("Only one researcher or reviewer: there is nothing independent to compare.")
    parallel = bool(team.get("parallel"))
    if parallel and workflow != "implement":
        error("Parallel writers are only for the implement workflow.")
    if not parallel and len(writers) > 1:
        error(f"Only one agent may change files ({', '.join(writers)} can). Turn on parallel writers "
              "(Git projects only) or remove write and shell from all but one implementer.")
    if parallel and len(writers) < 2:
        warn("Parallel writers are on, but only one implementer can write.")
    for key, value in (team.get("limits") or {}).items():
        if key not in LIMITS:
            error(f"Unknown limit {key!r}.")
            continue
        _, low, high, meaning = LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            error(f"Limit {key} ({meaning}) must be from {low:,} to {high:,}.")
    return problems


def check_model(agent, profiles, identifier, error, check_keys, prefix=""):
    profile = agent.get("profile")
    if not profile:
        error(f"{prefix}profile is not set.", identifier)
        return
    if profile not in profiles:
        error(f"{prefix}profile {profile!r} is not saved. Saved: {', '.join(profiles) or 'none'}. Create it with "
              f"python -m local_agent setup --provider ... --name {profile}", identifier)
        return
    saved = profiles[profile]
    model = agent.get("model") or saved.get("model")
    if not isinstance(model, str) or not model.strip():
        error(f"{prefix}no model: set one for this agent or in profile {profile!r}.", identifier)
    if check_keys:
        provider = saved.get("provider") or providers.guess_provider(saved.get("base_url"))
        preset = providers.PRESETS.get(provider) or providers.PRESETS["custom"]
        key, _ = providers.resolve_key({"profile": profile, "provider": provider})
        if preset.key_env and provider != "custom" and not key:
            error(f"{prefix}no API key for profile {profile!r}. Run: python -m local_agent key set {profile}  "
                  f"(or set {preset.key_env}).", identifier)


@dataclass
class AgentSpec:
    """A validated agent with its provider settings resolved. It holds no key; the provider object
    built from it looks the key up when it is created."""
    id: str
    name: str
    role: str
    profile: str
    provider: str
    base_url: str
    model: str
    wire: str | None
    tools: tuple
    groups: tuple
    instructions: str
    context_chars: int
    max_tokens: int | None
    local: bool
    fallback: dict | None = None
    config: dict = field(default_factory=dict, repr=False)

    @property
    def writes(self):
        return bool(WRITE_GROUPS & set(self.groups))

    def label(self):
        return f"{self.profile}/{self.model}"


def is_local(base_url):
    host = (urlsplit(base_url or "").hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local")


def resolve_agent(agent, data_dir, override=None):
    """The AgentSpec for one agent; override is a fallback {"profile", "model"}."""
    source = override or agent
    config = load_config(Path(data_dir), source["profile"])
    model = (source.get("model") or "").strip() or config["model"]
    config = {**config, "model": model}
    if agent.get("max_tokens"):
        config["max_tokens"] = agent["max_tokens"]
    groups = tuple(dict.fromkeys(group for group in agent.get("tools", []) if group in TOOL_GROUPS))
    tools = tuple(name for group in groups for name in TOOL_GROUPS[group])
    return AgentSpec(agent["id"], agent.get("name") or agent["id"], agent["role"], source["profile"],
                     config["provider"], config["base_url"], model, config.get("wire"), tools, groups,
                     str(agent.get("instructions") or INSTRUCTIONS[agent["role"]]),
                     agent.get("context_chars") or config["context_chars"], config.get("max_tokens"),
                     config["provider"] == "ollama" or is_local(config["base_url"]), agent.get("fallback"), config)


def resolve(team, data_dir):
    """Validate a team and resolve its agents. Raises TeamError listing every error."""
    problems = validate(team, data_dir)
    errors = [str(problem) for problem in problems if problem.level == "error"]
    if errors:
        raise TeamError("The team can't run:\n- " + "\n- ".join(errors))
    return [resolve_agent(agent, data_dir) for agent in team["agents"]], problems


def build_provider(spec, timeout):
    """A new provider object for one agent: agents never share request state."""
    provider = providers.make_provider(spec.config)
    provider.timeout = timeout
    provider.streaming = True
    return provider


def describe(team, specs=None, settings=None):
    """Plain-text assignments of a team, for the CLI and the preflight."""
    lines = [f"{team.get('name')}: {team.get('title') or team.get('workflow')}"
             + ("  (parallel writers)" if team.get("parallel") else "")]
    by_id = {spec.id: spec for spec in specs or []}
    for agent in team.get("agents", []):
        spec = by_id.get(agent.get("id"))
        saved = ((settings or {}).get("profiles") or {}).get(agent.get("profile"), {})
        model = spec.label() if spec else \
            f"{agent.get('profile')}/{agent.get('model') or saved.get('model') or '(profile model)'}"
        tools = ",".join(agent.get("tools") or []) or "none"
        fallback = agent.get("fallback")
        extra = f"  fallback {fallback.get('profile')}/{fallback.get('model') or '(profile model)'}" if fallback else ""
        lines.append(f"  {agent.get('id', '?'):<14} {agent.get('role', '?'):<12} {model:<36} tools {tools}{extra}")
    limits = limits_of(team)
    lines.append("  limits " + ", ".join(f"{key} {value}" for key, value in limits.items()))
    return "\n".join(lines)


def mixed(specs):
    return len({(spec.profile, spec.model) for spec in specs}) > 1
