"""`python -m local_agent team ...`: configure, check, run and inspect agent teams without the
full-screen interface. Standard library only."""
import os
import sys
import threading

from . import project_commands
from .orchestrator import RunStore, TeamRun, preflight, result_text
from .projects import ProjectRegistry, handoff_prompt
from .config import read_settings
from .state import State
from .teams import LIMITS, TOOL_GROUPS, TeamStore, add_writer, assign, describe, preset, validate


def printable(text):
    return "\n".join("".join(char if char.isprintable() else "?" for char in line) for line in str(text).splitlines())


def ask_approval(request, stop):
    """One question at a time on the terminal, naming the agent and what it wants to do."""
    if not sys.stdin.isatty():
        print(f"Declined without asking (no terminal): {request['agent']} wants to {request['summary']}",
              file=sys.stderr)
        return False
    print(f"\n[{request['project']}] {request['agent_name']} ({request['role']}, {request['model']}) wants to "
          f"{request['tool'].replace('_', ' ')}: {printable(request['target'])}")
    if request.get("danger"):
        print(f"  ! Dangerous: this command {request['danger']}.")
    for line in str(request["detail"]).splitlines()[:60]:
        print(f"    {printable(line)}")
    print("Allow it? [y/N] ", end="", flush=True)
    answer = read_approval(stop)
    return answer.strip().lower() in {"y", "yes"} and not stop.is_set()


def read_approval(stop):
    """Read a terminal line while allowing cancellation to release the worker."""
    if os.name == "nt":
        import msvcrt
        answer = ""
        while not stop.wait(0.05):
            if not msvcrt.kbhit():
                continue
            char = msvcrt.getwch()
            if char in {"\r", "\n"}:
                print()
                return answer
            if char in {"\x00", "\xe0"}:  # Extended key plus scan code
                msvcrt.getwch()
            elif char == "\b" and answer:
                answer = answer[:-1]
                print("\b \b", end="", flush=True)
            elif char.isprintable():
                answer += char
                print(char, end="", flush=True)
    else:
        import select
        while not stop.is_set():
            readable, _, _ = select.select([sys.stdin], [], [], 0.1)
            if readable:
                return sys.stdin.readline()
    return ""


def parse_fallback(value):
    if value is None:
        return ...
    if value.lower() in {"", "none", "off"}:
        return None
    profile, _, model = value.partition(":")
    return {"profile": profile.strip(), "model": model.strip() or None}


def team_command(args):
    registry = ProjectRegistry(args.data_dir)
    project = project_commands.resolve_project(registry, args.project).require_folder()
    store = TeamStore(args.data_dir, project.id)
    action = args.team_action
    if action == "list":
        for name, team in store.teams().items():
            errors = [problem for problem in validate(team, args.data_dir) if problem.level == "error"]
            models = {f"{agent.get('profile')}/{agent.get('model') or '(profile model)'}" for agent in team["agents"]}
            mode = "same model" if len(models) == 1 else "mixed models"
            print(f"{name:<14} {team['workflow']:<11} {len(team['agents'])} agents  {mode:<13}"
                  + ("  built-in" if team.get("builtin") else "") + (f"  {len(errors)} problem(s)" if errors else "")
                  + ("  parallel writers" if team.get("parallel") else ""))
        return 0
    if action == "show":
        team = store.get(args.name)
        print(printable(describe(team, settings=read_settings(args.data_dir))))
        for agent in team["agents"]:
            print(f"\n{agent['id']} instructions:\n  {printable(agent['instructions'])}")
        problems = validate(team, args.data_dir)
        print("\n" + ("\n".join(str(problem) for problem in problems) or "No problems found."))
        return 0
    if action == "init":
        team = preset(args.workflow, args.profile or store.default_profile(), args.model)
        store.save_team(args.name, team)
        print(f"Saved team {args.name!r} ({args.workflow}) for {project.name}; every agent uses "
              f"{team['agents'][0]['profile']}/{args.model or '(profile model)'}. Change one with: "
              f"python -m local_agent team assign {args.name} AGENT --profile NAME --model MODEL")
        return 0
    if action == "add-writer":
        team = store.get(args.name)
        add_writer(team, args.agent, args.profile, args.model)
        store.save_team(args.name, team)
        print(printable(describe(team, settings=read_settings(args.data_dir))))
        return 0
    if action == "assign":
        team = store.get(args.name)
        tools = None
        if args.tools is not None:
            tools = [item.strip() for item in args.tools.split(",") if item.strip()]
        assign(team, args.agent, profile=args.profile, model=... if args.model is None else args.model,
               tools=tools, instructions=args.instructions, name=args.display_name,
               context_chars=... if args.context_chars is None else (args.context_chars or None),
               max_tokens=... if args.max_tokens is None else (args.max_tokens or None),
               fallback=parse_fallback(args.fallback))
        store.save_team(args.name, team)
        print(printable(describe(team, settings=read_settings(args.data_dir))))
        report(validate(team, args.data_dir))
        return 0
    if action == "limit":
        team = store.get(args.name)
        if args.key not in LIMITS:
            raise ValueError(f"Unknown limit. Choose: {', '.join(LIMITS)}.")
        team.setdefault("limits", {})[args.key] = args.value
        store.save_team(args.name, team)
        report(validate(team, args.data_dir))
        print(f"{args.name}: {args.key} = {args.value}")
        return 0
    if action == "parallel":
        team = store.get(args.name)
        team["parallel"] = args.state == "on"
        store.save_team(args.name, team)
        report(validate(team, args.data_dir))
        print(f"{args.name}: parallel writers {args.state}.")
        return 0
    if action == "delete":
        store.delete(args.name)
        print(f"Deleted team {args.name!r}." + (" The built-in preset is used again." if args.name in
                                                ("brainstorm", "implement", "review") else ""))
        return 0
    if action == "check":
        ok, text, _ = preflight(store.get(args.name), args.data_dir, project, online=args.online)
        print(printable(text))
        print("Ready to run." if ok else "Not ready: fix the errors above.")
        return 0 if ok else 1
    if action == "runs":
        runs = RunStore(args.data_dir, project.id).runs(args.limit)
        for record in runs:
            print(f"{record['id']}  {record['state']:<16} {record['team'].get('name', '?'):<12} "
                  f"{printable(record['prompt'])[:60]}")
        if not runs:
            print("No team runs yet.")
        return 0
    if action == "result":
        record = RunStore(args.data_dir, project.id).load(args.run)
        if args.agent:
            agent = record["agents"].get(args.agent)
            if agent is None:
                raise ValueError(f"No agent {args.agent!r} in this run.")
            state = State(args.data_dir).for_project(f"{project.id}@team")
            for message in state.transcript(agent["session"]):
                if message.get("role") in {"user", "assistant"} and message.get("content"):
                    print(f"{message['role']}> {printable(message['content'])}")
                for item in message.get("tool_calls") or []:
                    print(f"tool> {item['function']['name']} {printable(item['function']['arguments'])[:300]}")
            return 0
        print(printable(result_text(record)))
        return 0
    if action == "apply":
        from .worktrees import apply
        record = RunStore(args.data_dir, project.id).load(args.run)
        print(apply(project.path, record))
        return 0
    if action == "run":
        team = store.get(args.name)
        prompt = args.prompt
        if args.task and not prompt:
            prompt = handoff_prompt(project.knowledge.find(args.task, ["task"]))
        if not prompt:
            raise ValueError("Describe the task, or pass --task.")
        ok, text, _ = preflight(team, args.data_dir, project, online=args.check)
        print(printable(text), file=sys.stderr)
        if not ok:
            print("Not started: fix the errors above.", file=sys.stderr)
            return 1
        return run_in_terminal(project, State(args.data_dir), args, team, prompt)
    raise ValueError(f"Unknown team action {action!r}.")


def report(problems):
    for problem in problems:
        print(str(problem), file=sys.stderr)


def run_in_terminal(project, state, args, team, prompt):
    shown = {}

    def on_event(kind, data):
        if kind == "note":
            print(f"· {printable(data['text'])}", file=sys.stderr)
        elif kind == "update":
            for agent in data["agents"]:
                key = (agent["state"], agent["action"].split(":")[0])
                if shown.get(agent["id"]) != key and agent["state"] not in {"pending"}:
                    shown[agent["id"]] = key
                    print(f"  {agent['id']:<14} {agent['model']:<30} {agent['state']:<16} {printable(agent['action'])}",
                          file=sys.stderr)

    run = TeamRun(project, state, args.data_dir, team, prompt, task=args.task, session=args.session,
                  approve=ask_approval, auto_approve=args.yes, on_event=on_event)
    print(f"Team run {run.id} started. Ctrl+C cancels it.", file=sys.stderr)
    finished = threading.Event()

    def execute():
        try:
            run.run()
        finally:
            finished.set()

    threading.Thread(target=execute, name="team-run").start()
    while not finished.is_set():
        try:
            finished.wait(0.2)
        except KeyboardInterrupt:
            run.cancel()
            print("\nCancelling the run. Saving the final record.", file=sys.stderr)
    record = run.record()
    print(printable(run.markdown()))
    print(f"\nFull record: python -m local_agent team result {run.id}", file=sys.stderr)
    return 0 if record["state"] == "completed" else 1


def add_parser(commands, which):
    team = commands.add_parser("team", help="Configure, check and run agent teams in a project")
    actions = team.add_subparsers(dest="team_action", required=True)
    subs = [actions.add_parser("list", help="Teams of the project (built-in presets included)")]
    show = actions.add_parser("show", help="A team's agents, models, tools, limits and problems")
    show.add_argument("name")
    init = actions.add_parser("init", help="Save a team from a preset; every agent uses one profile/model")
    init.add_argument("name")
    init.add_argument("--workflow", required=True, choices=("brainstorm", "implement", "review"))
    init.add_argument("--profile", help="Saved profile for every agent (default: the active one)")
    init.add_argument("--model", help="Model for every agent (default: the profile's)")
    writer = actions.add_parser("add-writer", help="Add an implementer to a parallel team")
    writer.add_argument("name")
    writer.add_argument("agent")
    writer.add_argument("--profile", help="Saved provider profile (default: the existing implementer's)")
    writer.add_argument("--model", help="Model override (default: inherit when using the same profile)")
    assign_parser = actions.add_parser("assign", help="Set one agent's profile, model, tools or instructions")
    assign_parser.add_argument("name")
    assign_parser.add_argument("agent")
    assign_parser.add_argument("--profile", help="A saved profile (see: providers)")
    assign_parser.add_argument("--model", help="Model for this agent; empty string uses the profile's")
    assign_parser.add_argument("--tools", help="Comma-separated tool groups: " + ", ".join(TOOL_GROUPS))
    assign_parser.add_argument("--fallback", help="PROFILE[:MODEL] used only if this agent's provider fails; "
                                                  "'none' removes it")
    assign_parser.add_argument("--instructions", help="The agent's instructions")
    assign_parser.add_argument("--name", dest="display_name", help="Display name")
    assign_parser.add_argument("--context-chars", type=int, help="Context budget (0: the profile's)")
    assign_parser.add_argument("--max-tokens", type=int, help="Longest reply in tokens (0: the profile's)")
    limit = actions.add_parser("limit", help="Set a run limit: " + ", ".join(LIMITS))
    limit.add_argument("name")
    limit.add_argument("key")
    limit.add_argument("value", type=int)
    parallel = actions.add_parser("parallel", help="Parallel writers in Git worktrees (implement teams)")
    parallel.add_argument("name")
    parallel.add_argument("state", choices=("on", "off"))
    delete = actions.add_parser("delete", help="Delete a saved team")
    delete.add_argument("name")
    check = actions.add_parser("check", help="Validate a team; --online also asks each model")
    check.add_argument("name")
    check.add_argument("--online", action="store_true")
    run = actions.add_parser("run", help="Run a team on a request or a project task")
    run.add_argument("name")
    run.add_argument("prompt", nargs="?")
    run.add_argument("--task", help="Project task to implement (id, title or part of either)")
    run.add_argument("--session", default="default", help="Conversation that gets the result (default: default)")
    run.add_argument("--yes", action="store_true", help="Allow file changes and commands without asking "
                                                        "(dangerous commands still ask)")
    run.add_argument("--check", action="store_true", help="Ask each model before starting")
    runs = actions.add_parser("runs", help="Recent runs of the project")
    runs.add_argument("--limit", type=int, default=20)
    result = actions.add_parser("result", help="A run's record; --agent prints that agent's transcript")
    result.add_argument("run")
    result.add_argument("--agent")
    apply_parser = actions.add_parser("apply", help="Merge a parallel run's integration branch (clean tree only)")
    apply_parser.add_argument("run")
    for sub in (*subs, show, init, writer, assign_parser, limit, parallel, delete, check, run, runs, result, apply_parser):
        sub.add_argument("--project", help=which)
