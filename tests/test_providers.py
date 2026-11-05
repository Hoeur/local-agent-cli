"""Cloud providers: Claude (Anthropic Messages), OpenAI Responses, profiles, keys and the CLI."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from local_agent import provider as base
from local_agent import providers
from local_agent.__main__ import main
from local_agent.config import load_config, read_settings, save_config, set_active
from local_agent.provider import ChatProvider
from local_agent.provider_anthropic import AnthropicProvider, convert_messages
from local_agent.provider_responses import ResponsesProvider


class Server(BaseHTTPRequestHandler):
    """Records requests and replies from Server.replies: (status, content type, body) tuples."""
    replies, received = [], []

    def reply(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else None
        Server.received.append({"path": self.path, "headers": {key.lower(): value for key, value in self.headers.items()}, "body": body})
        status, kind, payload = Server.replies.pop(0)
        payload = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = reply

    def log_message(self, *args):
        pass


def sse(*events):
    """Anthropic/Responses style: an event line, then a data line."""
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def as_json(data):
    return (200, "application/json", json.dumps(data))


TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
CONVERSATION = [
    {"role": "system", "content": "Be brief."},
    {"role": "user", "content": "Read a.txt"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}]},
    {"role": "tool", "tool_call_id": "c1", "content": '{"text": "hello"}'},
]


class FakeServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Server.replies, Server.received = [], []


class AnthropicTests(FakeServerTest):
    def test_streams_text_and_tool_calls_and_converts_the_request(self):
        Server.replies = [(200, "text/event-stream", sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 20, "cache_read_input_tokens": 80}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Done"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "."}},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "t2", "name": "read_file", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"pa'}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'th": "b"}'}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
            {"type": "message_stop"},
        ))]
        provider = AnthropicProvider(self.base, "claude-x", "sk-ant-test-123")
        fragments = []
        reply = provider.complete(CONVERSATION, TOOLS, on_text=lambda kind, text: fragments.append(text))
        self.assertEqual(fragments, ["Done", "."])
        self.assertEqual(reply["content"], "Done.")
        self.assertEqual(reply["tool_calls"][0]["id"], "t2")
        self.assertEqual(json.loads(reply["tool_calls"][0]["function"]["arguments"]), {"path": "b"})
        self.assertEqual(provider.last_usage, {"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107})
        sent = Server.received[0]
        self.assertEqual(sent["path"], "/v1/messages")
        self.assertEqual(sent["headers"]["x-api-key"], "sk-ant-test-123")
        self.assertEqual(sent["headers"]["anthropic-version"], "2023-06-01")
        body = sent["body"]
        self.assertTrue(body["stream"])
        self.assertEqual(body["system"][0]["text"], "Be brief.")
        self.assertIn("cache_control", body["system"][0])
        self.assertEqual(body["tools"][0]["input_schema"]["properties"]["path"]["type"], "string")
        self.assertEqual([turn["role"] for turn in body["messages"]], ["user", "assistant", "user"])
        self.assertEqual(body["messages"][1]["content"][0]["input"], {"path": "a.txt"})
        self.assertEqual(body["messages"][2]["content"][0]["tool_use_id"], "c1")

    def test_plain_request_and_cut_off_reply(self):
        Server.replies = [
            as_json({"content": [{"type": "text", "text": "Hi"}], "stop_reason": "end_turn",
                     "usage": {"input_tokens": 3, "output_tokens": 1}}),
            as_json({"content": [{"type": "text", "text": "Hi"}], "stop_reason": "max_tokens"}),
        ]
        provider = AnthropicProvider(self.base, "claude-x", None)
        self.assertEqual(provider.complete([{"role": "user", "content": "hi"}], [])["content"], "Hi")
        self.assertNotIn("tools", Server.received[0]["body"])
        self.assertEqual(Server.received[0]["body"]["max_tokens"], 8192)
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            provider.complete([{"role": "user", "content": "hi"}], [])

    def test_stream_errors_are_reported(self):
        Server.replies = [(200, "text/event-stream", sse(
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))]
        with self.assertRaisesRegex(RuntimeError, "Overloaded"):
            AnthropicProvider(self.base, "m", None).complete([{"role": "user", "content": "x"}], [],
                                                            on_text=lambda *_: None)

    def test_tool_results_and_text_merge_into_one_user_turn(self):
        turns = convert_messages([*CONVERSATION[1:], {"role": "user", "content": "Thanks"}])
        self.assertEqual([turn["role"] for turn in turns], ["user", "assistant", "user"])
        self.assertEqual([block["type"] for block in turns[2]["content"]], ["tool_result", "text"])
        self.assertEqual(convert_messages([{"role": "assistant", "content": "hi"}])[0]["role"], "user")


class ResponsesTests(FakeServerTest):
    def test_streams_text_then_reads_calls_from_the_final_response(self):
        final = {"status": "completed", "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
                 "output": [{"type": "reasoning", "summary": []},
                            {"type": "message", "content": [{"type": "output_text", "text": "Reading"}]},
                            {"type": "function_call", "call_id": "f1", "name": "read_file",
                             "arguments": '{"path": "c"}'}]}
        Server.replies = [(200, "text/event-stream", sse(
            {"type": "response.created", "response": {}},
            {"type": "response.output_text.delta", "delta": "Read"},
            {"type": "response.output_text.delta", "delta": "ing"},
            {"type": "response.completed", "response": final},
        ))]
        provider = ResponsesProvider(self.base, "gpt-codex", "sk-test-4567")
        fragments = []
        reply = provider.complete(CONVERSATION, TOOLS, on_text=lambda kind, text: fragments.append(text))
        self.assertEqual("".join(fragments), "Reading")
        self.assertEqual(reply["content"], "Reading")
        self.assertEqual(reply["tool_calls"][0]["id"], "f1")
        self.assertEqual(provider.last_usage["total_tokens"], 13)
        sent = Server.received[0]
        self.assertEqual(sent["path"], "/v1/responses")
        self.assertEqual(sent["headers"]["authorization"], "Bearer sk-test-4567")
        body = sent["body"]
        self.assertEqual(body["instructions"], "Be brief.")
        self.assertFalse(body["store"])
        self.assertEqual(body["tools"][0]["name"], "read_file")
        self.assertEqual([item.get("type", item.get("role")) for item in body["input"]],
                         ["user", "function_call", "function_call_output"])

    def test_incomplete_and_failed_responses_are_errors(self):
        Server.replies = [
            as_json({"status": "incomplete", "output": [], "incomplete_details": {"reason": "max_output_tokens"}}),
            as_json({"status": "failed", "output": [], "error": {"message": "server boom"}}),
        ]
        provider = ResponsesProvider(self.base, "m", None)
        with self.assertRaisesRegex(RuntimeError, "max_output_tokens"):
            provider.complete([{"role": "user", "content": "x"}], [])
        with self.assertRaisesRegex(RuntimeError, "server boom"):
            provider.complete([{"role": "user", "content": "x"}], [])


class HttpTests(FakeServerTest):
    def test_error_messages_include_the_providers_explanation(self):
        Server.replies = [(401, "application/json", json.dumps({"error": {"message": "Authentication Fails"}}))]
        with self.assertRaisesRegex(RuntimeError, r"HTTP 401\. Authentication Fails"):
            ChatProvider(self.base, "m", None).complete([{"role": "user", "content": "x"}], [])

    def test_hosted_endpoints_retry_overloads_but_not_empty_quotas(self):
        ok = as_json({"choices": [{"message": {"content": "fine"}}]})
        with mock.patch.object(base, "RETRY_DELAYS", (0, 0)):
            provider = ChatProvider(self.base, "m", None)
            provider.retries = 2  # As for a hosted endpoint.
            Server.replies = [(529, "application/json", "{}"), (503, "text/plain", "busy"), ok]
            self.assertEqual(provider.complete([{"role": "user", "content": "x"}], [])["content"], "fine")
            Server.replies = [(429, "application/json", json.dumps({"error": {"message": "Insufficient balance"}}))]
            with self.assertRaisesRegex(RuntimeError, "Insufficient balance"):
                provider.complete([{"role": "user", "content": "x"}], [])
            self.assertEqual(Server.replies, [])
        self.assertEqual(ChatProvider(self.base, "m", None).retries, 0)  # Local servers don't retry.

    def test_check_tries_a_message_when_the_model_is_not_listed(self):
        Server.replies = [as_json({"data": [{"id": "other"}]}), as_json({"choices": [{"message": {"content": "OK"}}]})]
        self.assertEqual(ChatProvider(self.base, "alias", None).check()[0], "online")
        Server.replies = [as_json({"data": [{"id": "other"}]}), (404, "application/json", "{}")]
        status, detail = ChatProvider(self.base, "alias", None).check()
        self.assertEqual(status, "missing")
        self.assertIn("other", detail)
        Server.replies = [(404, "text/html", "<html>"), (401, "application/json", '{"error": "bad key"}')]
        self.assertEqual(ChatProvider(self.base, "m", None).check(), ("offline", "Model endpoint returned HTTP 401. bad key"))


class ProfileAndKeyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        clean = {key: value for key, value in os.environ.items()
                 if not key.endswith("_API_KEY") and not key.startswith("AGENT_")}
        clean["AGENT_KEYS_FILE"] = str(self.root / "keys" / "keys.json")
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv, stdin=""):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            code = main(["--data-dir", str(self.root / "state"), *argv])
        return code, output.getvalue()

    def test_older_single_model_configs_still_load(self):
        (self.root / "config.json").write_text('{"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:4b"}')
        config = load_config(self.root)
        self.assertEqual((config["provider"], config["profile"], config["model"]), ("ollama", "ollama", "qwen3:4b"))
        save_config(self.root, "https://api.deepseek.com/v1", "deepseek-chat", 100_000, provider="deepseek")
        self.assertEqual(set(read_settings(self.root)["profiles"]), {"ollama", "deepseek"})
        self.assertEqual(load_config(self.root)["profile"], "deepseek")
        self.assertEqual(load_config(self.root, "ollama")["model"], "qwen3:4b")
        set_active(self.root, "ollama", "qwen3:8b")
        self.assertEqual(load_config(self.root)["model"], "qwen3:8b")
        with self.assertRaisesRegex(ValueError, "No saved profile"):
            load_config(self.root, "nope")

    def test_keys_come_from_the_environment_then_the_store_and_never_cross_providers(self):
        profile = {"profile": "anthropic", "provider": "anthropic"}
        self.assertEqual(providers.resolve_key(profile), (None, None))
        os.environ["AGENT_API_KEY"] = "sk-generic-key"  # Meant for local or custom endpoints only.
        self.assertEqual(providers.resolve_key(profile), (None, None))
        providers.set_key("anthropic", "sk-ant-stored-1")
        self.assertEqual(providers.resolve_key(profile), ("sk-ant-stored-1", "key store"))
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-env-2"
        self.assertEqual(providers.resolve_key(profile), ("sk-ant-env-2", "ANTHROPIC_API_KEY"))
        self.assertEqual(providers.resolve_key({"profile": "my-shop", "provider": "custom"})[0], "sk-generic-key")
        providers.set_key("pasted", "\x16 'sk-pasted-key-9876'\r\n")  # Ctrl+V junk, quotes, CRLF.
        self.assertEqual(providers.load_keys()["pasted"], "sk-pasted-key-9876")
        with self.assertRaisesRegex(ValueError, "Nothing was pasted"):
            providers.set_key("x", "\x16\r\n")
        for bad in ("short", "has space in it", "sk-ключ-123456789"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                providers.set_key("x", bad)
        self.assertTrue(providers.remove_key("anthropic"))
        self.assertFalse(providers.remove_key("anthropic"))

    def test_missing_keys_give_the_fix(self):
        config = {"profile": "deepseek", "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
                  "model": "deepseek-chat"}
        with self.assertRaisesRegex(ValueError, "key set deepseek"):
            providers.make_provider(config)
        self.assertIsInstance(providers.make_provider(config, require_key=False), ChatProvider)
        self.assertIsInstance(providers.make_provider({**config, "provider": "anthropic"}, False), AnthropicProvider)
        self.assertIsInstance(providers.make_provider({**config, "wire": "responses"}, False), ResponsesProvider)

    def test_cli_setup_key_use_and_providers(self):
        code, output = self.run_cli("setup", "--provider", "deepseek")
        self.assertEqual(code, 0, output)
        self.assertIn("key set deepseek", output)
        code, output = self.run_cli("key", "set", "deepseek", stdin="sk-deepseek-abcdef1234\n")
        self.assertEqual(code, 0, output)
        self.assertNotIn("abcdef1234", output)
        self.assertNotIn("sk-deepseek", (self.root / "state" / "config.json").read_text())
        self.assertIn("…1234 from key store", self.run_cli("key", "list")[1])
        code, output = self.run_cli("use", "claude")  # An alias; creates the profile from the preset.
        self.assertEqual(code, 0, output)
        self.assertEqual(load_config(self.root / "state")["model"], "claude-sonnet-5-5")
        self.run_cli("use", "claude", "--model", "claude-opus-5-5")
        self.assertEqual(load_config(self.root / "state", "anthropic")["model"], "claude-opus-5-5")
        listing = self.run_cli("providers")[1]
        self.assertIn("* anthropic", listing)
        self.assertIn("alibaba", listing)
        self.assertEqual(self.run_cli("setup", "--provider", "custom", "--model", "m")[0], 1)
        code, output = self.run_cli("setup", "--provider", "custom", "--name", "shop", "--wire", "anthropic",
                                    "--base-url", "https://api.example-shop.com/v1", "--model", "claude-sonnet-5-5")
        self.assertEqual(code, 0, output)
        self.assertEqual(load_config(self.root / "state")["wire"], "anthropic")
        self.assertEqual(self.run_cli("setup", "--provider", "nope")[0], 1)

    def test_doctor_reports_missing_keys(self):
        self.run_cli("setup", "--provider", "anthropic")
        code, output = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("key set anthropic", output)


if __name__ == "__main__":
    unittest.main()
