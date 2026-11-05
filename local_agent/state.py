import json
import sqlite3
from contextlib import closing
from pathlib import Path


class State:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "state.sqlite3"
        with closing(self.connect()) as db, db:
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
            """)

    def connect(self):
        return sqlite3.connect(self.path)

    def history(self, session: str):
        with closing(self.connect()) as db:
            rows = db.execute("""
                SELECT number, messages FROM turns WHERE session = ?
                ORDER BY number DESC LIMIT 20
            """, (session,)).fetchall()
        turns = []
        remaining = 48_000
        for _, messages in rows:
            if len(messages) > remaining:
                break
            turns.append(json.loads(messages))
            remaining -= len(messages)
        return (rows[0][0] if rows else 0), [
            message for turn in reversed(turns) for message in turn
        ]

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

    def commit_turn(self, session, previous, messages, changes):
        with closing(self.connect()) as db, db:
            inserted = db.execute("""
                INSERT OR IGNORE INTO turns VALUES (?, ?, ?)
            """, (session, previous + 1, json.dumps(messages, ensure_ascii=False)))
            if inserted.rowcount != 1:
                raise RuntimeError("This session advanced in another process. Try the prompt again.")
            for (kind, name), value in changes.items():
                if value is None:
                    db.execute("DELETE FROM library WHERE kind = ? AND name = ?", (kind, name))
                    continue
                db.execute("""
                    INSERT INTO library VALUES (?, ?, ?)
                    ON CONFLICT (kind, name) DO UPDATE SET value = excluded.value
                """, (kind, name, json.dumps(value, ensure_ascii=False)))
            if changes and db.execute("""
                SELECT 1 FROM library GROUP BY kind HAVING COUNT(*) > 20
            """).fetchone():
                raise RuntimeError("Another process filled the memory or skill store. Try the prompt again.")
