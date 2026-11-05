import asyncio
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from local_agent.system_monitor import SystemMetrics, SystemSampler


class SamplerTests(unittest.TestCase):
    def test_cpu_first_sample_and_cross_platform_memory_calculation(self):
        sampler = SystemSampler(Path.cwd())
        sampler.backend = backend = MagicMock()
        backend.Error = OSError
        backend.cpu_percent.return_value = 36.5
        backend.cpu_count.return_value = 8
        backend.virtual_memory.return_value = SimpleNamespace(total=16 * 1024 ** 3,
                                                             available=4 * 1024 ** 3, percent=75)
        backend.Process.return_value.memory_info.return_value = SimpleNamespace(rss=100 * 1024 ** 2)
        with patch("local_agent.system_monitor.shutil.disk_usage",
                   return_value=SimpleNamespace(free=100 * 1024 ** 3, total=500 * 1024 ** 3)):
            sample = sampler.sample()
        backend.cpu_percent.assert_called_once_with(interval=0.1)
        self.assertEqual(sample.cpu, 36.5)
        self.assertEqual(sample.memory_used, 12 * 1024 ** 3)
        self.assertEqual(sample.app_memory, 100 * 1024 ** 2)
        self.assertEqual(sample.disk_free, 100 * 1024 ** 3)

    def test_missing_library_and_partial_metric_failures(self):
        sampler = SystemSampler(Path.cwd())
        sampler.backend = None
        self.assertIn("Install", sampler.sample().error)
        sampler.backend = backend = MagicMock()
        backend.Error = OSError
        backend.cpu_percent.return_value = 12.5
        backend.cpu_count.return_value = 4
        backend.virtual_memory.side_effect = OSError("Not available")
        backend.Process.side_effect = OSError("Access denied")
        with patch("local_agent.system_monitor.shutil.disk_usage", side_effect=OSError("Drive unavailable")):
            sample = sampler.sample()
        self.assertEqual(sample.cpu, 12.5)
        self.assertIsNone(sample.memory_percent)
        self.assertIsNone(sample.app_memory)
        self.assertIsNone(sample.disk_free)


class MonitorUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            from local_agent.tui import ChatApp
        except ModuleNotFoundError:
            self.skipTest("Install the tui extra")
        from local_agent.agent import Agent
        from local_agent.hologram import Capabilities
        from local_agent.state import State
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        provider = SimpleNamespace(model="test")
        self.app = ChatApp(Agent(provider, State(Path(self.temporary.name))), "monitor", False,
                           animated=False, capabilities=Capabilities(True, True, True, False, False, None))

    async def test_metrics_remain_visible_and_refresh_without_overlapping(self):
        from local_agent.monitor_widget import SystemMonitor
        metrics = SystemMetrics(cpu=36.5, cores=8, memory_percent=75,
                                memory_used=12 * 1024 ** 3, memory_total=16 * 1024 ** 3,
                                disk_free=100 * 1024 ** 3, disk_label="C:", app_memory=100 * 1024 ** 2)
        with patch.object(SystemSampler, "sample", return_value=metrics) as sample:
            async with self.app.run_test(size=(100, 24)) as pilot:
                panel = self.app.query_one("#system-monitor", SystemMonitor)
                async with asyncio.timeout(3):
                    while panel.latest is None:
                        await pilot.pause(0.05)
                await pilot.pause()
                self.assertIn("36.5%", str(panel.content))
                self.assertIn("12.0/16.0G 75%", str(panel.content))
                self.assertIn("100 MiB RAM", str(panel.content))
                self.assertLessEqual(panel.region.bottom, self.app.query_one("#status").content_region.bottom)
                self.assertFalse(self.app.query_one("#orb").active)
                calls = sample.call_count
                await pilot.pause(1.1)
                self.assertGreater(sample.call_count, calls)
                await pilot.resize_terminal(60, 20)
                self.assertFalse(self.app.query_one("#status").display)
                await pilot.pause()
                calls = sample.call_count
                panel.refresh_metrics()
                await pilot.pause(0.05)
                self.assertEqual(sample.call_count, calls)
                await pilot.resize_terminal(100, 30)
                self.assertTrue(self.app.query_one("#status").display)
                self.app.welcome = False
                self.app.layout_hologram()
                self.assertTrue(self.app.query_one("#orb").active)

    async def test_slow_or_failed_sampling_keeps_prompt_responsive(self):
        from local_agent.monitor_widget import SystemMonitor
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow_sample():
            entered.set()
            release.wait(3)
            raise OSError("Cannot read hardware")

        with patch.object(SystemSampler, "sample", side_effect=slow_sample) as sample:
            async with self.app.run_test(size=(100, 24)) as pilot:
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await pilot.pause(0.05)
                panel = self.app.query_one("#system-monitor", SystemMonitor)
                panel.refresh_metrics()
                await pilot.press("h", "i")
                self.assertEqual(self.app.query_one("#prompt").value, "hi")
                self.assertEqual(sample.call_count, 1)
                release.set()
                async with asyncio.timeout(3):
                    while panel.latest is None:
                        await pilot.pause(0.05)
                self.assertIn("unavailable", str(panel.content))
                self.assertIn("N/A", str(panel.content))
                panel.unicode = panel.color = False
                content = panel.metrics_text(SystemMetrics(cpu=50, cores=8))
                self.assertTrue(content.plain.isascii())
