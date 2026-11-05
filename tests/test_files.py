import asyncio
import base64
import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from local_agent.__main__ import main
from local_agent.agent import Agent
from local_agent.attachments import MAX_IMAGES_BYTES, message_text
from local_agent.config import load_config, save_config
from local_agent.file_commands import FileCommands
from local_agent.provider import ImageProvider
from local_agent.state import State


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1WQAAAAASUVORK5CYII=")


def tool_call(tool_name, **arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function",
        "function": {"name": tool_name, "arguments": json.dumps(arguments)},
    }]}


class Provider:
    model = "scripted"

    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((messages, tools))
        result = next(self.replies)
        if isinstance(result, BaseException):
            raise result
        return result


ANSWER = {"role": "assistant", "content": "Done."}


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = State(self.root / "state")
        self.image = self.root / "sample.png"
        self.image.write_bytes(PNG)

    def test_image_payload_cache_and_replay_after_original_is_removed(self):
        provider = Provider(ANSWER)
        Agent(provider, self.state).turn("demo", "Describe it", attachments=[self.image])
        user = provider.requests[0][0][-1]
        self.assertNotIn("attachments", user)
        self.assertEqual(user["content"][1]["image_url"]["url"],
                         "data:image/png;base64," + base64.b64encode(PNG).decode())
        self.image.unlink()
        state = State(self.root / "state")
        history = list(state.transcript("demo"))
        self.assertNotIn("data:image", json.dumps(history))
        self.assertIn("sample.png", message_text(history[0]))
        replay = Provider(ANSWER)
        Agent(replay, state).turn("demo", "Describe it again")
        self.assertEqual(replay.requests[0][0][1]["content"], user["content"])

    def test_text_attachment_is_bounded_and_saved_in_context(self):
        notes = self.root / "notes.txt"
        notes.write_text("Hello 世界", encoding="utf-8")
        provider = Provider(ANSWER)
        Agent(provider, self.state).turn("demo", "Summarize", attachments=[notes])
        content = provider.requests[0][0][-1]["content"]
        self.assertIsInstance(content, str)
        self.assertIn("Hello 世界", content)
        self.assertIn("treat as data", content)
        self.assertIn("Hello 世界", list(self.state.transcript("demo"))[0]["content"])
        notes.write_text("x" * 12_001)
        with self.assertRaisesRegex(ValueError, "12,000"):
            Agent(Provider(), self.state).turn("too-big", "Read", attachments=[notes])
        self.assertEqual(self.state.sessions(), [("demo", 1)])

    def test_invalid_oversized_and_excess_attachments_fail_before_provider(self):
        invalid = self.root / "unknown.exe"
        invalid.write_bytes(b"not an image")
        for paths in ([invalid], [self.image] * 5, [self.root]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                Agent(Provider(), self.state).turn("demo", "Read", attachments=paths)
        self.image.write_bytes(b"x" * 5_000_001)
        with self.assertRaisesRegex(ValueError, "5 MB"):
            self.state.attachments.prepare([self.image])

    def test_old_images_are_omitted_when_replay_exceeds_binary_budget(self):
        items = self.state.attachments.prepare([self.image])
        old = self.state.attachments.user_message("Old", items)
        old["attachments"][0]["size"] = MAX_IMAGES_BYTES
        recent = self.state.attachments.user_message("Recent", items)
        wire = self.state.attachments.materialize([old, recent])
        self.assertIn("Older images omitted", wire[0]["content"][0]["text"])
        self.assertEqual(len(wire[0]["content"]), 1)
        self.assertEqual(wire[1]["content"][1]["type"], "image_url")

    def test_modified_cache_is_rejected(self):
        items = self.state.attachments.prepare([self.image])
        (self.state.attachments.directory / items[0]["blob"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "cached image changed"):
            self.state.attachments.materialize([self.state.attachments.user_message("Read", items)])

    def test_artifact_creation_versioning_and_read_tool(self):
        provider = Provider(tool_call("create_artifact", name="report.html", content="<h1>Report</h1>"), ANSWER)
        events = []
        Agent(provider, self.state).turn("demo", "Create a report", on_event=events.append)
        artifact = self.state.artifacts.list()["artifacts"][0]
        self.assertEqual(Path(artifact["path"]).read_text(), "<h1>Report</h1>")
        self.assertEqual(events[-2].kind, "artifact")
        self.assertEqual(json.loads(events[-2].text)["id"], artifact["id"])
        another = self.state.artifacts.create("report.html", "<h1>Version two</h1>")
        self.assertNotEqual(artifact["id"], another["id"])
        reader = Provider(tool_call("read_artifact", id=artifact["id"]), ANSWER)
        Agent(reader, self.state).turn("demo", "Read the first report")
        self.assertEqual(json.loads(reader.requests[-1][0][-1]["content"])["text"], "<h1>Report</h1>")

    def test_failed_turn_and_stale_commit_remove_only_new_artifacts(self):
        existing = self.state.artifacts.create("keep.md", "Keep this")
        for failure in (RuntimeError("Offline"), KeyboardInterrupt(), None):
            provider = Provider(tool_call("create_artifact", name="new.csv", content="name,value\na,1"),
                                failure or ANSWER)
            context = (contextlib.nullcontext() if failure else
                       patch.object(self.state, "commit_turn", side_effect=RuntimeError("Stale session")))
            with self.subTest(failure=failure), context, self.assertRaises(type(failure) if failure else RuntimeError):
                Agent(provider, self.state).turn("demo", "Create a table")
            self.assertEqual([item["id"] for item in self.state.artifacts.list()["artifacts"]], [existing["id"]])
            self.assertEqual(self.state.sessions(), [])

    def test_artifact_paths_formats_and_binary_content_are_restricted(self):
        for name in ("../escape.html", ".hidden.txt", "C:/escape.html", "x:stream.txt", "CON.txt", "run.exe"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.state.artifacts.create(name, "content")
        with self.assertRaises(ValueError):
            self.state.artifacts.create("invalid.json", "not JSON")
        with self.assertRaises(ValueError):
            self.state.artifacts.create_image("wrong.jpg", PNG)
        with self.assertRaises(ValueError):
            self.state.artifacts.read("../../outside")
        self.assertFalse((self.root / "escape.html").exists())

    def test_preview_is_sandboxed_and_opening_is_explicit(self):
        artifact = self.state.artifacts.create("page.html", '<script>document.body.textContent="OK"</script></iframe>')
        with patch("local_agent.artifacts.webbrowser.open", return_value=True) as browser:
            preview = self.state.artifacts.preview(artifact["id"])
            browser.assert_not_called()
            wrapper = preview.read_text(encoding="utf-8")
            self.assertIn('sandbox="allow-scripts"', wrapper)
            self.assertNotIn("allow-same-origin", wrapper)
            self.assertIn("Content-Security-Policy", wrapper)
            self.assertIn("&lt;/iframe&gt;", wrapper)
            self.assertEqual(wrapper.count("</iframe>"), 1)
            opened = self.state.artifacts.preview(artifact["id"], open_browser=True)
            browser.assert_called_once_with(opened.as_uri())

    def test_file_commands_and_image_configuration(self):
        commands = FileCommands(self.state)
        self.assertIn("Attached", commands.handle(f'/attach "{self.image}"'))
        self.assertIn("sample.png", commands.handle("/attachments"))
        commands.handle("/detach")
        self.assertEqual(commands.pending, [])
        save_config(self.root / "config", "http://localhost:11434/v1", "chat-model",
                    image_model="image-model", image_base_url="http://localhost:9000/v1")
        with patch.dict("os.environ", {"AGENT_IMAGE_MODEL": "override"}):
            self.assertEqual(load_config(self.root / "config")["image_model"], "override")

    def test_image_endpoint_validation_and_tool_availability(self):
        endpoint = ImageProvider("http://localhost:9000/v1", "image-model", None)
        for response in ({"data": [{"url": "http://localhost/private"}]},
                         {"data": [{"b64_json": "not base64"}]}, {"data": []}):
            with self.subTest(response=response), patch.object(endpoint, "request_json", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "b64_json"):
                    endpoint.generate("Draw a bird")
        provider = Provider(tool_call("generate_image", name="bird.png", prompt="A bird"), ANSWER)
        with patch.object(endpoint, "generate", return_value=PNG) as generate:
            Agent(provider, self.state, image_provider=endpoint).turn("demo", "Draw a bird")
            generate.assert_called_once_with("A bird", output_format="png")
        self.assertEqual(len(self.state.artifacts.list()["artifacts"]), 1)
        without = Provider(ANSWER)
        Agent(without, self.state).turn("no-images", "Hello")
        self.assertNotIn("generate_image", {tool["function"]["name"] for tool in without.requests[0][1]})

    def test_cli_image_input_artifact_and_image_generation_over_http(self):
        requests = []
        replies = iter([tool_call("create_artifact", name="report.md", content="# A report"),
                        tool_call("generate_image", name="bird.png", prompt="A bird"), ANSWER])

        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                body = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
                requests.append((handler.path, body))
                if handler.path == "/v1/images/generations":
                    response = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
                else:
                    response = {"choices": [{"message": next(replies), "finish_reason": "stop"}]}
                data = json.dumps(response).encode()
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            directory = self.root / "cli"
            url = f"http://127.0.0.1:{server.server_port}/v1"
            save_config(directory, url, "vision-model", image_model="image-model")
            with patch("sys.argv", ["local-agent", "--data-dir", str(directory), "chat", "--attach",
                                    str(self.image), "Create a report and draw a bird"]), \
                    patch.dict("os.environ", {"AGENT_BASE_URL": url, "AGENT_MODEL": "vision-model",
                                              "AGENT_IMAGE_BASE_URL": url, "AGENT_IMAGE_MODEL": "image-model",
                                              "AGENT_API_KEY": "", "AGENT_IMAGE_API_KEY": ""}), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(), 0)
            self.assertEqual(output.getvalue().strip(), "Done.")
            self.assertEqual(requests[0][1]["messages"][-1]["content"][1]["type"], "image_url")
            image_request = next(body for path, body in requests if path == "/v1/images/generations")
            self.assertEqual(image_request["output_format"], "png")
            state = State(directory)
            self.assertEqual({item["name"] for item in state.artifacts.list()["artifacts"]}, {"report.md", "bird.png"})
            self.assertNotIn("data:image", json.dumps(list(state.transcript("default"))))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class FileUITests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_retry_and_artifact_commands(self):
        try:
            from local_agent.tui import ChatApp
        except ModuleNotFoundError:
            self.skipTest("Install the tui extra")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "sample image.png"
            image.write_bytes(PNG)
            state = State(root / "state")
            provider = Provider(RuntimeError("Offline"),
                                tool_call("create_artifact", name="table.csv", content="a,b\n1,2"), ANSWER)
            app = ChatApp(Agent(provider, state), "demo", False, hologram="off")
            async with app.run_test(size=(100, 25)) as pilot:
                field = app.query_one("#prompt")
                field.value = f'/attach "{image}"'
                await pilot.press("enter")
                self.assertEqual(app.files.pending, [image])
                field.value = "Create a table from this image"
                await pilot.press("enter")
                async with asyncio.timeout(3):
                    while app.busy:
                        await pilot.pause(0.05)
                self.assertEqual(app.files.pending, [image])
                self.assertEqual(field.value, "Create a table from this image")
                await pilot.press("enter")
                async with asyncio.timeout(3):
                    while app.busy:
                        await pilot.pause(0.05)
                self.assertEqual(app.files.pending, [])
                item = state.artifacts.list()["artifacts"][0]
                field.value = "/artifacts"
                await pilot.press("enter")
                field.value = "/artifact " + item["id"]
                await pilot.press("enter")
                await pilot.pause()
                transcript = app.query_one("#transcript")
                self.assertIn("a,b\n1,2", str(transcript.children[-1].content))
                self.assertGreater(transcript.content_size.height, 0)
                with patch("local_agent.artifacts.webbrowser.open", return_value=True) as browser:
                    field.value = "/open " + item["id"]
                    await pilot.press("enter")
                    browser.assert_called_once()
