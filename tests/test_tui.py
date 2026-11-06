"""Headless tests for the terminal UI. Skipped when Textual is not installed."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("textual") is None:  # The UI is an optional extra: pip install -e ".[tui]"
    raise unittest.SkipTest("Textual is not installed")

from local_agent.agent import Agent
from local_agent.state import State
from local_agent.tui import effects
from local_agent.tui.theme import ASCII, DEFAULT_PREFS, PALETTES, UNICODE, detect_glyphs, detect_motion, load_prefs, save_prefs
from local_agent.tui.widgets import split_reasoning

try:
    from fakes import StreamingFake, call, text
except ImportError:
    from tests.fakes import StreamingFake, call, text

CONFIG = {"model": "demo-model", "base_url": "http://127.0.0.1:11434/v1", "context_chars": 12_000}


class EffectTests(unittest.TestCase):
    def test_logo_rows_are_even(self):
        rows = effects.logo_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({len(row) for row in rows}), 1)
        self.assertEqual(len({len(row) for row in effects.logo_rows(ascii_only=True)}), 1)

    def test_gradient_and_blend(self):
        stops = ["#000000", "#ffffff"]
        self.assertEqual(effects.gradient_at(stops, 0), "#000000")
        self.assertEqual(effects.gradient_at(stops, 1), "#ffffff")
        self.assertEqual(effects.blend("#000000", "#ffffff", 0.5), "#808080")

    def test_frames_are_sized_and_static_without_motion(self):
        palette = PALETTES["neon"]
        orb = effects.render_orb(3.0, palette, 9, 19, motion=False)
        self.assertEqual([line.cell_len for line in orb], [19] * 9)
        self.assertEqual([line.plain for line in orb],
                         [line.plain for line in effects.render_orb(9.0, palette, 9, 19, motion=False)])
        self.assertEqual(effects.glow_line(40, 1.0, palette, True).cell_len, 40)
        self.assertEqual(effects.meter(0.5, 8, palette).plain, "▰▰▰▰▱▱▱▱")

    def test_split_reasoning(self):
        self.assertEqual(split_reasoning("<think>plan</think>\n\nAnswer"), ("plan", "Answer", False))
        self.assertEqual(split_reasoning("<think>still"), ("still", "", True))
        self.assertEqual(split_reasoning("<thi"), ("", "", True))
        self.assertEqual(split_reasoning("Plain"), ("", "Plain", False))


class ThemeTests(unittest.TestCase):
    def test_glyph_and_motion_detection(self):
        self.assertIs(detect_glyphs({}, "win32"), ASCII)
        self.assertIs(detect_glyphs({"WT_SESSION": "1"}, "win32"), UNICODE)
        self.assertIs(detect_glyphs({"AGENT_UI_GLYPHS": "ascii"}, "linux"), ASCII)
        self.assertEqual(detect_motion({"TEXTUAL_ANIMATIONS": "none"}), "off")
        self.assertEqual(detect_motion({"AGENT_UI_MOTION": "reduced"}, "off"), "reduced")
        self.assertEqual(detect_motion({}, "reduced"), "reduced")
        self.assertEqual(detect_motion({}), "full")
        self.assertEqual(detect_motion({"SSH_CONNECTION": "1 2 3 4"}), "reduced")
        self.assertEqual(detect_motion({"SSH_CONNECTION": "1 2 3 4"}, "full"), "full")

    def test_prefs_round_trip_and_reject_bad_values(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            self.assertEqual(load_prefs(folder)["theme"], "neon")
            save_prefs(folder, {"theme": "aurora", "motion": "off", "sidebar": False, "sidebar_width": 40})
            self.assertEqual(load_prefs(folder), {**DEFAULT_PREFS, "theme": "aurora", "motion": "off",
                                                  "sidebar": False, "sidebar_width": 40})
            (folder / "ui.json").write_text('{"theme": "nope", "sidebar_width": 999}')
            self.assertEqual(load_prefs(folder)["theme"], "neon")
            self.assertEqual(load_prefs(folder)["sidebar_width"], 34)


class ClipboardTests(unittest.TestCase):
    SAMPLE = 'def greet():\n    print("សួស្តី — héllo ✓")\n\treturn 1\n'

    def test_windows_text_uses_crlf_once(self):
        from local_agent.tui.clipboard import windows_text
        self.assertEqual(windows_text("a\nb\r\nc\rd"), "a\r\nb\r\nc\r\nd")
        self.assertEqual(windows_text(self.SAMPLE).replace("\r\n", "\n"), self.SAMPLE)

    def test_native_copy_is_used_on_local_windows_consoles(self):
        from local_agent.tui import app as app_module
        app = app_module.AgentApp.__new__(app_module.AgentApp)
        app._clipboard = ""
        app._driver = None
        with mock.patch.object(app_module.os, "name", "nt"), \
                mock.patch.dict(app_module.os.environ, {}, clear=False), \
                mock.patch.object(app_module.clipboard, "copy", return_value=True) as native:
            app_module.os.environ.pop("SSH_CONNECTION", None)
            app.copy_to_clipboard(self.SAMPLE)
        native.assert_called_once_with(self.SAMPLE)
        self.assertEqual(app.clipboard, self.SAMPLE)
        with mock.patch.object(app_module.os, "name", "nt"), \
                mock.patch.dict(app_module.os.environ, {"SSH_CONNECTION": "1 2 3 4"}), \
                mock.patch.object(app_module.clipboard, "copy") as native:
            app.copy_to_clipboard("over ssh")
        native.assert_not_called()  # Over SSH the remote terminal's OSC 52 copy is the right one.
        self.assertEqual(app.clipboard, "over ssh")

    @unittest.skipUnless(os.name == "nt", "Windows clipboard")
    def test_windows_clipboard_round_trip_keeps_unicode_and_indentation(self):
        from local_agent.tui import clipboard
        if not clipboard.copy(self.SAMPLE):
            self.skipTest("The Windows clipboard is not available in this session.")
        self.assertEqual(clipboard.paste(), clipboard.windows_text(self.SAMPLE))


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # unittest runs these loops in asyncio debug mode, which logs every UI frame slower than 0.1 s.
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 5

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = State(self.root / "data")
        self.env = mock.patch.dict(os.environ, {"AGENT_UI_MOTION": "full", "AGENT_UI_GLYPHS": "unicode",
                                                "TERM": "xterm-256color"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def make(self, provider, **options):
        from local_agent.tui.app import AgentApp
        return AgentApp(provider, self.state, self.root / "data", CONFIG, options.pop("session", "s"),
                        prefs={"theme": "neon"}, **options)

    async def wait_idle(self, pilot, app, limit=5.0):
        for _ in range(int(limit / 0.05)):
            await pilot.pause(0.05)
            if not app.busy:
                await pilot.pause(0.05)
                return
        self.fail("The turn did not finish.")

    async def send(self, pilot, app, message):
        from local_agent.tui.widgets import Composer
        app.query_one(Composer).load_text(message)
        await pilot.press("enter")
        await pilot.pause(0.05)

    async def test_streams_a_reply_and_saves_the_turn(self):
        from local_agent.tui.widgets import AssistantMessage, StatusBar, UserMessage, Welcome
        provider = StreamingFake(text("**Hello** there, friend.", reasoning="Say hello."), delay=0.1)
        app = self.make(provider)
        async with app.run_test(size=(120, 40)) as pilot:
            self.assertTrue(app.query(Welcome))
            provider.release.clear()
            await self.send(pilot, app, "Hi")
            self.assertTrue(app.busy)
            self.assertFalse(app.query(Welcome))
            provider.release.set()
            await self.wait_idle(pilot, app)
            reply = app.query_one(AssistantMessage)
            self.assertEqual(reply.raw, "**Hello** there, friend.")
            self.assertIn("Say hello.", reply.reasoning_text)
            self.assertIsNone(reply.thinking)
            self.assertEqual(app.query_one(UserMessage).text, "Hi")
            self.assertEqual(app.query_one(StatusBar).state, "idle")
            self.assertGreater(app.query_one(StatusBar).completion_tokens, 0)
        self.assertEqual(self.state.sessions(), [("s", 1)])
        _, tools = provider.requests[0]
        self.assertEqual([tool["function"]["name"] for tool in tools],
                         ["read_skill", "todo", "session_search", "create_artifact", "list_artifacts", "read_artifact"])

    async def test_attachment_queue_survives_failure_and_clears_after_commit(self):
        notes = self.root / "some notes.txt"
        notes.write_text("Test attachment content", encoding="utf-8")
        resolved_notes = notes.resolve()
        provider = StreamingFake(RuntimeError("Test failure"), text("Received"))
        app = self.make(provider)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, f'/attach "{notes}"')
            self.assertEqual(app.file_commands.pending, [resolved_notes])
            await self.send(pilot, app, "Read the notes")
            await self.wait_idle(pilot, app)
            self.assertEqual(app.file_commands.pending, [resolved_notes])
            await self.send(pilot, app, "Try again")
            await self.wait_idle(pilot, app)
            self.assertFalse(app.file_commands.pending)
        self.assertIn("Test attachment content", provider.requests[1][0][-1]["content"])

    async def test_quitting_during_a_turn_ignores_its_late_events(self):
        provider = StreamingFake(text("A reply that arrives after quitting."), delay=0.01)
        app = self.make(provider)
        async with app.run_test(size=(100, 30)) as pilot:
            provider.release.clear()
            await self.send(pilot, app, "Slow question")
            self.assertTrue(app.busy)
            await pilot.press("ctrl+q")
            provider.release.set()
            await pilot.pause(0.3)
        self.assertEqual(self.state.sessions(), [])  # The cancelled turn saved nothing.

    async def test_copies_dragged_text_and_exact_code_from_a_reply(self):
        from textual.widgets import Button
        from local_agent.tui.widgets import Composer, CopyableCodeBlock

        code = 'if ready:\n    print("សួស្តី")\n'
        reply = "Copy these words.\n\n```python\n" + code + "```\n\n```text\nsecond block\n```"
        provider = StreamingFake(text(reply), text("Next reply"))
        app = self.make(provider)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, "Show the code")
            await self.wait_idle(pilot, app)
            blocks = list(app.query(CopyableCodeBlock))
            self.assertEqual(len(blocks), 2)
            await pilot.click(blocks[1].query_one(Button))
            self.assertEqual(app.clipboard, "second block\n")
            await pilot.click(blocks[0].query_one(Button))
            self.assertEqual(app.clipboard, code)

            provider.release.clear()
            app.query_one(Composer).focus()
            await self.send(pilot, app, "Slow follow-up")
            paragraph = app.query_one("MarkdownParagraph")
            await pilot.mouse_down(paragraph, offset=(0, 0))
            await pilot.mouse_up(paragraph, offset=(9, 0))
            await pilot.press("ctrl+c")
            self.assertEqual(app.clipboard, "Copy these")
            self.assertTrue(app.busy)
            app.screen.clear_selection()
            await pilot.press("ctrl+c")
            self.assertFalse(app.busy)
            provider.release.set()

    async def test_copy_reply_button_and_shortcut_are_available_for_plain_replies(self):
        from local_agent.tui.widgets import Composer

        app = self.make(StreamingFake(text("A reply without any code.")))
        async with app.run_test(size=(80, 24)) as pilot:
            button = app.query_one("#copy-reply")
            self.assertTrue(button.is_on_screen)
            await self.send(pilot, app, "Show a reply")
            await self.wait_idle(pilot, app)
            self.assertTrue(await pilot.click(button))
            self.assertEqual(app.clipboard, "A reply without any code.")
            self.assertIsInstance(app.focused, Composer)
            app.copy_to_clipboard("other text")
            await pilot.press("f4")
            self.assertEqual(app.clipboard, "A reply without any code.")

    async def test_tool_calls_show_status(self):
        from local_agent.tui.widgets import ToolCall
        provider = StreamingFake(call("read_skill", name="missing"), text("No such skill."))
        app = self.make(provider)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, "Load a skill")
            await self.wait_idle(pilot, app)
            tool = app.query_one(ToolCall)
            self.assertEqual(tool.state, "error")
            self.assertIn("unavailable", tool.error)
            await pilot.press("ctrl+o")
            await pilot.pause(0.05)
            self.assertFalse(tool.details.collapsed)

    async def test_cancel_discards_the_turn_and_keeps_input_live(self):
        from local_agent.tui.widgets import Composer
        provider = StreamingFake(text("late"), text("second"))
        provider.release.clear()  # Hold the provider like a slow model.
        app = self.make(provider)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, "Slow question")
            self.assertTrue(app.busy)
            await pilot.press("h", "i")  # Typing still works mid-turn.
            self.assertEqual(app.query_one(Composer).text, "hi")
            await pilot.press("escape")
            await pilot.pause(0.05)
            self.assertFalse(app.busy)
            provider.release.set()
            await pilot.pause(0.3)
            self.assertEqual(self.state.sessions(), [])
            app.query_one(Composer).load_text("")
            await self.send(pilot, app, "Again")
            await self.wait_idle(pilot, app)
        self.assertEqual(self.state.sessions(), [("s", 1)])
        self.assertEqual(self.state.transcript("s").__next__()["content"], "Again")

    async def test_approval_dialog_saves_or_discards(self):
        from local_agent.tui.screens import ApprovalScreen
        for key, expected in (("y", {"style": "concise"}), ("n", {})):
            with self.subTest(key=key):
                self.state = State(self.root / f"data-{key}")
                provider = StreamingFake(call("remember", name="style", value="concise"), text("Noted."))
                app = self.make(provider, learn=True, session=f"approve-{key}")
                async with app.run_test(size=(120, 40)) as pilot:
                    await self.send(pilot, app, "Remember I like concise replies")
                    for _ in range(40):
                        await pilot.pause(0.05)
                        if isinstance(app.screen, ApprovalScreen):
                            break
                    self.assertIsInstance(app.screen, ApprovalScreen)
                    await pilot.press(key)
                    await self.wait_idle(pilot, app)
                self.assertEqual(self.state.library("memory"), expected)
                self.assertEqual(len(self.state.sessions()), 1)

    async def test_errors_render_a_card_and_go_offline(self):
        from local_agent.tui.widgets import ErrorCard, TopBar
        provider = StreamingFake(RuntimeError("Could not reach the model endpoint: refused"))
        app = self.make(provider)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, "Hello?")
            await self.wait_idle(pilot, app)
            card = app.query_one(ErrorCard)
            self.assertEqual(card.title, "Couldn't reach the model")
            self.assertIn("/doctor", card.hint)
            self.assertEqual(app.query_one(TopBar).connection, "offline")
        self.assertEqual(self.state.sessions(), [])

    async def test_non_streaming_provider(self):
        from local_agent.tui.widgets import AssistantMessage
        provider = StreamingFake(text("Whole reply."))
        provider.streaming = False
        app = self.make(provider)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, "Hi")
            await self.wait_idle(pilot, app)
            self.assertEqual(app.query_one(AssistantMessage).raw, "Whole reply.")

    async def wait_for_screen(self, pilot, app, kind):
        for _ in range(60):
            await pilot.pause(0.05)
            if isinstance(app.screen, kind):
                return
        self.fail(f"{kind.__name__} did not open.")

    async def test_action_dialog_allows_or_declines_file_changes(self):
        from local_agent.tui.screens import ActionScreen
        for key, expected in (("y", "new text\n"), ("n", None)):
            with self.subTest(key=key):
                workspace = self.root / f"work-{key}"
                workspace.mkdir()
                provider = StreamingFake(call("write_file", path="notes.txt", content="new text\n"), text("Done."))
                app = self.make(provider, workspace=workspace, write=True, session=f"write-{key}")
                async with app.run_test(size=(120, 40)) as pilot:
                    await self.send(pilot, app, "Write notes.txt")
                    await self.wait_for_screen(pilot, app, ActionScreen)
                    self.assertEqual(app.screen.action["summary"], "Create notes.txt")
                    await pilot.press(key)
                    await self.wait_idle(pilot, app)
                target = workspace / "notes.txt"
                self.assertEqual(target.read_text(encoding="utf-8") if target.exists() else None, expected)

    async def test_long_diffs_scroll_and_keep_the_buttons_visible(self):
        from local_agent.tui.screens import ActionScreen
        from textual.widgets import Button
        workspace = self.root / "work"
        workspace.mkdir()
        content = "".join(f"<p>line {index}</p>\n" for index in range(120))
        for size in ((100, 24), (140, 36)):
            with self.subTest(size=size):
                provider = StreamingFake(call("write_file", path="index.html", content=content), text("Done."))
                app = self.make(provider, workspace=workspace, write=True, session=f"long-{size[1]}")
                async with app.run_test(size=size) as pilot:
                    await self.send(pilot, app, "Write index.html")
                    await self.wait_for_screen(pilot, app, ActionScreen)
                    await pilot.pause(0.2)
                    for button in app.screen.query(Button):
                        self.assertLessEqual(button.region.bottom, size[1], button.id)
                        self.assertGreater(button.region.height, 0, button.id)
                    body = app.screen.query_one(".dialog-body")
                    self.assertGreater(body.max_scroll_y, 0)
                    await pilot.press("pagedown")
                    await pilot.pause(0.1)
                    self.assertGreater(body.scroll_y, 0)
                    await pilot.press("y")
                    await self.wait_idle(pilot, app)
                self.assertEqual((workspace / "index.html").read_text(encoding="utf-8"), content)
                (workspace / "index.html").unlink()

    async def test_dangerous_commands_ask_even_with_auto_approve(self):
        from local_agent.tui.screens import ActionScreen
        from textual.widgets import Button
        workspace = self.root / "work"
        workspace.mkdir()
        provider = StreamingFake(call("terminal", command="rm -rf build"), text("Okay."))
        app = self.make(provider, workspace=workspace, shell=True, auto_approve=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, "Clean up")
            await self.wait_for_screen(pilot, app, ActionScreen)
            self.assertEqual(app.focused.id, "decline")
            self.assertIsInstance(app.focused, Button)
            await pilot.press("enter")  # Enter on the focused Decline button.
            await self.wait_idle(pilot, app)
        result = [m for m in self.state.transcript("s") if m["role"] == "tool"][0]
        self.assertIn("declined", result["content"])

    async def test_cancel_closes_the_action_dialog(self):
        from local_agent.tui.screens import ActionScreen
        workspace = self.root / "work"
        workspace.mkdir()
        provider = StreamingFake(call("terminal", command="echo hi"), text("Ran."))
        app = self.make(provider, workspace=workspace, shell=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, "Say hi")
            await self.wait_for_screen(pilot, app, ActionScreen)
            await app.cancel_turn()
            await pilot.pause(0.3)
            self.assertNotIsInstance(app.screen, ActionScreen)
            self.assertFalse(app.busy)
        self.assertEqual(self.state.sessions(), [])

    async def test_action_toggles_and_new_commands(self):
        self.state.commit_turn("s", 0, [{"role": "user", "content": "Plan the Lisbon trip"}], {},
                               todo=["[x] book flights", "[ ] pack"])
        self.state.update_library({("memory", "style"): "concise"})
        app = self.make(StreamingFake())
        shown = []
        original = app.note
        app.note = lambda markdown, title: shown.append(markdown) or original(markdown, title)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, "/write")
            self.assertFalse(app.write)  # No workspace.
            await self.send(pilot, app, "/web on")
            self.assertTrue(app.web)
            self.assertIn("web", app.actions_label())
            await self.send(pilot, app, "/todo")
            await self.send(pilot, app, "/search lisbon")
            await self.send(pilot, app, "/forget style")
            await pilot.pause(0.1)
            notes = " ".join(shown)
            self.assertIn("book flights", notes)
            self.assertIn("Lisbon", notes)
        self.assertEqual(self.state.library("memory"), {})

    async def test_slash_commands(self):
        from local_agent.tui.screens import HelpScreen
        from local_agent.tui.widgets import NoteCard, StatusBar, TopBar
        app = self.make(StreamingFake())
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, "/theme aurora")
            self.assertEqual(app.theme, "agent-aurora")
            self.assertEqual(load_prefs(self.root / "data")["theme"], "aurora")
            await self.send(pilot, app, "/motion off")
            self.assertEqual(app.motion, "off")
            self.assertIsNone(app.clock.timer)
            await self.send(pilot, app, "/learn")
            self.assertTrue(app.query_one(StatusBar).learn)
            await self.send(pilot, app, "/memory")
            self.assertTrue(app.query(NoteCard))
            await self.send(pilot, app, "/new other")
            self.assertEqual(app.session, "other")
            self.assertEqual(app.query_one(TopBar).session, "other")
            await self.send(pilot, app, "/bogus")
            self.assertEqual(app.session, "other")
            await self.send(pilot, app, "/help")
            self.assertIsInstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await self.send(pilot, app, "/exit")
        self.assertEqual(self.state.sessions(), [])

    async def test_provider_and_model_switching(self):
        from local_agent.config import load_config, save_config
        from local_agent.provider_anthropic import AnthropicProvider
        from local_agent.tui.widgets import NoteCard, TopBar
        data = self.root / "data"
        save_config(data, "http://127.0.0.1:9/v1", "local-model", 12_000, provider="ollama")
        save_config(data, "http://127.0.0.1:9/v1", "claude-x", 50_000, provider="custom", wire="anthropic", name="shop")
        app = self.make(StreamingFake())
        with mock.patch.dict(os.environ, {"AGENT_KEYS_FILE": str(self.root / "keys.json")}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self.send(pilot, app, "/provider")
                self.assertTrue(app.query(NoteCard))
                await self.send(pilot, app, "/provider shop")
                self.assertIsInstance(app.provider, AnthropicProvider)
                self.assertEqual(app.query_one(TopBar).model, "claude-x")
                self.assertEqual(app.config["context_chars"], 50_000)
                await self.send(pilot, app, "/model claude-y")
                self.assertEqual(app.provider.model, "claude-y")
                self.assertEqual(load_config(data)["model"], "claude-y")
                await self.send(pilot, app, "/provider nope")
                self.assertEqual(app.config["profile"], "shop")
                await self.send(pilot, app, "/provider ollama")
                self.assertEqual(app.provider.model, "local-model")
                await self.send(pilot, app, "/exit")

    async def test_autocomplete_newlines_and_history(self):
        from local_agent.tui.widgets import Composer
        app = self.make(StreamingFake(text("one")))
        async with app.run_test(size=(120, 40)) as pilot:
            composer = app.query_one(Composer)
            await pilot.press("/", "t", "h")
            await pilot.pause(0.05)
            self.assertTrue(app.query_one("#suggestions").display)
            await pilot.press("tab")
            self.assertEqual(composer.text, "/theme")
            composer.load_text("")
            await pilot.press("a", "ctrl+j", "b")
            self.assertEqual(composer.text, "a\nb")
            self.assertFalse(app.busy)
            await pilot.press("enter")
            await self.wait_idle(pilot, app)
            self.assertEqual(composer.text, "")
            await pilot.press("up")
            self.assertEqual(composer.text, "a\nb")

    async def test_file_mentions_and_preview(self):
        from local_agent.tui.screens import FilePreview
        from local_agent.tui.widgets import Composer
        workspace = self.root / "ws"
        (workspace / "docs").mkdir(parents=True)
        (workspace / "docs" / "notes.md").write_text("# Notes\n", encoding="utf-8")
        app = self.make(StreamingFake(), workspace=workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            composer = app.query_one(Composer)
            await pilot.press("@", "n", "o", "t")
            await pilot.pause(0.05)
            await pilot.press("tab")
            self.assertEqual(composer.text, "@docs/notes.md ")
            app.action_open_ref(app.register_ref("docs/notes.md"))
            await pilot.pause(0.05)
            self.assertIsInstance(app.screen, FilePreview)
            await pilot.press("escape")
            app.action_open_ref(app.register_ref("../outside"))
            await pilot.pause(0.05)
            self.assertNotIsInstance(app.screen, FilePreview)

    async def test_resize_and_sidebar(self):
        from local_agent.tui.widgets import Sidebar
        app = self.make(StreamingFake())
        async with app.run_test(size=(140, 44)) as pilot:
            sidebar = app.query_one(Sidebar)
            self.assertTrue(sidebar.display)
            await pilot.resize_terminal(70, 20)
            await pilot.pause(0.1)
            self.assertFalse(sidebar.display)
            self.assertTrue(app.screen.has_class("-narrow"))
            await pilot.press("ctrl+b")
            await pilot.pause(0.4)
            self.assertTrue(sidebar.display)
            self.assertTrue(sidebar.has_class("-overlay"))
            await pilot.press("escape")
            await pilot.pause(0.4)
            self.assertFalse(sidebar.display)
            await pilot.resize_terminal(50, 12)
            await pilot.pause(0.1)
            self.assertTrue(app.screen.has_class("-tiny"))
            await pilot.resize_terminal(140, 44)
            await pilot.pause(0.1)
            self.assertTrue(sidebar.display)

    async def test_history_is_rendered_on_resume(self):
        from local_agent.tui.widgets import AssistantMessage, ToolCall, UserMessage, Welcome

        class Scripted:
            def __init__(self, *replies):
                self.replies = list(replies)

            def complete(self, messages, tools):
                return self.replies.pop(0)

        Agent(Scripted(call("read_skill", name="x"), text("Earlier answer.")), self.state).turn("s", "Earlier")
        app = self.make(StreamingFake())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.1)
            self.assertFalse(app.query(Welcome))
            self.assertEqual(app.query_one(UserMessage).text, "Earlier")
            self.assertEqual(app.query_one(AssistantMessage).raw, "Earlier answer.")
            self.assertEqual(app.query_one(ToolCall).state, "error")

    # ── Holographic core ──────────────────────────────────────────────────────────────────

    async def test_hologram_follows_the_turn(self):
        from local_agent.tui.widgets import Welcome
        provider = StreamingFake(call("read_skill", name="missing"), text("A streamed answer " * 8), delay=0.02)
        app = self.make(provider)
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause(0.2)
            self.assertTrue(app.query(Welcome))
            self.assertTrue(app.query_one("#hero-holo").display)
            self.assertFalse(app.query_one("#holo-panel").display)  # The welcome has the big one.
            seen, rounds, flashes = [], [], []
            original_state, original_round, original_flash = app.holo.set_state, app.holo.tool_round, app.holo.flash
            app.holo.set_state = lambda state, detail="": (seen.append(state), original_state(state, detail))
            app.holo.tool_round = lambda total: (rounds.append(total), original_round(total))
            app.holo.flash = lambda kind: (flashes.append(kind), original_flash(kind))
            provider.release.clear()
            await self.send(pilot, app, "Use a tool")
            self.assertEqual(app.holo.state, "thinking")
            self.assertTrue(app.query_one("#holo-panel").display)
            self.assertTrue(app.query_one("#side-holo").animating)
            provider.release.set()
            await self.wait_idle(pilot, app)
            self.assertIn("tool", seen)
            self.assertIn("generating", seen)
            self.assertEqual(rounds, [1])
            self.assertEqual(app.holo.state, "idle")
            self.assertEqual(flashes, ["glitch", "success"])  # The missing skill failed; the turn did not.
            self.assertEqual((app.holo.tools_total, app.holo.tools_done), (0, 0))

    async def test_hologram_error_flash(self):
        app = self.make(StreamingFake(RuntimeError("Could not reach the model endpoint: refused")))
        async with app.run_test(size=(140, 44)) as pilot:
            flashes, original = [], app.holo.flash
            app.holo.flash = lambda kind: (flashes.append(kind), original(kind))
            await self.send(pilot, app, "Hello?")
            await self.wait_idle(pilot, app)
            self.assertEqual(flashes, ["error"])
            self.assertEqual(app.holo.state, "idle")
            app.holo.flash("error")  # While the flash is live, the header orb turns red too.
            self.assertEqual(app.hologram_color(), PALETTES["neon"]["error"])

    async def test_hologram_commands_and_layout(self):
        from local_agent.tui.widgets import NoteCard
        app = self.make(StreamingFake(text("ok")))
        async with app.run_test(size=(140, 44)) as pilot:
            await self.send(pilot, app, "Hi")
            await self.wait_idle(pilot, app)
            panel, view = app.query_one("#holo-panel"), app.query_one("#side-holo")
            self.assertTrue(panel.display)
            self.assertEqual(app.hologram_renderer, "braille")
            await self.send(pilot, app, "/hologram off")
            await pilot.pause(0.1)
            self.assertFalse(panel.display)
            self.assertFalse(view.animating)
            self.assertNotIn(view, app.clock.subscribers)  # Hidden means no frames at all.
            self.assertFalse(load_prefs(self.root / "data")["hologram"])
            await pilot.press("f3")
            await pilot.pause(0.1)
            self.assertTrue(panel.display)
            self.assertTrue(view.animating)
            await self.send(pilot, app, "/hologram large")
            await pilot.pause(0.1)
            self.assertEqual(view.size.width, 40)
            await self.send(pilot, app, "/hologram ascii")
            await pilot.pause(0.1)
            self.assertEqual(app.query_one("#side-holo").mode, "ascii")
            self.assertEqual(load_prefs(self.root / "data")["hologram_mode"], "ascii")
            await self.send(pilot, app, "/hologram image")  # Not probed at start-up: falls back.
            self.assertEqual(app.hologram_renderer, "braille")
            await self.send(pilot, app, "/hologram still")
            await pilot.pause(0.1)
            self.assertFalse(app.query_one("#side-holo").animating)
            await self.send(pilot, app, "/hologram live")
            await pilot.press("alt+right")
            self.assertGreater(app.holo_control.yaw, 0)
            await self.send(pilot, app, "/hologram reset")
            self.assertEqual(app.holo_control.yaw, 0)
            notes = len(app.query(NoteCard))
            await self.send(pilot, app, "/hologram status")
            self.assertEqual(len(app.query(NoteCard)), notes + 1)
            self.assertIn("side panel", app.hologram_status())
            await self.send(pilot, app, "/hologram 99")
            self.assertEqual(app.hologram_width, 40)
            await pilot.resize_terminal(90, 24)
            await pilot.pause(0.2)
            self.assertTrue(panel.display)  # The sidebar gives way first.
            await pilot.resize_terminal(70, 20)
            await pilot.pause(0.2)
            self.assertFalse(panel.display)
            await pilot.press("ctrl+b")  # Overlay sidebar on a narrow screen must not squeeze the chat.
            await pilot.pause(0.4)
            self.assertGreaterEqual(app.query_one("#center").size.width, 30)

    async def test_hologram_motion_levels(self):
        app = self.make(StreamingFake(text("ok")))
        async with app.run_test(size=(140, 44)) as pilot:
            await app.hide_welcome()
            await pilot.pause(0.1)
            view = app.query_one("#side-holo")
            await self.send(pilot, app, "/motion reduced")
            await pilot.pause(0.1)
            self.assertFalse(view.animating)  # Reduced: still while idle...
            app.set_state("thinking", "")
            await pilot.pause(0.1)
            self.assertTrue(view.animating)  # ...and a slow status indicator while busy.
            self.assertLessEqual(view.governor.fps, 4)
            await self.send(pilot, app, "/motion off")
            await pilot.pause(0.1)
            self.assertFalse(view.animating)
            self.assertIsNone(app.clock.timer)
            self.assertTrue(view.strips)  # A static frame is still drawn.

    async def test_audio_levels_from_another_thread(self):
        import threading
        app = self.make(StreamingFake())
        async with app.run_test(size=(140, 44)) as pilot:
            worker = threading.Thread(target=app.set_audio_level, args=(0.8,))
            worker.start()
            worker.join(2)  # Never blocks, even while the event loop is waiting here.
            await pilot.pause(0.1)
            self.assertEqual(app.holo.effective_state(), "voice")

    async def test_image_renderer_and_runtime_fallback(self):
        if importlib.util.find_spec("textual_image") is None or importlib.util.find_spec("PIL") is None:
            self.skipTest("The hologram-image extra is not installed")
        from textual_image.widget import HalfcellImage
        from local_agent.tui.app import AgentApp
        from local_agent.tui.holoview import HologramImage
        support = {"protocol": "sixel", "widget": HalfcellImage, "cell_size": (10, 20), "error": None}
        app = AgentApp(StreamingFake(), self.state, self.root / "data", CONFIG, "s",
                       prefs={"theme": "neon", "hologram_mode": "image"}, image_support=support)
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause(0.4)
            self.assertEqual(app.hologram_renderer, "image")
            view = app.query_one("#hero-holo")
            self.assertIsInstance(view, HologramImage)
            self.assertIsNotNone(view.picture.image)
            app.hologram_failed("image renderer failed: test")
            await pilot.pause(0.3)
            self.assertEqual(app.hologram_renderer, "braille")
            self.assertNotIsInstance(app.query_one("#hero-holo"), HologramImage)


if __name__ == "__main__":
    unittest.main()
