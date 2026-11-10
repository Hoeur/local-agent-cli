"""Headless tests for the project features of the terminal UI. Skipped without Textual.

These drive the app through Textual's test pilot at fixed terminal sizes. They check behavior and
layout geometry; they are not a substitute for trying the interface in a real terminal."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("textual") is None:
    raise unittest.SkipTest("Textual is not installed")

from local_agent.projects import KNOWLEDGE_DIR, ProjectRegistry
from local_agent.state import State

try:
    from fakes import StreamingFake, text
except ImportError:
    from tests.fakes import StreamingFake, text

CONFIG = {"model": "demo-model", "base_url": "http://127.0.0.1:11434/v1", "context_chars": 12_000}


class ProjectAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 5

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.state = State(self.data)
        self.registry = ProjectRegistry(self.data)
        for name in ("alpha", "beta"):
            (self.root / name).mkdir()
        self.alpha = self.registry.add(self.root / "alpha", "Alpha")
        self.beta = self.registry.add(self.root / "beta", "Beta")
        env = mock.patch.dict(os.environ, {"AGENT_UI_MOTION": "off", "AGENT_UI_GLYPHS": "unicode",
                                           "TERM": "xterm-256color"})
        env.start()
        self.addCleanup(env.stop)

    def make(self, provider, project=None, **options):
        from local_agent.tui.app import AgentApp
        state = self.state.for_project(project.id) if project else self.state
        workspace = project.path if project else options.pop("workspace", None)
        return AgentApp(provider, state, self.data, CONFIG, options.pop("session", "default"), workspace,
                        prefs={"theme": "neon", "hologram": False}, project=project, **options)

    async def send(self, pilot, app, message):
        from local_agent.tui.widgets import Composer
        app.query_one(Composer).load_text(message)
        await pilot.press("enter")
        await pilot.pause(0.05)

    async def wait_idle(self, pilot, app, limit=5.0):
        for _ in range(int(limit / 0.05)):
            await pilot.pause(0.05)
            if not app.busy:
                await pilot.pause(0.05)
                return
        self.fail("The turn did not finish.")

    async def wait_for(self, pilot, condition, limit=3.0):
        for _ in range(int(limit / 0.05)):
            if condition():
                return
            await pilot.pause(0.05)
        self.fail("Condition not reached.")

    def composer(self, app):
        from local_agent.tui.widgets import Composer
        return app.query_one(Composer)

    async def test_register_and_open_a_project_from_the_switcher(self):
        from textual.widgets import Input
        from local_agent.tui.project_screens import ProjectHome, ProjectsScreen, PromptScreen
        from local_agent.tui.widgets import TopBar
        third = self.root / "gamma"
        third.mkdir()
        app = self.make(StreamingFake())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f5")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ProjectsScreen)
            await pilot.press("a")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, PromptScreen)
            app.screen.query_one("#field-folder", Input).value = str(third)
            app.screen.query_one("#field-name", Input).value = "Gamma"
            await pilot.press("enter", "enter")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, ProjectsScreen)
            self.assertIn("gamma", [project.id for project in self.registry.projects()])
            await pilot.press("enter")  # The new project is highlighted.
            await self.wait_for(pilot, lambda: isinstance(app.screen, ProjectHome))
            self.assertEqual(app.project.id, "gamma")
            self.assertEqual(app.workspace, third.resolve())
            self.assertEqual(app.query_one(TopBar).project, "Gamma")
            self.assertEqual(self.registry.last().id, "gamma")
            self.assertFalse((third / KNOWLEDGE_DIR).exists())
            await pilot.press("escape")
            await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, ProjectHome)
            self.assertIs(app.focused, self.composer(app))
            self.composer(app).load_text("/project ")
            await pilot.pause(0.1)
            self.assertEqual(app.suggestion_values, ["/project alpha", "/project beta", "/project gamma",
                                                     "/project none"])
            self.composer(app).load_text("/sav")
            await pilot.pause(0.1)
            self.assertEqual(app.suggestion_values, ["/save"])

    async def test_home_shows_empty_states_and_creates_a_note(self):
        from textual.widgets import Input, OptionList, TextArea
        from local_agent.tui.project_screens import ChoiceScreen, NoteEditor, ProjectHome
        app = self.make(StreamingFake(), self.alpha)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f6")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ProjectHome)
            tasks = app.screen.query_one("#home-tasks", OptionList)
            self.assertIn("press t", str(tasks.get_option_at_index(0).prompt))
            await pilot.press("n")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ChoiceScreen)
            await pilot.press("i")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, NoteEditor)
            app.screen.query_one("#editor-name", Input).value = "Offline mode"
            app.screen.query_one("#editor-body", TextArea).load_text("# Offline mode\n\nCache everything.\n")
            await pilot.press("ctrl+s")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, ProjectHome)
            saved = self.alpha.knowledge.get("idea-offline-mode")
            self.assertIn("Cache everything.", saved.path.read_text(encoding="utf-8"))
            notes = app.screen.query_one("#home-notes", OptionList)
            self.assertIn("Offline mode", str(notes.get_option_at_index(0).prompt))

    async def test_switching_keeps_each_projects_draft_attachments_and_session(self):
        from local_agent.tui.widgets import AttachmentBar
        attachment = self.root / "notes.txt"
        attachment.write_text("classic attachment", encoding="utf-8")
        app = self.make(StreamingFake(), session="classic-chat")
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, f'/attach "{attachment}"')
            self.composer(app).load_text("unfinished classic draft")
            self.assertTrue(await app.open_project(self.alpha))
            await pilot.pause(0.1)
            self.assertEqual(self.composer(app).text, "")
            self.assertEqual(app.file_commands.pending, [])
            self.assertFalse(app.query_one(AttachmentBar).display)
            self.assertEqual(app.state.project, "alpha")
            self.composer(app).load_text("alpha draft")
            await app.open_project(None)
            await pilot.pause(0.1)
            self.assertEqual(self.composer(app).text, "unfinished classic draft")
            self.assertEqual(app.file_commands.pending, [attachment.resolve()])
            self.assertEqual(app.session, "classic-chat")
            await app.open_project(self.registry.find("alpha"))
            await pilot.pause(0.1)
            self.assertEqual(self.composer(app).text, "alpha draft")

    async def test_switching_during_a_turn_asks_first_and_ignores_late_events(self):
        from local_agent.tui.project_screens import ConfirmScreen
        from local_agent.tui.widgets import AssistantMessage
        provider = StreamingFake(text("A late reply from alpha."), delay=0.01)
        app = self.make(provider, self.alpha)
        async with app.run_test(size=(120, 40)) as pilot:
            provider.release.clear()
            await self.send(pilot, app, "Slow alpha question")
            self.assertTrue(app.busy)
            await app.open_project(self.beta)
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ConfirmScreen)
            await pilot.press("n")  # Stay: nothing changes.
            await pilot.pause(0.1)
            self.assertTrue(app.busy)
            self.assertEqual(app.project.id, "alpha")
            await app.open_project(self.beta)
            await pilot.pause(0.1)
            await pilot.press("y")
            await self.wait_for(pilot, lambda: app.project is not None and app.project.id == "beta")
            self.assertFalse(app.busy)
            provider.release.set()
            await pilot.pause(0.4)
            self.assertFalse(any("late reply" in widget.raw for widget in app.query(AssistantMessage)))
            self.assertEqual(self.state.for_project("alpha").sessions(), [])
            self.assertEqual(self.state.for_project("beta").sessions(), [])
            self.assertEqual(app.recoverable_prompt, "")  # Alpha's failed prompt stays with alpha.
            await app.open_project(self.registry.find("alpha"))
            await pilot.pause(0.1)
            self.assertEqual(app.recoverable_prompt, "Slow alpha question")

    async def test_context_sources_are_shown_and_selected_notes_are_sent(self):
        from local_agent.tui.widgets import NoteCard
        knowledge = self.alpha.knowledge
        knowledge.create("overview", "Alpha", "# Alpha\n\nA ledger for bakeries.\n")
        knowledge.create("decision", "Use SQLite", "# Use SQLite\n\nStore the ledger in SQLite.\n")
        knowledge.create("idea", "Dark theme", "# Dark theme\n\nA darker palette.\n")
        provider = StreamingFake(text("Noted."))
        app = self.make(provider, self.alpha)
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, "/context add dark theme")
            self.assertEqual(app.state.conversation("default")["notes"], ["idea-dark-theme"])
            await self.send(pilot, app, "Where is the ledger stored? sqlite?")
            await self.wait_idle(pilot, app)
            cards = [card for card in app.query(NoteCard) if card.has_class("context-sources")]
            self.assertEqual(len(cards), 1)
            line = str(cards[0].render())
            for identifier in ("idea-dark-theme", "overview", "decision-use-sqlite"):
                self.assertIn(identifier, line)
            system = provider.requests[0][0][0]["content"]
            notes = json.loads(system.rsplit("Saved context:\n", 1)[1])["project"]["notes"]
            self.assertEqual([note["id"] for note in notes], ["idea-dark-theme", "overview", "decision-use-sqlite"])
            self.assertTrue(app.context_sources[2]["reason"].startswith("matched"))
            cards = len(app.query(NoteCard))
            await self.send(pilot, app, "/context")
            self.assertEqual(len(app.query(NoteCard)), cards + 1)

    async def test_a_reply_is_reviewed_before_it_is_saved_as_a_decision(self):
        from textual.widgets import Input, TextArea
        from local_agent.tui.project_screens import ChoiceScreen, NoteEditor
        reply = "## Recommendation\nUse SQLite for the ledger.\n\n## Why\nIt is one file.\n"
        app = self.make(StreamingFake(text(reply)), self.alpha, session="brainstorm-1")
        async with app.run_test(size=(120, 40)) as pilot:
            await self.send(pilot, app, "Which database?")
            await self.wait_idle(pilot, app)
            self.assertTrue(app.query_one("#save-reply").display)
            await pilot.press("f9")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ChoiceScreen)
            await pilot.press("d")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, NoteEditor)
            self.assertEqual(app.screen.query_one("#editor-name", Input).value, "Use SQLite for the ledger")
            body = app.screen.query_one("#editor-body", TextArea)
            self.assertIn("## Why\n\nIt is one file.", body.text)
            self.assertFalse((self.alpha.path / KNOWLEDGE_DIR).exists())  # Nothing saved before review.
            body.load_text(body.text.replace("It is one file.", "It is one file, easy to back up."))
            await pilot.press("ctrl+s")
            await pilot.pause(0.2)
            decision = self.alpha.knowledge.get("decision-use-sqlite-for-the-ledger")
            self.assertIn("easy to back up", decision.body)
            self.assertEqual(decision.conversations, ["brainstorm-1"])

    async def test_editor_never_overwrites_an_external_edit_without_asking(self):
        from textual.widgets import TextArea
        from local_agent.tui.project_screens import ChoiceScreen, NoteEditor
        note = self.alpha.knowledge.create("note", "Ledger format")
        app = self.make(StreamingFake(), self.alpha)
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(NoteEditor(note=self.alpha.knowledge.get(note.id)))
            await pilot.pause(0.1)
            app.screen.query_one("#editor-body", TextArea).load_text("# Ledger format\n\nMine.\n")
            note.path.write_text(note.path.read_text(encoding="utf-8") + "\nTheirs.\n", encoding="utf-8")
            await pilot.press("ctrl+s")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ChoiceScreen)
            await pilot.press("k")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, NoteEditor)
            self.assertIn("Theirs.", note.path.read_text(encoding="utf-8"))
            await pilot.press("ctrl+s")
            await pilot.pause(0.1)
            await pilot.press("o")
            await pilot.pause(0.2)
            self.assertNotIsInstance(app.screen, NoteEditor)
            content = note.path.read_text(encoding="utf-8")
            self.assertIn("Mine.", content)
            self.assertNotIn("Theirs.", content)

    async def test_task_handoff_and_outcome_keep_the_user_in_charge_of_completion(self):
        from textual.widgets import TextArea
        from local_agent.tui.project_screens import NotesScreen, OutcomeScreen
        self.alpha.knowledge.create("task", "Export CSV", "# Export CSV\n\n## Description\n\nRows to CSV.\n\n"
                                    "## Acceptance criteria\n\n- [ ] Header row\n\n## Outcome\n\n_Not recorded yet._\n")
        provider = StreamingFake(text("Done! The task is complete."))
        app = self.make(provider, self.alpha)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f7")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, NotesScreen)
            await pilot.press("i")
            await self.wait_for(pilot, lambda: app.session == "impl-export-csv")
            self.assertNotIsInstance(app.screen, NotesScreen)
            self.assertIn("- [ ] Header row", self.composer(app).text)
            self.assertEqual(app.state.todo("impl-export-csv"), ["[ ] Header row"])
            await pilot.press("enter")
            await self.wait_idle(pilot, app)
            notes = json.loads(provider.requests[0][0][0]["content"].rsplit("Saved context:\n", 1)[1])["project"]
            self.assertEqual(notes["notes"][0]["id"], "task-export-csv")
            self.assertEqual(self.alpha.knowledge.get("task-export-csv").status, "in-progress")
            await self.send(pilot, app, "/outcome")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, OutcomeScreen)
            self.assertIn("Done!", app.screen.query_one("#outcome-outcome", TextArea).text)
            app.screen.query_one("#outcome-verification", TextArea).load_text("Opened the CSV in Excel.")
            await pilot.press("ctrl+s")
            await pilot.pause(0.2)
            task = self.alpha.knowledge.get("task-export-csv")
            self.assertEqual(task.status, "in-progress")
            self.assertIn("**Verification:** Opened the CSV in Excel.", task.body)
            self.assertIn("impl-export-csv", task.conversations)

    async def test_notes_browser_is_keyboard_driven_and_adapts_to_narrow_terminals(self):
        from textual.widgets import Input
        from local_agent.tui.project_screens import NoteView, NotesScreen
        knowledge = self.alpha.knowledge
        knowledge.create("idea", "Offline ledger", "# Offline ledger\n\nWorks without network.\n")
        knowledge.create("decision", "Use SQLite", "# Use SQLite\n\nOne file.\n")
        app = self.make(StreamingFake(), self.alpha)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f7")
            await pilot.pause(0.1)
            screen = app.screen
            self.assertIsInstance(screen, NotesScreen)
            self.assertTrue(screen.query_one("#notes-preview").display)
            self.assertEqual(len(screen.notes), 2)
            await pilot.press("tab")
            await pilot.pause(0.05)
            self.assertEqual(screen.kind, "task")
            await pilot.press("tab")
            await pilot.pause(0.05)
            self.assertEqual([note.id for note in screen.notes], ["decision-use-sqlite"])
            await pilot.press("shift+tab", "shift+tab")
            await pilot.press("slash")
            self.assertIsInstance(app.focused, Input)
            await pilot.press(*"offline")
            await pilot.pause(0.1)
            self.assertEqual([note.id for note in screen.notes], ["idea-offline-ledger"])
            await pilot.press("enter")  # Back to the list.
            await pilot.press("c")
            self.assertEqual(app.state.conversation("default")["notes"], ["idea-offline-ledger"])
            for width, height in ((80, 24), (60, 18)):
                await pilot.resize_terminal(width, height)
                await pilot.pause(0.1)
                self.assertTrue(screen.has_class("-narrow"))
                self.assertFalse(screen.query_one("#notes-preview").display)
                listing = screen.query_one("#notes-list")
                self.assertLessEqual(listing.region.right, width)
            await pilot.press("enter")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, NoteView)
            await pilot.press("escape")
            await pilot.pause(0.1)
            self.assertIs(app.screen, screen)
            await pilot.press("escape")
            await pilot.pause(0.1)
            self.assertIs(app.focused, self.composer(app))

    async def test_project_screens_fit_small_and_large_terminals(self):
        from local_agent.tui.project_screens import NotesScreen, ProjectHome, ProjectsScreen
        self.alpha.knowledge.create("task", "Ship", "# Ship\n\n## Acceptance criteria\n\n- [ ] Done\n")
        app = self.make(StreamingFake(), self.alpha)
        async with app.run_test(size=(120, 40)) as pilot:
            for screen_type, key in ((ProjectHome, "f6"), (NotesScreen, "f7"), (ProjectsScreen, "f5")):
                await pilot.press(key)
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, screen_type)
                for width, height in ((140, 44), (80, 24), (50, 16)):
                    await pilot.resize_terminal(width, height)
                    await pilot.pause(0.1)
                    keys = app.screen.query_one(".screen-keys")
                    self.assertLessEqual(keys.region.bottom, height, (screen_type.__name__, width))
                    self.assertLessEqual(keys.region.right, width, (screen_type.__name__, width))
                    if screen_type is ProjectHome:
                        right = app.screen.query_one("#home-right")
                        stacked = right.region.x < app.screen.query_one("#home-left").region.right
                        self.assertEqual(stacked, width < 100, width)
                await pilot.resize_terminal(120, 40)
                await pilot.press("escape")
                await pilot.pause(0.1)

    async def test_without_projects_the_classic_interface_is_unchanged(self):
        from local_agent.tui.widgets import Welcome
        app = self.make(StreamingFake(text("Hi.")))
        async with app.run_test(size=(120, 40)) as pilot:
            self.assertIsNone(app.project)
            self.assertTrue(app.query(Welcome))
            self.assertFalse(app.query_one("#save-reply").display)
            await self.send(pilot, app, "Hello")
            await self.wait_idle(pilot, app)
            await self.send(pilot, app, "/notes")
            self.assertEqual(len(app.screen_stack), 1)
        self.assertEqual(self.state.sessions(), [("default", 1)])
        self.assertEqual(self.state.for_project("alpha").sessions(), [])


if __name__ == "__main__":
    unittest.main()
