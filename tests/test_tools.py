"""Tests for file, patch, terminal, todo, session search, web and compression tools."""
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from local_agent import tools
from local_agent.__main__ import auto_confirm, main
from local_agent.agent import Agent, TurnCancelled, strip_reasoning
from local_agent.provider import ChatProvider
from local_agent.state import SCHEMA_VERSION, State


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((json.loads(json.dumps(messages)), tools))
        return self.replies.pop(0)


def text(content):
    return {"role": "assistant", "content": content}


def call(tool, call_id="c1", /, **arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)},
    }]}


def tool_results(provider):
    messages, _ = provider.requests[-1]
    return [json.loads(m["content"]) for m in messages if m["role"] == "tool"]


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = State(self.root / "state")
        self.workspace = self.root / "work"
        self.workspace.mkdir()
        self.asked = []

    def confirm(self, answer=True):
        return lambda action: self.asked.append(action) or answer

    def agent(self, provider, **options):
        options.setdefault("confirm", self.confirm())
        return Agent(provider, self.state, self.workspace, **options)


class FileToolTests(Base):
    def test_read_file_pages_with_line_numbers(self):
        (self.workspace / "a.txt").write_text("\n".join(f"row {i}" for i in range(1, 1001)), encoding="utf-8")
        files = tools.Workspace(self.workspace, 300)
        first = files.read_file("a.txt")
        self.assertTrue(first["text"].startswith("   1| row 1\n   2| row 2"))
        self.assertTrue(first["truncated"])
        second = files.read_file("a.txt", first["next_start_line"])
        self.assertEqual(second["start_line"], first["end_line"] + 1)
        self.assertEqual(second["total_lines"], 1000)
        with self.assertRaisesRegex(ValueError, "1000 lines"):
            files.read_file("a.txt", 5000)
        (self.workspace / "bin.dat").write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(ValueError):
            files.read_file("bin.dat")

    def test_search_files_by_pattern_and_glob(self):
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "main.py").write_text("def main():\n    return 42\n", encoding="utf-8")
        (self.workspace / "notes.md").write_text("main ideas\n", encoding="utf-8")
        (self.workspace / ".secret").write_text("def main(): pass\n", encoding="utf-8")
        (self.workspace / "node_modules").mkdir()
        (self.workspace / "node_modules" / "x.py").write_text("def main(): pass\n", encoding="utf-8")
        files = tools.Workspace(self.workspace)
        self.assertEqual(files.search_files(r"def \w+")["matches"], ["src/main.py:1: def main():"])
        self.assertEqual(files.search_files(glob="*.MD")["matches"], ["notes.md"])
        self.assertEqual(files.search_files("main", "*.py")["matches"], ["src/main.py:1: def main():"])
        with self.assertRaisesRegex(ValueError, "regular expression"):
            files.search_files("(")
        with self.assertRaises(ValueError):
            files.search_files()

    def test_write_file_asks_first_and_creates_folders(self):
        provider = Scripted(call("write_file", path="docs/new.md", content="# Title\n"), text("Written."))
        self.agent(provider, write=True).turn("s", "Write it")
        self.assertEqual((self.workspace / "docs" / "new.md").read_text(encoding="utf-8"), "# Title\n")
        self.assertEqual(self.asked[0]["tool"], "write_file")
        self.assertIn("Create docs/new.md", self.asked[0]["summary"])
        self.assertIn("+# Title", self.asked[0]["detail"])
        self.assertTrue(tool_results(provider)[0]["created"])

    def test_declined_actions_change_nothing(self):
        provider = Scripted(call("write_file", path="x.txt", content="hi"), text("Okay, I won't."))
        self.agent(provider, write=True, confirm=self.confirm(False)).turn("s", "Write it")
        self.assertFalse((self.workspace / "x.txt").exists())
        self.assertIn("declined", tool_results(provider)[0]["error"])
        provider = Scripted(call("write_file", path="x.txt", content="hi"), text("No."))
        Agent(provider, self.state, self.workspace, write=True).turn("s", "Write it")  # No confirm: declined.
        self.assertFalse((self.workspace / "x.txt").exists())

    def test_write_refuses_hidden_linked_and_outside_paths(self):
        files = tools.Workspace(self.workspace)
        for path in ("../escape.txt", ".git/config", "a/../../escape.txt", "C:/x.txt", ".", "con"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                files.plan_write(path, "x")
        (self.workspace / "dir").mkdir()
        with self.assertRaises(ValueError):
            files.plan_write("dir", "x")

    def test_patch_replaces_one_exact_match(self):
        target = self.workspace / "app.py"
        target.write_bytes(b"a = 1\r\nb = 2\r\nb = 2\r\n")
        files = tools.Workspace(self.workspace)
        with self.assertRaisesRegex(ValueError, "2 times"):
            files.plan_patch("app.py", "b = 2", "b = 3")
        with self.assertRaisesRegex(ValueError, "not found"):
            files.plan_patch("app.py", "c = 3", "c = 4")
        plan = files.plan_patch("app.py", "a = 1\nb = 2\n", "a = 10\nb = 2\n")  # \n matched against \r\n
        self.assertIn("+a = 10", plan["diff"])
        files.apply(plan)
        self.assertEqual(target.read_bytes(), b"a = 10\r\nb = 2\r\nb = 2\r\n")

    def test_line_numbers_copied_from_read_file_are_not_saved(self):
        files = tools.Workspace(self.workspace)
        (self.workspace / "page.html").write_text("<p>\n  hi\n</p>\n", encoding="utf-8")
        shown = files.read_file("page.html")["text"]
        self.assertEqual(shown.splitlines()[1], "   2|   hi")
        plan = files.plan_write("page.html", shown.replace("hi", "hello") + "\n")
        self.assertEqual(plan["content"], "<p>\n  hello\n</p>\n")
        self.assertIn("prefixes", files.apply(plan)["note"])
        plan = files.plan_patch("page.html", "   2|   hello", "   2|   bye")
        self.assertEqual(plan["content"], "<p>\n  bye\n</p>\n")
        table = "| a | b |\n|---|---|\n| 1 | 2 |\n"  # Real content that looks similar is untouched.
        self.assertEqual(files.plan_write("t.md", table)["content"], table)
        self.assertNotIn("note", files.plan_write("t.md", table))

    def test_patch_through_a_turn(self):
        (self.workspace / "x.txt").write_text("hello world\n", encoding="utf-8")
        provider = Scripted(call("patch", path="x.txt", old_text="world", new_text="there"), text("Edited."))
        self.agent(provider, write=True).turn("s", "Edit")
        self.assertEqual((self.workspace / "x.txt").read_text(encoding="utf-8"), "hello there\n")
        self.assertEqual(self.asked[0]["summary"], "Edit x.txt")

    def test_patch_refuses_edits_made_during_approval(self):
        target = self.workspace / "x.txt"
        target.write_text("original", encoding="utf-8")

        def approve(action):
            target.write_text("concurrent edit", encoding="utf-8")
            return True

        provider = Scripted(call("patch", path="x.txt", old_text="original", new_text="approved"), text("Stopped."))
        self.agent(provider, write=True, confirm=approve).turn("s", "Edit")
        self.assertIn("request approval again", tool_results(provider)[0]["error"])
        self.assertEqual(target.read_text(encoding="utf-8"), "concurrent edit")
        self.assertEqual(list(self.workspace.iterdir()), [target])

    def test_write_refuses_a_file_created_after_planning(self):
        files = tools.Workspace(self.workspace)
        plan = files.plan_write("x.txt", "approved")
        target = self.workspace / "x.txt"
        target.write_text("created elsewhere", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "request approval again"):
            files.apply(plan)
        self.assertEqual(target.read_text(encoding="utf-8"), "created elsewhere")

    def test_write_and_shell_need_a_workspace(self):
        with self.assertRaisesRegex(ValueError, "workspace"):
            Agent(None, self.state, write=True)
        with self.assertRaisesRegex(ValueError, "workspace"):
            Agent(None, self.state, shell=True)

    def test_tools_appear_only_when_enabled(self):
        names = lambda **flags: Agent(None, self.state, self.workspace, **flags).tool_names()
        self.assertNotIn("write_file", names())
        self.assertIn("patch", names(write=True))
        self.assertIn("terminal", names(shell=True))
        self.assertIn("web_extract", names(web=True))
        self.assertEqual(Agent(None, self.state).tool_names(),
                         ["read_skill", "todo", "session_search", "create_artifact", "list_artifacts", "read_artifact"])


class TerminalTests(Base):
    def test_runs_in_the_workspace_and_reports_exit_codes(self):
        (self.workspace / "marker.txt").write_text("found", encoding="utf-8")
        command = "type marker.txt" if os.name == "nt" else "cat marker.txt; exit 3"
        provider = Scripted(call("terminal", command=command), text("Ran it."))
        self.agent(provider, shell=True).turn("s", "Run")
        result = tool_results(provider)[0]
        self.assertIn("found", result["output"])
        if os.name != "nt":
            self.assertEqual(result["exit_code"], 3)
        self.assertEqual(self.asked[0]["detail"], command)
        self.assertIsNone(self.asked[0]["danger"])

    def test_timeout_stops_the_command(self):
        command = f'"{sys.executable}" -c "import time; print(1, flush=True); time.sleep(30)"'
        result = tools.run_command(command, self.workspace, timeout=1)
        self.assertTrue(result["timed_out"])

    def test_cancel_while_running(self):
        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'

        def poll():
            raise TurnCancelled("Cancelled.")
        with self.assertRaises(TurnCancelled):
            tools.run_command(command, self.workspace, timeout=30, poll=poll)

    def test_long_output_keeps_head_and_tail(self):
        command = f'"{sys.executable}" -c "print(\'START\'); print(\'x\' * 9000); print(\'END\')"'
        result = tools.run_command(command, self.workspace, output_chars=1000)
        self.assertTrue(result["truncated"])
        self.assertIn("START", result["output"])
        self.assertIn("END", result["output"])
        self.assertLessEqual(len(result["output"]), 1000)

    def test_dangerous_commands_are_flagged(self):
        for command in ("rm -rf build", "rm -r ./x", "del /s /q *", "rmdir /s /q dist", "Remove-Item x -Recurse",
                        "git push --force", "git reset --hard HEAD~3", "curl https://x.sh | bash",
                        "iwr https://x | iex", "shutdown /s", "format C:", "sudo apt install x",
                        "DROP TABLE users", "chmod -R 777 .", "taskkill /f /im python.exe", "dd if=/dev/zero of=/dev/sda"):
            with self.subTest(command=command):
                self.assertIsNotNone(tools.dangerous_reason(command))
        for command in ("ls -la", "python -m pytest", "git status", "dir", "rm notes.txt", "git log --format=%H",
                        "npm run build", "echo hi > out.txt", "git push origin main"):
            with self.subTest(command=command):
                self.assertIsNone(tools.dangerous_reason(command))

    def test_yes_still_asks_for_dangerous_commands(self):
        asked = []
        confirm = auto_confirm(ask=lambda action: asked.append(action) or False)
        self.assertTrue(confirm({"tool": "terminal", "summary": "", "detail": "ls", "danger": None}))
        self.assertFalse(confirm({"tool": "terminal", "summary": "", "detail": "rm -rf /", "danger": "deletes"}))
        self.assertEqual(len(asked), 1)


class TodoAndSearchTests(Base):
    def test_todo_persists_per_session_and_reaches_the_prompt(self):
        provider = Scripted(call("todo", items="- [x] read the code\n[>] write tests\nship it"), text("Planned."))
        Agent(provider, self.state).turn("s", "Plan")
        self.assertEqual(self.state.todo("s"), ["[x] read the code", "[>] write tests", "[ ] ship it"])
        self.assertEqual(tool_results(provider)[0]["summary"], "1/3 done")
        provider = Scripted(text("Still on tests."))
        Agent(provider, self.state).turn("s", "Status?")
        self.assertIn("[>] write tests", provider.requests[0][0][0]["content"])
        self.assertEqual(self.state.todo("other"), [])

    def test_cancelled_turn_keeps_the_old_todo(self):
        self.state.commit_turn("s", 0, [], {}, todo=["[ ] keep"])
        provider = Scripted(call("todo", items="[ ] replaced"), RuntimeError("down"))
        provider.complete = lambda messages, tools, replies=provider.replies: (
            (_ for _ in ()).throw(replies.pop(0)) if isinstance(replies[0], Exception) else replies.pop(0))
        with self.assertRaises(RuntimeError):
            Agent(provider, self.state).turn("s", "Plan")
        self.assertEqual(self.state.todo("s"), ["[ ] keep"])

    def test_todo_limits(self):
        self.assertEqual(tools.parse_todo("1. one\n2) two\n\n* three"), ["[ ] one", "[ ] two", "[ ] three"])
        with self.assertRaises(ValueError):
            tools.parse_todo("\n".join(f"task {i}" for i in range(31)))

    def test_session_search_finds_earlier_conversations(self):
        self.state.commit_turn("travel", 0, [{"role": "user", "content": "Book a flight to Chiang Mai in March"},
                                             text("I'll look at flights.")], {})
        self.state.commit_turn("code", 0, [{"role": "user", "content": "Fix the 100% CPU bug"}, text("Done.")], {})
        results = self.state.search("chiang flight")
        self.assertEqual([(r["session"], r["role"]) for r in results], [("travel", "user")])
        self.assertIn("Chiang Mai", results[0]["excerpt"])
        self.assertEqual(self.state.search("100%")[0]["session"], "code")
        self.assertEqual(self.state.search("_"), [])
        provider = Scripted(call("session_search", query="Chiang Mai"), text("You asked about flights."))
        Agent(provider, self.state).turn("new", "What trip did I plan?")
        self.assertEqual(tool_results(provider)[0]["results"][0]["session"], "travel")

    def test_delete_session(self):
        self.state.commit_turn("old", 0, [{"role": "user", "content": "hi"}], {}, todo=["[ ] x"])
        self.assertEqual(self.state.delete_session("old"), 1)
        self.assertEqual(self.state.sessions(), [])
        self.assertEqual(self.state.todo("old"), [])


class ReliabilityTests(Base):
    def test_think_blocks_are_not_saved(self):
        self.assertEqual(strip_reasoning("<think>\nplan\n</think>\n\nAnswer"), "Answer")
        self.assertEqual(strip_reasoning("Plain <think>kept</think>"), "Plain <think>kept</think>")
        self.assertEqual(strip_reasoning("<think>never closed"), "")
        provider = Scripted(text("<think>long reasoning " + "x" * 500 + "</think>Short answer."))
        self.assertEqual(Agent(provider, self.state).turn("s", "Hi"), "Short answer.")
        self.assertNotIn("think", json.dumps(list(self.state.transcript("s"))))

    def test_declined_memory_is_not_reported_as_staged_later(self):
        provider = Scripted(call("remember", name="style", value="terse"), text("Saved."))
        Agent(provider, self.state, approve=lambda changes: False).turn("s", "Remember", learn=True)
        saved = [m for m in self.state.transcript("s") if m["role"] == "tool"]
        self.assertIn("declined", json.loads(saved[0]["content"])["error"])

    def test_missing_tool_call_ids_and_empty_arguments_are_accepted(self):
        provider = ChatProvider("http://localhost:11434/v1", "model", None)
        provider.request_json = lambda request: {"choices": [{"message": {"content": None, "tool_calls": [
            {"type": "function", "function": {"name": "todo", "arguments": ""}},
            {"function": {"name": "read_skill", "arguments": {"name": "x"}}},
        ]}}]}
        reply = provider.complete([], [])
        self.assertEqual([c["id"] for c in reply["tool_calls"]], ["call_0", "call_1"])
        self.assertEqual(reply["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertEqual(json.loads(reply["tool_calls"][1]["function"]["arguments"]), {"name": "x"})

    def test_argument_validation(self):
        self.assertEqual(tools.validate_arguments("read_file", {"path": "a", "start_line": "5"}),
                         {"path": "a", "start_line": 5})
        for args in ({"path": "a", "start_line": 0}, {"path": "a", "start_line": True}, {"path": ""},
                     {"path": "a", "extra": 1}, {}, []):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "declared parameters"):
                tools.validate_arguments("read_file", args)
        self.assertEqual(tools.validate_arguments("patch", {"path": "a", "old_text": "x", "new_text": ""})["new_text"], "")

    def test_schema_upgrade_from_an_unversioned_database(self):
        folder = self.root / "legacy"
        folder.mkdir()
        with closing(sqlite3.connect(folder / "state.sqlite3")) as db, db:
            db.executescript("""
                CREATE TABLE turns (session TEXT NOT NULL, number INTEGER NOT NULL, messages TEXT NOT NULL,
                                    PRIMARY KEY (session, number));
                CREATE TABLE library (kind TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
                                      PRIMARY KEY (kind, name));
                INSERT INTO turns VALUES ('demo', 1, '[{"role": "user", "content": "hi"}]');
            """)
        state = State(folder)
        self.assertEqual(state.sessions(), [("demo", 1)])
        self.assertEqual(state.todo("demo"), [])
        with closing(sqlite3.connect(folder / "state.sqlite3")) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        with self.assertRaisesRegex(RuntimeError, "newer version"):
            State(folder)


class Page(BaseHTTPRequestHandler):
    pages = {}

    def do_GET(self):
        Page.last_host = self.headers.get("Host")
        status, kind, body, headers = Page.pages.get(self.path.split("?")[0], (404, "text/plain", "missing", {}))
        if self.path.startswith("/search"):
            Page.last_query = self.path
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class WebTests(Base):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_extract_readable_text_and_follow_redirects(self):
        Page.pages = {
            "/article": (200, "text/html; charset=utf-8", "<html><head><title>Hello Page</title><style>p{}</style>"
                         "</head><body><nav>Menu</nav><p>First   para.</p><script>alert(1)</script><p>Second</p>"
                         "</body></html>", {}),
            "/old": (301, "text/plain", "", {"Location": "/article"}),
        }
        result = tools.web_extract(self.base + "/old", allow_private=True)
        self.assertEqual(result["title"], "Hello Page")
        self.assertEqual(result["text"], "Menu\nFirst para.\nSecond")
        self.assertTrue(result["url"].endswith("/article"))

    def test_private_addresses_are_refused(self):
        for url in (self.base + "/article", "http://localhost/", "http://10.0.0.1/", "file:///etc/passwd",
                    "http://user:pw@example.com/", "http://@example.com/", "http://[::ffff:127.0.0.1]/",
                    "http://224.0.0.1/", "http://[fec0::1]/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                tools.web_extract(url)

    def test_nonpublic_answers_are_rejected_before_any_connection(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 80))
        for address in ("::127.0.0.1", "::ffff:127.0.0.1", "64:ff9b:1::7f00:1",
                        "64:ff9b::7f00:1", "2002:7f00:1::1", "3fff::1", "::ffff:192.0.0.8"):
            with self.subTest(address=address):
                private = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 80, 0, 0))
                with mock.patch("socket.getaddrinfo", return_value=[public, private]), \
                     mock.patch("socket.socket") as connect:
                    with self.assertRaisesRegex(ValueError, "Private, local and reserved"):
                        tools.web_extract("http://fixture.test/")
                    connect.assert_not_called()
        self.assertTrue(tools.is_public_address("64:ff9b::808:808"))

    def test_fetch_connects_to_the_checked_address_and_checks_redirects(self):
        Page.pages = {
            "/article": (200, "text/plain", "Public article", {}),
            "/redirect": (302, "text/plain", "", {"Location": self.base + "/article"}),
        }
        loopback = self.server.server_address
        public = ("93.184.215.14", loopback[1])
        port = loopback[1]
        resolver = socket.getaddrinfo
        connect = socket.socket.connect
        peers = []
        lookups = []

        def connect_fixture(peer, address):
            peers.append(address)
            self.assertEqual(address, public)
            return connect(peer, loopback)

        def resolve_fixture(host, port, *args, **kwargs):
            lookups.append(host)
            address = public[0] if host == "rebind.test" and len(lookups) == 1 else loopback[0]
            return resolver(address, port, *args, **kwargs)

        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "no_proxy": ""}), \
             mock.patch("socket.getaddrinfo", side_effect=resolve_fixture) as dns, \
             mock.patch.object(socket.socket, "connect", connect_fixture):
            result = tools.web_extract(f"http://rebind.test:{port}/article")
            self.assertEqual(result["text"], "Public article")
            self.assertEqual(Page.last_host, f"rebind.test:{port}")
            dns.assert_called_once_with("rebind.test", port, type=socket.SOCK_STREAM)
            peers.clear()
            lookups.clear()
            with self.assertRaisesRegex(ValueError, "Private, local and reserved"):
                tools.web_extract(f"http://rebind.test:{port}/redirect")
            self.assertEqual(peers, [public])

    def test_search_parses_results(self):
        Page.pages = {"/search": (200, "text/html", """
            <div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">
            Example <b>A</b></a><a class="result__snippet">About   A.</a></div>
            <div class="result"><a class="result__a" href="https://example.org/b">B</a>
            <div class="result__snippet">About B.</div></div>""", {})}
        result = tools.web_search("local agent", search_url=self.base + "/search", allow_private=True)
        self.assertEqual(result["results"], [
            {"title": "Example A", "url": "https://example.com/a", "snippet": "About A."},
            {"title": "B", "url": "https://example.org/b", "snippet": "About B."},
        ])
        self.assertIn("q=local+agent", Page.last_query)


class CommandLineTests(Base):
    def run_cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output), mock.patch("sys.stderr", io.StringIO()) as errors:
            code = main(["--data-dir", str(self.root / "state"), *args])
        return code, output.getvalue() + errors.getvalue()

    def test_memory_skill_session_and_search_commands(self):
        self.assertEqual(self.run_cli("remember", "style", "concise")[0], 0)
        self.assertIn("style  concise", self.run_cli("memory")[1])
        self.assertEqual(self.run_cli("forget", "style")[0], 0)
        self.assertEqual(self.state.library("memory"), {})
        self.assertEqual(self.run_cli("forget", "style")[0], 1)
        self.assertEqual(self.run_cli("remember", "Bad Name", "x")[0], 1)
        self.state.update_library({("skill", "release"): {"description": "Ship", "steps": "1. go"}})
        self.assertEqual(self.run_cli("skill", "release", "--delete")[0], 0)
        self.assertEqual(self.state.library("skill"), {})
        self.state.commit_turn("trip", 0, [{"role": "user", "content": "Visit Bangkok"}], {}, todo=["[ ] pack"])
        self.assertIn("trip #1 user> Visit Bangkok", self.run_cli("search", "bangkok")[1])
        self.assertIn("[ ] pack", self.run_cli("todo", "trip")[1])
        self.assertEqual(self.run_cli("delete-session", "trip")[0], 0)
        self.assertEqual(self.run_cli("delete-session", "trip")[0], 1)

    def test_write_and_shell_require_a_workspace(self):
        (self.root / "state").mkdir(exist_ok=True)
        (self.root / "state" / "config.json").write_text('{"base_url": "http://127.0.0.1:9/v1", "model": "m"}')
        code, output = self.run_cli("chat", "--shell", "hello")
        self.assertEqual(code, 1)
        self.assertIn("--workspace", output)


if __name__ == "__main__":
    unittest.main()
