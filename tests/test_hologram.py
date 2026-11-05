import asyncio
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent.agent import Agent
from local_agent.events import AgentEvent
from local_agent.hologram import Capabilities, HologramScene
from local_agent.provider import ChatProvider
from local_agent.state import State


class Terminal:
    encoding = "utf-8"

    def isatty(self):
        return True


class SceneTests(unittest.TestCase):
    def test_capability_profiles(self):
        for env in ({"WT_SESSION": "windows"}, {"TERM": "xterm-256color"},
                    {"TERM_PROGRAM": "Apple_Terminal"}, {"TERM": "xterm-kitty"}):
            with self.subTest(env=env):
                cap = Capabilities.detect(env, Terminal())
                self.assertTrue(cap.interactive and cap.unicode and cap.color)
                self.assertFalse(cap.remote)
        cap = Capabilities.detect({"SSH_CONNECTION": "remote", "NO_COLOR": "", "AGENT_NO_ANIMATION": "1"}, Terminal())
        self.assertTrue(cap.remote and cap.reduced_motion)
        self.assertFalse(cap.color)
        self.assertFalse(Capabilities.detect({"TERM": "dumb"}, Terminal()).interactive)
        self.assertFalse(Capabilities.detect({}, io.StringIO()).interactive)

    def test_projection_bounds_motion_and_ascii(self):
        now = [0.0]
        scene = HologramScene(clock=lambda: now[0])
        first = scene.cells(40, 14)
        for _ in range(20):
            now[0] += 0.05
            scene.advance()
        self.assertNotEqual(first, scene.cells(40, 14))
        for width, height in ((1, 1), (12, 5), (80, 24)):
            cells = scene.cells(width, height, unicode=False)
            self.assertEqual(len(cells), height)
            self.assertTrue(all(len(row) == width for row in cells))
            self.assertTrue(all(ord(char) < 128 for row in cells for char, _ in row))

    def test_states_audio_and_transient_expiration(self):
        now = [0.0]
        scene = HologramScene(clock=lambda: now[0])
        scene.event(AgentEvent("thinking"))
        scene.event(AgentEvent("token", text="Hello"))
        self.assertEqual(scene.state, "generating")
        self.assertGreater(scene.activity, 0)
        scene.event(AgentEvent("tool_start", completed=1, total=3))
        self.assertEqual(scene.progress, (1, 3))
        scene.event(AgentEvent("audio"))
        self.assertEqual(scene.state, "tool")
        scene.event(AgentEvent("audio", level=0.7))
        self.assertEqual(scene.state, "voice")
        now[0] = 2
        scene.advance()
        self.assertEqual(scene.state, "idle")
        for state in ("success", "error"):
            scene.event(AgentEvent(state))
            self.assertEqual(scene.state, state)
            now[0] += 2
            scene.advance()
            self.assertEqual(scene.state, "idle")


def sse(*chunks, done=True):
    data = "".join("data: " + json.dumps(chunk) + "\r\n\r\n" for chunk in chunks)
    if done:
        data += "data: [DONE]\r\n\r\n"
    response = io.BytesIO(data.encode())
    response.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
    return response


def chunk(delta, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.provider = ChatProvider("http://localhost:11434/v1", "test", None)

    def test_unicode_tokens_and_fragmented_tool_arguments(self):
        tokens = []
        response = sse(chunk({"content": "Hello 世界"}), chunk({"tool_calls": [{
            "index": 0, "id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":'},
        }]}), chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"notes.txt"}'}}]}, "tool_calls"))
        with patch.object(self.provider.opener, "open", return_value=response):
            reply = self.provider.complete_stream([], [], tokens.append)
        self.assertEqual(tokens, ["Hello 世界"])
        self.assertEqual(json.loads(reply["tool_calls"][0]["function"]["arguments"]), {"path": "notes.txt"})

    def test_partial_stream_and_truncated_completion_fail(self):
        for response in (sse(chunk({"content": "partial"}), done=False),
                         sse(chunk({"content": "partial"}, "length")),
                         sse(chunk({"content": "partial"})),
                         sse({"error": {"message": "failed"}})):
            with self.subTest(), patch.object(self.provider.opener, "open", return_value=response):
                with self.assertRaises(RuntimeError):
                    self.provider.complete_stream([], [], lambda _: None)

    def test_non_streaming_endpoint_and_explicit_rejection(self):
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": "JSON reply"}}]}).encode())
        response.headers = {"Content-Type": "application/json"}
        with patch.object(self.provider.opener, "open", return_value=response):
            self.assertEqual(self.provider.complete_stream([], [], lambda _: None)["content"], "JSON reply")
        from urllib.error import HTTPError
        with patch.object(self.provider.opener, "open", side_effect=HTTPError("url", 422, "", {}, None)), \
                patch.object(self.provider, "complete", return_value={"content": "fallback"}) as complete:
            self.assertEqual(self.provider.complete_stream([], [], lambda _: None)["content"], "fallback")
            complete.assert_called_once()

    def test_events_wrap_real_tools_and_commit_and_observer_cannot_break_turn(self):
        class Provider:
            count = 0

            def complete_stream(self, messages, tools, on_token):
                self.count += 1
                if self.count == 1:
                    return {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "one", "type": "function", "function": {
                            "name": "remember", "arguments": '{"name":"style","value":"brief"}',
                        },
                    }]}
                on_token("Saved.")
                return {"role": "assistant", "content": "Saved."}

        with tempfile.TemporaryDirectory() as temporary:
            state = State(Path(temporary))
            events = []
            Agent(Provider(), state).turn("demo", "remember", True, on_event=events.append)
            self.assertEqual([event.kind for event in events],
                             ["thinking", "tool_start", "tool_end", "thinking", "token", "success"])
            self.assertEqual(state.library("memory"), {"style": "brief"})
            Agent(Provider(), state).turn("demo", "remember", True, on_event=lambda _: 1 / 0)
            self.assertEqual(state.sessions(), [("demo", 2)])

    def test_stream_failure_rolls_back_pending_learning(self):
        first = sse(chunk({"tool_calls": [{"index": 0, "id": "one", "type": "function", "function": {
            "name": "remember", "arguments": '{"name":"style","value":"brief"}',
        }}]}, "tool_calls"))
        second = sse(chunk({"content": "Partial reply"}), done=False)
        with tempfile.TemporaryDirectory() as temporary:
            state = State(Path(temporary))
            events = []
            with patch.object(self.provider.opener, "open", side_effect=[first, second]):
                with self.assertRaises(RuntimeError):
                    Agent(self.provider, state).turn("demo", "remember", True, on_event=events.append)
            self.assertEqual(events[-1].kind, "error")
            self.assertEqual(state.sessions(), [])
            self.assertEqual(state.library("memory"), {})


class HologramUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            from local_agent.tui import ChatApp
        except ModuleNotFoundError:
            self.skipTest("Install the tui extra")
        self.ChatApp = ChatApp
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_paused_initial_frame_and_resize(self):
        class Provider:
            model = "test"
        app = self.ChatApp(Agent(Provider(), State(Path(self.temp.name))), "demo", False,
                           hologram="unicode", animated=False,
                           capabilities=Capabilities(True, True, True, False, False, None))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            core = app.query_one("#welcome-core")
            self.assertIn("IDLE", str(core.content))
            first = str(core.content)
            phase = core.scene.phase
            await pilot.resize_terminal(90, 30)
            await pilot.pause()
            self.assertIn("IDLE", str(core.content))
            self.assertNotEqual(first, str(core.content))
            self.assertEqual(phase, core.scene.phase)

    async def test_commands_dismiss_welcome_and_leave_output_visible(self):
        class Provider:
            model = "test"

        for command, expected in (("/help", "Enter sends a prompt"),
                                  ("/sessions", "No saved sessions yet"),
                                  ("/hologram status", "Hologram:")):
            with self.subTest(command=command):
                app = self.ChatApp(Agent(Provider(), State(Path(self.temp.name))), "demo", False,
                                   animated=False,
                                   capabilities=Capabilities(True, True, True, False, False, None))
                async with app.run_test(size=(100, 25)) as pilot:
                    self.assertTrue(app.query_one("#welcome").display)
                    app.query_one("#prompt").value = command
                    await pilot.press("enter")
                    await pilot.pause()
                    transcript = app.query_one("#transcript")
                    self.assertFalse(app.query_one("#welcome").display)
                    self.assertGreaterEqual(transcript.content_size.height, 8)
                    self.assertIn(expected, str(transcript.children[-1].content))
                    await pilot.resize_terminal(120, 40)
                    self.assertFalse(app.query_one("#welcome").display)

    async def test_paused_enhanced_frame_redraws_latest_event(self):
        from rich.text import Text
        from local_agent.hologram_widget import GraphicsWorker

        class Provider:
            model = "test"

        entered = asyncio.Event()
        release = asyncio.Event()
        self.addCleanup(release.set)
        snapshots = []

        async def delayed_frame(worker, data):
            snapshots.append(data["state"])
            if data["state"] == "thinking":
                entered.set()
                await release.wait()
            return Text("SNAPSHOT=" + data["state"])

        app = self.ChatApp(Agent(Provider(), State(Path(self.temp.name))), "demo", False,
                           animated=False,
                           capabilities=Capabilities(True, True, True, False, False, "mock-bun"))
        with patch.object(GraphicsWorker, "frame", delayed_frame):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                core = app.query_one("#welcome-core")
                phase = core.scene.phase
                core.handle_event(AgentEvent("thinking"))
                async with asyncio.timeout(3):
                    await entered.wait()
                core.handle_event(AgentEvent("token", text="Finished"))
                core.handle_event(AgentEvent("success"))
                release.set()
                async with asyncio.timeout(3):
                    while "SNAPSHOT=success" not in str(core.content):
                        await pilot.pause(0.05)
                self.assertEqual(snapshots[-2:], ["thinking", "success"])
                self.assertFalse(core.animated)
                self.assertEqual(phase, core.scene.phase)

    async def test_responsive_controls_streaming_and_cleanup(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class Provider:
            model = "test"

            def complete_stream(self, messages, tools, on_token):
                on_token("Hello ")
                time.sleep(0.07)
                on_token("world")
                release.wait(5)
                return {"role": "assistant", "content": "Hello world"}

        from local_agent.hologram_widget import HologramRenderer
        cap = Capabilities(True, True, True, False, False, None)
        app = self.ChatApp(Agent(Provider(), State(Path(self.temp.name))), "demo", False, capabilities=cap)
        async with app.run_test(size=(120, 40)) as pilot:
            welcome = app.query_one("#welcome-core", HologramRenderer)
            orb = app.query_one("#orb", HologramRenderer)
            self.assertTrue(welcome.active)
            self.assertFalse(orb.active)
            field = app.query_one("#prompt")
            field.value = "hello"
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertTrue(app.busy)
            self.assertFalse(welcome.active)
            self.assertTrue(orb.active)
            self.assertEqual(app.streaming_text, "Hello world")
            for command in ("pause", "left", "large", "hide"):
                field.value = "/hologram " + command
                await pilot.press("enter")
            phase = orb.scene.phase
            await pilot.pause(0.15)
            self.assertEqual(phase, orb.scene.phase)
            self.assertFalse(orb.active)
            self.assertTrue(app.busy)
            release.set()
            async with asyncio.timeout(3):
                while app.busy:
                    await pilot.pause(0.05)
            self.assertEqual(str(app.streaming_row.content), "AGENT\nHello world")
            field.value = "/hologram show"
            await pilot.press("enter")
            self.assertTrue(orb.active)
            await pilot.resize_terminal(40, 14)
            self.assertFalse(orb.active)
            self.assertFalse(welcome.active)
            self.assertTrue(field.display)
            await pilot.resize_terminal(100, 30)
            self.assertTrue(orb.active)
        self.assertIsNone(orb.worker)
        self.assertFalse(orb.closing)

    async def test_enhanced_failure_does_not_break_input(self):
        from local_agent.hologram_widget import GraphicsWorker, HologramRenderer

        class Provider:
            model = "test"

        cap = Capabilities(True, True, True, False, False, "missing-bun")
        app = self.ChatApp(Agent(Provider(), State(Path(self.temp.name))), "demo", False, capabilities=cap)
        with patch.object(GraphicsWorker, "frame", side_effect=RuntimeError("GPU unavailable")):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.2)
                core = app.query_one("#welcome-core", HologramRenderer)
                self.assertEqual(core.mode, "ascii")
                self.assertIn("failed", core.reason)
                self.assertIsNone(core.worker)
                await pilot.press("h", "e", "l", "l", "o")
                self.assertEqual(app.query_one("#prompt").value, "hello")


if __name__ == "__main__":
    unittest.main()
