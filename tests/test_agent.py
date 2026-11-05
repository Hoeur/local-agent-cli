import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from local_agent.agent import Agent
from local_agent.config import save_config
from local_agent.provider import ChatProvider
from local_agent.state import State


def tool_call(tool_name, **arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function",
        "function": {"name": tool_name, "arguments": json.dumps(arguments)},
    }]}


class ScriptedProvider:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((messages, tools))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = State(self.root / "state")

    def test_memory_and_session_survive_restart(self):
        provider = ScriptedProvider(
            tool_call("remember", name="style", value="concise"),
            {"role": "assistant", "content": "Saved."},
        )
        Agent(provider, self.state).turn("first", "Remember my style", learn=True)
        restarted = State(self.root / "state")
        self.assertEqual(restarted.library("memory"), {"style": "concise"})
        self.assertEqual(restarted.sessions(), [("first", 1)])
        provider = ScriptedProvider({"role": "assistant", "content": "Concise."})
        Agent(provider, restarted).turn("first", "What style?")
        messages, _ = provider.requests[0]
        self.assertIn("concise", messages[0]["content"])
        self.assertIn({"role": "assistant", "content": "Saved."}, messages)

    def test_failed_turn_rolls_back_staged_memory(self):
        provider = ScriptedProvider(
            tool_call("remember", name="style", value="concise"),
            RuntimeError("Provider unavailable"),
        )
        with self.assertRaisesRegex(RuntimeError, "Provider unavailable"):
            Agent(provider, self.state).turn("first", "Remember my style", learn=True)
        self.assertEqual(self.state.library("memory"), {})
        self.assertEqual(self.state.sessions(), [])

    def test_disabled_learning_cannot_be_called(self):
        provider = ScriptedProvider(
            tool_call("remember", name="style", value="concise"),
            {"role": "assistant", "content": "Learning is disabled."},
        )
        Agent(provider, self.state).turn("first", "Remember my style")
        messages, tools = provider.requests[-1]
        names = {entry["function"]["name"] for entry in tools}
        self.assertIn("read_skill", names)
        self.assertNotIn("remember", names)
        self.assertNotIn("save_skill", names)
        self.assertIn("error", json.loads(messages[-1]["content"]))
        self.assertEqual(self.state.library("memory"), {})

    def test_concurrent_session_commit_is_rejected_atomically(self):
        self.state.commit_turn("first", 0, [], {})
        with self.assertRaisesRegex(RuntimeError, "another process"):
            self.state.commit_turn("first", 0, [], {("memory", "stale"): "discard"})
        self.assertEqual(self.state.library("memory"), {})

    def test_repeated_tool_calls_stop_without_saving(self):
        provider = ScriptedProvider(*[tool_call("remember", name="style", value="concise")] * 7)
        with self.assertRaisesRegex(RuntimeError, "tool-call limit"):
            Agent(provider, self.state).turn("first", "Remember my style", learn=True)
        self.assertEqual(self.state.sessions(), [])
        self.assertEqual(self.state.library("memory"), {})

    def test_workspace_bounds_and_truncation(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "sample.txt").write_text("x" * 2500, encoding="utf-8")
        (workspace / ".env").write_text("secret", encoding="utf-8")
        agent = Agent(None, self.state, workspace)
        self.assertEqual(agent.list_files(".")["entries"], ["sample.txt"])
        self.assertEqual(agent.read_file("sample.txt"), {"text": "x" * 2400, "truncated": True})
        for path in ("../outside", ".env", "sample.txt:stream", str(self.root)):
            with self.subTest(path=path), self.assertRaises(ValueError):
                agent.read_file(path)

    def test_context_keeps_complete_turns(self):
        for index in range(4):
            self.state.commit_turn("large", index, [
                {"role": "user", "content": str(index)},
                {"role": "assistant", "content": "x" * 20_000},
            ], {})
        number, messages = self.state.history("large")
        self.assertEqual(number, 4)
        self.assertEqual([m["content"] for m in messages if m["role"] == "user"], ["2", "3"])
        self.assertEqual(len(list(self.state.transcript("large"))), 8)

    def test_config_replacement_and_url_validation(self):
        save_config(self.root, "http://localhost:11434/v1", "first")
        save_config(self.root, "http://localhost:11434/v1", "second")
        self.assertEqual(json.loads((self.root / "config.json").read_text())["model"], "second")
        with self.assertRaises(ValueError):
            ChatProvider("https://user:password@example.com/v1", "model", None)
        with self.assertRaises(ValueError):
            ChatProvider("http://example.com/v1", "model", "key")


class TerminalChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_activity_and_retry(self):
        try:
            from local_agent.tui import ChatApp
        except ModuleNotFoundError as error:
            if error.name == "textual":
                self.skipTest("Install the tui extra to exercise interactive chat.")
            raise

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "notes.txt").write_text("Local notes", encoding="utf-8")
            provider = ScriptedProvider(
                RuntimeError("Provider unavailable"),
                tool_call("read_file", path="notes.txt"),
                {"role": "assistant", "content": "Read the notes."},
            )
            provider.model = "scripted"
            state = State(root / "state")
            app = ChatApp(Agent(provider, state, root), "demo", False)

            async with app.run_test(size=(100, 24)) as pilot:
                field = app.query_one("#prompt")
                field.value = "Read my notes"

                async def submit():
                    await pilot.press("enter")
                    async with asyncio.timeout(3):
                        while app.busy:
                            await pilot.pause(0.05)
                    await pilot.pause()

                await submit()
                self.assertEqual(field.value, "Read my notes")
                self.assertEqual(state.sessions(), [])
                await submit()

                lines = [str(row.content) for row in app.query_one("#transcript").children]
                self.assertIn("TURN FAILED\nProvider unavailable", lines)
                self.assertEqual(lines[-2:], ["  ✓ Read notes.txt", "AGENT\nRead the notes."])
                self.assertEqual(state.sessions(), [("demo", 1)])
                self.assertTrue(app.query_one("#status").display)
                await pilot.resize_terminal(60, 20)
                self.assertFalse(app.query_one("#status").display)
                await pilot.press("ctrl+q")
            self.assertEqual(app.return_value, 0)


if __name__ == "__main__":
    unittest.main()
