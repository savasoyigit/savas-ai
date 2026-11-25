"""
Task store (roadmap Faz E, docs/plan-e.md).

SQLite persistence for tasks, task events and approvals; stdlib only.
main.py keeps its in-memory dicts and writes every change through to this
store; on start-up it loads back what must survive a restart.

One connection, used from the event loop of the single uvicorn process
(CLAUDE.md). Calls are short and synchronous.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 10

# Each entry moves the schema from version N to N + 1 (index N).
MIGRATIONS = [
    """
    CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY,
        parent_id TEXT,
        kind TEXT NOT NULL DEFAULT 'task',
        worker TEXT,
        message TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        error TEXT,
        result TEXT,
        context TEXT,
        idempotency_key TEXT UNIQUE,
        daily_memory TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT
    );
    CREATE INDEX tasks_status ON tasks (status);
    CREATE INDEX tasks_parent ON tasks (parent_id);

    CREATE TABLE task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        agent TEXT,
        event TEXT NOT NULL,
        status TEXT,
        details TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX task_events_task ON task_events (task_id);

    CREATE TABLE approvals (
        approval_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        agent TEXT,
        action TEXT NOT NULL,
        arguments TEXT NOT NULL DEFAULT '{}',
        reason TEXT,
        tool_call_id TEXT,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        approved_at TEXT,
        rejected_at TEXT,
        executed_at TEXT
    );
    CREATE INDEX approvals_status ON approvals (status);
    CREATE INDEX approvals_task ON approvals (task_id);
    """,
    # v2 (Faz F, plan-f): episodes + full-text index.
    """
    CREATE TABLE episodes (
        episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL UNIQUE,
        worker TEXT,
        workers TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT
    );
    CREATE INDEX episodes_created ON episodes (created_at);

    CREATE VIRTUAL TABLE episodes_fts USING fts5(
        title, summary,
        content='episodes', content_rowid='episode_id',
        tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER episodes_ai AFTER INSERT ON episodes BEGIN
        INSERT INTO episodes_fts (rowid, title, summary)
        VALUES (new.episode_id, replace(replace(replace(new.title, 'I', 'i'), 'İ', 'i'), 'ı', 'i'), replace(replace(replace(new.summary, 'I', 'i'), 'İ', 'i'), 'ı', 'i'));
    END;
    CREATE TRIGGER episodes_ad AFTER DELETE ON episodes BEGIN
        INSERT INTO episodes_fts (episodes_fts, rowid, title, summary)
        VALUES ('delete', old.episode_id, replace(replace(replace(old.title, 'I', 'i'), 'İ', 'i'), 'ı', 'i'), replace(replace(replace(old.summary, 'I', 'i'), 'İ', 'i'), 'ı', 'i'));
    END;
    CREATE TRIGGER episodes_au AFTER UPDATE ON episodes BEGIN
        INSERT INTO episodes_fts (episodes_fts, rowid, title, summary)
        VALUES ('delete', old.episode_id, replace(replace(replace(old.title, 'I', 'i'), 'İ', 'i'), 'ı', 'i'), replace(replace(replace(old.summary, 'I', 'i'), 'İ', 'i'), 'ı', 'i'));
        INSERT INTO episodes_fts (rowid, title, summary)
        VALUES (new.episode_id, replace(replace(replace(new.title, 'I', 'i'), 'İ', 'i'), 'ı', 'i'), replace(replace(replace(new.summary, 'I', 'i'), 'İ', 'i'), 'ı', 'i'));
    END;
    """,
    # v3 (Faz F, plan-f): lasting facts + full-text index.
    """
    CREATE TABLE facts (
        fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL,
        key_norm TEXT NOT NULL,
        value TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT 'global',
        importance INTEGER NOT NULL,
        source TEXT NOT NULL,
        task_id TEXT,
        status TEXT NOT NULL,
        confirmations INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT
    );
    CREATE INDEX facts_key ON facts (key_norm, scope, status);
    CREATE INDEX facts_status ON facts (status);

    CREATE VIRTUAL TABLE facts_fts USING fts5(
        key, value,
        content='facts', content_rowid='fact_id',
        tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN
        INSERT INTO facts_fts (rowid, key, value) VALUES (new.fact_id, replace(replace(replace(new.key, 'I', 'i'), 'İ', 'i'), 'ı', 'i'), replace(replace(replace(new.value, 'I', 'i'), 'İ', 'i'), 'ı', 'i'));
    END;
    CREATE TRIGGER facts_ad AFTER DELETE ON facts BEGIN
        INSERT INTO facts_fts (facts_fts, rowid, key, value)
        VALUES ('delete', old.fact_id, replace(replace(replace(old.key, 'I', 'i'), 'İ', 'i'), 'ı', 'i'), replace(replace(replace(old.value, 'I', 'i'), 'İ', 'i'), 'ı', 'i'));
    END;
    CREATE TRIGGER facts_au AFTER UPDATE OF key, value ON facts BEGIN
        INSERT INTO facts_fts (facts_fts, rowid, key, value)
        VALUES ('delete', old.fact_id, replace(replace(replace(old.key, 'I', 'i'), 'İ', 'i'), 'ı', 'i'), replace(replace(replace(old.value, 'I', 'i'), 'İ', 'i'), 'ı', 'i'));
        INSERT INTO facts_fts (rowid, key, value) VALUES (new.fact_id, replace(replace(replace(new.key, 'I', 'i'), 'İ', 'i'), 'ı', 'i'), replace(replace(replace(new.value, 'I', 'i'), 'İ', 'i'), 'ı', 'i'));
    END;
    """,
    # v4 (Faz G, plan-g karar D): worker task dependencies and review steps.
    """
    ALTER TABLE tasks ADD COLUMN depends_on TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE tasks ADD COLUMN step TEXT;
    """,
    # v5 (Faz H, plan-h karar 5): search results a worker task got as sources.
    """
    CREATE TABLE sources (
        task_id    TEXT NOT NULL,
        n          INTEGER NOT NULL,
        url        TEXT NOT NULL,
        title      TEXT NOT NULL DEFAULT '',
        snippet    TEXT NOT NULL DEFAULT '',
        engine     TEXT NOT NULL,
        query      TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (task_id, n)
    );
    """,
    # v6 (K-5, plan-k karar I): direct chat with a worker; one row per turn (kind 'chat').
    """
    ALTER TABLE tasks ADD COLUMN conversation_id TEXT;
    CREATE INDEX tasks_conversation ON tasks (conversation_id, created_at);
    """,
    # v7 (J-3, plan-j karar C): job listings collected by host automations.
    """
    CREATE TABLE jobs (
        job_id        TEXT PRIMARY KEY,
        source        TEXT NOT NULL,
        external_id   TEXT NOT NULL,
        url           TEXT NOT NULL,
        title         TEXT NOT NULL,
        company       TEXT NOT NULL DEFAULT '',
        location      TEXT NOT NULL DEFAULT '',
        remote        INTEGER NOT NULL DEFAULT 0,
        language      TEXT NOT NULL DEFAULT '',
        description   TEXT NOT NULL DEFAULT '',
        posted_at     TEXT,
        found_at      TEXT NOT NULL,
        status        TEXT NOT NULL,
        filter_reason TEXT,
        score         INTEGER,
        score_reason  TEXT,
        track         TEXT,
        updated_at    TEXT NOT NULL,
        UNIQUE (source, external_id)
    );
    CREATE INDEX jobs_status ON jobs (status, found_at);
    CREATE INDEX jobs_url ON jobs (url);
    """,
    # v8 (J-5, plan-j karar G): one application package per listing; a new run replaces it.
    """
    CREATE TABLE job_packages (
        job_id     TEXT PRIMARY KEY REFERENCES jobs (job_id),
        status     TEXT NOT NULL,
        language   TEXT NOT NULL DEFAULT '',
        cv         TEXT,
        letter     TEXT,
        review     TEXT,
        steps      TEXT,
        error      TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # v9 (J-5 download): the tailored CV as data, laid out in the user's design.
    "ALTER TABLE job_packages ADD COLUMN cv_data TEXT;",
    # v10 (L-3a, plan-l3 karar B): a goal the user starts; its steps are run by a recipe in code.
    """
    CREATE TABLE goals (
        goal_id    TEXT PRIMARY KEY,
        kind       TEXT NOT NULL,
        params     TEXT,
        status     TEXT NOT NULL,
        budget     INTEGER NOT NULL,
        used       INTEGER NOT NULL DEFAULT 0,
        steps      TEXT,
        summary    TEXT,
        error      TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX goals_created ON goals (created_at);
    """,
]

assert len(MIGRATIONS) == SCHEMA_VERSION

TASK_STATUSES = (
    "queued", "running", "waiting_approval",
    "completed", "needs_review", "failed", "blocked", "cancelled", "skipped",
)
# needs_review (J-6): finished, but the reviewer did not approve the output.
# skipped: a worker task not run because a task it depends on failed (Faz G).
FINAL_TASK_STATUSES = ("completed", "needs_review", "failed", "blocked", "cancelled", "skipped")
# Statuses whose run is lost when the process stops (plan-e karar B).
INTERRUPTIBLE_STATUSES = ("queued", "running")
INTERRUPTED_ERROR = "interrupted by restart"

APPROVAL_STATUSES = (
    "pending", "approved", "rejected", "executed", "failed",
    "interrupted", "cancelled",
)

TASK_FIELDS = (
    "task_id", "parent_id", "kind", "worker", "message", "status", "error",
    "result", "context", "idempotency_key", "daily_memory",
    "created_at", "updated_at", "completed_at", "depends_on", "step",
    "conversation_id",
)
EPISODE_FIELDS = (
    "task_id", "worker", "workers", "status", "title", "summary",
    "created_at", "expires_at",
)
# FTS5 (unicode61) lowercases "I" to "i" while Turkish lowercases it to
# "ı", so "Işık" and "ışık" would not meet. The full-text indexes get
# I, İ and ı as "i" (the triggers above); memory.fts_query does the same
# to the query (F-4).
SOURCE_FIELDS = (
    "task_id", "n", "url", "title", "snippet", "engine", "query", "fetched_at",
)
FACT_FIELDS = (
    "key", "key_norm", "value", "scope", "importance", "source", "task_id",
    "status", "confirmations", "created_at", "updated_at", "expires_at",
)
FACT_STATUSES = ("active", "flagged", "superseded", "deleted")
APPROVAL_FIELDS = (
    "approval_id", "task_id", "agent", "action", "arguments", "reason",
    "tool_call_id", "status", "created_at", "updated_at",
    "approved_at", "rejected_at", "executed_at",
)
JOB_FIELDS = (
    "job_id", "source", "external_id", "url", "title", "company", "location", "remote", "language",
    "description", "posted_at", "found_at", "status", "filter_reason", "score", "score_reason", "track",
    "updated_at",
)
# new/filtered come from the collector; the rest are the user's (J-5).
JOB_STATUSES = ("new", "filtered", "dismissed", "package_ready", "applied", "interview", "rejected", "offer")
PACKAGE_FIELDS = ("job_id", "status", "language", "cv", "cv_data", "letter", "review", "steps", "error", "created_at", "updated_at")
JSON_PACKAGE_FIELDS = ("review", "steps", "cv_data")
PACKAGE_STATUSES = ("running", "ready", "needs_fix", "failed")
GOAL_FIELDS = ("goal_id", "kind", "params", "status", "budget", "used", "steps", "summary", "error", "created_at", "updated_at")
JSON_GOAL_FIELDS = ("params", "steps")
# waiting: the recipe needs something from the user (a listing text) to go on.
GOAL_STATUSES = ("running", "waiting", "completed", "stopped", "failed")
EVENT_COLUMNS = ("task_id", "timestamp", "agent", "event", "status")

# Fields kept as JSON text.
JSON_TASK_FIELDS = ("result", "depends_on")
JSON_APPROVAL_FIELDS = ("arguments",)

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500


class StoreError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_json(value) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def from_json(text: str | None):
    return None if text is None else json.loads(text)


# ----------------------------------------------------------------------
# Task context (main.TASK_CONTEXTS[task_id])
# ----------------------------------------------------------------------

def encode_context(context: dict) -> str:
    """
    TASK_CONTEXTS entry -> JSON. "reads" is a set of tuples
    (("search", query), ("note", path, offset)); JSON keeps it as a
    sorted list of lists.
    """
    data = dict(context)
    if "reads" in data:
        data["reads"] = sorted([list(key) for key in data["reads"]], key=repr)
    return json.dumps(data, ensure_ascii=False)


def decode_context(text: str) -> dict:
    data = json.loads(text)
    if "reads" in data:
        data["reads"] = {tuple(key) for key in data["reads"]}
    return data


# ----------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------

class TaskStore:

    def __init__(self, path: str | Path):
        """
        Opens (creating if needed) the database at `path` and brings its
        schema up to date. A database from a newer version of the code
        is refused: StoreError.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Autocommit; multi-statement changes use self.transaction().
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")

        try:
            self.migrate()
        except BaseException:
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def schema_version(self) -> int:
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if not exists:
            return 0
        row = self.conn.execute("SELECT version FROM schema_version").fetchone()
        return row["version"] if row else 0

    def migrate(self):
        version = self.schema_version()

        if version > SCHEMA_VERSION:
            raise StoreError(
                f"Database schema {version} is newer than this code "
                f"({SCHEMA_VERSION}): {self.path}"
            )

        for step in range(version, SCHEMA_VERSION):
            # executescript commits on its own, so the whole step runs
            # as one script inside BEGIN / COMMIT.
            try:
                self.conn.executescript(
                    "BEGIN;\n"
                    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);\n"
                    f"{MIGRATIONS[step]}\n"
                    "DELETE FROM schema_version;\n"
                    f"INSERT INTO schema_version (version) VALUES ({step + 1});\n"
                    "COMMIT;"
                )
            except sqlite3.Error as exc:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise StoreError(
                    f"Migration to schema {step + 1} failed: {exc}"
                ) from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def exists(self, table: str, key: str, value) -> bool:
        return self.conn.execute(
            f"SELECT 1 FROM {table} WHERE {key} = ?", (value,)
        ).fetchone() is not None

    def write_row(self, table: str, key: str, fields: tuple, row: dict, exists: bool):
        """INSERT a new row, or UPDATE only the given columns of an existing one."""
        columns = [field for field in fields if field in row]

        if exists:
            changed = [c for c in columns if c != key]
            self.conn.execute(
                f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in changed)} WHERE {key} = ?",
                [row[c] for c in changed] + [row[key]],
            )
        else:
            self.conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [row[c] for c in columns],
            )

    @staticmethod
    def limit(value: int | None) -> int:
        if value is None:
            return DEFAULT_LIST_LIMIT
        return max(1, min(int(value), MAX_LIST_LIMIT))

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def save_task(self, task: dict):
        """
        Insert or update a task. Only the fields present in `task` are
        written; unknown keys are ignored. `updated_at` is set to now
        unless given. `context` is written with save_context().
        """
        now = utc_now()
        row = {
            field: task[field]
            for field in TASK_FIELDS
            if field in task and field != "context"
        }
        if "task_id" not in row:
            raise StoreError("tasks: task_id is required")
        row.setdefault("updated_at", now)

        if "status" in row and row["status"] not in TASK_STATUSES:
            raise StoreError(f"Unknown task status: {row['status']}")

        for field in JSON_TASK_FIELDS:
            if field in row:
                row[field] = to_json(row[field])

        exists = self.exists("tasks", "task_id", row["task_id"])
        if not exists:
            row.setdefault("created_at", now)
            if "status" not in row:
                raise StoreError("A new task needs a status")

        try:
            self.write_row("tasks", "task_id", TASK_FIELDS, row, exists)
        except sqlite3.IntegrityError as exc:
            raise StoreError(f"Task not saved: {exc}") from exc

    def task_from_row(self, row: sqlite3.Row) -> dict:
        task = {field: row[field] for field in TASK_FIELDS if field != "context"}
        for field in JSON_TASK_FIELDS:
            task[field] = from_json(task[field])
        task["has_context"] = row["context"] is not None
        return task

    def get_task(self, task_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self.task_from_row(row) if row else None

    def list_tasks(
        self,
        status: str | None = None,
        parent_id: str | None = None,
        kind: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Newest first."""
        where, args = [], []
        for column, value in (("status", status), ("parent_id", parent_id), ("kind", kind)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)

        sql = "SELECT * FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(self.limit(limit))

        return [self.task_from_row(row) for row in self.conn.execute(sql, args)]

    def list_conversation(self, conversation_id: str, limit: int | None = None) -> list[dict]:
        """The newest `limit` turns of a chat, oldest first (K-5)."""
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE kind = 'chat' AND conversation_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (conversation_id, self.limit(limit)),
        ).fetchall()
        return [self.task_from_row(row) for row in reversed(rows)]

    def latest_conversation(self, worker: str) -> str | None:
        row = self.conn.execute(
            "SELECT conversation_id FROM tasks WHERE kind = 'chat' AND worker = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (worker,),
        ).fetchone()
        return row["conversation_id"] if row else None

    def find_task_by_key(self, idempotency_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return self.task_from_row(row) if row else None

    def release_idempotency_keys(self, created_before: str) -> int:
        """Keys of tasks created before the given time can be used again."""
        cursor = self.conn.execute(
            "UPDATE tasks SET idempotency_key = NULL "
            "WHERE idempotency_key IS NOT NULL AND created_at < ?",
            (created_before,),
        )
        return cursor.rowcount

    def interrupt_tasks(self) -> list[str]:
        """
        Start-up: tasks that were queued or running when the process
        stopped become failed (plan-e karar B). Returns their ids.
        """
        now = utc_now()
        marks = ", ".join("?" for _ in INTERRUPTIBLE_STATUSES)
        with self.transaction():
            ids = [
                row["task_id"]
                for row in self.conn.execute(
                    f"SELECT task_id FROM tasks WHERE status IN ({marks}) ORDER BY rowid",
                    INTERRUPTIBLE_STATUSES,
                )
            ]
            self.conn.execute(
                f"UPDATE tasks SET status = 'failed', error = ?, context = NULL, "
                f"updated_at = ?, completed_at = ? WHERE status IN ({marks})",
                (INTERRUPTED_ERROR, now, now, *INTERRUPTIBLE_STATUSES),
            )
        return ids

    # ------------------------------------------------------------------
    # Task context
    # ------------------------------------------------------------------

    def save_context(self, task_id: str, context: dict):
        cursor = self.conn.execute(
            "UPDATE tasks SET context = ?, updated_at = ? WHERE task_id = ?",
            (encode_context(context), utc_now(), task_id),
        )
        if cursor.rowcount == 0:
            raise StoreError(f"Task not found: {task_id}")

    def load_context(self, task_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT context FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row or row["context"] is None:
            return None
        return decode_context(row["context"])

    def checkpoint(self, task: dict, context: dict, approvals: list[dict]):
        """
        The moment a task stops for approval: its status, its context
        and its approvals are written together or not at all.
        """
        with self.transaction():
            self.save_task(task)
            self.save_context(task["task_id"], context)
            for approval in approvals:
                self.save_approval(approval)

    def clear_context(self, task_id: str):
        self.conn.execute(
            "UPDATE tasks SET context = NULL WHERE task_id = ?", (task_id,)
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def add_event(self, activity: dict) -> int:
        """
        One add_activity() record. task_id, timestamp, agent, event and
        status get their own columns; the rest goes to details (JSON).
        """
        if not activity.get("task_id") or not activity.get("event"):
            raise StoreError("An event needs task_id and event")

        details = {k: v for k, v in activity.items() if k not in EVENT_COLUMNS}
        cursor = self.conn.execute(
            "INSERT INTO task_events (task_id, timestamp, agent, event, status, details) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                activity["task_id"],
                activity.get("timestamp") or utc_now(),
                activity.get("agent"),
                activity["event"],
                activity.get("status"),
                json.dumps(details, ensure_ascii=False, default=str),
            ),
        )
        return cursor.lastrowid

    def list_events(self, task_id: str, limit: int | None = None) -> list[dict]:
        """Oldest first, as add_activity() wrote them."""
        rows = self.conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id LIMIT ?",
            (task_id, self.limit(limit if limit is not None else MAX_LIST_LIMIT)),
        )
        return [
            {**{c: row[c] for c in EVENT_COLUMNS}, **json.loads(row["details"])}
            for row in rows
        ]

    def metric_rows(self, since: str, events: tuple[str, ...]) -> dict:
        """L-1a: tasks, the named events and approvals created since `since` (UTC ISO)."""
        tasks = [
            dict(row) for row in self.conn.execute(
                "SELECT task_id, parent_id, kind, worker, substr(message, 1, 80) AS message, status, created_at "
                "FROM tasks WHERE created_at >= ?", (since,)
            )
        ]
        marks = ", ".join("?" for _ in events)
        rows = self.conn.execute(
            f"SELECT * FROM task_events WHERE timestamp >= ? AND event IN ({marks}) ORDER BY id",
            (since, *events),
        )
        found = [{**{c: row[c] for c in EVENT_COLUMNS}, **json.loads(row["details"])} for row in rows]
        approvals = [
            dict(row) for row in self.conn.execute(
                "SELECT status, action, created_at FROM approvals WHERE created_at >= ?", (since,)
            )
        ]
        return {"tasks": tasks, "events": found, "approvals": approvals}

    def purge_events(self, before: str) -> int:
        cursor = self.conn.execute(
            "DELETE FROM task_events WHERE timestamp < ?", (before,)
        )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------

    def save_approval(self, approval: dict):
        row = {field: approval[field] for field in APPROVAL_FIELDS if field in approval}
        if "approval_id" not in row:
            raise StoreError("approvals: approval_id is required")
        now = utc_now()
        row.setdefault("updated_at", now)

        if "status" in row and row["status"] not in APPROVAL_STATUSES:
            raise StoreError(f"Unknown approval status: {row['status']}")

        for field in JSON_APPROVAL_FIELDS:
            if field in row:
                row[field] = to_json(row[field])

        exists = self.exists("approvals", "approval_id", row["approval_id"])
        if not exists:
            row.setdefault("created_at", now)
            missing = [f for f in ("task_id", "action", "status") if f not in row]
            if missing:
                raise StoreError(f"A new approval needs {', '.join(missing)}")

        self.write_row("approvals", "approval_id", APPROVAL_FIELDS, row, exists)

    def approval_from_row(self, row: sqlite3.Row) -> dict:
        approval = {field: row[field] for field in APPROVAL_FIELDS}
        for field in JSON_APPROVAL_FIELDS:
            approval[field] = from_json(approval[field])
        return approval

    def get_approval(self, approval_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        return self.approval_from_row(row) if row else None

    def list_approvals(
        self,
        status: str | None = None,
        task_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Oldest first."""
        where, args = [], []
        for column, value in (("status", status), ("task_id", task_id)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)

        sql = "SELECT * FROM approvals"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, rowid LIMIT ?"
        args.append(self.limit(limit if limit is not None else MAX_LIST_LIMIT))

        return [self.approval_from_row(row) for row in self.conn.execute(sql, args)]

    # ------------------------------------------------------------------
    # Episodes (Faz F)
    # ------------------------------------------------------------------

    def save_episode(self, episode: dict) -> int:
        """
        One episode per task: saving again for the same task replaces
        it (the full-text index follows through triggers).
        """
        missing = [f for f in ("task_id", "status", "title") if not episode.get(f)]
        if "summary" not in episode:
            missing.append("summary")
        if missing:
            raise StoreError(f"An episode needs {', '.join(missing)}")

        row = {field: episode.get(field) for field in EPISODE_FIELDS}
        row["workers"] = json.dumps(list(row["workers"] or []), ensure_ascii=False)
        row["created_at"] = row["created_at"] or utc_now()

        updates = ", ".join(f"{f} = excluded.{f}" for f in EPISODE_FIELDS if f != "task_id")
        self.conn.execute(
            f"INSERT INTO episodes ({', '.join(EPISODE_FIELDS)}) "
            f"VALUES ({', '.join('?' for _ in EPISODE_FIELDS)}) "
            f"ON CONFLICT (task_id) DO UPDATE SET {updates}",
            [row[f] for f in EPISODE_FIELDS],
        )
        return self.conn.execute(
            "SELECT episode_id FROM episodes WHERE task_id = ?", (row["task_id"],)
        ).fetchone()["episode_id"]

    def episode_from_row(self, row: sqlite3.Row) -> dict:
        episode = {"episode_id": row["episode_id"], **{f: row[f] for f in EPISODE_FIELDS}}
        episode["workers"] = json.loads(episode["workers"])
        return episode

    def get_episode(self, task_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM episodes WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self.episode_from_row(row) if row else None

    def search_episodes(self, query: str, now: str, limit: int, exclude_task: str | None = None) -> list[dict]:
        """Unexpired episodes matching an FTS5 query, best first."""
        rows = self.conn.execute(
            "SELECT episodes.* FROM episodes_fts JOIN episodes ON episodes.episode_id = episodes_fts.rowid "
            "WHERE episodes_fts MATCH ? AND episodes.expires_at >= ? AND episodes.task_id IS NOT ? "
            "ORDER BY bm25(episodes_fts) LIMIT ?",
            (query, now, exclude_task, limit),
        )
        return [self.episode_from_row(row) for row in rows]

    def list_episodes(self, limit: int | None = None, worker: str | None = None) -> list[dict]:
        """Newest first. worker: episodes of that worker's tasks or ones it took part in."""
        sql, args = "SELECT * FROM episodes", []
        if worker is not None:
            sql += " WHERE worker = ? OR EXISTS (SELECT 1 FROM json_each(workers) WHERE value = ?)"
            args += [worker, worker]
        sql += " ORDER BY created_at DESC, episode_id DESC LIMIT ?"
        args.append(self.limit(limit))
        return [self.episode_from_row(row) for row in self.conn.execute(sql, args)]

    def purge_episodes(self, now: str) -> int:
        """Episodes whose expiry has passed."""
        cursor = self.conn.execute(
            "DELETE FROM episodes WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
        )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Sources (Faz H)
    # ------------------------------------------------------------------

    def save_sources(self, task_id: str, sources: list[dict], fetched_at: str | None = None) -> int:
        """The numbered sources of one worker task; saving again replaces them."""
        fetched_at = fetched_at or utc_now()
        with self.transaction():
            self.conn.execute("DELETE FROM sources WHERE task_id = ?", (task_id,))
            self.conn.executemany(
                f"INSERT INTO sources ({', '.join(SOURCE_FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in SOURCE_FIELDS)})",
                [
                    [task_id, s["n"], s["url"], s.get("title") or "", s.get("snippet") or "",
                     s["engine"], s["query"], fetched_at]
                    for s in sources
                ],
            )
        return len(sources)

    def list_sources(self, task_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE task_id = ? ORDER BY n", (task_id,)
        )
        return [{f: row[f] for f in SOURCE_FIELDS} for row in rows]

    # ------------------------------------------------------------------
    # Jobs (J-3)
    # ------------------------------------------------------------------

    def import_jobs(self, jobs: list[dict], now: str | None = None) -> dict:
        """
        New listings only: one already known by (source, external_id), by
        its url, or by the same company and title (one posting in several
        countries' alerts) is skipped and left as it is.
        """
        now = now or utc_now()
        added = []
        with self.transaction():
            for job in jobs:
                if job.get("status") not in ("new", "filtered"):
                    raise StoreError(f"An imported job is new or filtered, not {job.get('status')}")
                known = self.conn.execute(
                    "SELECT 1 FROM jobs WHERE (source = ? AND external_id = ?) OR url = ?",
                    (job["source"], job["external_id"], job["url"]),
                ).fetchone()
                if not known and (job.get("company") or "").strip():
                    known = self.conn.execute(
                        "SELECT 1 FROM jobs WHERE lower(trim(company)) = lower(trim(?)) AND lower(trim(title)) = lower(trim(?))",
                        (job["company"], job["title"]),
                    ).fetchone()
                if known:
                    continue
                row = {f: job[f] for f in JOB_FIELDS if f in job}
                row["remote"] = int(bool(row.get("remote")))
                row.setdefault("found_at", now)
                row["updated_at"] = now
                self.write_row("jobs", "job_id", JOB_FIELDS, row, False)
                added.append(row["job_id"])
        return {"added": len(added), "duplicates": len(jobs) - len(added), "job_ids": added}

    def job_from_row(self, row: sqlite3.Row) -> dict:
        job = {f: row[f] for f in JOB_FIELDS}
        job["remote"] = bool(job["remote"])
        return job

    def get_job(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self.job_from_row(row) if row else None

    def list_jobs(self, status: str | None = None, limit: int | None = None) -> list[dict]:
        """Newest first."""
        sql, args = "SELECT * FROM jobs", []
        if status is not None:
            sql, args = sql + " WHERE status = ?", [status]
        rows = self.conn.execute(sql + " ORDER BY found_at DESC, rowid DESC LIMIT ?", [*args, self.limit(limit)])
        return [self.job_from_row(row) for row in rows]

    def jobs_to_score(self, limit: int, rescore: bool = False) -> list[dict]:
        """New listings without a score (with rescore: every new one), oldest first (J-4)."""
        unscored = "" if rescore else " AND score IS NULL"
        rows = self.conn.execute(
            f"SELECT * FROM jobs WHERE status = 'new'{unscored} ORDER BY found_at, rowid LIMIT ?",
            (max(1, int(limit)),),
        )
        return [self.job_from_row(row) for row in rows]

    def count_unscored(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'new' AND score IS NULL").fetchone()[0]

    def score_job(self, job_id: str, score: int, reason: str, track: str | None):
        self.conn.execute(
            "UPDATE jobs SET score = ?, score_reason = ?, track = ?, updated_at = ? WHERE job_id = ?",
            (int(score), reason, track, utc_now(), job_id),
        )

    def set_job(self, job_id: str, fields: dict):
        """The user's status or a pasted description (J-5)."""
        allowed = {k: v for k, v in fields.items() if k in ("status", "description")}
        if "status" in allowed and allowed["status"] not in JOB_STATUSES:
            raise StoreError(f"Unknown job status: {allowed['status']}")
        if not allowed:
            return
        sets = ", ".join(f"{k} = ?" for k in allowed)
        self.conn.execute(f"UPDATE jobs SET {sets}, updated_at = ? WHERE job_id = ?",
                          [*allowed.values(), utc_now(), job_id])

    def save_package(self, package: dict):
        row = {f: package[f] for f in PACKAGE_FIELDS if f in package}
        if row.get("status") is not None and row["status"] not in PACKAGE_STATUSES:
            raise StoreError(f"Unknown package status: {row['status']}")
        for field in JSON_PACKAGE_FIELDS:
            if field in row:
                row[field] = to_json(row[field])
        row["updated_at"] = utc_now()
        exists = self.exists("job_packages", "job_id", row["job_id"])
        if not exists:
            row.setdefault("created_at", row["updated_at"])
        self.write_row("job_packages", "job_id", PACKAGE_FIELDS, row, exists)

    def get_package(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM job_packages WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            return None
        package = {f: row[f] for f in PACKAGE_FIELDS}
        for field in JSON_PACKAGE_FIELDS:
            package[field] = from_json(package[field])
        return package

    def interrupt_packages(self) -> int:
        """A package being written when the process stopped will not finish."""
        cursor = self.conn.execute(
            "UPDATE job_packages SET status = 'failed', error = 'interrupted by a restart', updated_at = ? "
            "WHERE status = 'running'", (utc_now(),))
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Goals (L-3a)
    # ------------------------------------------------------------------

    def save_goal(self, goal: dict):
        row = {f: goal[f] for f in GOAL_FIELDS if f in goal}
        if row.get("status") is not None and row["status"] not in GOAL_STATUSES:
            raise StoreError(f"Unknown goal status: {row['status']}")
        for field in JSON_GOAL_FIELDS:
            if field in row:
                row[field] = to_json(row[field])
        row["updated_at"] = utc_now()
        exists = self.exists("goals", "goal_id", row["goal_id"])
        if not exists:
            row.setdefault("created_at", row["updated_at"])
        self.write_row("goals", "goal_id", GOAL_FIELDS, row, exists)

    def goal_from_row(self, row: sqlite3.Row) -> dict:
        goal = {f: row[f] for f in GOAL_FIELDS}
        for field in JSON_GOAL_FIELDS:
            goal[field] = from_json(goal[field])
        return goal

    def get_goal(self, goal_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
        return self.goal_from_row(row) if row else None

    def list_goals(self, limit: int | None = None) -> list[dict]:
        """Newest first."""
        rows = self.conn.execute("SELECT * FROM goals ORDER BY created_at DESC, rowid DESC LIMIT ?", (self.limit(limit),))
        return [self.goal_from_row(row) for row in rows]

    def interrupt_goals(self) -> int:
        """A goal running when the process stopped will not go on by itself."""
        cursor = self.conn.execute(
            "UPDATE goals SET status = 'failed', error = 'interrupted by a restart', updated_at = ? "
            "WHERE status = 'running'", (utc_now(),))
        return cursor.rowcount

    def count_jobs(self) -> dict:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {row["status"]: row["n"] for row in rows}

    # ------------------------------------------------------------------
    # Facts (Faz F)
    # ------------------------------------------------------------------

    def save_fact(self, fact: dict) -> int:
        missing = [f for f in ("key", "key_norm", "value", "importance", "source", "status") if fact.get(f) in (None, "")]
        if missing:
            raise StoreError(f"A fact needs {', '.join(missing)}")
        if fact["status"] not in FACT_STATUSES:
            raise StoreError(f"Unknown fact status: {fact['status']}")

        now = utc_now()
        row = {field: fact.get(field) for field in FACT_FIELDS}
        row["scope"] = row["scope"] or "global"
        row["confirmations"] = row["confirmations"] or 1
        row["created_at"] = row["created_at"] or now
        row["updated_at"] = row["updated_at"] or now

        cursor = self.conn.execute(
            f"INSERT INTO facts ({', '.join(FACT_FIELDS)}) "
            f"VALUES ({', '.join('?' for _ in FACT_FIELDS)})",
            [row[f] for f in FACT_FIELDS],
        )
        return cursor.lastrowid

    def fact_from_row(self, row: sqlite3.Row) -> dict:
        return {"fact_id": row["fact_id"], **{f: row[f] for f in FACT_FIELDS}}

    def get_fact(self, fact_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
        return self.fact_from_row(row) if row else None

    def find_active_fact(self, key_norm: str, scope: str, now: str) -> dict | None:
        """The unexpired active fact for this key and scope, if any."""
        row = self.conn.execute(
            "SELECT * FROM facts WHERE key_norm = ? AND scope = ? AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at >= ?) ORDER BY fact_id DESC LIMIT 1",
            (key_norm, scope, now),
        ).fetchone()
        return self.fact_from_row(row) if row else None

    def find_flagged_facts(self, key_norm: str, scope: str) -> list[dict]:
        """Facts waiting for the user's choice for this key and scope (F-5)."""
        rows = self.conn.execute(
            "SELECT * FROM facts WHERE key_norm = ? AND scope = ? AND status = 'flagged' ORDER BY fact_id",
            (key_norm, scope),
        )
        return [self.fact_from_row(row) for row in rows]

    def resolve_fact(self, fact_id: int, use_new: bool, now: str) -> dict:
        """
        The user's choice for a flagged fact (plan-f karar J), in one
        transaction. use_new: the flagged fact becomes active and the
        active one for its key and scope is superseded. Otherwise the
        flagged fact is superseded. Other flagged facts stay flagged.
        """
        with self.transaction():
            fact = self.get_fact(fact_id)
            if fact is None or fact["status"] != "flagged":
                raise StoreError(f"Fact {fact_id} is not flagged")

            superseded = []
            if use_new:
                old = self.find_active_fact(fact["key_norm"], fact["scope"], now)
                if old:
                    self.set_fact_status(old["fact_id"], "superseded")
                    superseded.append(old["fact_id"])
                self.set_fact_status(fact_id, "active")
            else:
                self.set_fact_status(fact_id, "superseded")
                superseded.append(fact_id)

        return {
            "fact_id": fact_id,
            "status": "active" if use_new else "superseded",
            "superseded": superseded,
        }

    def confirm_fact(self, fact_id: int, expires_at: str | None):
        """The same fact was given again: one more confirmation, expiry as given."""
        self.conn.execute(
            "UPDATE facts SET confirmations = confirmations + 1, expires_at = ?, updated_at = ? "
            "WHERE fact_id = ?",
            (expires_at, utc_now(), fact_id),
        )

    def set_fact_status(self, fact_id: int, status: str) -> bool:
        if status not in FACT_STATUSES:
            raise StoreError(f"Unknown fact status: {status}")
        cursor = self.conn.execute(
            "UPDATE facts SET status = ?, updated_at = ? WHERE fact_id = ?",
            (status, utc_now(), fact_id),
        )
        return cursor.rowcount == 1

    def list_facts(
        self,
        status: str | None = None,
        scope: str | None = None,
        now: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Newest first. now: leave out facts that expired before it."""
        where, args = [], []
        for column, value in (("status", status), ("scope", scope)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        if now is not None:
            where.append("(expires_at IS NULL OR expires_at >= ?)")
            args.append(now)

        sql = "SELECT * FROM facts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, fact_id DESC LIMIT ?"
        args.append(self.limit(limit))
        return [self.fact_from_row(row) for row in self.conn.execute(sql, args)]

    def search_facts(self, query: str, scopes: list[str], now: str, limit: int) -> list[dict]:
        """Active, unexpired facts in these scopes matching an FTS5 query, best first."""
        rows = self.conn.execute(
            "SELECT facts.* FROM facts_fts JOIN facts ON facts.fact_id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? AND facts.status = 'active' "
            f"AND facts.scope IN ({', '.join('?' for _ in scopes)}) "
            "AND (facts.expires_at IS NULL OR facts.expires_at >= ?) "
            "ORDER BY bm25(facts_fts) LIMIT ?",
            [query, *scopes, now, limit],
        )
        return [self.fact_from_row(row) for row in rows]

    def purge_facts(self, expired_before: str) -> int:
        """Delete facts that expired before the given time."""
        cursor = self.conn.execute(
            "DELETE FROM facts WHERE expires_at IS NOT NULL AND expires_at < ?", (expired_before,)
        )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Backup (plan-e karar F)
    # ------------------------------------------------------------------

    def backup(self, destination: str | Path) -> Path:
        """Consistent online copy (sqlite3 backup API)."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        return destination
