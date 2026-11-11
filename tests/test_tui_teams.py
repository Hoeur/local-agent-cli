"""Headless tests for agent teams in the terminal UI, with scripted models. Skipped without Textual.
They check behavior and layout geometry, not how it looks in a real terminal."""
import importlib.util
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("textual") is None:
    raise unittest.SkipTest("Textual is not installed")

from local_agent.config import save_config
from local_agent.orchestrator import RunStore
from local_agent.projects import ProjectRegistry
from local_agent.state import State
from local_agent.teams import TeamStore

try:
    from fakes import StreamingFake, text
    from test_teams import Fake, call, standard
except ImportError:
    from tests.fakes import StreamingFake, text
    from tests.test_teams import Fake, call, standard

CONFIG = {"model": "qwen3:4b", "base_url": "http://127.0.0.1:11434/v1", "context_chars": 12_000}


class TeamAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 5

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = mock.patch.dict(os.environ, {"AGENT_UI_MOTION": "off", "AGENT_UI_GLYPHS": "unicode",
                                           "TERM": "xterm-256color", "AGENT_KEYS_FILE": str(self.root / "keys.json")})
        env.start()
        self.addCleanup(env.stop)
        self.data = self.root / "data"
        save_config(self.data, "http://127.0.0.1:11434/v1", "qwen3:4b", 12_000, provider="ollama", name="local")
        self.state = State(self.data)
        self.registry = ProjectRegistry(self.data)
        for name in ("alpha", "beta"):
            (self.root / name).mkdir()
            (self.root / name / "app.txt").write_text("hello\n", encoding="utf-8")
        self.alpha = self.registry.add(self.root / "alpha", "Alpha")
        self.beta = self.registry.add(self.root / "beta", "Beta")
        self.fakes = []

    def make(self, script=standard, delay=0.0, project=None):
        from local_agent.tui.app import AgentApp
        project = project or self.alpha
        app = AgentApp(StreamingFake(text("single agent reply")), self.state.for_project(project.id), self.data,
                       CONFIG, "default", project.path, prefs={"theme": "neon", "hologram": False}, project=project)

        def factory(spec, timeout):
            fake = Fake(spec, script, delay)
            self.fakes.append(fake)
            return fake
        app.team_provider_factory = factory
        return app

    async def send(self, pilot, text_value):
        from local_agent.tui.widgets import Composer
        pilot.app.query_one(Composer).load_text(text_value)
        await pilot.press("enter")
        await pilot.pause(0.1)

    async def wait_for(self, pilot, condition, limit=8.0):
        for _ in range(int(limit / 0.05)):
            if condition():
                return
            await pilot.pause(0.05)
        self.fail("Condition not reached.")

    async def approve_all(self, pilot, app, limit=8.0):
        from local_agent.tui.screens import ActionScreen
        for _ in range(int(limit / 0.05)):
            if isinstance(app.screen, ActionScreen) and app.screen.is_mounted:
                await pilot.press("y")
            if app.team_run is None:
                return
            await pilot.pause(0.05)
        self.fail("The team run did not finish.")

    async def test_choose_a_team_check_it_and_run_it(self):
        from local_agent.tui.project_screens import ConfirmScreen
        from local_agent.tui.screens import ActionScreen
        from local_agent.tui.team_screens import TeamRunPanel, TeamScreen
        from local_agent.tui.widgets import AssistantMessage, StatusBar
        app = self.make()
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.press("f8")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, TeamScreen)
            listing = app.screen.query_one("#team-list")
            listing.highlighted = 1  # implement
            await pilot.press("enter")
            await pilot.pause(0.1)
            self.assertEqual(app.team_mode, "implement")
            await pilot.press("escape")
            await pilot.pause(0.1)
            self.assertEqual(app.query_one(StatusBar).team, "implement")
            self.assertIn("Team implement", app.query_one("#composer").placeholder)
            await self.send(pilot, "Change the greeting to hi")
            self.assertIsInstance(app.screen, ConfirmScreen)  # The preflight comes first.
            self.assertIn("local/qwen3:4b", app.screen.message)
            self.assertIn("same provider and model", app.screen.message)
            self.assertEqual(app.state.sessions(), [])
            await pilot.press("y")
            await self.wait_for(pilot, lambda: isinstance(app.screen, ActionScreen) and app.screen.is_mounted)
            dialog = str(app.screen.query_one(".dialog-title").render())
            self.assertIn("Implementer (Implementer) wants to write a file", dialog)
            self.assertEqual(app.screen.action["project"], "Alpha")
            self.assertEqual(app.screen.action["target"], "app.txt")
            await self.approve_all(pilot, app)
            self.assertFalse(app.busy)
            panel = app.query_one(TeamRunPanel)
            self.assertEqual(len(panel.rows), 3)
            self.assertIn("qwen3:4b", panel.rows["implementer"].title)
            final = list(app.query(AssistantMessage))[-1]
            self.assertIn("Team run", final.raw)
            self.assertIn("echo checked", final.raw)
            self.assertEqual(self.state.for_project("alpha").sessions(), [("default", 1)])
            self.assertEqual((self.alpha.path / "app.txt").read_text(encoding="utf-8"), "hi\n")
            self.assertEqual(RunStore(self.data, "alpha").runs()[0]["state"], "completed")
            self.assertEqual(self.registry.find("alpha").settings["team_mode"], "implement")
            await pilot.press("ctrl+o")  # Expands every agent's output.
            await pilot.pause(0.1)
            self.assertFalse(panel.rows["reviewer"].collapsed)

    async def test_stop_during_approval_releases_the_agent_and_ignores_late_events(self):
        from local_agent.tui.screens import ActionScreen
        from local_agent.tui.team_screens import TeamRunPanel
        app = self.make()
        async with app.run_test(size=(120, 40)) as pilot:
            app.set_team_mode("implement")
            await self.send(pilot, "Change the greeting")
            await pilot.press("y")
            await self.wait_for(pilot, lambda: isinstance(app.screen, ActionScreen))
            run = app.team_run
            await app.action_stop_reply()
            await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, ActionScreen)
            self.assertFalse(app.busy)
            self.assertEqual(app.recoverable_prompt, "Change the greeting")
            self.assertTrue(app.query_one("#restore-prompt").display)
            for _ in range(100):
                if run.record()["state"] == "cancelled":
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.3)
            self.assertEqual(run.record()["state"], "cancelled")
            self.assertEqual((self.alpha.path / "app.txt").read_text(encoding="utf-8"), "hello\n")
            self.assertEqual(len(app.query(TeamRunPanel)), 1)
            self.assertNotIn("Team run", "".join(m.raw for m in app.query("AssistantMessage")))

    async def test_switching_projects_during_a_run_asks_and_drops_its_events(self):
        from local_agent.tui.project_screens import ConfirmScreen
        app = self.make(delay=0.4)
        async with app.run_test(size=(120, 40)) as pilot:
            app.set_team_mode("implement")
            await self.send(pilot, "Change the greeting")
            await pilot.press("y")
            await self.wait_for(pilot, lambda: app.team_run is not None)
            run = app.team_run
            await app.open_project(self.beta)
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await self.wait_for(pilot, lambda: app.project.id == "beta")
            self.assertIsNone(app.team_run)
            self.assertIsNone(app.team_mode)  # Beta has its own setting.
            for _ in range(100):
                if run.record()["state"] in {"cancelled", "completed", "failed"}:
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.3)
            self.assertEqual(run.record()["state"], "cancelled")
            self.assertEqual(self.state.for_project("beta").sessions(), [])
            await app.open_project(self.registry.find("alpha"))
            await pilot.pause(0.1)
            self.assertEqual(app.team_mode, "implement")

    async def test_editing_assignments_and_narrow_layouts(self):
        from textual.widgets import Input
        from local_agent.tui.project_screens import PromptScreen
        from local_agent.tui.team_screens import TeamScreen
        save_config(self.data, "http://127.0.0.1:11434/v1", "qwen3:8b", 12_000, provider="ollama", name="bigger")
        app = self.make()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f8")
            await pilot.pause(0.1)
            screen = app.screen
            screen.query_one("#team-list").highlighted = 1
            await pilot.pause(0.05)
            await pilot.press("tab")
            screen.query_one("#team-agents").highlighted = 2  # reviewer
            await pilot.press("e")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, PromptScreen)
            app.screen.query_one("#field-profile", Input).value = "bigger"
            app.screen.query_one("#field-model", Input).value = "qwen3:14b"
            app.screen.query_one("#field-tools", Input).value = "read,shell"  # Not allowed for a reviewer.
            await pilot.click("#ok")
            await pilot.pause(0.1)
            self.assertNotIn("implement", TeamStore(self.data, "alpha").load())  # Refused: nothing saved.
            await pilot.press("e")
            await pilot.pause(0.1)
            app.screen.query_one("#field-profile", Input).value = "bigger"
            app.screen.query_one("#field-model", Input).value = "qwen3:14b"
            await pilot.click("#ok")
            await pilot.pause(0.1)
            saved = TeamStore(self.data, "alpha").get("implement")
            self.assertEqual((saved["agents"][2]["profile"], saved["agents"][2]["model"]), ("bigger", "qwen3:14b"))
            self.assertIn("qwen3:14b", str(screen.query_one("#team-detail-text").render()))
            for width, height in ((80, 24), (60, 18)):
                await pilot.resize_terminal(width, height)
                await pilot.pause(0.1)
                self.assertTrue(screen.has_class("-narrow"))
                keys = screen.query_one(".screen-keys")
                self.assertLessEqual(keys.region.bottom, height)
                self.assertLessEqual(screen.query_one("#team-detail").region.right, width)
            await pilot.press("escape", "escape")
            await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, TeamScreen)

    async def test_single_agent_mode_and_broken_team_files_leave_chat_working(self):
        from local_agent.tui.widgets import AssistantMessage
        store = TeamStore(self.data, "alpha")
        store.file.parent.mkdir(parents=True, exist_ok=True)
        store.file.write_text("{broken", encoding="utf-8")
        app = self.make()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f8")
            await pilot.pause(0.1)
            await pilot.press("escape")
            await self.send(pilot, "Hello")
            for _ in range(60):
                if not app.busy:
                    break
                await pilot.pause(0.05)
            self.assertEqual(list(app.query(AssistantMessage))[-1].raw, "single agent reply")
            await self.send(pilot, "/team implement")
            self.assertIsNone(app.team_mode)  # The broken file is reported, not used.

    async def test_a_task_starts_a_team_implementation_run_and_records_the_outcome(self):
        from local_agent.tui.project_screens import ConfirmScreen, NotesScreen
        self.alpha.knowledge.create("task", "Say hi", "# Say hi\n\n## Acceptance criteria\n\n- [ ] Greeting says hi\n")
        app = self.make()
        app.auto_approve = True
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f7")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, NotesScreen)
            await pilot.press("g")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ConfirmScreen)
            self.assertIn("Task: task-say-hi", app.screen.message)
            await pilot.press("y")
            await self.approve_all(pilot, app)
            task = self.alpha.knowledge.get("task-say-hi")
            self.assertEqual(task.status, "in-progress")
            self.assertIn("**Team run:**", task.body)


if __name__ == "__main__":
    unittest.main()
