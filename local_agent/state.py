import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .artifacts import ArtifactStore
from .attachments import AttachmentStore

# Bump when the tables change, and add the upgrade step to State.__init__.
SCHEMA_VERSION = 3
STORE_LIMIT = 20


class State:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "state.sqlite3"
        self.attachments = AttachmentStore(directory / "attachments")
        self.artifacts = ArtifactStore(directory / "artifacts")
        with closing(self.connect()) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("This data directory was created by a newer version of Local Agent.")
            # Version 0 and 1 (before versioning) had turns and library; 2 adds per-session task lists;
            # 3 adds a revision to each task list so a turn can't overwrite edits made while it ran.
            db.executescript("""
                CREATE TABLE IF NOT EXISTS turns (
                    session TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    messages TEXT NOT NULL,
                    PRIMARY KEY (session, number)
                );
                CREATE TABLE IF NOT EXISTS library (
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (kind, name)
                );
                CREATE TABLE IF NOT EXISTS todos (
                    session TEXT PRIMARY KEY,
                    items TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(todos)")}
            if "revision" not in columns:
                db.execute("ALTER TABLE todos ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            if version < SCHEMA_VERSION:
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def connect(self):
        return sqlite3.connect(self.path)

    def history(self, session: str, budget: int):
        """Return the latest turn number and the newest complete turns that fit in budget characters,
        oldest first, each as a list of messages."""
        with closing(self.connect()) as db:
            rows = db.execute("""
                SELECT number, messages FROM turns WHERE session = ?
                ORDER BY number DESC LIMIT 20
            """, (session,)).fetchall()
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
                SELECT session, COUNT(*) FROM turns GROUP BY session ORDER BY session
            """).fetchall()

    def transcript(self, session: str):
        with closing(self.connect()) as db:
            for (messages,) in db.execute(
                "SELECT messages FROM turns WHERE session = ? ORDER BY number", (session,)
            ):
                yield from json.loads(messages)

    def todo(self, session: str):
        return self.todo_revision(session)[0]

    def todo_revision(self, session: str):
        """Return (items, revision). The revision goes up with every saved change."""
        with closing(self.connect()) as db:
            row = db.execute("SELECT items, revision FROM todos WHERE session = ?", (session,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else ([], 0)

    @staticmethod
    def write_todo(db, session, items, expected):
        """Save items if the list is still at revision expected. Returns the new revision, or None."""
        encoded = json.dumps(items, ensure_ascii=False)
        if expected == 0:
            inserted = db.execute("INSERT OR IGNORE INTO todos VALUES (?, ?, 1)", (session, encoded))
            if inserted.rowcount == 1:
                return 1
        updated = db.execute("UPDATE todos SET items = ?, revision = revision + 1 WHERE session = ? AND revision = ?",
                             (encoded, session, expected))
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
                SELECT session, number, messages FROM turns WHERE {pattern}
                ORDER BY rowid DESC LIMIT 200
            """, values)
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
            count = db.execute("DELETE FROM turns WHERE session = ?", (session,)).rowcount
            db.execute("DELETE FROM todos WHERE session = ?", (session,))
        return count

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
                INSERT OR IGNORE INTO turns VALUES (?, ?, ?)
            """, (session, previous + 1, json.dumps(messages, ensure_ascii=False)))
            if inserted.rowcount != 1:
                raise RuntimeError("This session advanced in another process. Try the prompt again.")
            self.apply_changes(db, changes)
            if todo is None:
                return True
            if todo_revision is None:
                row = db.execute("SELECT revision FROM todos WHERE session = ?", (session,)).fetchone()
                todo_revision = row[0] if row else 0
            return self.write_todo(db, session, todo, todo_revision) is not None
