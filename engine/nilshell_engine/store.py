"""SQLite persistence. Secrets are deliberately excluded from the database."""

import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            directory.chmod(0o700)
        self.path = directory / "nilshell.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS providers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                    base_url TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assistants (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, provider_id TEXT NOT NULL,
                    model TEXT NOT NULL, system_prompt TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, assistant_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL
                );
            """)
            # A killed engine cannot resume an in-flight network stream.
            db.execute("UPDATE messages SET status='interrupted' WHERE status='streaming'")
        if os.name != "nt":
            self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def all(self, table):
        if table not in {"providers", "assistants", "conversations"}:
            raise ValueError("Invalid table")
        with self.connect() as db:
            order = "created_at DESC, rowid DESC" if table == "conversations" else "rowid"
            rows = [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        if table == "conversations":
            for row in rows:
                row["assistant"] = json.loads(row.pop("assistant_json"))
        return rows

    def provider(self, provider_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
        if row is None:
            raise ValueError("Fournisseur introuvable")
        return dict(row)

    def save_provider(self, values):
        data = {k: values[k] for k in ("name", "kind", "base_url")}
        data["id"] = values.get("id") or str(uuid4())
        with self.connect() as db:
            db.execute(
                """INSERT INTO providers VALUES (:id,:name,:kind,:base_url)
                ON CONFLICT(id) DO UPDATE SET name=:name,kind=:kind,base_url=:base_url""",
                data,
            )
        return data

    def save_assistant(self, values):
        self.provider(values["provider_id"])
        data = {k: values[k] for k in ("name", "provider_id", "model", "system_prompt")}
        data["id"] = values.get("id") or str(uuid4())
        with self.connect() as db:
            db.execute(
                """INSERT INTO assistants VALUES
                (:id,:name,:provider_id,:model,:system_prompt)
                ON CONFLICT(id) DO UPDATE SET name=:name,provider_id=:provider_id,
                model=:model,system_prompt=:system_prompt""",
                data,
            )
        return data

    def new_conversation(self, assistant_id):
        assistants = {a["id"]: a for a in self.all("assistants")}
        if assistant_id not in assistants:
            raise ValueError("Assistant introuvable")
        identifier = str(uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO conversations(id,title,assistant_json) VALUES (?,?,?)",
                (identifier, "Nouvelle conversation", json.dumps(assistants[assistant_id])),
            )
        return self.conversation(identifier)

    def conversation(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("Conversation introuvable")
            result = dict(row)
            result["assistant"] = json.loads(result.pop("assistant_json"))
            result["messages"] = [
                dict(m)
                for m in db.execute(
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (identifier,)
                )
            ]
        return result

    def add_message(self, conversation_id, role, content, status="complete"):
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO messages(conversation_id,role,content,status) VALUES (?,?,?,?)",
                (conversation_id, role, content, status),
            )
            if role == "user":
                db.execute(
                    """UPDATE conversations SET title=? WHERE id=? AND
                    (SELECT count(*) FROM messages WHERE conversation_id=? AND role='user')=1""",
                    (content[:70].replace("\n", " "), conversation_id, conversation_id),
                )
            return cursor.lastrowid

    def update_message(self, identifier, content, status):
        with self.connect() as db:
            db.execute(
                "UPDATE messages SET content=?,status=? WHERE id=?", (content, status, identifier)
            )

    def delete_conversation(self, identifier):
        with self.connect() as db:
            db.execute("DELETE FROM conversations WHERE id=?", (identifier,))
