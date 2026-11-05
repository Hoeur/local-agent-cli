"""Local system metrics. This data is never added to model requests or history."""
from dataclasses import dataclass
from pathlib import Path
import shutil


@dataclass(frozen=True)
class SystemMetrics:
    cpu: float | None = None
    cores: int | None = None
    memory_percent: float | None = None
    memory_used: int | None = None
    memory_total: int | None = None
    disk_free: int | None = None
    disk_total: int | None = None
    disk_label: str = ""
    app_memory: int | None = None
    error: str = ""


class SystemSampler:
    def __init__(self, path):
        self.path = Path(path).resolve()
        try:
            import psutil
        except ImportError:
            self.backend = None
        else:
            self.backend = psutil

    def sample(self):
        if self.backend is None:
            return SystemMetrics(error="Install the updated .[tui] extra")
        psutil = self.backend

        def read(function):
            try:
                return function()
            except (OSError, psutil.Error):
                return None

        # An explicit interval provides a meaningful first sample even when workers change threads.
        cpu = read(lambda: psutil.cpu_percent(interval=0.1))
        memory = read(psutil.virtual_memory)
        disk = read(lambda: shutil.disk_usage(self.path))
        app = read(lambda: psutil.Process().memory_info())
        cores = read(psutil.cpu_count)
        return SystemMetrics(
            cpu=cpu, cores=cores,
            memory_percent=memory.percent if memory else None,
            memory_used=memory.total - memory.available if memory else None,
            memory_total=memory.total if memory else None,
            disk_free=disk.free if disk else None, disk_total=disk.total if disk else None,
            disk_label=self.path.anchor.rstrip("\\/") or "/",
            app_memory=app.rss if app else None,
        )
