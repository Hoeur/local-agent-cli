import json
import sqlite3
from contextlib import closing
import time
from pathlib import Path

from .artifacts import ArtifactStore
from .attachments import AttachmentStore

# Bump when the tables change, and add the upgrade step to State.migrate.
SCHEMA_VERSION = 4
STORE_LIMIT = 20
CONVERSATION_KINDS = ("chat", "brainstorm", "implementation")
# Version 4 scopes conversations and task lists to a project. Existing data has project '', the
# classic scope used when no project is open, so it looks exactly as it did before the upgrade.
TABLES = """
    CREATE TABLE IF NOT EXISTS turns (
        project TEXT NOT NULL DEFAULT '',
        session TEXT NOT NULL,
        number INTEGER NOT NULL,
        messages TEXT NOT NULL,
        PRIMARY KEY (project, session, number)
    );
    CREATE TABLE IF NOT EXISTS library (
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (kind, name)
    );
    CREATE TABLE IF NOT EXISTS todos (
        project TEXT NOT NULL DEFAULT '',
        session TEXT NOT NULL,
        items TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (project, session)
    );
    CREATE TABLE IF NOT EXISTS conversations (
        project TEXT NOT NULL DEFAULT '',
        session TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'chat',
        task TEXT,
        notes TEXT NOT NULL DEFAULT '[]',
        updated REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (project, session)
    );
"""


def columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


class State:
    def __init__(self, directory: Path, project: str = ""):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "state.sqlite3"
        self.project = project
        self.attachments = AttachmentStore(directory / "attachments")
        # Generated files are kept apart per project, so one project's artifacts never show up in
        # another project's conversations.
        self.artifacts = ArtifactStore(directory / "artifacts" if not project
                                       else directory / "projects" / project / "artifacts")
        self.migrate()

    def for_project(self, project: str):
        """The same data directory, scoped to another project ('' is the classic, project-less scope)."""
        scoped = object.__new__(State)
        scoped.path, scoped.project = self.path, project or ""
        scoped.attachments = self.attachments
        scoped.artifacts = ArtifactStore(self.path.parent / "artifacts" if not project
                                         else self.path.parent / "projects" / project / "artifacts")
        return scoped

    def migrate(self):
        """Create or upgrade the tables in one transaction. Safe to repeat and to run from two
        processes at once: the second one finds the work done."""
        with closing(self.connect()) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("This data directory was created by a newer version of Local Agent.")
            db.execute("BEGIN IMMEDIATE")
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                # Version 0 and 1 (before versioning) had turns and library; 2 added per-session task lists;
                # 3 a revision to each task list so a turn can't overwrite edits made while it ran.
                if "turns" in existing and "project" not in columns(db, "turns"):
                    db.execute("ALTER TABLE turns RENAME TO turns_v3")
                    existing.discard("turns")
                    existing.add("turns_v3")
                if "todos" in existing and "project" not in columns(db, "todos"):
                    db.execute("ALTER TABLE todos RENAME TO todos_v3")
                    existing.discard("todos")
                    existing.add("todos_v3")
                for statement in TABLES.split(";"):
                    if statement.strip():
                        db.execute(statement)
                if "turns_v3" in existing:
                    # Keep row ids, so "most recent" orderings carry over.
                    db.execute("INSERT INTO turns (rowid, project, session, number, messages) "
                               "SELECT rowid, '', session, number, messages FROM turns_v3")
                    db.execute("DROP TABLE turns_v3")
                if "todos_v3" in existing:
                    revision = "revision" if "revision" in columns(db, "todos_v3") else "0"
                    db.execute("INSERT INTO todos (project, session, items, revision) "
                               f"SELECT '', session, items, {revision} FROM todos_v3")
                    db.execute("DROP TABLE todos_v3")
                if version < SCHEMA_VERSION:
                    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def connect(self):
        return sqlite3.connect(self.path)

    def history(self, session: str, budget: int):
        """Return the latest turn number and the newest complete turns that fit in budget characters,
        oldest first, each as a list of messages."""
        with closing(self.connect()) as db:
            rows = db.execute("""
                SELECT number, messages FROM turns WHERE project = ? AND session = ?
                ORDER BY number DESC LIMIT 20
            """, (self.project, session)).fetchall()
        turns = []
        remaining = budget
        for _, messages in rows:
            if len(messages) > remaining:
                break
            turns.append(json.loads(messages))
            remaining -= len(messages)
        return (rows[0][0] if rows else 0), list(reversed(turns))

    def library(self, kind: str):
        with closing(self.connect()) as db:
            return {
                name: json.loads(value) for name, value in db.execute(
                    "SELECT name, value FROM library WHERE kind = ? ORDER BY name", (kind,)
                )
            }

    def sessions(self):
        with closing(self.connect()) as db:
            return db.execute("""
                SELECT session, COUNT(number) FROM (
                    SELECT session, number FROM turns WHERE project = ?
                    UNION ALL
                    SELECT session, NULL FROM todos WHERE project = ?
                ) GROUP BY session ORDER BY session
            """, (self.project, self.project)).fetchall()

    def transcript(self, session: str):
        with closing(self.connect()) as db:
            for (messages,) in db.execute(
                "SELECT messages FROM turns WHERE project = ? AND session = ? ORDER BY number",
                (self.project, session)
            ):
                yield from json.loads(messages)

    def todo(self, session: str):
        return self.todo_revision(session)[0]

    def todo_revision(self, session: str):
        """Return (items, revision). The revision goes up with every saved change."""
        with closing(self.connect()) as db:
            row = db.execute("SELECT items, revision FROM todos WHERE project = ? AND session = ?",
                             (self.project, session)).fetchone()
        return (json.loads(row[0]), row[1]) if row else ([], 0)

    def write_todo(self, db, session, items, expected):
        """Save items if the list is still at revision expected. Returns the new revision, or None."""
        encoded = json.dumps(items, ensure_ascii=False)
        if expected == 0:
            inserted = db.execute("INSERT OR IGNORE INTO todos (project, session, items, revision) "
                                  "VALUES (?, ?, ?, 1)", (self.project, session, encoded))
            if inserted.rowcount == 1:
                return 1
        updated = db.execute("UPDATE todos SET items = ?, revision = revision + 1 "
                             "WHERE project = ? AND session = ? AND revision = ?",
                             (encoded, self.project, session, expected))
        return expected + 1 if updated.rowcount == 1 else None

    def save_todo(self, session: str, items, expected: int):
        """Save a task list edited by the user. Returns the new revision, or None if it changed meanwhile."""
        with closing(self.connect()) as db, db:
            return self.write_todo(db, session, items, expected)

    def search(self, query: str, limit: int = 8):
        """Find past user and assistant messages containing every word of query, newest first."""
        words = [word for word in query.casefold().split() if word][:6]
        if not words:
            return []
        pattern = " AND ".join("messages LIKE ? ESCAPE '\\'" for _ in words)
        values = ["%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for word in words]
        results = []
        with closing(self.connect()) as db:
            rows = db.execute(f"""
                SELECT session, number, messages FROM turns WHERE project = ? AND {pattern}
                ORDER BY rowid DESC LIMIT 200
            """, [self.project, *values])
            for session, number, messages in rows:
                for message in json.loads(messages):
                    content = message.get("content")
                    if message.get("role") not in {"user", "assistant"} or not isinstance(content, str):
                        continue
                    folded = content.casefold()
                    if all(word in folded for word in words):
                        at = folded.find(words[0])
                        start = max(0, at - 80)
                        excerpt = " ".join(content[start:at + 160].split())
                        results.append({"session": session, "turn": number, "role": message["role"],
                                        "excerpt": ("…" if start else "") + excerpt})
                        break
                if len(results) >= limit:
                    break
        return results

    def delete_session(self, session: str):
        with closing(self.connect()) as db, db:
            scope = (self.project, session)
            count = db.execute("DELETE FROM turns WHERE project = ? AND session = ?", scope).rowcount
            tasks = db.execute("DELETE FROM todos WHERE project = ? AND session = ?", scope).rowcount
            db.execute("DELETE FROM conversations WHERE project = ? AND session = ?", scope)
        return count or tasks

    def update_library(self, changes):
        """Apply memory or skill changes made by the user directly (not through a turn)."""
        with closing(self.connect()) as db, db:
            self.apply_changes(db, changes)

    @staticmethod
    def apply_changes(db, changes):
        for (kind, name), value in changes.items():
            if value is None:
                db.execute("DELETE FROM library WHERE kind = ? AND name = ?", (kind, name))
                continue
            db.execute("""
                INSERT INTO library VALUES (?, ?, ?)
                ON CONFLICT (kind, name) DO UPDATE SET value = excluded.value
            """, (kind, name, json.dumps(value, ensure_ascii=False)))
        if changes and db.execute(f"""
            SELECT 1 FROM library GROUP BY kind HAVING COUNT(*) > {STORE_LIMIT}
        """).fetchone():
            raise RuntimeError("Another process filled the memory or skill store. Try the prompt again.")

    def commit_turn(self, session, previous, messages, changes, todo=None, todo_revision=None):
        """Save a completed turn. A task list the turn changed is saved only if nobody edited the list
        since the turn last read it (todo_revision); returns False when the turn's list was not saved."""
        with closing(self.connect()) as db, db:
            inserted = db.execute("""
                INSERT OR IGNORE INTO turns (project, session, number, messages) VALUES (?, ?, ?, ?)
            """, (self.project, session, previous + 1, json.dumps(messages, ensure_ascii=False)))
            if inserted.rowcount != 1:
                raise RuntimeError("This session advanced in another process. Try the prompt again.")
            self.apply_changes(db, changes)
            self.touch(db, session)
            if todo is None:
                return True
            if todo_revision is None:
                row = db.execute("SELECT revision FROM todos WHERE project = ? AND session = ?",
                                 (self.project, session)).fetchone()
                todo_revision = row[0] if row else 0
            return self.write_todo(db, session, todo, todo_revision) is not None

    # ── Conversation details (kind, linked task, selected notes) ─────────────────────────

    def touch(self, db, session):
        db.execute("""
            INSERT INTO conversations (project, session, updated) VALUES (?, ?, ?)
            ON CONFLICT (project, session) DO UPDATE SET updated = excluded.updated
        """, (self.project, session, time.time()))

    def conversation(self, session: str):
        """How a conversation was started: kind (chat, brainstorm, implementation), the task it
        implements and the notes the user picked for it. Plain chats have no saved row."""
        with closing(self.connect()) as db:
            row = db.execute("SELECT kind, task, notes, updated FROM conversations WHERE project = ? AND session = ?",
                             (self.project, session)).fetchone()
        if row is None:
            return {"kind": "chat", "task": None, "notes": [], "updated": 0}
        try:
            notes = [str(item) for item in json.loads(row[2]) if isinstance(item, str)]
        except (TypeError, ValueError):
            notes = []
        return {"kind": row[0] if row[0] in CONVERSATION_KINDS else "chat", "task": row[1], "notes": notes,
                "updated": row[3]}

    def set_conversation(self, session: str, *, kind=None, task=..., notes=None):
        if kind is not None and kind not in CONVERSATION_KINDS:
            raise ValueError(f"Conversation kinds are {', '.join(CONVERSATION_KINDS)}.")
        current = self.conversation(session)
        kind = kind or current["kind"]
        task = current["task"] if task is ... else task
        notes = current["notes"] if notes is None else list(dict.fromkeys(notes))
        with closing(self.connect()) as db, db:
            db.execute("""
                INSERT INTO conversations (project, session, kind, task, notes, updated) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (project, session) DO UPDATE SET kind = excluded.kind, task = excluded.task,
                    notes = excluded.notes
            """, (self.project, session, kind, task, json.dumps(notes, ensure_ascii=False), time.time()))

    def recent_conversations(self, limit: int = 8):
        """This scope's conversations with saved turns, most recently active first."""
        with closing(self.connect()) as db:
            rows = db.execute("""
                SELECT t.session, COUNT(*), MAX(t.rowid), c.kind, c.task FROM turns t
                LEFT JOIN conversations c ON c.project = t.project AND c.session = t.session
                WHERE t.project = ? GROUP BY t.session ORDER BY MAX(t.rowid) DESC LIMIT ?
            """, (self.project, limit)).fetchall()
            result = []
            for session, count, _, kind, task in rows:
                (first,) = db.execute("SELECT messages FROM turns WHERE project = ? AND session = ? "
                                      "ORDER BY number LIMIT 1", (self.project, session)).fetchone()
                try:
                    opener = next((m.get("content") for m in json.loads(first) if m.get("role") == "user"), "")
                except (TypeError, ValueError, AttributeError):
                    opener = ""
                result.append({"session": session, "turns": count, "kind": kind or "chat", "task": task,
                               "opener": " ".join(str(opener or "").split())[:120]})
        return result

    def turn_count(self, session: str):
        with closing(self.connect()) as db:
            return db.execute("SELECT COUNT(*) FROM turns WHERE project = ? AND session = ?",
                              (self.project, session)).fetchone()[0]

    def projects_with_data(self):
        with closing(self.connect()) as db:
            return {row[0] for row in db.execute("SELECT DISTINCT project FROM turns WHERE project != ''")}
