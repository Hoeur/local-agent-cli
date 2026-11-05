"""Checks for the monitor in the installed, sidebar-based terminal UI."""
import asyncio
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_agent.state import State
from local_agent.system_monitor import SystemMetrics, SystemSampler

try:
    from fakes import StreamingFake
except ImportError:
    from tests.fakes import StreamingFake


CONFIG = {"model": "demo-model", "base_url": "http://127.0.0.1:11434/v1", "context_chars": 12_000}


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if importlib.util.find_spec("textual") is None:
            self.skipTest("Install the tui extra")
        from local_agent.tui.app import AgentApp
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.app = AgentApp(StreamingFake(), State(directory), directory, CONFIG, "monitor",
                            prefs={"motion": "off", "hologram": False},
                            environ={"AGENT_UI_MOTION": "off", "AGENT_UI_GLYPHS": "ascii"})

    async def test_sidebar_metrics_visible_refresh_and_follow_theme(self):
        from local_agent.tui.system_monitor import SystemMonitor
        metrics = SystemMetrics(cpu=36.5, cores=8, memory_percent=75,
                                memory_used=12 * 1024 ** 3, memory_total=16 * 1024 ** 3,
                                disk_free=100 * 1024 ** 3, disk_label="E:", app_memory=100 * 1024 ** 2)
        with patch.object(SystemSampler, "sample", return_value=metrics) as sample:
            async with self.app.run_test(size=(120, 30)) as pilot:
                panel = self.app.query_one(SystemMonitor)
                async with asyncio.timeout(3):
                    while panel.latest is None:
                        await pilot.pause(.05)
                await pilot.pause()
                self.assertGreaterEqual(panel.content_size.height, 7)
                visible_rows = "\n".join(panel.render_line(row).text for row in range(panel.content_size.height))
                for label in ("SYSTEM MONITOR", "CPU", "RAM", "DISK", "APP"):
                    self.assertIn(label, visible_rows)
                self.assertIn("SYSTEM MONITOR", panel.render().plain)
                self.assertIn("36.5%", panel.render().plain)
                self.assertIn("12.0/16.0G 75%", panel.render().plain)
                self.assertTrue(panel.render().plain.isascii())
                sidebar = self.app.query_one("#sidebar")
                self.assertLessEqual(panel.region.bottom, sidebar.content_region.bottom)
                self.assertLessEqual(panel.region.bottom, self.app.query_one("#sidebar-scroll").region.y)
                original_style = panel.render().spans[0].style
                self.app.set_theme("daylight")
                self.assertNotEqual(panel.render().spans[0].style, original_style)
                calls = sample.call_count
                await pilot.pause(1.1)
                self.assertGreater(sample.call_count, calls)
                await pilot.resize_terminal(80, 24)
                self.assertFalse(sidebar.display)
                await pilot.pause()
                calls = sample.call_count
                panel.refresh_metrics()
                await pilot.pause(.05)
                self.assertEqual(sample.call_count, calls)
                await pilot.press("ctrl+b")
                self.assertTrue(sidebar.display)
                self.assertTrue(panel.is_on_screen)

    async def test_slow_and_failed_sampling_does_not_block_composer(self):
        from local_agent.tui.system_monitor import SystemMonitor
        from local_agent.tui.widgets import Composer
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow_sample():
            entered.set()
            release.wait(3)
            raise OSError("Unavailable")

        with patch.object(SystemSampler, "sample", side_effect=slow_sample) as sample:
            async with self.app.run_test(size=(120, 30)) as pilot:
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await pilot.pause(.05)
                panel = self.app.query_one(SystemMonitor)
                panel.refresh_metrics()
                await pilot.press("h", "i")
                self.assertEqual(self.app.query_one(Composer).text, "hi")
                self.assertEqual(sample.call_count, 1)
                release.set()
                async with asyncio.timeout(3):
                    while panel.latest is None:
                        await pilot.pause(.05)
                self.assertIn("unavailable", panel.render().plain)
                self.assertIn("N/A", panel.render().plain)
