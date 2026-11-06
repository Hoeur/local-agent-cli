"""Release check: drive the installed terminal UI through a complete coding workflow.

Run it with the interpreter of a clean environment where only the wheel is installed:

    python -I scripts/workflow_check.py
    python -I scripts/workflow_check.py --ollama qwen3:0.6b

The default run serves a scripted model from a local OpenAI-compatible fixture, so every step is
deterministic: it keeps a session task list, reads a file, makes an edit you approve, asks for a
shell command you decline, copies the reply and a code block, then restarts the app and resumes the
session. --ollama adds a live turn against a local Ollama model and resumes that session too.
On Windows, --native-clipboard opts into replacing and checking the desktop clipboard.
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CODE = 'def status():\n    return "ready ✓ សួស្តី"\n'
REPLY = "Updated notes.txt and skipped the command.\n\n```python\n" + CODE + "```"
TASKS = "[>] Inspect notes.txt\n[ ] Mark the release ready\n[ ] Run the check script"
DONE_TASKS = "[x] Inspect notes.txt\n[x] Mark the release ready\n[ ] Run the check script"


def call(name, call_id, **arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


SCRIPT = [
    call("todo", "c1", items=TASKS),
    call("read_file", "c2", path="notes.txt"),
    call("patch", "c3", path="notes.txt", old_text="status = draft", new_text="status = ready"),
    call("terminal", "c4", command="python -c \"open('side_effect.txt', 'w').write('ran')\""),
    call("todo", "c5", items=DONE_TASKS),
    {"role": "assistant", "content": REPLY},
]


class Fixture(BaseHTTPRequestHandler):
    """A scripted OpenAI-compatible chat endpoint. It records what the agent sends back."""
    replies, requests = [], []

    def log_message(self, *args):
        pass

    def send(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send(200, {"data": [{"id": "fixture-model"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fixture.requests.append(payload)
        reply = Fixture.replies.pop(0) if Fixture.replies else {"role": "assistant", "content": "Done."}
        self.send(200, {"choices": [{"finish_reason": "stop", "message": reply}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def check(condition, message):
    if not condition:
        raise SystemExit(f"FAIL: {message}")
    print(f"ok   {message}")


async def wait_for(pilot, condition, seconds=20.0):
    for _ in range(int(seconds / 0.05)):
        await pilot.pause(0.05)
        if condition():
            return True
    return False


def make_app(data, workspace, base_url, model, session, native_clipboard=False):
    from local_agent import providers
    from local_agent.state import State
    from local_agent.tui.app import AgentApp

    class CheckedApp(AgentApp):
        CSS_PATH = str(Path(sys.modules[AgentApp.__module__].__file__).with_name("app.tcss"))

        def use_system_clipboard(self):
            return native_clipboard and os.name == "nt"

    config = {"provider": "ollama", "profile": "ollama", "base_url": base_url, "model": model,
              "context_chars": 24_000, "max_tokens": None, "image_model": "", "image_base_url": ""}
    provider = providers.make_provider(config)
    return CheckedApp(provider, State(data), data, config, session, workspace, write=True, shell=True,
                    max_rounds=8, prefs={"theme": "neon", "hologram": False})


async def send(pilot, app, text):
    from local_agent.tui.widgets import Composer

    app.query_one(Composer).load_text(text)
    await pilot.press("enter")


async def answer_dialogs(pilot, app, decisions, seconds=20.0):
    """Answer each approval dialog in order with y or n, then wait for the turn to end."""
    from local_agent.tui.screens import ActionScreen

    seen = []
    while app.busy or decisions:
        if not await wait_for(pilot, lambda: isinstance(app.screen, ActionScreen) or not app.busy, seconds):
            raise SystemExit("FAIL: the turn did not finish")
        if isinstance(app.screen, ActionScreen):
            seen.append(app.screen.action["tool"])
            await pilot.press(decisions.pop(0) if decisions else "n")
            await pilot.pause(0.1)
        elif not app.busy:
            break
    return seen


async def scripted_workflow(root, base_url, native_clipboard=False):
    from textual.widgets import Button

    from local_agent.tui import clipboard
    from local_agent.tui.widgets import AssistantMessage, CopyableCodeBlock, UserMessage

    data, workspace = root / "data", root / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("release notes\nstatus = draft\n", encoding="utf-8")
    Fixture.replies[:] = [dict(item) for item in SCRIPT]

    app = make_app(data, workspace, base_url, "fixture-model", "workflow", native_clipboard)
    async with app.run_test(size=(120, 40)) as pilot:
        await send(pilot, app, "Mark the release notes ready, then run the check script.")
        tools = await answer_dialogs(pilot, app, ["y", "n"])
        check(tools == ["patch", "terminal"], "the edit and the command each asked for approval")
        check(app.last_reply == REPLY, "the final reply arrived")
        await pilot.press("f4")
        check(app.clipboard == REPLY, "F4 copied the latest reply")
        if native_clipboard:
            check(clipboard.paste() == clipboard.windows_text(REPLY), "the Windows clipboard holds the reply")
        blocks = list(app.query(CopyableCodeBlock))
        check(len(blocks) == 1, "the reply shows one code block with a Copy code button")
        await pilot.click(blocks[0].query_one(Button))
        check(app.clipboard == CODE, "Copy code copied the block with its indentation and Unicode")
        if native_clipboard:
            check(clipboard.paste() == clipboard.windows_text(CODE), "the Windows clipboard holds the code")
        await pilot.press("ctrl+q")

    check((workspace / "notes.txt").read_text(encoding="utf-8") == "release notes\nstatus = ready\n",
          "the approved edit changed notes.txt")
    check(not (workspace / "side_effect.txt").exists(), "the declined command had no side effect")
    declined = [m for p in Fixture.requests for m in p["messages"]
                if m.get("role") == "tool" and m.get("tool_call_id") == "c4"]
    check(bool(declined) and "declin" in declined[-1]["content"].lower(), "the model was told the command was declined")

    app = make_app(data, workspace, base_url, "fixture-model", "workflow")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        check(len(app.query(UserMessage)) == 1 and len(app.query(AssistantMessage)) == 1,
              "the restarted app reloaded the saved transcript")
        check(app.last_reply == REPLY, "the resumed session can copy its last reply")
        check(app.state.todo("workflow") == DONE_TASKS.splitlines(), "the saved task list reloaded")
        await send(pilot, app, "/todo")
        await pilot.pause(0.3)
        await pilot.press("ctrl+q")


async def live_workflow(root, model):
    from local_agent.tui.widgets import AssistantMessage

    data, workspace = root / "live-data", root / "live-workspace"
    workspace.mkdir()
    base_url = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:11434/v1")
    app = make_app(data, workspace, base_url, model, "live")
    async with app.run_test(size=(120, 40)) as pilot:
        await send(pilot, app, "Reply with one short sentence that says hello.")
        await answer_dialogs(pilot, app, [], seconds=300)  # A local model may need to load first.
        check(bool(app.last_reply.strip()), f"Ollama ({model}) answered in the terminal UI")
        await pilot.press("ctrl+q")
    app = make_app(data, workspace, base_url, model, "live")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        check(len(app.query(AssistantMessage)) == 1, "the live session resumed after a restart")
        await pilot.press("ctrl+q")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ollama", metavar="MODEL", help="Also run a live turn against this Ollama model")
    parser.add_argument("--native-clipboard", action="store_true", help="Replace and verify the Windows clipboard")
    args = parser.parse_args()
    if args.native_clipboard and os.name != "nt":
        parser.error("--native-clipboard requires Windows")
    import local_agent

    print(f"python {sys.version.split()[0]} at {sys.executable}")
    print(f"local_agent from {Path(local_agent.__file__).parent}")
    os.environ.setdefault("AGENT_UI_MOTION", "off")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            asyncio.run(scripted_workflow(root, f"http://127.0.0.1:{server.server_port}/v1", args.native_clipboard))
            if args.ollama:
                asyncio.run(live_workflow(root, args.ollama))
    finally:
        server.shutdown()
    print("Workflow check passed.")


if __name__ == "__main__":
    main()
