import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .artifacts import ArtifactStore
from .attachments import AttachmentStore

# Bump when the tables change, and add the upgrade step to State.__init__.
SCHEMA_VERSION = 2
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
            # Version 0 and 1 (before versioning) had turns and library; 2 adds per-session task lists.
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
                    items TEXT NOT NULL
                );
            """)
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
        with closing(self.connect()) as db:
            row = db.execute("SELECT items FROM todos WHERE session = ?", (session,)).fetchone()
        return json.loads(row[0]) if row else []

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

    def commit_turn(self, session, previous, messages, changes, todo=None):
        with closing(self.connect()) as db, db:
            inserted = db.execute("""
                INSERT OR IGNORE INTO turns VALUES (?, ?, ?)
            """, (session, previous + 1, json.dumps(messages, ensure_ascii=False)))
            if inserted.rowcount != 1:
                raise RuntimeError("This session advanced in another process. Try the prompt again.")
            self.apply_changes(db, changes)
            if todo is not None:
                db.execute("""
                    INSERT INTO todos VALUES (?, ?) ON CONFLICT (session) DO UPDATE SET items = excluded.items
                """, (session, json.dumps(todo, ensure_ascii=False)))
