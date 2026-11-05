import argparse
import os
import sqlite3
import sys
from pathlib import Path

from .agent import Agent
from .attachments import message_text
from .config import load_config, save_config
from .file_commands import FILE_HELP, FileCommands, artifact_list, artifact_text
from .provider import ChatProvider, ImageProvider
from .state import State


def main():
    parser = argparse.ArgumentParser(prog="python -m local_agent", description="A local tool-using agent.")
    parser.add_argument("--data-dir", type=Path, default=Path(".agent"), help="SQLite state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Save your model endpoint and model name")
    setup.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    setup.add_argument("--model", required=True)
    setup.add_argument("--image-model", default="", help="Enable an optional image generation model")
    setup.add_argument("--image-base-url", default="", help="Image API base URL (defaults to the chat base URL)")
    commands.add_parser("doctor", help="Check the configured endpoint and model availability")
    commands.add_parser("sessions", help="List saved sessions")
    history = commands.add_parser("history", help="Show a saved conversation")
    history.add_argument("session")
    chat = commands.add_parser("chat", help="Start or resume a named chat")
    chat.add_argument("--session", default="default")
    chat.add_argument("--workspace", type=Path, help="Allow the model to read files in this directory")
    chat.add_argument("--attach", action="append", type=Path, default=[], help="Attach an image or text file (up to four)")
    chat.add_argument("--learn", action="store_true", help="Enable memory and skill changes")
    chat.add_argument("--plain", action="store_true", help="Use the line-by-line interactive prompt")
    chat.add_argument("--hologram", choices=["auto", "ascii", "unicode", "enhanced", "image", "off"], default="auto")
    chat.add_argument("--no-animation", action="store_true", help="Keep the AI core static")
    chat.add_argument("prompt", nargs="?", help="Send one message, or omit to chat interactively")
    commands.add_parser("memory", help="Show saved memories")
    commands.add_parser("skills", help="List saved workflows")
    skill = commands.add_parser("skill", help="Show one saved workflow")
    skill.add_argument("name")
    commands.add_parser("artifacts", help="List generated files")
    artifact = commands.add_parser("artifact", help="Inspect a generated file or open its preview")
    artifact.add_argument("id")
    artifact.add_argument("--open", action="store_true", help="Open an isolated preview in the browser")
    args = parser.parse_args()

    try:
        if args.command == "setup":
            if not args.model.strip():
                raise ValueError("Choose a model name.")
            ChatProvider(args.base_url.strip(), args.model.strip(), os.environ.get("AGENT_API_KEY"))
            if args.image_base_url and not args.image_model:
                raise ValueError("Choose --image-model when setting --image-base-url.")
            if args.image_model and not args.image_model.strip():
                raise ValueError("Choose an image model name.")
            if args.image_model:
                ImageProvider((args.image_base_url or args.base_url).strip(), args.image_model.strip(),
                              os.environ.get("AGENT_IMAGE_API_KEY"))
            save_config(args.data_dir, args.base_url.strip(), args.model.strip(),
                        image_model=args.image_model.strip(), image_base_url=args.image_base_url.strip())
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
            if args.command == "artifacts":
                print(artifact_list(state.artifacts))
            elif args.command == "artifact":
                if args.open:
                    print(state.artifacts.preview(args.id, open_browser=True))
                else:
                    print(artifact_text(state.artifacts, args.id))
            elif args.command == "sessions":
                for session, count in state.sessions():
                    print(f"{session}  {count} turns")
            elif args.command == "history":
                found = False
                for message in state.transcript(args.session):
                    found = True
                    if message["role"] in {"user", "assistant"} and message.get("content"):
                        print(f"{message['role']}> {message_text(message)}")
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
        image_provider = None
        if config["image_model"]:
            image_url = config["image_base_url"] or config["base_url"]
            image_key = os.environ.get("AGENT_IMAGE_API_KEY")
            if image_key is None and image_url.rstrip("/") == config["base_url"].rstrip("/"):
                image_key = os.environ.get("AGENT_API_KEY")
            image_provider = ImageProvider(image_url, config["image_model"], image_key)
        agent = Agent(provider, State(args.data_dir), args.workspace, image_provider=image_provider)
        if args.prompt is not None:
            print(agent.turn(args.session, args.prompt, args.learn, attachments=args.attach))
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
                               image_factory=image_factory, attachments=args.attach).run()

        print(f"Session {args.session!r}. Enter /exit to leave.")
        files = FileCommands(agent.state, args.attach)
        while True:
            prompt = input("You> ").strip()
            if prompt == "/exit":
                return 0
            if prompt.split()[:1] == ["/hologram"]:
                print("Holograms require the interactive TUI. Install .[tui] and omit --plain.")
                continue
            if prompt:
                try:
                    if prompt == "/help":
                        print(FILE_HELP + "\n/sessions lists chats; /exit closes chat.")
                        continue
                    if prompt == "/sessions":
                        print("\n".join(f"{name} ({count} turns)" for name, count in agent.state.sessions())
                              or "No saved sessions yet.")
                        continue
                    output = files.handle(prompt)
                    if output is not None:
                        print(output)
                        continue
                    print(f"Agent> {agent.turn(args.session, prompt, args.learn, attachments=tuple(files.pending))}")
                    files.pending.clear()
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
