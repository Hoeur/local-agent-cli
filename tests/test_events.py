"""Tests for the hooks the terminal UI relies on: streaming, turn events and cancellation."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from local_agent.agent import Agent, TurnCancelled
from local_agent.provider import ChatProvider
from local_agent.state import State


def sse(*chunks):
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


class Endpoint(BaseHTTPRequestHandler):
    """A tiny OpenAI-compatible endpoint. The test sets `responses` to a list of (status, type, body)."""
    responses = []
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        Endpoint.received.append(json.loads(self.rfile.read(length)))
        status, kind, body = Endpoint.responses.pop(0)
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class StreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Endpoint.responses = []
        Endpoint.received = []

    def test_streams_text_and_tool_calls(self):
        Endpoint.responses = [(200, "text/event-stream", sse(
            {"choices": [{"delta": {"reasoning": "hmm"}}]},
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                                    "function": {"name": "read_skill", "arguments": "{\"na"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "me\": \"x\"}"}}]},
                          "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}},
        ))]
        provider = ChatProvider(self.base, "m", None)
        fragments = []
        reply = provider.complete([{"role": "user", "content": "hi"}], [],
                                  on_text=lambda kind, text: fragments.append((kind, text)))
        self.assertEqual(fragments, [("reasoning", "hmm"), ("content", "Hel"), ("content", "lo")])
        self.assertEqual(reply["content"], "Hello")
        self.assertEqual(reply["tool_calls"][0]["function"], {"name": "read_skill", "arguments": '{"name": "x"}'})
        self.assertEqual(provider.last_usage["total_tokens"], 13)
        self.assertTrue(Endpoint.received[0]["stream"])

    def test_falls_back_when_streaming_is_rejected(self):
        Endpoint.responses = [
            (400, "application/json", "{}"),
            (200, "application/json", json.dumps({"choices": [{"message": {"content": "plain"}}]})),
        ]
        provider = ChatProvider(self.base, "m", None)
        reply = provider.complete([{"role": "user", "content": "hi"}], [], on_text=lambda *_: None)
        self.assertEqual(reply["content"], "plain")
        self.assertFalse(provider.streaming)
        self.assertNotIn("stream", Endpoint.received[1])

    def test_without_a_callback_the_request_is_not_streamed(self):
        Endpoint.responses = [(200, "application/json", json.dumps({
            "choices": [{"message": {"content": "plain"}}], "usage": {"total_tokens": 4},
        }))]
        provider = ChatProvider(self.base, "m", None)
        self.assertEqual(provider.complete([], [])["content"], "plain")
        self.assertNotIn("stream", Endpoint.received[0])
        self.assertEqual(provider.last_usage, {"total_tokens": 4})

    def test_cut_off_stream_is_an_error(self):
        Endpoint.responses = [(200, "text/event-stream", sse(
            {"choices": [{"delta": {"content": "partial"}, "finish_reason": "length"}]},
        ))]
        provider = ChatProvider(self.base, "m", None)
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            provider.complete([], [], on_text=lambda *_: None)


class Scripted:
    streaming = False

    def __init__(self, *replies):
        self.replies = list(replies)

    def complete(self, messages, tools):
        return self.replies.pop(0)


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = State(Path(self.temporary.name))

    def test_events_follow_the_turn(self):
        events = []
        provider = Scripted(
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "read_skill", "arguments": '{"name": "nope"}'}},
            ]},
            {"role": "assistant", "content": "Done."},
        )
        agent = Agent(provider, self.state, on_event=lambda kind, data: events.append((kind, data)))
        self.assertEqual(agent.turn("s", "hi"), "Done.")
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds, ["request", "reply", "tool_start", "tool_end", "request", "reply", "commit", "done"])
        self.assertEqual(events[3][1]["error"], "Tool or saved skill is unavailable.")
        self.assertLessEqual(events[0][1]["used"], events[0][1]["budget"])

    def test_cancelling_saves_nothing(self):
        provider = Scripted({"role": "assistant", "content": "Done."})

        def handler(kind, data):
            if kind == "reply":
                raise TurnCancelled("Cancelled.")

        with self.assertRaises(TurnCancelled):
            Agent(provider, self.state, on_event=handler).turn("s", "hi")
        self.assertEqual(self.state.sessions(), [])


if __name__ == "__main__":
    unittest.main()


class CliTests(unittest.TestCase):
    def run_cli(self, *args, stdin=""):
        import subprocess
        import sys
        env = {**os.environ, "AGENT_BASE_URL": "http://127.0.0.1:9/v1", "AGENT_MODEL": "m"}
        with tempfile.TemporaryDirectory() as folder:
            return subprocess.run([sys.executable, "-m", "local_agent", "--data-dir", folder, *args],
                                  input=stdin, capture_output=True, text=True, env=env, timeout=60,
                                  cwd=Path(__file__).resolve().parents[1])

    def test_piped_chat_keeps_the_classic_format(self):
        result = self.run_cli("chat", "--session", "s", stdin="/exit\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Session 's'. Enter /exit to leave.\nYou> ")

    def test_piped_errors_are_plain_and_the_loop_continues(self):
        result = self.run_cli("chat", "--plain", stdin="hello\n/exit\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Error: Could not reach the model endpoint", result.stderr)
        self.assertNotIn("\x1b[", result.stdout + result.stderr)
