"""Agent teams: configuration, routing, permissions, orchestration, approvals, limits, cancellation,
failures, persistence and task outcomes. Every model here is a deterministic fake; no credentials or
network are used."""
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from local_agent import orchestrator, teams
from local_agent.__main__ import main
from local_agent.config import save_config
from local_agent.orchestrator import RunStore, TeamRun, preflight
from local_agent.projects import ProjectRegistry
from local_agent.state import State
from local_agent.teams import TeamError, TeamStore, assign, preset, validate


def call(name, **arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": f"c-{name}-{time.monotonic_ns()}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def text(content):
    return {"role": "assistant", "content": content}


class Fake:
    """A scripted model. script(fake, assignment, attempt, messages, tools) returns the reply."""

    def __init__(self, spec, script, delay=0.0, usage=True):
        self.spec, self.script, self.delay, self.usage = spec, script, delay, usage
        self.streaming = True
        self.timeout = None
        self.requests = []
        self.last_usage = None
        self.counts = {}

    def complete(self, messages, tools, on_text=None):
        self.requests.append((json.loads(json.dumps(messages)), tools))
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        assignment = re.search(r"## Assignment (\S+):", user).group(1)
        self.counts[assignment] = self.counts.get(assignment, 0) + 1
        reply = self.script(self, assignment, self.counts[assignment], messages, tools)
        if isinstance(reply, Exception):
            raise reply
        time.sleep(self.delay)
        if on_text is not None and reply.get("content"):
            for index in range(0, len(reply["content"]), 8):
                on_text("content", reply["content"][index:index + 8])
                time.sleep(self.delay / 4)
        self.last_usage = {"prompt_tokens": 100, "completion_tokens": 10} if self.usage else None
        return reply

    def check(self):
        return "online", "fake"


def tool_names(tools):
    return [tool["function"]["name"] for tool in tools]


def standard(fake, assignment, attempt, messages, tools):
    """A cooperative team: a two-step plan, a file change and a check, an approving review."""
    names = tool_names(tools)
    if assignment == "plan":
        if attempt == 1:
            return call("submit_plan", subtasks=[
                {"id": "inspect", "title": "Find the greeting", "agent": "reviewer",
                 "instructions": "Find where the greeting lives.", "expected_output": "The file"},
                {"id": "change", "title": "Update the greeting", "agent": "implementer",
                 "instructions": "Change hello to hi.", "expected_output": "Changed file", "depends_on": ["inspect"]}])
        return text("Plan submitted.")
    if assignment == "inspect":
        return call("submit_result", summary="The greeting is in app.txt", status="done") if attempt == 1 \
            else text("SECRET-THOUGHT of the reviewer. Done.")
    if assignment == "change":
        steps = [call("write_file", path="app.txt", content="hi\n"), call("terminal", command="echo checked"),
                 call("submit_result", summary="Changed app.txt to hi", status="done", files=["app.txt"])]
        return steps[attempt - 1] if attempt <= len(steps) else text("IMPLEMENTER-PRIVATE-NOTE all done.")
    if assignment.startswith("review"):
        return call("submit_review", verdict="approve", summary="Looks right", criteria=[
            {"criterion": "Greeting says hi", "met": "yes", "evidence": "app.txt"}]) if attempt == 1 else text("ok")
    if assignment == "synthesis":
        return call("submit_synthesis", summary="Greeting updated.", verification="echo checked passed",
                    unresolved=[], disagreements=[]) if attempt == 1 else text("Done.")
    if assignment.startswith("proposal") or assignment.startswith("critique"):
        return call("submit_result", summary=f"{fake.spec.id} says {assignment}", status="done") if attempt == 1 \
            else text("ok")
    if assignment.startswith("inspection"):
        return call("submit_review", verdict="approve", summary=f"{fake.spec.id} inspected",
                    findings=[{"severity": "low", "summary": "minor"}]) if attempt == 1 else text("ok")
    return text("unexpected")


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        clean = {key: value for key, value in os.environ.items()
                 if not key.endswith("_API_KEY") and not key.startswith("AGENT_")}
        clean["AGENT_KEYS_FILE"] = str(self.root / "keys" / "keys.json")
        env = mock.patch.dict(os.environ, clean, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.data = self.root / "data"
        save_config(self.data, "http://127.0.0.1:11434/v1", "qwen3:4b", 12_000, provider="ollama", name="local")
        save_config(self.data, "https://api.deepseek.com/v1", "deepseek-chat", 100_000, provider="deepseek",
                    name="deepseek")
        save_config(self.data, "http://127.0.0.1:11434/v1", "qwen3:4b", 12_000, provider="ollama", name="local")
        os.environ["DEEPSEEK_API_KEY"] = "sk-test-secret-key-1234567890"
        self.folder = self.root / "app"
        self.folder.mkdir()
        (self.folder / "app.txt").write_text("hello\n", encoding="utf-8")
        self.project = ProjectRegistry(self.data).add(self.folder, "App")
        self.state = State(self.data)
        self.fakes = []

    def factory(self, script=standard, delay=0.0, usage=True, fail=None):
        def make(spec, timeout):
            if fail and fail(spec):
                fake = Fake(spec, lambda *a: RuntimeError(f"{spec.label()} is unreachable"), delay, usage)
            else:
                fake = Fake(spec, script, delay, usage)
            fake.timeout = timeout
            self.fakes.append(fake)
            return fake
        return make

    def team(self, workflow="implement", **limits):
        team = preset(workflow, "local")
        team["limits"].update(limits)
        return team

    def run_team(self, team=None, prompt="Change the greeting to hi", script=standard, approve=None, **options):
        approvals = []

        def default_approve(request, stop):
            approvals.append(request)
            return True
        run = TeamRun(self.project, self.state, self.data, team or self.team(), prompt,
                      approve=approve or default_approve, provider_factory=options.pop("factory", None) or
                      self.factory(script), **options)
        record = run.run()
        return run, record, approvals


class ConfigurationTests(Base):
    def test_add_second_writer_through_cli(self):
        prefix = ["--data-dir", str(self.data), "team"]
        project = ["--project", self.project.id]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(prefix + ["parallel", "implement", "on", *project]), 0)
            self.assertEqual(main(prefix + ["add-writer", "implement", "writer-b", "--model", "b", *project]), 0)
        team = TeamStore(self.data, self.project.id).get("implement")
        writers = [spec for spec in teams.resolve(team, self.data)[0] if spec.writes]
        self.assertEqual([(spec.id, spec.model) for spec in writers], [("implementer", "qwen3:4b"), ("writer-b", "b")])
        with self.assertRaises(TeamError):
            teams.add_writer(team, "writer-b")

    def test_presets_validate_and_report_missing_profiles_models_and_keys(self):
        for workflow in teams.WORKFLOWS:
            self.assertEqual([p for p in validate(preset(workflow, "local"), self.data) if p.level == "error"], [])
        team = preset("implement", "local")
        with self.assertRaisesRegex(TeamError, "read-only role"):
            assign(team, "reviewer", tools=["read", "shell"])
        team["agents"][2]["tools"] = ["read", "shell"]  # As if teams.json were edited by hand.
        assign(team, "coordinator", profile="nope")
        assign(team, "implementer", profile="deepseek", model="deepseek-reasoner")
        del os.environ["DEEPSEEK_API_KEY"]
        team["limits"]["concurrency"] = 99
        messages = [str(problem) for problem in validate(team, self.data)]
        self.assertTrue(any("reviewer" in m and "read-only" in m for m in messages))
        self.assertTrue(any("profile 'nope' is not saved" in m for m in messages))
        self.assertTrue(any("no API key for profile 'deepseek'" in m and "key set deepseek" in m for m in messages))
        self.assertTrue(any("concurrency" in m for m in messages))
        with self.assertRaisesRegex(TeamError, "can't run"):
            teams.resolve(team, self.data)
        single = preset("implement", "local")
        single["agents"].append(teams.member("implementer-2", "implementer", "local"))
        self.assertTrue(any("Only one agent may change files" in str(p) for p in validate(single, self.data)))

    def test_team_store_is_project_scoped_and_never_holds_keys(self):
        store = TeamStore(self.data, self.project.id)
        self.assertEqual(list(store.teams()), ["brainstorm", "implement", "review"])
        team = store.get("implement")
        assign(team, "implementer", profile="deepseek", model="deepseek-reasoner",
               fallback={"profile": "local", "model": "qwen3:8b"})
        store.save_team("implement", team)
        raw = store.file.read_text(encoding="utf-8")
        self.assertNotIn("sk-test-secret", raw)
        self.assertEqual(store.get("implement")["agents"][1]["model"], "deepseek-reasoner")
        self.assertEqual(TeamStore(self.data, "other").get("implement")["agents"][1]["profile"], "local")
        store.file.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(TeamError, "chat still works"):
            store.teams()

    def test_broken_team_files_do_not_stop_ordinary_chat(self):
        store = TeamStore(self.data, self.project.id)
        store.file.parent.mkdir(parents=True, exist_ok=True)
        store.file.write_text("{broken", encoding="utf-8")
        provider = Fake(None, lambda *a: text("Single agent reply."))
        provider.complete = lambda messages, tools, on_text=None: text("Single agent reply.")
        with mock.patch("local_agent.providers.make_provider", return_value=provider), \
                redirect_stdout(io.StringIO()) as output, mock.patch("sys.stderr", io.StringIO()):
            code = main(["--data-dir", str(self.data), "--profile", "local", "chat", "--project", "app", "hi"])
        self.assertEqual(code, 0)
        self.assertIn("Single agent reply.", output.getvalue())


class RoutingTests(Base):
    def test_same_model_team_gets_separate_provider_instances(self):
        run, record, _ = self.run_team()
        self.assertEqual(record["state"], "completed", record["notes"])
        self.assertEqual(len(self.fakes), 3)
        self.assertEqual(len({id(fake) for fake in self.fakes}), 3)
        self.assertEqual({fake.spec.label() for fake in self.fakes}, {"local/qwen3:4b"})
        self.assertEqual({agent["model"] for agent in record["agents"].values()}, {"qwen3:4b"})
        self.assertTrue(all(fake.timeout == 180 for fake in self.fakes))

    def test_mixed_providers_and_models_are_routed_and_reported(self):
        team = self.team()
        assign(team, "implementer", profile="deepseek")
        assign(team, "reviewer", model="qwen3:8b")
        run, record, _ = self.run_team(team)
        routes = {fake.spec.id: (fake.spec.provider, fake.spec.model, fake.spec.base_url) for fake in self.fakes}
        self.assertEqual(routes["implementer"], ("deepseek", "deepseek-chat", "https://api.deepseek.com/v1"))
        self.assertEqual(routes["reviewer"][:2], ("ollama", "qwen3:8b"))
        self.assertEqual(routes["coordinator"][:2], ("ollama", "qwen3:4b"))
        self.assertEqual(record["agents"]["implementer"]["profile"], "deepseek")
        self.assertNotIn("sk-test-secret", json.dumps(record))
        for fake in self.fakes:
            for messages, _ in fake.requests:
                self.assertNotIn("sk-test-secret", json.dumps(messages))

    def test_usage_is_attributed_and_estimates_are_kept_apart(self):
        run, record, _ = self.run_team(factory=self.factory(usage=False))
        usage = record["agents"]["implementer"]["usage"]
        self.assertEqual((usage["prompt"], usage["completion"]), (0, 0))
        self.assertGreater(usage["estimated"], 0)
        run, record, _ = self.run_team()
        self.assertEqual(record["agents"]["implementer"]["usage"]["estimated"], 0)
        self.assertGreater(record["agents"]["implementer"]["usage"]["prompt"], 0)


class WorkflowTests(Base):
    def test_implement_run_plans_executes_reviews_and_records_everything(self):
        task = self.project.knowledge.create("task", "Say hi", "# Say hi\n\n## Acceptance criteria\n\n"
                                             "- [ ] Greeting says hi\n\n## Outcome\n\n_Not recorded yet._\n")
        run, record, approvals = self.run_team(task="say hi", session="main")
        self.assertEqual(record["state"], "completed", record["notes"])
        self.assertEqual((self.folder / "app.txt").read_text(encoding="utf-8"), "hi\n")
        order = {item["id"]: item for item in record["subtasks"]}
        self.assertLessEqual(order["inspect"]["finished"], order["change"]["started"])  # Dependencies respected.
        self.assertEqual([item["id"] for item in record["subtasks"]],
                         ["plan", "inspect", "change", "review-reviewer", "synthesis"])
        self.assertEqual([(a["agent"], a["role"], a["project"], a["tool"]) for a in approvals],
                         [("implementer", "Implementer", "App", "write_file"), ("implementer", "Implementer", "App",
                                                                                 "terminal")])
        self.assertEqual(approvals[0]["target"], "app.txt")
        self.assertEqual(approvals[1]["target"], "echo checked")
        self.assertEqual(record["changes"][0]["path"], "app.txt")
        self.assertEqual([(e["command"], e["exit_code"]) for e in record["evidence"]], [("echo checked", 0)])
        result = record["result"]
        self.assertEqual((result["verification"], result["changes"]), ("passed", ["app.txt"]))
        self.assertEqual(result["coordinator_verification"], "echo checked passed")
        # The project task gets the outcome, but stays open until the user completes it.
        task = self.project.knowledge.get(task.id)
        self.assertEqual(task.status, "in-progress")
        self.assertIn(f"**Team run:** `{run.id}`", task.body)
        self.assertIn("echo checked → exit 0", task.body)
        self.assertIn("implementer implementer local/qwen3:4b", task.body)
        # The main conversation gets one readable turn; agent transcripts stay in the run's own scope.
        scoped = self.state.for_project("app")
        self.assertEqual(scoped.sessions(), [("main", 1)])
        self.assertIn("Team run", list(scoped.transcript("main"))[-1]["content"])
        self.assertIn(f"{run.id}.implementer", dict(self.state.for_project("app@team").sessions()))
        saved = RunStore(self.data, "app").load(run.id)
        self.assertEqual(saved["state"], "completed")

    def test_read_only_agents_get_no_write_or_shell_tools(self):
        attempted = {}

        def script(fake, assignment, attempt, messages, tools):
            names = tool_names(tools)
            if fake.spec.role != "implementer":
                attempted.setdefault(fake.spec.id, set()).update(names)
            if assignment == "inspect" and attempt == 1:
                return call("write_file", path="hacked.txt", content="x")
            if assignment == "inspect" and attempt == 2:
                return call("terminal", command="echo hacked > hacked2.txt")
            if assignment == "inspect" and attempt == 3:
                return call("submit_result", summary="nothing", status="done")
            return standard(fake, assignment, attempt if assignment != "inspect" else 9, messages, tools)

        run, record, _ = self.run_team(script=script)
        for names in attempted.values():
            self.assertFalse({"write_file", "patch", "terminal"} & names, names)
            self.assertFalse({"remember", "save_skill", "session_search", "read_skill"} & names, names)
        self.assertFalse((self.folder / "hacked.txt").exists())
        self.assertFalse((self.folder / "hacked2.txt").exists())
        reviewer = next(fake for fake in self.fakes if fake.spec.id == "reviewer")
        tool_messages = [m for messages, _ in reviewer.requests for m in messages if m["role"] == "tool"]
        self.assertTrue(any("unavailable" in m["content"] for m in tool_messages))

    def test_context_is_bounded_to_handoffs_and_this_project(self):
        knowledge = self.project.knowledge
        knowledge.create("overview", "App", "# App\n\nA greeting app.\n")
        knowledge.create("decision", "Greeting style", "# Greeting style\n\nGreetings are lowercase.\n")
        other = self.root / "other"
        other.mkdir()
        ProjectRegistry(self.data).add(other, "Other").knowledge.create("note", "Greeting other",
                                                                        "# Greeting other\n\nOTHER-PROJECT-NOTE\n")
        run, record, _ = self.run_team(selected=["decision-greeting-style"])
        reviewer = next(fake for fake in self.fakes if fake.spec.id == "reviewer")
        review_request = next(messages for messages, _ in reviewer.requests
                              if "## Assignment review-reviewer" in json.dumps(messages))
        dumped = json.dumps(review_request)
        self.assertIn("Changed app.txt to hi", dumped)          # The implementer's submitted result.
        self.assertNotIn("IMPLEMENTER-PRIVATE-NOTE", dumped)     # Not its transcript.
        self.assertNotIn("OTHER-PROJECT-NOTE", dumped)
        self.assertIn("Greetings are lowercase", dumped)        # The selected decision.
        self.assertIn("echo checked", dumped)                   # Recorded evidence.
        system = review_request[0]["content"]
        self.assertIn("reference material", system)
        self.assertIn("read-only", system)
        for fake in self.fakes:
            for messages, _ in fake.requests:
                self.assertLessEqual(len(json.dumps(messages)), fake.spec.context_chars + 4_000)

    def test_brainstorm_and_review_workflows(self):
        run, record, _ = self.run_team(self.team("brainstorm"), prompt="How should we cache?")
        ids = [item["id"] for item in record["subtasks"]]
        self.assertEqual(ids, ["proposal-proposer-a", "proposal-proposer-b", "critique-proposer-a",
                               "critique-proposer-b", "synthesis"])
        critique = next(fake for fake in self.fakes if fake.spec.id == "proposer-a")
        request = next(m for m, _ in critique.requests if "## Assignment critique-proposer-a" in json.dumps(m))
        assignment = request[-1]["content"]  # Its own proposal is in its own history; the handoff has the others'.
        self.assertIn("proposer-b says proposal-proposer-b", assignment)
        self.assertNotIn("proposer-a says proposal-proposer-a", assignment)
        self.assertEqual(record["state"], "completed")
        self.fakes.clear()
        run, record, approvals = self.run_team(self.team("review"), prompt="Review the greeting")
        self.assertEqual(approvals, [])
        self.assertEqual(sorted(record["result"]["reviews"]), ["reviewer-a", "reviewer-b"])

    def test_invalid_plan_falls_back_to_an_explicit_default_plan(self):
        def script(fake, assignment, attempt, messages, tools):
            if assignment == "plan":
                if attempt == 1:
                    return call("submit_plan", subtasks=[{"id": "x", "title": "t", "agent": "coordinator",
                                                          "instructions": "i", "expected_output": "o"}])
                return text("I could not plan.")
            if assignment == "implement":
                return standard(fake, "change", attempt, messages, tools)
            return standard(fake, assignment, attempt, messages, tools)

        run, record, _ = self.run_team(script=script)
        self.assertEqual(record["plan"]["source"], "default")
        self.assertIn("did not submit a valid plan", record["plan"]["note"])
        coordinator = next(fake for fake in self.fakes if fake.spec.id == "coordinator")
        tool_errors = [m["content"] for messages, _ in coordinator.requests for m in messages if m["role"] == "tool"]
        self.assertTrue(any("not a team member who can take work" in item for item in tool_errors))
        self.assertEqual(record["state"], "completed")

    def test_unstructured_answers_are_never_counted_as_passing(self):
        def script(fake, assignment, attempt, messages, tools):
            if assignment.startswith("review"):
                return text("LGTM, everything is verified and complete.")
            return standard(fake, assignment, attempt, messages, tools)

        run, record, _ = self.run_team(script=script)
        self.assertEqual(record["result"]["reviews"]["reviewer"]["verdict"], "unknown")
        self.assertIn("reviewer gave no structured verdict.", record["result"]["unresolved"])

    def test_review_and_revision_cycles_are_bounded(self):
        def script(fake, assignment, attempt, messages, tools):
            if assignment.startswith("review"):
                return call("submit_review", verdict="changes_requested", summary="Still wrong",
                            findings=[{"severity": "high", "summary": "Wrong greeting"}]) if attempt == 1 else text("no")
            if assignment.startswith("revision"):
                return call("submit_result", summary="Tried again", status="done") if attempt == 1 else text("ok")
            return standard(fake, assignment, attempt, messages, tools)

        run, record, _ = self.run_team(self.team(revision_rounds=1), script=script)
        kinds = [item["id"] for item in record["subtasks"]]
        self.assertEqual(kinds, ["plan", "inspect", "change", "review-reviewer", "synthesis", "revision-1",
                                 "review-reviewer-1"])
        self.assertTrue(any("after 1 revision cycle" in note for note in record["notes"]))
        self.assertTrue(any("Wrong greeting" in item for item in record["result"]["unresolved"]))
        self.assertTrue(any("implementer reported the work done" in item for item in record["result"]["disagreements"]))


class LimitAndFailureTests(Base):
    def test_failed_coordinator_fails_run(self):
        _, record, _ = self.run_team(factory=self.factory(fail=lambda spec: spec.role == "coordinator"))
        self.assertEqual(record["state"], "failed")
        self.assertEqual([(task["id"], task["state"]) for task in record["subtasks"]], [("plan", "failed")])

    def test_token_budget_with_tool_only_reply_and_no_usage(self):
        _, record, _ = self.run_team(self.team(max_tokens=100), factory=self.factory(usage=False))
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["usage"]["requests"], 1)
        self.assertGreater(record["usage"]["estimated"], 100)
        calls = [{"function": {"arguments": "a" * 400}}]
        counts = orchestrator.reply_usage({"prompt_tokens": 7}, 2000, None, calls)
        self.assertGreaterEqual(counts["estimated"], 100)
        self.assertLess(counts["estimated"], 120)
        self.assertEqual(orchestrator.reply_usage({"total_tokens": 17}, 2000, None, calls)["unattributed"], 17)

    def test_fallback_does_not_repeat_approved_command(self):
        team = self.team()
        assign(team, "implementer", fallback={"profile": "deepseek"})

        def script(fake, assignment, attempt, messages, tools):
            if assignment == "change":
                if attempt == 1:
                    return call("terminal", command="echo effect >> effects.txt")
                return RuntimeError("stream interrupted")
            return standard(fake, assignment, attempt, messages, tools)

        _, record, _ = self.run_team(team, script=script)
        self.assertEqual((self.folder / "effects.txt").read_text().strip(), "effect")
        self.assertFalse(record["fallbacks"])
        change = next(task for task in record["subtasks"] if task["id"] == "change")
        self.assertEqual(change["state"], "failed")
        self.assertIn("already approved", change["error"])

    def test_concurrency_limits_including_local_models(self):
        active, peak, lock = [0], [0], threading.Lock()

        def script(fake, assignment, attempt, messages, tools):
            if attempt == 1 and assignment.startswith("proposal"):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.3)
                with lock:
                    active[0] -= 1
            return standard(fake, assignment, attempt, messages, tools)

        team = self.team("brainstorm", concurrency=2, local_concurrency=2)
        self.run_team(team, script=script)
        self.assertEqual(peak[0], 2)
        peak[0] = 0
        team = self.team("brainstorm", concurrency=2, local_concurrency=1)  # Every agent is on local Ollama.
        self.run_team(team, script=script)
        self.assertEqual(peak[0], 1)

    def test_request_budget_stops_the_run_with_a_partial_record(self):
        run, record, _ = self.run_team(self.team(max_requests=3))
        self.assertEqual(record["state"], "failed")
        self.assertIn("3 model requests", record["stop_reason"])
        self.assertTrue(any("Stopped early" in item for item in record["result"]["unresolved"]))
        self.assertLessEqual(record["usage"]["requests"], 4)

    def test_provider_failure_uses_the_configured_fallback_visibly(self):
        team = self.team()
        assign(team, "implementer", fallback={"profile": "deepseek", "model": "deepseek-chat"})
        factory = self.factory(fail=lambda spec: spec.id == "implementer" and spec.profile == "local")
        run, record, _ = self.run_team(team, factory=factory)
        self.assertEqual(record["state"], "completed", record["notes"])
        self.assertEqual(record["agents"]["implementer"]["fallback"]["from"], "local/qwen3:4b")
        self.assertEqual(record["agents"]["implementer"]["model"], "deepseek-chat")
        self.assertTrue(any("Fallback: implementer switched" in note for note in record["notes"]))
        self.assertIn("via fallback", run.markdown())

    def test_provider_failure_without_fallback_fails_that_work_and_keeps_going(self):
        factory = self.factory(fail=lambda spec: spec.id == "implementer")
        run, record, _ = self.run_team(factory=factory)
        states = {item["id"]: item["state"] for item in record["subtasks"]}
        self.assertEqual((states["change"], states["review-reviewer"], states["synthesis"]),
                         ("failed", "skipped", "completed"))
        self.assertTrue(any("unreachable" in item for item in record["result"]["unresolved"]))
        self.assertEqual(record["result"]["verification"], "none")


class CancellationTests(Base):
    def test_terminal_approval_releases_reader_without_enter(self):
        from local_agent.team_commands import read_approval
        stop = threading.Event()
        reading = threading.Event()
        answers = []
        def idle(*args):
            reading.set()
            return False if os.name == "nt" else ([], [], [])
        poll = "msvcrt.kbhit" if os.name == "nt" else "select.select"
        with mock.patch(poll, side_effect=idle):
            worker = threading.Thread(target=lambda: answers.append(read_approval(stop)), daemon=True)
            worker.start()
            try:
                self.assertTrue(reading.wait(2))
            finally:
                stop.set()
                worker.join(2)
        self.assertEqual(answers, [""])

    def start(self, team=None, script=standard, approve=None, delay=0.0):
        events = []
        run = TeamRun(self.project, self.state, self.data, team or self.team(), "Change the greeting",
                      approve=approve or (lambda request, stop: True), provider_factory=self.factory(script, delay),
                      on_event=lambda kind, data: events.append(kind))
        thread = threading.Thread(target=run.run, daemon=True)
        thread.start()
        return run, thread, events

    def wait(self, condition, limit=5.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail("Condition not reached.")

    def test_cancel_during_streaming_stops_scheduling(self):
        run, thread, events = self.start(delay=0.4)
        self.wait(lambda: any(item.state == "running" for item in run.subtasks))
        run.cancel()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        record = run.record()
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual([item["state"] for item in record["subtasks"]], ["cancelled"])
        self.assertEqual(events[-1], "done")

    def test_cancel_during_a_command_stops_the_process(self):
        command = "ping -n 30 127.0.0.1" if os.name == "nt" else "sleep 30"

        def script(fake, assignment, attempt, messages, tools):
            if assignment == "change" and attempt == 2:
                return call("terminal", command=command, timeout=60)
            return standard(fake, assignment, attempt, messages, tools)

        run, thread, _ = self.start(script=script)
        self.wait(lambda: any("terminal" in agent["action"] for agent in run.agents.values()), 10)
        time.sleep(0.5)
        started = time.monotonic()
        run.cancel()
        thread.join(10)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(run.record()["state"], "cancelled")
        self.assertEqual(run.record()["evidence"], [])  # The stopped command is not counted as a check.

    def test_cancel_releases_an_agent_waiting_for_approval(self):
        asked = threading.Event()

        def approve(request, stop):
            asked.set()
            while not stop.wait(0.05):
                pass
            return False

        run, thread, _ = self.start(approve=approve)
        self.assertTrue(asked.wait(10))
        self.assertEqual(run.record()["agents"]["implementer"]["state"], "waiting_approval")
        run.cancel()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(run.record()["state"], "cancelled")
        self.assertEqual((self.folder / "app.txt").read_text(encoding="utf-8"), "hello\n")

    def test_interrupted_runs_are_marked_and_never_replayed(self):
        run, thread, _ = self.start(delay=0.3)
        self.wait(lambda: any(item.state == "running" for item in run.subtasks))
        record = run.record()
        record["heartbeat"] = time.time() - 600
        record["id"] = "r-20200101-000000-dead"
        store = RunStore(self.data, "app")
        store.save(record)
        run.cancel()
        thread.join(5)
        requests = sum(len(fake.requests) for fake in self.fakes)
        loaded = store.load("r-20200101-000000-dead")
        self.assertEqual(loaded["state"], "interrupted")
        self.assertIn("Nothing was replayed", loaded["notes"][-1])
        self.assertEqual(sum(len(fake.requests) for fake in self.fakes), requests)
        self.assertIn("r-20200101-000000-dead", [item["id"] for item in store.runs()])


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


class ParallelTests(Base):
    def setUp(self):
        super().setUp()
        git("init", "-q", cwd=self.folder)
        git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=self.folder)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base", cwd=self.folder)
        (self.folder / "notes.txt").write_text("user's uncommitted work\n", encoding="utf-8")
        (self.folder / "app.txt").write_text("hello, user edit\n", encoding="utf-8")

    def parallel_team(self):
        team = self.team(concurrency=3, local_concurrency=3)
        team["parallel"] = True
        team["agents"].append(teams.member("implementer-b", "implementer", "local", name="Implementer B"))
        return team

    def test_apply_keeps_ignored_local_file(self):
        from local_agent.worktrees import apply
        def commit(message):
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message, cwd=self.folder)
        git("restore", "app.txt", cwd=self.folder)
        (self.folder / ".gitignore").write_text("local.cfg\n")
        git("add", ".gitignore", cwd=self.folder)
        commit("ignore local settings")
        branch = git("branch", "--show-current", cwd=self.folder).strip()
        git("switch", "-c", "integration", cwd=self.folder)
        target = self.folder / "local.cfg"
        target.write_text("team version")
        git("add", "-f", "local.cfg", cwd=self.folder)
        commit("track defaults")
        git("switch", branch, cwd=self.folder)
        head = git("rev-parse", "HEAD", cwd=self.folder)
        target.write_text("local settings")
        with self.assertRaises(TeamError):
            apply(self.folder, {"result": {"integration": {"branch": "integration", "integrated": True}}})
        self.assertEqual(target.read_text(), "local settings")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.folder), head)

    def test_parallel_checks_are_scoped_to_workspace(self):
        run, _, _ = self.run_team(self.parallel_team(), script=self.script(), worktree_root=self.root / "work trees")
        def check(workspace, code):
            return {"agent": "implementer", "workspace": str(workspace), "command": "test", "exit_code": code,
                    "timed_out": False}
        run.evidence = [check(run.worktrees.path("implementer"), 1), check(run.worktrees.path("implementer-b"), 0)]
        result = run.synthesize()
        self.assertEqual(result["verification"], "failed")
        self.assertEqual(result["integration"]["verification"], "none")
        run.evidence.extend([check(run.worktrees.path("implementer"), 0), check(run.worktrees.integration_path, 0)])
        result = run.synthesize()
        self.assertEqual((result["verification"], result["integration"]["verification"]), ("passed", "passed"))
        self.assertEqual(len(result["checks"]), 3)

    def script(self, same_file=False):
        def script(fake, assignment, attempt, messages, tools):
            if assignment == "plan":
                return call("submit_plan", subtasks=[
                    {"id": "part-a", "title": "A", "agent": "implementer", "instructions": "Write a.txt",
                     "expected_output": "a.txt", "paths": ["a.txt"]},
                    {"id": "part-b", "title": "B", "agent": "implementer-b", "instructions": "Write b.txt",
                     "expected_output": "b.txt", "paths": ["b.txt"]}]) if attempt == 1 else text("ok")
            if assignment.startswith("part-"):
                name = "shared.txt" if same_file else f"{assignment[-1]}.txt"
                steps = [call("write_file", path=name, content=f"from {assignment}\n"),
                         call("submit_result", summary=f"wrote {name}", status="done")]
                return steps[attempt - 1] if attempt <= 2 else text("ok")
            return standard(fake, assignment, attempt, messages, tools)
        return script

    def test_parallel_writers_are_isolated_and_integrated_without_touching_the_user_tree(self):
        in_flight, peak, lock = [0], [0], threading.Lock()

        def approve(request, stop):
            with lock:
                in_flight[0] += 1
                peak[0] = max(peak[0], in_flight[0])
            time.sleep(0.2)
            with lock:
                in_flight[0] -= 1
            return True

        run, record, _ = self.run_team(self.parallel_team(), script=self.script(), approve=approve,
                                       worktree_root=self.root / "worktrees")
        self.assertEqual(peak[0], 1)  # Approval dialogs never overlap.
        self.assertEqual(record["state"], "completed", record["notes"])
        integration = record["result"]["integration"]
        self.assertEqual(integration["conflicts"], [])
        files = git("show", "--name-only", "--format=", integration["branch"], cwd=self.folder)
        tree = git("ls-tree", "-r", "--name-only", integration["branch"], cwd=self.folder).split()
        self.assertIn("a.txt", tree)
        self.assertIn("b.txt", tree)
        self.assertFalse((self.folder / "a.txt").exists())  # Nothing applied to the user's tree.
        self.assertEqual((self.folder / "app.txt").read_text(encoding="utf-8"), "hello, user edit\n")
        self.assertEqual((self.folder / "notes.txt").read_text(encoding="utf-8"), "user's uncommitted work\n")
        self.assertEqual(integration["kept"], [])
        self.assertFalse((self.root / "worktrees" / "app" / run.id / "implementer").exists())
        from local_agent.worktrees import apply
        with self.assertRaisesRegex(TeamError, "uncommitted changes"):
            apply(self.folder, record)
        git("checkout", "--", "app.txt", cwd=self.folder)
        self.assertIn("Nothing was pushed", apply(self.folder, record))
        self.assertTrue((self.folder / "a.txt").exists())
        self.assertTrue((self.folder / "notes.txt").exists())
        self.assertIsNotNone(files)

    def test_overlapping_edits_conflict_and_every_branch_is_kept(self):
        run, record, _ = self.run_team(self.parallel_team(), script=self.script(same_file=True),
                                       worktree_root=self.root / "worktrees")
        integration = record["result"]["integration"]
        self.assertEqual(integration["overlaps"], ["shared.txt"])
        self.assertTrue(integration["conflicts"])
        states = {item["id"]: item["state"] for item in record["subtasks"]}
        self.assertEqual((states["integrate"], states["review-reviewer"]), ("failed", "skipped"))
        for agent in ("implementer", "implementer-b"):
            self.assertIn("shared.txt", git("ls-tree", "-r", "--name-only", f"local-agent/{run.id}/{agent}",
                                            cwd=self.folder))
        self.assertTrue(integration["kept"])

    def test_plans_must_give_parallel_writers_separate_areas(self):
        with self.assertRaisesRegex(ValueError, "overlaps"):
            orchestrator.check_plan({"subtasks": [
                {"id": "a", "title": "a", "agent": "implementer", "instructions": "i", "expected_output": "o",
                 "paths": ["src"]},
                {"id": "b", "title": "b", "agent": "implementer-b", "instructions": "i", "expected_output": "o",
                 "paths": ["src/app.py"]}]}, teams.resolve(self.parallel_team(), self.data)[0],
                teams.limits_of(self.parallel_team()), True)

    def test_non_git_projects_explain_why_parallel_writers_are_unavailable(self):
        plain = self.root / "plain"
        plain.mkdir()
        project = ProjectRegistry(self.data).add(plain, "Plain")
        ok, text_report, _ = preflight(self.parallel_team(), self.data, project)
        self.assertFalse(ok)
        self.assertIn("not a Git repository", text_report)
        ok, _, _ = preflight(self.team(), self.data, project)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
