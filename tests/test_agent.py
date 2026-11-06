import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_agent.__main__ import confirm_changes
from local_agent.agent import Agent
from local_agent.config import load_config, save_config
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


def text(content):
    return {"role": "assistant", "content": content}


def calls(*entries):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": f"call-{index}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    } for index, (name, arguments) in enumerate(entries)]}


def last_tool_result(provider):
    messages, _ = provider.requests[-1]
    return json.loads(messages[-1]["content"])


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
        self.assertEqual([entry["function"]["name"] for entry in tools],
                         ["read_skill", "todo", "session_search", "create_artifact", "list_artifacts", "read_artifact"])
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
        (workspace / "docs").mkdir()
        agent = Agent(None, self.state, workspace)
        self.assertEqual(agent.list_files(".")["entries"], ["docs/", "sample.txt"])
        result = agent.read_file("sample.txt")
        self.assertEqual(len(result["text"]), 2400)
        self.assertTrue(result["text"].startswith("   1| xxx"))
        self.assertTrue(result["truncated"])
        self.assertIn("cut", result["note"])
        for path in ("../outside", ".env", "sample.txt:stream", str(self.root), "docs/../../outside"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                agent.read_file(path)

    def test_context_keeps_complete_turns(self):
        for index in range(4):
            self.state.commit_turn("large", index, [
                {"role": "user", "content": str(index)},
                {"role": "assistant", "content": "x" * 20_000},
            ], {})
        number, turns = self.state.history("large", 48_000)
        messages = [message for turn in turns for message in turn]
        self.assertEqual(number, 4)
        self.assertEqual([m["content"] for m in messages if m["role"] == "user"], ["2", "3"])
        self.assertEqual(len(list(self.state.transcript("large"))), 8)

    def test_config_replacement_and_url_validation(self):
        save_config(self.root, "http://localhost:11434/v1", "first")
        save_config(self.root, "http://localhost:11434/v1", "second")
        self.assertEqual(load_config(self.root)["model"], "second")
        with self.assertRaises(ValueError):
            ChatProvider("https://user:password@example.com/v1", "model", None)
        with self.assertRaises(ValueError):
            ChatProvider("http://example.com/v1", "model", "key")

    def test_extra_calls_in_a_round_are_skipped_not_fatal(self):
        provider = ScriptedProvider(
            calls(*[("remember", {"name": f"m{index}", "value": "v"}) for index in range(5)]),
            text("Saved four."),
        )
        self.assertEqual(Agent(provider, self.state).turn("s", "Save five", learn=True), "Saved four.")
        messages, _ = provider.requests[-1]
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        self.assertEqual(len(results), 5)
        self.assertIn("Not run", results[4]["error"])
        self.assertEqual(sorted(self.state.library("memory")), ["m0", "m1", "m2", "m3"])

    def test_last_round_asks_for_text_without_tools(self):
        provider = ScriptedProvider(*[tool_call("read_skill", name="missing")] * 6, text("Final answer."))
        self.assertEqual(Agent(provider, self.state).turn("s", "Hi"), "Final answer.")
        self.assertEqual([bool(tools) for _, tools in provider.requests], [True] * 6 + [False])
        self.assertEqual(self.state.sessions(), [("s", 1)])

    def test_last_round_keeps_text_and_drops_unanswerable_calls(self):
        last = {**tool_call("read_skill", name="missing"), "content": "Here is what I know."}
        provider = ScriptedProvider(*[tool_call("read_skill", name="missing")] * 6, last)
        self.assertEqual(Agent(provider, self.state).turn("s", "Hi"), "Here is what I know.")
        self.assertNotIn("tool_calls", list(self.state.transcript("s"))[-1])

    def test_removing_a_missing_entry_reports_an_error(self):
        for name in ("forget", "delete_skill"):
            with self.subTest(tool=name):
                provider = ScriptedProvider(tool_call(name, name="nope"), text("Nothing to remove."))
                Agent(provider, self.state).turn("s", "Remove nope", learn=True)
                self.assertIn("No saved", last_tool_result(provider)["error"])

    def test_declined_changes_are_not_saved(self):
        asked = []
        provider = ScriptedProvider(tool_call("remember", name="style", value="concise"), text("Saved."))
        agent = Agent(provider, self.state, approve=lambda changes: asked.append(changes) or False)
        reply = agent.turn("s", "Remember my style", learn=True)
        self.assertEqual(asked, [{("memory", "style"): "concise"}])
        self.assertEqual(self.state.library("memory"), {})
        self.assertIn("Not saved", reply)
        self.assertIn("Not saved", list(self.state.transcript("s"))[-1]["content"])

    def test_prompt_keeps_project_files_out_of_artifacts_and_names_the_missing_switch(self):
        workspace = self.root / "project"
        workspace.mkdir()
        read_only = Agent(None, self.state, workspace).system_prompt(["read_file"], {})["content"]
        self.assertIn("/write on", read_only)
        self.assertIn("/shell on", read_only)
        self.assertIn("never in create_artifact", read_only)
        editing = Agent(None, self.state, workspace, write=True, shell=True).system_prompt(["write_file"], {})["content"]
        self.assertNotIn("/write on", editing)
        self.assertIn("Use write_file to create a file in the workspace", editing)
        no_workspace = Agent(None, self.state).system_prompt(["create_artifact"], {})["content"]
        self.assertIn("use create_artifact", no_workspace)
        self.assertIn("--workspace . --write", no_workspace)
        self.assertNotIn("/write on", no_workspace)

    def test_approved_changes_are_saved_and_unchanged_turns_skip_the_prompt(self):
        asked = []
        approve = lambda changes: asked.append(changes) or True
        provider = ScriptedProvider(tool_call("remember", name="style", value="concise"), text("Saved."))
        Agent(provider, self.state, approve=approve).turn("s", "Remember my style", learn=True)
        Agent(ScriptedProvider(text("Hello.")), self.state, approve=approve).turn("s", "Hi", learn=True)
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.state.library("memory"), {"style": "concise"})

    def test_skill_save_read_and_delete(self):
        steps = "1. Run tests\n2. Tag release"
        provider = ScriptedProvider(
            tool_call("save_skill", name="release", description="Ship a release", steps=steps), text("Saved."),
        )
        Agent(provider, self.state).turn("s", "Save it", learn=True)
        provider = ScriptedProvider(tool_call("read_skill", name="release"), text("Loaded."))
        Agent(provider, self.state).turn("s", "Release")
        self.assertIn("Ship a release", provider.requests[0][0][0]["content"])
        self.assertEqual(last_tool_result(provider), {"description": "Ship a release", "steps": steps})
        provider = ScriptedProvider(tool_call("delete_skill", name="release"), text("Deleted."))
        Agent(provider, self.state).turn("s", "Delete it", learn=True)
        self.assertEqual(self.state.library("skill"), {})

    def test_store_holds_twenty_entries(self):
        self.state.commit_turn("seed", 0, [], {("memory", f"m{index}"): "v" for index in range(20)})
        provider = ScriptedProvider(tool_call("remember", name="extra", value="v"), text("Full."))
        Agent(provider, self.state).turn("s", "Remember", learn=True)
        self.assertIn("full", last_tool_result(provider)["error"])
        self.assertEqual(len(self.state.library("memory")), 20)

    def test_tool_arguments_must_match_the_schema(self):
        available = {"read_skill": lambda name: {}}
        for arguments in ('{"name": 1}', '{"name": "a", "extra": "b"}', "{}", "[]", "not json",
                          '{"name": "  "}', json.dumps({"name": "x" * 65})):
            with self.subTest(arguments=arguments):
                call = {"id": "c", "type": "function", "function": {"name": "read_skill", "arguments": arguments}}
                self.assertIn("error", json.loads(Agent.dispatch(call, available)["content"]))

    def test_history_fits_the_context_budget(self):
        for index in range(3):
            self.state.commit_turn("s", index, [
                {"role": "user", "content": str(index)}, text("x" * 3_000),
            ], {})
        provider = ScriptedProvider(text("ok"))
        Agent(provider, self.state, context_chars=10_000).turn("s", "next")
        messages, tools = provider.requests[0]
        self.assertEqual([m["content"] for m in messages if m["role"] == "user"], ["2", "next"])
        self.assertLessEqual(len(json.dumps(messages)) + len(json.dumps(tools)), 10_000)

    def test_large_tool_results_are_compressed_instead_of_failing(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "big.txt").write_text("\n".join("line %d %s" % (i, "x" * 60) for i in range(200)),
                                           encoding="utf-8")
        events = []
        provider = ScriptedProvider(*[tool_call("read_file", path="big.txt")] * 3, text("Read it."))
        agent = Agent(provider, self.state, workspace, context_chars=6_000,
                      on_event=lambda kind, data: events.append((kind, data)))
        self.assertEqual(agent.turn("s", "Read it"), "Read it.")
        for messages, tools in provider.requests:
            self.assertLessEqual(len(json.dumps(messages, ensure_ascii=False))
                                 + len(json.dumps(tools, ensure_ascii=False)), 6_000)
        compressed = [data for kind, data in events if kind == "compress"]
        self.assertTrue(compressed)
        self.assertTrue(any(data["pruned"] or data["shortened"] for data in compressed))
        self.assertEqual(self.state.sessions(), [("s", 1)])

    def test_turn_stops_at_the_context_budget_without_saving(self):
        with self.assertRaisesRegex(ValueError, "context budget"):
            Agent(ScriptedProvider(text("x")), self.state, context_chars=4_000).turn("s", "x" * 3_900)
        with self.assertRaisesRegex(ValueError, "between 1 and 4,000"):
            Agent(ScriptedProvider(text("x")), self.state, context_chars=4_000).turn("s", "x" * 4_001)
        self.assertEqual(self.state.sessions(), [])

    def test_oldest_history_is_dropped_when_a_turn_grows(self):
        for index in range(3):
            self.state.commit_turn("s", index, [{"role": "user", "content": str(index)}, text("y" * 1_200)], {})
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "a.txt").write_text("\n".join("z" * 70 for _ in range(100)), encoding="utf-8")
        provider = ScriptedProvider(calls(("read_file", {"path": "a.txt"}), ("read_file", {"path": "a.txt",
                                                                                            "start_line": 40})),
                                    text("Done."))
        Agent(provider, self.state, workspace, context_chars=8_000).turn("s", "Read a")
        first, second = (messages for messages, _ in provider.requests)
        users = lambda messages: [m["content"] for m in messages if m["role"] == "user"]
        self.assertGreater(len(users(first)), len(users(second)))
        self.assertEqual(users(second)[-1], "Read a")

    def test_context_budget_setting(self):
        save_config(self.root, "http://localhost:11434/v1", "model", 20_000)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_config(self.root)["context_chars"], 20_000)
        with mock.patch.dict(os.environ, {"AGENT_CONTEXT_CHARS": "30000"}, clear=True):
            self.assertEqual(load_config(self.root)["context_chars"], 30_000)
        for value in ("lots", "100"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"AGENT_CONTEXT_CHARS": value}, clear=True):
                with self.assertRaises(ValueError):
                    load_config(self.root)
        (self.root / "config.json").write_text('{"base_url": "http://localhost:1/v1", "model": "m"}')
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_config(self.root)["context_chars"], 12_000)

    def test_workspace_rejects_links(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (self.root / "outside.txt").write_text("secret", encoding="utf-8")
        try:
            (workspace / "link.txt").symlink_to(self.root / "outside.txt")
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks needs extra permissions here.")
        with self.assertRaisesRegex(ValueError, "Linked"):
            Agent(None, self.state, workspace).read_file("link.txt")

    @unittest.skipUnless(os.name == "nt", "Only Windows paths ignore letter case.")
    def test_workspace_paths_ignore_case_on_windows(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("hi", encoding="utf-8")
        agent = Agent(None, self.state, workspace)
        self.assertEqual(agent.read_file("readme.md")["text"], "   1| hi")


class ConfirmTests(unittest.TestCase):
    changes = {("memory", "style"): "concise\x1b[2J", ("skill", "old"): None}

    def test_requires_a_terminal(self):
        with mock.patch("sys.stdin", io.StringIO("y\n")), mock.patch("sys.stderr", io.StringIO()):
            self.assertFalse(confirm_changes(self.changes))

    def test_shows_changes_and_asks(self):
        for answer, expected in (("y", True), ("", False), ("no", False)):
            with self.subTest(answer=answer), mock.patch("sys.stdin.isatty", return_value=True), \
                    mock.patch("builtins.input", return_value=answer), \
                    mock.patch("sys.stdout", io.StringIO()) as output:
                self.assertEqual(confirm_changes(self.changes), expected)
                shown = output.getvalue()
                self.assertIn("+ memory style: concise?[2J", shown)
                self.assertIn("- delete skill old", shown)


class ProviderTests(unittest.TestCase):
    def provider_returning(self, data):
        provider = ChatProvider("http://localhost:11434/v1", "model", None)
        provider.sent = []
        provider.request_json = lambda request: provider.sent.append(json.loads(request.data)) or data
        return provider

    def test_parses_tool_calls_and_omits_an_empty_tool_list(self):
        provider = self.provider_returning({"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None,
            "tool_calls": [{"id": "a", "type": "function", "function": {"name": "read_skill", "arguments": "{}"}}],
        }}]})
        reply = provider.complete([{"role": "user", "content": "hi"}], [])
        self.assertNotIn("tools", provider.sent[0])
        self.assertEqual(reply["tool_calls"][0]["function"]["name"], "read_skill")
        provider.complete([{"role": "user", "content": "hi"}], [{"type": "function"}])
        self.assertIn("tools", provider.sent[1])

    def test_rejects_invalid_or_incomplete_completions(self):
        call = {"id": "a", "type": "function", "function": {"name": "read_skill", "arguments": "{}"}}
        for data in (
            {},
            {"choices": []},
            {"choices": [{"message": {"content": 5}}]},
            {"choices": [{"message": {"content": None, "tool_calls": [call, call]}}]},
            {"choices": [{"message": {"content": None, "tool_calls": [{**call, "type": "other"}]}}]},
            {"choices": [{"finish_reason": "length", "message": {"content": "cut off"}}]},
        ):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                self.provider_returning(data).complete([], [])


if __name__ == "__main__":
    unittest.main()
