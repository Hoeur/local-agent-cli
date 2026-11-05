"""Textual integration. Textual remains the sole owner of terminal input/output."""
import asyncio
import json
import os
import time

from textual.widgets import Static
from rich.style import Style
from rich.text import Text

from .hologram import Capabilities, GRAPHICS_DIR, HologramScene


def prepare_image_protocol(capabilities):
    """Probe before Textual takes stdin. Only Kitty supports TGP placeholders reliably.

    Image animation is opt-in. Multiplexers/SSH never receive graphics probes.
    """
    if not capabilities.interactive or capabilities.remote or not capabilities.color:
        return None
    if not (os.getenv("KITTY_WINDOW_ID") or os.getenv("TERM") == "xterm-kitty"):
        return None
    try:
        from textual_image._terminal import get_cell_size
        from textual_image.renderable.tgp import Image, query_terminal_support
        get_cell_size()
        return Image if query_terminal_support() else None
    except Exception:
        return None


class GraphicsWorker:
    def __init__(self, executable):
        self.executable = executable
        self.process = None

    async def frame(self, data):
        if self.process is None:
            options = {"creationflags": 0x08000000} if os.name == "nt" else {}
            self.process = await asyncio.create_subprocess_exec(
                self.executable, str(GRAPHICS_DIR / "worker.ts"), cwd=GRAPHICS_DIR,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=1_000_000, **options,
            )
            ready = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 12))
            if not ready.get("ready"):
                raise RuntimeError(ready.get("error", "Graphics worker did not initialize"))
        self.process.stdin.write((json.dumps(data) + "\n").encode())
        await asyncio.wait_for(self.process.stdin.drain(), 1)
        result = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 2))
        if "lines" not in result:
            raise RuntimeError(result.get("error", "Graphics worker stopped"))
        text = Text(no_wrap=True, overflow="crop")
        for index, line in enumerate(result["lines"]):
            if index:
                text.append("\n")
            for span in line:
                fg, bg = ("#" + "".join(f"{c:02x}" for c in span[key]) for key in ("fg", "bg"))
                text.append(span["text"], Style(color=fg, bgcolor=bg))
        return text

    async def close(self):
        process, self.process = self.process, None
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
                await asyncio.wait_for(process.wait(), 0.3)
            except (TimeoutError, BrokenPipeError, ConnectionResetError):
                if process.returncode is None:
                    process.kill()
                await process.wait()


class SafeImageFrame:
    def __init__(self, image, fallback, failed):
        self.image, self.fallback, self.failed = image, fallback, failed

    def __rich_console__(self, console, options):
        try:
            # Realize the generator here so protocol/render errors are contained.
            segments = list(console.render(self.image, options))
        except Exception:
            self.failed()
            yield self.fallback
        else:
            yield from segments


class HologramRenderer(Static):
    DEFAULT_CSS = "HologramRenderer { height: 12; width: 1fr; overflow: hidden; }"

    def __init__(self, *, capabilities=None, mode="auto", animated=True, image_factory=None, **kwargs):
        super().__init__(**kwargs)
        self.capabilities = capabilities or Capabilities.detect()
        self.scene = HologramScene()
        self.requested_mode = mode
        self.animated = animated and not self.capabilities.reduced_motion
        self.active = True
        self.image_factory = image_factory
        self.mode = "ascii"
        self.reason = ""
        self.worker = None
        self.failed_enhanced = False
        self.image_frame = None
        self.in_flight = None
        self.redraw_pending = False
        self.closing = set()
        self.next_frame = 0.0
        self.frame_seconds = 0.0
        self.fps = 18.0
        self.last_frame = None

    def on_mount(self):
        self.timer = self.set_interval(1 / 30, self.tick, pause=True)
        self.choose_mode()
        self.set_active(self.active)

    def on_resize(self):
        # A paused widget still needs its first frame once layout has a size.
        self.last_frame = None
        self.call_later(self.tick, True)

    def choose_mode(self):
        cap = self.capabilities
        self.mode, self.reason = "ascii", ""
        if self.requested_mode == "image":
            if self.image_factory:
                self.mode = "image"
            else:
                self.reason = "Image protocol unavailable; using terminal cells."
        elif self.requested_mode in {"auto", "enhanced"}:
            if cap.bun and cap.interactive and cap.color and cap.unicode and not cap.remote and not self.failed_enhanced:
                self.mode = "enhanced"
            elif self.requested_mode == "enhanced":
                self.reason = "Enhanced mode unavailable (Bun/packages, local color terminal, or GPU); using terminal cells."

    def set_active(self, active):
        self.active = active
        self.display = active
        if not hasattr(self, "timer"):
            return
        self.scene.last = time.monotonic()
        if active:
            if self.animated:
                self.timer.resume()
            else:
                self.timer.pause()
            self.next_frame = 0
            self.tick(force=True)
        else:
            self.timer.pause()
            self.release_resources()

    def release_resources(self):
        self.redraw_pending = False
        if self.in_flight:
            self.in_flight.cancel()
        if self.worker:
            worker, self.worker = self.worker, None
            task = asyncio.create_task(worker.close())
            self.closing.add(task)
            task.add_done_callback(self.closing.discard)
        self.cleanup_image()

    def cleanup_image(self):
        if self.image_frame:
            try:
                self.image_frame.cleanup()
            except Exception:
                pass
            self.image_frame = None

    async def on_unmount(self):
        self.timer.stop()
        self.redraw_pending = False
        if self.in_flight:
            self.in_flight.cancel()
            try:
                await self.in_flight
            except asyncio.CancelledError:
                pass
        if self.worker:
            await self.worker.close()
            self.worker = None
        if self.closing:
            await asyncio.gather(*self.closing, return_exceptions=True)
        self.cleanup_image()

    def handle_event(self, event):
        self.scene.event(event)
        if not self.animated:
            self.tick(force=True)

    def terminal_frame(self, width, height):
        rows = self.scene.cells(width, height, self.capabilities.unicode and self.requested_mode != "ascii")
        color = self.scene.color
        palette = [Style(color="#" + "".join(f"{int(c * (0.25 + i * 0.15)):02x}" for c in
                                               ((22 + i * 8, 100 + i * 24, 255) if self.scene.state not in {"success", "error"} else color)))
                   if self.capabilities.color else Style() for i in range(6)]
        text = Text(no_wrap=True, overflow="crop")
        for y, row in enumerate(rows):
            if y:
                text.append("\n")
            for char, light in row:
                text.append(char, palette[light])
        return text

    def tick(self, force=False):
        if not self.active or not self.is_mounted or self.content_size.width < 2:
            return
        if self.in_flight and not self.in_flight.done():
            self.redraw_pending = self.redraw_pending or force
            return
        now = time.monotonic()
        if not force and now < self.next_frame:
            return
        target = 30 if self.mode == "enhanced" else 8 if self.mode == "image" else 18
        if self.capabilities.remote:
            target = 8
        if self.scene.state == "idle":
            target = min(target, 12)
        self.fps = max(4, min(target, 0.35 / max(0.001, self.frame_seconds)))
        self.next_frame = now + 1 / self.fps
        self.scene.advance(self.animated)
        self.redraw_pending = False
        self.in_flight = asyncio.create_task(self.draw_frame())

    async def draw_frame(self):
        started = time.monotonic()
        width, height = min(120, self.content_size.width), min(40, max(1, self.content_size.height - 1))
        try:
            if self.mode == "enhanced":
                if self.worker is None:
                    self.worker = GraphicsWorker(self.capabilities.bun)
                frame = await self.worker.frame(dict(width=width, height=height, **self.scene.snapshot()))
            else:
                frame = self.terminal_frame(width, height)
            if not self.active or not self.is_mounted:
                return
            if self.mode == "image":
                self.cleanup_image()
                pixels = self.scene.image(width * 8, height * 16)
                self.image_frame = self.image_factory(pixels, width=width, height=height)
                self.update(SafeImageFrame(self.image_frame, frame, self.image_failed))
            else:
                done, total = self.scene.progress
                label = self.scene.state.upper()
                if self.scene.state == "tool" and total:
                    label += f" {done}/{total}"
                frame.append("\n" + label.center(width), style="bold cyan" if self.capabilities.color else None)
                if frame != self.last_frame:
                    self.update(frame)
                    self.last_frame = frame
        except asyncio.CancelledError:
            raise
        except Exception:
            self.reason = "Graphics initialization/rendering failed; using terminal cells."
            self.failed_enhanced = True
            self.mode = "ascii"
            self.cleanup_image()
            if self.worker:
                worker, self.worker = self.worker, None
                await worker.close()
            if self.active and self.is_mounted:
                self.update(self.terminal_frame(width, height))
        finally:
            cost = time.monotonic() - started
            self.frame_seconds = self.frame_seconds * 0.8 + min(cost, 0.25) * 0.2
            if self.redraw_pending and self.active and self.is_mounted:
                self.redraw_pending = False
                self.call_later(self.tick, True)

    def image_failed(self):
        self.mode = "ascii"
        self.reason = "Image protocol failed; using terminal cells."
        self.call_later(self.cleanup_image)

    def configure(self, value):
        if value in {"auto", "ascii", "unicode", "enhanced", "image"}:
            self.release_resources()
            self.requested_mode = value
            self.failed_enhanced = False
            self.choose_mode()
        elif value in {"pause", "resume"}:
            self.animated = value == "resume"
        elif value in {"left", "right", "up", "down"}:
            if value in {"left", "right"}:
                self.scene.yaw += -0.25 if value == "left" else 0.25
            else:
                self.scene.pitch += -0.2 if value == "up" else 0.2
        self.last_frame = None
        self.set_active(self.active)
        self.call_later(self.tick, True)
