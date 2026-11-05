"""The classic line-based chat, with light styling when the terminal supports it.

Standard library only. Colors and the spinner turn on only for interactive terminals, and
NO_COLOR turns colors off. Piped input and output keep the original plain format.
"""
import itertools
import os
import sys
import threading
import time


def enable_ansi(stream):
    """Return True if stream is a terminal that accepts ANSI escapes (enabling them on Windows)."""
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11 if stream is sys.stdout else -12)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except (AttributeError, OSError):
        return False


class Style:
    def __init__(self, stream, environ=None):
        environ = os.environ if environ is None else environ
        self.enabled = not environ.get("NO_COLOR") and environ.get("TERM") != "dumb" and enable_ansi(stream)
        unicode = (getattr(stream, "encoding", "") or "").lower().replace("-", "") in {"utf8", "utf16"}
        self.unicode = unicode and not (os.name == "nt" and not environ.get("WT_SESSION"))

    def __call__(self, text, *codes):
        if not self.enabled or not codes:
            return text
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


CYAN, BLUE, PURPLE, GREEN, RED, YELLOW, DIM, BOLD = "38;5;51", "38;5;75", "38;5;141", "38;5;79", "38;5;204", \
    "38;5;221", "2", "1"


class Spinner:
    """A single status line on stderr while a turn runs. It never writes to stdout."""

    def __init__(self, style, stream=None):
        self.stream = stream or sys.stderr
        self.style = style
        self.active = self.stream.isatty() and style.enabled
        self.label = "thinking"
        self.thread = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self):
        if not self.active or self.thread is not None:
            return
        self.stop_event.clear()
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if self.style.unicode else "|/-\\"
        for frame in itertools.cycle(frames):
            if self.stop_event.wait(0.08):
                break
            with self.lock:
                elapsed = time.monotonic() - self.started
                self.stream.write(f"\r\x1b[2K{self.style(frame, CYAN)} {self.style(self.label, PURPLE)} "
                                  f"{self.style(f'{elapsed:.1f}s', DIM)}")
                self.stream.flush()

    def stop(self):
        if self.thread is None:
            return
        self.stop_event.set()
        self.thread.join()
        self.thread = None
        with self.lock:
            self.stream.write("\r\x1b[2K")
            self.stream.flush()

    def on_event(self, kind, data):
        if kind == "tool_start":
            self.label = f"running {data['name']}"
        elif kind == "compress":
            self.label = "trimming context"
        elif kind == "request":
            self.label = "thinking" if not data["round"] else f"thinking · round {data['round'] + 1}"


def chat_loop(agent, session, learn, model, printable, approve, errors, tui_hint=False):
    """Interactive line-based chat. Returns an exit code."""
    out = Style(sys.stdout)
    styled = out.enabled and sys.stdin.isatty()
    if styled:
        mark = "◆" if out.unicode else "*"
        print(out(f"{mark} LOCAL·AGENT" if out.unicode else "* LOCAL AGENT", BOLD, CYAN)
              + out(f"  session {session} · model {model} · /exit to leave", DIM))
        if tui_hint:
            print(out('  For the full-screen interface: pip install -e ".[tui]"', DIM))
    else:
        print(f"Session {session!r}. Enter /exit to leave.")
    spinner = Spinner(Style(sys.stderr))

    def paused(ask):
        """Stop the spinner while asking, so the question isn't drawn over."""
        def ask_with_pause(value):
            spinner.stop()
            try:
                return ask(value)
            finally:
                spinner.start()
        return ask_with_pause

    agent.approve = paused(approve) if agent.approve is not None else None
    agent.confirm = paused(agent.confirm) if agent.confirm is not None else None
    agent.on_event = spinner.on_event if spinner.active else None
    while True:
        prompt = input(out("You> ", BOLD, PURPLE) if styled else "You> ").strip()
        if prompt == "/exit":
            return 0
        if not prompt:
            continue
        spinner.label = "thinking"
        try:
            with spinner:
                reply = agent.turn(session, prompt, learn)
            print((out("Agent> ", BOLD, CYAN) if styled else "Agent> ") + reply)
        except KeyboardInterrupt:
            spinner.stop()
            print(out("Cancelled. Nothing from this turn was saved.", YELLOW) if styled else "Cancelled.",
                  file=sys.stderr)
        except errors as error:
            print(out(f"Error: {printable(error)}", RED) if styled else f"Error: {error}", file=sys.stderr)
