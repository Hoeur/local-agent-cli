"""Live performance panel; sampling runs off the terminal UI thread."""
from collections import deque

from rich.text import Text
from textual import work
from textual.message import Message
from textual.widgets import Static

from .system_monitor import SystemMetrics, SystemSampler


def memory_size(value):
    if value is None:
        return "N/A"
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.1f} GiB"
    return f"{value / 1024 ** 2:.0f} MiB"


class MetricsReady(Message):
    def __init__(self, metrics):
        super().__init__()
        self.metrics = metrics


class SystemMonitor(Static):
    DEFAULT_CSS = "SystemMonitor { height: auto; margin-bottom: 1; }"

    def __init__(self, path, *, unicode=True, color=True, sampler=None, **kwargs):
        super().__init__(Text("Reading system metrics..."), **kwargs)
        self.sampler = sampler or SystemSampler(path)
        self.unicode = unicode
        self.color = color
        self.history = deque(maxlen=24)
        self.sampling = False
        self.latest = None

    def on_mount(self):
        self.timer = self.set_interval(1, self.refresh_metrics)
        self.refresh_metrics()

    def on_unmount(self):
        self.timer.stop()

    def on_resize(self):
        self.refresh_metrics()

    def refresh_metrics(self):
        if self.sampling or not self.is_mounted or not self.is_on_screen:
            return
        self.sampling = True
        self.collect()

    @work(thread=True, group="system-monitor")
    def collect(self):
        try:
            metrics = self.sampler.sample()
        except Exception:
            metrics = SystemMetrics(error="System metrics unavailable")
        self.post_message(MetricsReady(metrics))

    def on_metrics_ready(self, message):
        self.sampling = False
        self.latest = message.metrics
        if message.metrics.cpu is not None:
            self.history.append(max(0, min(100, message.metrics.cpu)))
        self.update(self.metrics_text(message.metrics))

    def metrics_text(self, metrics):
        text = Text(no_wrap=True, overflow="crop")
        normal = "#93a9bf" if self.color else None
        value = "#62edff" if self.color else None
        cpu_color = ("#ffca80" if metrics.cpu is not None and metrics.cpu >= 85 else value)
        cpu = f"{metrics.cpu:5.1f}%" if metrics.cpu is not None else "N/A"
        cores = f"  {metrics.cores} threads" if metrics.cores else ""
        text.append("CPU  ", normal)
        text.append(cpu + cores + "\n", cpu_color)
        width = max(8, min(24, self.content_size.width or 24))
        filled = round(max(0, min(100, metrics.cpu or 0)) * width / 100)
        if metrics.cpu is None:
            text.append("No CPU sample\n", normal)
        else:
            text.append(("█" if self.unicode else "#") * filled, cpu_color)
            text.append(("░" if self.unicode else "-") * (width - filled) + "\n", normal)
        percent = f" {metrics.memory_percent:.0f}%" if metrics.memory_percent is not None else ""
        total = f"/{metrics.memory_total / 1024 ** 3:.1f}G" if metrics.memory_total else ""
        used = f"{metrics.memory_used / 1024 ** 3:.1f}" if metrics.memory_used is not None else "N/A"
        memory_color = "#ffca80" if self.color and (metrics.memory_percent or 0) >= 85 else value
        text.append("RAM  ", normal)
        text.append(used + total + percent + "\n", memory_color)
        text.append("DISK ", normal)
        free = f"{metrics.disk_free / 1024 ** 3:.1f}G" if metrics.disk_free is not None else "N/A"
        text.append(metrics.disk_label + " " + free + " free\n", value)
        text.append("APP  ", normal)
        text.append(memory_size(metrics.app_memory) + " RAM\n", value)
        if metrics.error:
            text.append(metrics.error, normal)
        else:
            levels = "▁▂▃▄▅▆▇█" if self.unicode else " .:-=+*#"
            text.append("".join(levels[min(7, int(sample * 8 / 100))]
                                for sample in list(self.history)[-width:]) or "Waiting for CPU history", value)
        return text
