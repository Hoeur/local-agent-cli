import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent import provider_anthropic, provider_responses, providers, tools
from local_agent.agent import Agent, TurnCancelled
from local_agent.config import load_config
from local_agent.file_commands import FileCommands
from local_agent.provider import ImageProvider
from local_agent.state import State

from fakes import StreamingFake, call, text


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aW2QAAAAASUVORK5CYII=")


class FileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = State(self.root / "data")

    def test_attachment_replays_without_original_file_or_inline_storage(self):
        image = self.root / "tiny.png"
        image.write_bytes(PNG)
        notes = self.root / "notes.txt"
        notes.write_text("Attachment test data", encoding="utf-8")
        provider = StreamingFake(text("First reply"), text("Second reply"))
        agent = Agent(provider, self.state, max_rounds=0)
        agent.turn("test", "Read both", attachments=[image, notes])
        image.unlink()
        agent.turn("test", "Recall the image")
        first_user = provider.requests[0][0][1]
        self.assertEqual(provider.requests[0][1], [])
        self.assertEqual(provider.requests[1][0][1], first_user)
        self.assertIn("Attachment test data", first_user["content"][0]["text"])
        data_url = first_user["content"][1]["image_url"]["url"]
        saved = list(self.state.transcript("test"))[0]
        self.assertIn("blob", saved["attachments"][0])
        self.assertNotIn(data_url, json.dumps(saved))
        response = provider_responses.convert_messages([first_user])[0]["content"]
        self.assertEqual(response[1], {"type": "input_image", "image_url": data_url, "detail": "auto"})
        anthropic = provider_anthropic.convert_messages([first_user])[0]["content"]
        self.assertEqual(anthropic[1]["source"], {
            "type": "base64", "media_type": "image/png", "data": data_url.split(",")[1],
        })

    def test_artifact_commands_and_isolated_static_preview(self):
        provider = StreamingFake(call("create_artifact", name="report.html", content="<h1>Sample</h1>"), text("Saved"))
        Agent(provider, self.state).turn("test", "Save a report")
        files = FileCommands(self.state)
        artifact = self.state.artifacts.list()["artifacts"][0]
        self.assertIn(artifact["id"], files.handle("/artifacts"))
        self.assertIn("<h1>Sample</h1>", files.handle(f"/artifact {artifact['id']}"))
        preview = self.state.artifacts.preview(artifact["id"]).read_text(encoding="utf-8")
        self.assertIn("<iframe sandbox ", preview)
        self.assertNotIn("allow-scripts", preview)
        self.assertIn("&lt;h1&gt;Sample&lt;/h1&gt;", preview)

    def test_cancelled_turn_removes_only_its_artifacts(self):
        kept = self.state.artifacts.create("report.txt", "Previous artifact")
        provider = StreamingFake(call("create_artifact", name="report.txt", content="Uncommitted"), text("Saved"))

        def cancel(event, _data):
            if event == "commit":
                raise TurnCancelled("Cancelled")

        with self.assertRaises(TurnCancelled):
            Agent(provider, self.state, on_event=cancel).turn("test", "Create a report")
        self.assertEqual(self.state.artifacts.list()["artifacts"], [kept])
        self.assertFalse(list(self.state.transcript("test")))

    def test_post_commit_cancellation_keeps_the_artifact(self):
        provider = StreamingFake(call("todo", "tasks", items="[ ] Model task"),
                                 call("create_artifact", "artifact", name="report.txt", content="Report"),
                                 text("Saved"))

        def cancel(event, _data):
            if event == "commit":
                self.state.save_todo("test", ["[ ] User task"], 0)
            if event == "todo_kept":
                raise TurnCancelled("Cancelled after commit")

        with self.assertRaises(TurnCancelled):
            Agent(provider, self.state, on_event=cancel).turn("test", "Create a report")
        self.assertTrue(list(self.state.transcript("test")))
        self.assertEqual(self.state.todo("test"), ["[ ] User task"])
        self.assertEqual([item["name"] for item in self.state.artifacts.list()["artifacts"]], ["report.txt"])

    def test_generated_image_returns_inline_bytes_and_is_saved_as_an_artifact(self):
        image = ImageProvider("http://localhost:8080/v1", "image-model", None)
        provider = StreamingFake(call("generate_image", name="result.png", prompt="A square"), text("Saved"))
        with patch.object(image, "request_json", return_value={"data": [{"b64_json": base64.b64encode(PNG).decode()}]}) as request:
            Agent(provider, self.state, image_provider=image).turn("test", "Generate an image")
        self.assertEqual(request.call_args.args[0].full_url, "http://localhost:8080/v1/images/generations")
        self.assertEqual(json.loads(request.call_args.args[0].data)["output_format"], "png")
        artifact = self.state.artifacts.list()["artifacts"][0]
        self.assertEqual(Path(artifact["path"]).read_bytes(), PNG)
        with patch.object(image, "request_json", return_value={"data": [{"url": "https://example.com/img"}]}):
            with self.assertRaisesRegex(RuntimeError, "base64"):
                image.generate("A square", output_format="png")

    def test_legacy_image_config_never_forwards_chat_key_to_another_endpoint(self):
        data_dir = self.state.path.parent
        (data_dir / "config.json").write_text(json.dumps({
            "base_url": "https://chat.example/v1", "model": "chat-model",
            "image_base_url": "https://image.example/v1", "image_model": "image-model",
        }), encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), patch.object(providers, "resolve_key", return_value=("secret", "test")):
            config = load_config(data_dir)
            self.assertNotIn("Authorization", providers.make_image_provider(config).headers)
            config["image_base_url"] = config["base_url"]
            self.assertEqual(providers.make_image_provider(config).headers["Authorization"], "Bearer secret")

    def test_search_uses_labeled_wikipedia_fallback_after_challenge(self):
        feed = json.dumps({"pages": [{"key": "Python_Software_Foundation", "title": "Python Software Foundation"}]})
        with patch.object(tools, "fetch", side_effect=[("", "", '<form id="challenge-form">'), ("", "", feed)]):
            result = tools.web_search("Python Software Foundation", search_url=tools.SEARCH_URL)
        self.assertIn("English Wikipedia", result["note"])
        self.assertEqual(result["results"][0]["url"], "https://en.wikipedia.org/wiki/Python_Software_Foundation")
