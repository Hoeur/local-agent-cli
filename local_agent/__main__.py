import argparse
import getpass
import os
import re
import sqlite3
import sys
from pathlib import Path

from .agent import MAX_TOOL_ROUNDS, Agent
from . import providers
from .config import DEFAULT_CONTEXT_CHARS, load_config, parse_context_chars, read_settings, save_config, set_active
from .file_commands import artifact_list, artifact_text
from .state import State


def printable(text):
    """Keep model-written text from sending terminal control sequences."""
    return "".join(char if char.isprintable() else "?" for char in str(text))


def confirm_changes(changes):
    if not sys.stdin.isatty():
        print("Memory and skill changes were not saved. Add --yes to save them without asking.", file=sys.stderr)
        return False
    print("The agent wants to make these changes:")
    for (kind, name), value in changes.items():
        if value is None:
            print(f"  - delete {kind} {printable(name)}")
        elif kind == "memory":
            print(f"  + memory {printable(name)}: {printable(value)}")
        else:
            print(f"  + skill {printable(name)}: {printable(value['description'])}")
            for line in str(value["steps"]).splitlines():
                print(f"      {printable(line)}")
    try:
        return input("Save them? [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def confirm_action(action):
    """Ask before a file change or command. Without a terminal there is nobody to ask, so it's declined."""
    if not sys.stdin.isatty():
        print(f"Declined without asking (no terminal): {printable(action['summary'])}. "
              "Add --yes to allow actions that aren't flagged as dangerous.", file=sys.stderr)
        return False
    print(f"The agent wants to {action['tool'].replace('_', ' ')}: {printable(action['summary'])}")
    if action["danger"]:
        print(f"  ! Dangerous: this command {action['danger']}.")
    for line in str(action["detail"]).splitlines()[:60]:
        print(f"    {printable(line)}")
    try:
        return input("Allow it? [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def auto_confirm(ask=confirm_action):
    """--yes: allow actions without asking, except commands flagged as dangerous, which still ask."""
    return lambda action: True if not action["danger"] else ask(action)


def check_name(name, kind):
    if not name.strip() or len(name) > 100:
        raise ValueError(f"{kind} names must contain between 1 and 100 characters.")
    return name


def urlsplit_host(url):
    from urllib.parse import urlsplit

    return urlsplit(url).netloc or url


def doctor_hint(config, status):
    """Next steps for a local Ollama setup that isn't ready yet."""
    if config.get("provider") != "ollama":
        return ""
    model = printable(config["model"])
    if status == "missing":
        return f"\nDownload the model with: ollama pull {model}"
    return f"\nStart Ollama (open the Ollama app, or run: ollama serve), then run: ollama pull {model}"


def setup_profile(args):
    provider = providers.preset_name(args.provider) if args.provider else None
    if args.provider and provider is None:
        raise ValueError(f"Unknown provider {args.provider!r}. Choose from: {', '.join(providers.PRESETS)}.")
    if provider is None:
        provider = providers.guess_provider(args.base_url) if args.base_url else "ollama"
    preset = providers.PRESETS[provider]
    base_url = (args.base_url or preset.base_url or "").strip()
    if not base_url:
        raise ValueError(f"{preset.label} needs --base-url, the endpoint your seller or server gave you.")
    model = (args.model or preset.model or "").strip()
    if not model:
        raise ValueError(f"Choose a model with --model for {preset.label}.")
    context_chars = parse_context_chars(args.context_chars or preset.context_chars)
    name = providers.check_profile_name(args.name or provider)
    profile = {"profile": name, "provider": provider, "base_url": base_url, "model": model, "wire": args.wire,
               "max_tokens": args.max_tokens}
    providers.make_provider(profile, require_key=False)  # Validates the URL and format.
    if args.image_base_url:
        from .provider import ImageProvider

        ImageProvider(args.image_base_url.strip(), args.image_model or "unused", None)
    save_config(args.data_dir, base_url, model, context_chars, provider=provider, wire=args.wire, name=name,
                max_tokens=args.max_tokens, image_model=args.image_model, image_base_url=args.image_base_url)
    print(f"Saved profile {name!r} ({preset.label}, model {model}) and made it active.")
    key, source = providers.resolve_key(profile)
    if preset.key_env and not key and provider not in {"custom"}:
        print(f"Next, add your API key:  python -m local_agent key set {name}")
        print(f"(or set the {preset.key_env} environment variable). Then run doctor to check the connection.")
    else:
        print("Run doctor to check the connection." + (f" Using the key from {source}." if key else ""))
    return 0


def key_command(args):
    if args.key_action == "list":
        names = set(providers.load_keys()) | set(read_settings(args.data_dir)["profiles"])
        names |= {name for name, preset in providers.PRESETS.items() if preset.key_env}
        print(f"Key store: {providers.key_path()}")
        for name in sorted(names):
            settings = read_settings(args.data_dir)["profiles"].get(name, {})
            key, source = providers.resolve_key({"profile": name, "provider": settings.get("provider", name)})
            print(f"  {name:<16} {providers.mask(key) + ' from ' + source if key else '-'}")
        return 0
    name = providers.preset_name(args.name) or args.name.strip().lower()
    providers.check_profile_name(name)
    if args.key_action == "remove":
        if not providers.remove_key(name):
            raise ValueError(f"No saved key for {name!r}.")
        print(f"Removed the saved key for {name}.")
        return 0
    if sys.stdin.isatty() and args.show:
        value = input(f"Paste the API key for {name}: ")
    elif sys.stdin.isatty():
        tip = " — right-click to paste in Command Prompt" if sys.platform == "win32" else ""
        value = getpass.getpass(f"Paste the API key for {name} (hidden{tip}), then press Enter: ")
    else:
        value = sys.stdin.readline()
    providers.set_key(name, value)
    print(f"Saved the key for {name} (ending {providers.mask(providers.check_key(value))[1:]}) in {providers.key_path()}, outside your project. Next: python -m local_agent doctor")
    return 0


def list_providers(data_dir):
    settings = read_settings(data_dir)
    print("Providers (python -m local_agent setup --provider <name>):")
    for name, preset in providers.PRESETS.items():
        key, _ = providers.resolve_key({"profile": name, "provider": name})
        status = "no key needed" if not preset.key_env else ("key set" if key else "no key")
        print(f"  {name:<15} {preset.label:<36} {status:<14} {preset.note}")
    if settings["profiles"]:
        print("\nSaved profiles (python -m local_agent use <name>):")
        for name, profile in settings["profiles"].items():
            marker = "*" if name == settings.get("active") else " "
            print(f"{marker} {name:<15} {profile.get('provider', '?'):<15} {printable(profile.get('model', '?'))}")
    return 0


def use_profile(data_dir, name, model=None):
    settings = read_settings(data_dir)
    if name not in settings["profiles"] and providers.preset_name(name) in settings["profiles"]:
        name = providers.preset_name(name)  # "use claude" finds the saved "anthropic" profile.
    if name not in settings["profiles"]:
        provider = providers.preset_name(name)
        preset = providers.PRESETS.get(provider)
        if not preset or not preset.base_url or not (model or preset.model):
            raise ValueError(f"No saved profile {name!r}. Create it with setup --provider "
                             f"{provider or name}" + ("" if preset else " (see the providers command)") + ".")
        name = save_config(data_dir, preset.base_url, model or preset.model, preset.context_chars,
                           provider=provider, name=provider)
    else:
        set_active(data_dir, name, model)
    config = load_config(data_dir)
    print(f"Now using {name} with model {printable(config['model'])}.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m local_agent", description="A local tool-using agent.")
    parser.add_argument("--data-dir", type=Path, default=Path(".agent"), help="SQLite state directory")
    parser.add_argument("--profile", help="Use this saved profile instead of the active one")
    commands = parser.add_subparsers(dest="command", required=True)
    names = ", ".join(providers.PRESETS)
    setup = commands.add_parser("setup", help="Save a model profile (provider, model, endpoint) and make it active",
                                description=f"Providers: {names}. Aliases: claude, codex, qwen.")
    setup.add_argument("--provider", help="ollama (default), anthropic, openai, deepseek, alibaba, openrouter, custom...")
    setup.add_argument("--model", help="Model name; cloud providers have a default")
    setup.add_argument("--base-url", help="API base URL; needed for custom, optional otherwise")
    setup.add_argument("--image-model", help="Optional image generation model (empty disables it)")
    setup.add_argument("--image-base-url", help="Optional base URL for image generation (defaults to chat URL)")
    setup.add_argument("--wire", choices=sorted(providers.WIRES),
                       help="API format for custom endpoints: chat (OpenAI-style, default), anthropic or responses")
    setup.add_argument("--name", help="Profile name (default: the provider name)")
    setup.add_argument(
        "--context-chars", type=int,
        help=f"Characters sent to the model per request (default {DEFAULT_CONTEXT_CHARS:,} for Ollama, "
             "100,000-150,000 for cloud providers)",
    )
    setup.add_argument("--max-tokens", type=int, help="Longest reply in tokens (Claude defaults to 8,192)")
    commands.add_parser("doctor", help="Check the configured endpoint and model availability")
    commands.add_parser("providers", help="List providers, saved profiles and which have API keys")
    use = commands.add_parser("use", help="Switch the active profile")
    use.add_argument("name")
    use.add_argument("--model", help="Also change that profile's model")
    commands.add_parser("models", help="List the models the active profile's endpoint offers")
    key = commands.add_parser("key", help="Save or remove an API key (kept outside the project folder)")
    key_actions = key.add_subparsers(dest="key_action", required=True)
    key_set = key_actions.add_parser("set", help="Save a key; you'll be asked to paste it")
    key_set.add_argument("name", help="Profile or provider name, e.g. deepseek")
    key_set.add_argument("--show", action="store_true", help="Show the key while you paste it")
    key_remove = key_actions.add_parser("remove", help="Delete a saved key")
    key_remove.add_argument("name")
    key_actions.add_parser("list", help="Show which keys are set (never the keys themselves)")
    commands.add_parser("sessions", help="List saved sessions")
    history = commands.add_parser("history", help="Show a saved conversation")
    history.add_argument("session")
    chat = commands.add_parser("chat", help="Start or resume a named chat")
    chat.add_argument("--session", default="default")
    chat.add_argument("--attach", action="append", default=[], type=Path, help="Attach an image or UTF-8 file")
    chat.add_argument("--workspace", type=Path, help="Allow the model to read files in this directory")
    chat.add_argument("--write", action="store_true", help="Allow file changes in the workspace (each one approved)")
    chat.add_argument("--shell", action="store_true", help="Allow shell commands in the workspace (each one approved)")
    chat.add_argument("--web", action="store_true", help="Allow web search and reading web pages")
    chat.add_argument("--all", action="store_true", help="Shorthand for --learn --write --shell --web")
    chat.add_argument("--learn", action="store_true", help="Enable memory and skill changes")
    chat.add_argument("--yes", action="store_true",
                      help="Save memory and skill changes and allow actions without asking "
                           "(dangerous commands still ask)")
    chat.add_argument("--max-rounds", type=int, default=MAX_TOOL_ROUNDS,
                      help=f"Tool rounds per turn, 0 to 50 (default {MAX_TOOL_ROUNDS}); 0 disables tools")
    chat.add_argument("--plain", action="store_true", help="Use the line-based interface instead of the full-screen UI")
    chat.add_argument("--no-stream", action="store_true", help="Request whole replies instead of streaming them")
    chat.add_argument("prompt", nargs="?", help="Send one message, or omit to chat interactively")
    commands.add_parser("artifacts", help="List generated artifacts")
    artifact = commands.add_parser("artifact", help="Read or preview a generated artifact")
    artifact.add_argument("id")
    artifact.add_argument("--open", action="store_true", help="Open an isolated browser preview")
    commands.add_parser("memory", help="Show saved memories")
    remember = commands.add_parser("remember", help="Save a memory yourself")
    remember.add_argument("name")
    remember.add_argument("value")
    forget = commands.add_parser("forget", help="Delete a saved memory")
    forget.add_argument("name")
    commands.add_parser("skills", help="List saved workflows")
    skill = commands.add_parser("skill", help="Show one saved workflow")
    skill.add_argument("name")
    skill.add_argument("--delete", action="store_true", help="Delete it instead of showing it")
    search = commands.add_parser("search", help="Search past conversations")
    search.add_argument("query")
    todo = commands.add_parser("todo", help="Show a session's task list")
    todo.add_argument("session", nargs="?", default="default")
    delete = commands.add_parser("delete-session", help="Delete a saved conversation and its task list")
    delete.add_argument("session")
    args = parser.parse_args(argv)

    try:
        if args.command == "setup":
            return setup_profile(args)
        if args.command == "key":
            return key_command(args)
        if args.command == "providers":
            return list_providers(args.data_dir)
        if args.command == "use":
            return use_profile(args.data_dir, args.name, args.model)
        if args.command in {"chat", "doctor", "models"}:
            config = load_config(args.data_dir, args.profile)
            provider = providers.make_provider(config)
            if args.command == "models":
                for model in sorted(provider.models()):
                    print(("* " if model == config["model"] else "  ") + printable(model))
                return 0
            if args.command == "doctor":
                status, detail = provider.check()
                if status != "online":
                    raise ValueError(detail + doctor_hint(config, status))
                print(f"Connected to {config['profile']} ({urlsplit_host(config['base_url'])}). {detail}")
                return 0
        if args.command != "chat":
            state = State(args.data_dir)
            if args.command == "sessions":
                for session, count in state.sessions():
                    print(f"{session}  {count} turns")
            elif args.command in {"artifacts", "artifact"}:
                if args.command == "artifacts":
                    output = artifact_list(state.artifacts)
                elif args.open:
                    output = f"Opened artifact preview: {state.artifacts.preview(args.id, open_browser=True)}"
                else:
                    output = artifact_text(state.artifacts, args.id)
                print("\n".join(printable(line) for line in output.splitlines()))
            elif args.command == "history":
                found = False
                for message in state.transcript(args.session):
                    found = True
                    if message["role"] in {"user", "assistant"} and message.get("content"):
                        print(f"{message['role']}> {message['content']}")
                if not found:
                    raise ValueError(f"Unknown session {args.session!r}.")
            elif args.command == "memory":
                for name, value in state.library("memory").items():
                    print(f"{name}  {value}")
            elif args.command == "remember":
                if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.name):
                    raise ValueError("Use a lowercase name with letters, numbers, hyphens or underscores.")
                if not args.value.strip() or len(args.value) > 300:
                    raise ValueError("Memories must contain between 1 and 300 characters.")
                memory = state.library("memory")
                if args.name not in memory and len(memory) >= 20:
                    raise ValueError("The memory store is full (20 entries). Forget one first.")
                state.update_library({("memory", args.name): args.value})
                print(f"Saved memory {args.name}.")
            elif args.command == "forget":
                if args.name not in state.library("memory"):
                    raise ValueError(f"Unknown memory {args.name!r}.")
                state.update_library({("memory", args.name): None})
                print(f"Deleted memory {args.name}.")
            elif args.command == "search":
                results = state.search(args.query)
                for result in results:
                    print(printable(f"{result['session']} #{result['turn']} {result['role']}> {result['excerpt']}"))
                if not results:
                    print("No matches.")
            elif args.command == "todo":
                for item in state.todo(args.session):
                    print(printable(item))
            elif args.command == "delete-session":
                if not state.delete_session(check_name(args.session, "Session")):
                    raise ValueError(f"Unknown session {args.session!r}.")
                print(f"Deleted session {args.session}.")
            else:
                skills = state.library("skill")
                if args.command == "skills":
                    for name, entry in skills.items():
                        print(f"{name}  {entry['description']}")
                elif args.name not in skills:
                    raise ValueError(f"Unknown skill {args.name!r}.")
                elif args.delete:
                    state.update_library({("skill", args.name): None})
                    print(f"Deleted skill {args.name}.")
                else:
                    print(skills[args.name]["steps"])
            return 0

        check_name(args.session, "Session")
        if args.all:
            args.learn = args.write = args.shell = args.web = True
        if not 0 <= args.max_rounds <= 50:
            raise ValueError("Choose between 0 and 50 tool rounds.")
        if (args.write or args.shell) and args.workspace is None:
            raise ValueError("--write and --shell need --workspace, the directory they may change.")
        approve = (lambda changes: True) if args.yes else confirm_changes
        confirm = auto_confirm() if args.yes else confirm_action
        options = {"write": args.write, "shell": args.shell, "web": args.web, "max_rounds": args.max_rounds}
        state = State(args.data_dir)
        agent = Agent(provider, state, args.workspace, config["context_chars"], approve,
                      confirm=confirm, image_provider=providers.make_image_provider(config), **options)
        if args.prompt is not None:
            provider.streaming = False
            print(agent.turn(args.session, args.prompt, args.learn, attachments=args.attach))
            return 0

        from . import plain, tui

        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if interactive and not args.plain and os.environ.get("AGENT_UI", "").lower() != "plain" and tui.available():
            provider.streaming = not args.no_stream
            return tui.run(provider, state, args.data_dir, config, args.session, args.workspace,
                           args.learn, args.yes, attachments=args.attach, **options)
        provider.streaming = False  # The line-based chat prints whole replies.
        return plain.chat_loop(agent, args.session, args.learn, config["model"], printable, agent.approve,
                               (OSError, RuntimeError, ValueError, sqlite3.Error),
                               tui_hint=interactive and not args.plain and not tui.available(), attachments=args.attach)
    except EOFError:
        return 0
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
