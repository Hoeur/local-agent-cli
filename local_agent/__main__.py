import argparse
import os
import sqlite3
import sys
from pathlib import Path

from .agent import Agent
from .config import load_config, save_config
from .provider import ChatProvider
from .state import State


def main():
    parser = argparse.ArgumentParser(prog="python -m local_agent", description="A local tool-using agent.")
    parser.add_argument("--data-dir", type=Path, default=Path(".agent"), help="SQLite state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Save your model endpoint and model name")
    setup.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    setup.add_argument("--model", required=True)
    commands.add_parser("doctor", help="Check the configured endpoint and model availability")
    commands.add_parser("sessions", help="List saved sessions")
    history = commands.add_parser("history", help="Show a saved conversation")
    history.add_argument("session")
    chat = commands.add_parser("chat", help="Start or resume a named chat")
    chat.add_argument("--session", default="default")
    chat.add_argument("--workspace", type=Path, help="Allow the model to read files in this directory")
    chat.add_argument("--learn", action="store_true", help="Enable memory and skill changes")
    chat.add_argument("--plain", action="store_true", help="Use the line-by-line interactive prompt")
    chat.add_argument("--hologram", choices=["auto", "ascii", "unicode", "enhanced", "image", "off"], default="auto")
    chat.add_argument("--no-animation", action="store_true", help="Keep the AI core static")
    chat.add_argument("prompt", nargs="?", help="Send one message, or omit to chat interactively")
    commands.add_parser("memory", help="Show saved memories")
    commands.add_parser("skills", help="List saved workflows")
    skill = commands.add_parser("skill", help="Show one saved workflow")
    skill.add_argument("name")
    args = parser.parse_args()

    try:
        if args.command == "setup":
            if not args.model.strip():
                raise ValueError("Choose a model name.")
            ChatProvider(args.base_url.strip(), args.model.strip(), os.environ.get("AGENT_API_KEY"))
            save_config(args.data_dir, args.base_url.strip(), args.model.strip())
            print(f"Saved settings to {args.data_dir / 'config.json'}. Run doctor to check the connection.")
            return 0
        if args.command in {"chat", "doctor"}:
            config = load_config(args.data_dir)
            provider = ChatProvider(config["base_url"], config["model"], os.environ.get("AGENT_API_KEY"))
            if args.command == "doctor":
                models = provider.models()
                if config["model"] not in models:
                    raise ValueError(f"Model {config['model']!r} is unavailable. Available models: {', '.join(models) or '(none)' }.")
                print(f"Connected. Model {config['model']!r} is available.")
                return 0
        if args.command != "chat":
            state = State(args.data_dir)
            if args.command == "sessions":
                for session, count in state.sessions():
                    print(f"{session}  {count} turns")
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
            else:
                skills = state.library("skill")
                if args.command == "skills":
                    for name, entry in skills.items():
                        print(f"{name}  {entry['description']}")
                elif args.name in skills:
                    print(skills[args.name]["steps"])
                else:
                    raise ValueError(f"Unknown skill {args.name!r}.")
            return 0

        if not args.session.strip() or len(args.session) > 100:
            raise ValueError("Session names must contain between 1 and 100 characters.")
        agent = Agent(provider, State(args.data_dir), args.workspace)
        if args.prompt is not None:
            print(agent.turn(args.session, args.prompt, args.learn))
            return 0

        if not args.plain and sys.stdin.isatty() and sys.stdout.isatty() and os.getenv("TERM") != "dumb":
            try:
                from .hologram import Capabilities
                from .hologram_widget import prepare_image_protocol
                capabilities = Capabilities.detect()
                image_factory = prepare_image_protocol(capabilities)
                from .tui import ChatApp
            except ModuleNotFoundError as error:
                if error.name != "textual":
                    raise
            else:
                return ChatApp(agent, args.session, args.learn, hologram=args.hologram,
                               animated=not args.no_animation, capabilities=capabilities,
                               image_factory=image_factory).run()

        print(f"Session {args.session!r}. Enter /exit to leave.")
        while True:
            prompt = input("You> ").strip()
            if prompt == "/exit":
                return 0
            if prompt.split()[:1] == ["/hologram"]:
                print("Holograms require the interactive TUI. Install .[tui] and omit --plain.")
                continue
            if prompt:
                try:
                    print(f"Agent> {agent.turn(args.session, prompt, args.learn)}")
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                    print(f"Error: {error}", file=sys.stderr)
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
