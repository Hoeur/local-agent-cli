"""Project operations shared by the command line, the line-based chat and the terminal UI.

Everything here is plain text in, plain text out, so the same commands work without Textual.
Standard library only.
"""
import time
from pathlib import Path

from .projects import (KINDS, TASK_STATUSES, ProjectContext, ProjectError, ProjectRegistry, draft_from_reply,
                       handoff_prompt, normalize_status, record_outcome, todo_from_criteria)

PROJECT_COMMANDS = {"project", "projects", "notes", "note", "find", "tasks", "task", "decisions", "save",
                    "context", "outcome", "brainstorm", "implement"}
KIND_PLURALS = {"overviews": "overview", "notes": "note", "ideas": "idea", "decisions": "decision", "tasks": "task"}
STATUS_MARKS = {"pending": "[ ]", "in-progress": "[>]", "completed": "[x]"}


def resolve_project(registry: ProjectRegistry, reference=None, cwd=None):
    """--project, else the registered project containing the current folder, else the last opened one."""
    if reference:
        return registry.find(reference)
    project = registry.containing(cwd or Path.cwd()) or registry.last()
    if project is None:
        raise ProjectError("No project selected. Pass --project NAME, run this inside a registered project folder, "
                           "or register one with: python -m local_agent project add FOLDER")
    return project


def kind_of(word):
    word = (word or "").strip().lower()
    word = KIND_PLURALS.get(word, word)
    if word not in KINDS:
        raise ProjectError(f"Choose a kind: {', '.join(KINDS)}.")
    return word


# ── Text views ───────────────────────────────────────────────────────────────────────────

def project_list(registry, current=None):
    projects = registry.projects()
    if not projects:
        return "No projects yet. Register a folder with: python -m local_agent project add FOLDER"
    last = registry.load().get("last")
    lines = []
    for project in projects:
        mark = "*" if project.id == current else ("·" if project.id == last else " ")
        status = "" if project.available else "  (folder missing)"
        lines.append(f"{mark} {project.id:<20} {project.name}  —  {project.path}{status}")
    return "\n".join(lines)


def note_line(note):
    status = f"{STATUS_MARKS.get(note.status, '[ ]')} " if note.kind == "task" else ""
    warn = "  (!)" if note.problems else ""
    return f"{note.id:<32} {status}{note.title}{warn}"


def note_list(knowledge, kinds=None, include_completed=True):
    notes = knowledge.notes(kinds)
    if not include_completed:
        notes = [note for note in notes if note.kind != "task" or note.status != "completed"]
    if not notes:
        what = " or ".join(kinds) + "s" if kinds else "notes"
        return f"No {what} yet in {knowledge.directory}."
    lines = [note_line(note) for note in notes]
    lines += [f"skipped {path.name}: {message}" for path, message in knowledge.skipped]
    return "\n".join(lines)


def note_text(knowledge, note):
    lines = [f"{note.title}", f"id {note.id} · {note.kind}" + (f" · {note.status}" if note.status else ""),
             f"file {note.path}"]
    links = note.links()
    if links:
        known = {item.id: item for item in knowledge.notes()}
        lines.append("links " + ", ".join(f"{identifier}" + ("" if identifier in known else " (missing)")
                                          for identifier in links))
    backlinks = [item.id for item in knowledge.backlinks(note.id)]
    if backlinks:
        lines.append("linked from " + ", ".join(backlinks))
    if note.conversations:
        lines.append("conversations " + ", ".join(note.conversations))
    for problem in note.problems:
        lines.append(f"warning: {problem}")
    return "\n".join(lines) + "\n\n" + note.body.strip()


def home_text(project, state):
    """The project home as text: what `project show` prints."""
    lines = [f"{project.name}  ({project.id})", f"folder {project.path}"]
    if not project.available:
        return "\n".join(lines + ["", project.missing_message()])
    knowledge = project.knowledge
    lines.append(f"notes  {knowledge.directory}")
    overview = knowledge.overview()
    lines += ["", "Overview"]
    lines.append("  " + (overview.summary(300) or "(empty)") if overview else
                 "  No overview yet. Create one: python -m local_agent note new overview \"" + project.name + "\"")
    tasks = [note for note in knowledge.notes(["task"]) if note.status != "completed"]
    lines += ["", f"Open tasks ({len(tasks)})"]
    lines += [f"  {STATUS_MARKS[note.status]} {note.id}  {note.title}" for note in
              sorted(tasks, key=lambda note: (note.status != "in-progress", note.updated))[:10]] or ["  none"]
    recent = sorted((note for note in knowledge.notes(["note", "idea", "decision"])),
                    key=lambda note: note.updated, reverse=True)[:8]
    lines += ["", "Recent notes and decisions"]
    lines += [f"  {note.kind:<9} {note.id}  {note.title}" for note in recent] or ["  none"]
    lines += ["", "Recent conversations"]
    lines += [f"  {item['session']:<24} {item['kind']:<14} {item['turns']} turns  {item['opener'][:50]}"
              for item in state.recent_conversations(6)] or ["  none"]
    return "\n".join(lines)


def sources_text(sources):
    if not sources:
        return "No project notes were included."
    lines = []
    for source in sources:
        if source.get("included"):
            extra = " (shortened)" if source.get("truncated") else ""
            lines.append(f"+ {source['id']} — {source['reason']}{extra}")
        else:
            lines.append(f"- {source['id']} — not included: {source.get('note', source['reason'])}")
    return "\n".join(lines)


# ── Actions ──────────────────────────────────────────────────────────────────────────────

def brainstorm_session(state, name=None):
    session = name or time.strftime("brainstorm-%m%d-%H%M%S")
    state.set_conversation(session, kind="brainstorm")
    return session


def start_implementation(project, state, reference, session=None):
    """Open (or resume) the implementation conversation for a task. Returns (session, task, prompt).

    The conversation is linked to the task, its todo list starts from the acceptance criteria, and a
    pending task becomes in-progress, because the user chose to start it. Nothing marks it complete."""
    knowledge = project.require_folder().knowledge
    task = knowledge.find(reference, ["task"])
    session = session or f"impl-{task.id.removeprefix('task-')}"[:100]
    state.set_conversation(session, kind="implementation", task=task.id)
    items, revision = state.todo_revision(session)
    if not items and task.criteria():
        state.save_todo(session, todo_from_criteria(task), revision)
    changed = False
    if session not in task.conversations:
        task.conversations = [*task.conversations, session]
        changed = True
    if task.status == "pending":
        task.status = "in-progress"
        changed = True
    if changed:
        task = knowledge.save(task)
    return session, task, handoff_prompt(task)


def last_reply(state, session):
    for message in reversed(list(state.transcript(session))):
        if message.get("role") == "assistant" and message.get("content") and not message.get("tool_calls"):
            return message["content"]
    return ""


class ProjectCommands:
    """Slash commands for the line-based chat. The terminal UI has its own screens for the same
    operations and uses the text views above."""

    def __init__(self, project, state, ask=input, out=print):
        self.project = project
        self.state = state
        self.ask = ask
        self.out = out

    def handle(self, prompt, session):
        """Return text to print, or None when prompt is not a project command."""
        command, _, argument = prompt.strip().partition(" ")
        name = command[1:].lower() if command.startswith("/") else ""
        if name not in PROJECT_COMMANDS:
            return None
        argument = argument.strip()
        if self.project is None:
            return ("No project is open. Restart chat with --project NAME. Register a folder with: "
                    "python -m local_agent project add FOLDER")
        knowledge = self.project.require_folder().knowledge
        if name in {"project", "projects"}:
            return home_text(self.project, self.state)
        if name in {"notes", "tasks", "decisions"}:
            kinds = None if name == "notes" and not argument else [kind_of(argument or name)]
            return note_list(knowledge, kinds)
        if name == "find":
            if not argument:
                return "Usage: /find WORDS"
            return "\n".join(note_line(note) for note in knowledge.search(argument)) or "No matching notes."
        if name in {"note", "task"}:
            if not argument:
                return f"Usage: /{name} ID"
            return note_text(knowledge, knowledge.find(argument, ["task"] if name == "task" else None))
        if name == "context":
            return self.context(argument, session)
        if name == "save":
            return self.save(argument, session)
        if name == "outcome":
            return self.outcome(argument, session)
        return f"/{name} is available in the full-screen interface, or start chat with --brainstorm / --task."

    def context(self, argument, session):
        action, _, rest = argument.partition(" ")
        details = self.state.conversation(session)
        notes = list(details["notes"])
        if action in {"add", "remove"} and rest.strip():
            note = self.project.knowledge.find(rest.strip())
            notes = [*notes, note.id] if action == "add" else [item for item in notes if item != note.id]
            self.state.set_conversation(session, notes=notes)
        elif action == "clear":
            notes = []
            self.state.set_conversation(session, notes=notes)
        elif action:
            return "Usage: /context [add ID | remove ID | clear]"
        return "Selected for this conversation: " + (", ".join(notes) or "none") + \
            "\nThe overview and up to three matching notes are added automatically."

    def save(self, argument, session):
        kind, _, title = argument.partition(" ")
        try:
            kind = kind_of(kind or "")
        except ProjectError:
            return "Usage: /save idea|note|decision|task [title]"
        reply = last_reply(self.state, session)
        if not reply:
            return "There is no reply in this conversation to save yet."
        title, body = draft_from_reply(kind, reply, session, title.strip() or None)
        self.out(f"--- {kind}: {title} ---\n{body}\n---")
        if self.ask("Save this draft? You can edit the file afterwards. [y/N] ").strip().lower() not in {"y", "yes"}:
            return "Not saved."
        note = self.project.knowledge.create(kind, title, body, conversations=[session])
        return f"Saved {note.id} in {note.path}"

    def outcome(self, argument, session):
        details = self.state.conversation(session)
        reference = argument or details["task"]
        if not reference:
            return "Usage: /outcome TASK-ID (or use it inside an implementation conversation)"
        knowledge = self.project.knowledge
        task = knowledge.find(reference, ["task"])
        outcome = self.ask("Outcome (what changed): ").strip()
        if not outcome:
            return "Not recorded."
        verification = self.ask("Verification performed: ").strip()
        unresolved = self.ask("Unresolved issues: ").strip()
        status = self.ask(f"Status [{'/'.join(TASK_STATUSES)}, Enter keeps {task.status}]: ").strip()
        task = record_outcome(knowledge, task, outcome, verification, unresolved, session,
                              normalize_status(status) if status else None)
        return f"Recorded the outcome in {task.path} (status {task.status})."


def project_context(project, state):
    return ProjectContext(project, state)
