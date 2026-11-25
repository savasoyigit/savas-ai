import sqlite3

import pytest

import task_store as ts
from task_store import StoreError, TaskStore


@pytest.fixture
def store(tmp_path):
    store = TaskStore(tmp_path / "data" / "savas.db")
    yield store
    store.close()


def reopen(store):
    """A new process on the same file."""
    store.close()
    return TaskStore(store.path)


def task(task_id="t1", **fields):
    return {"task_id": task_id, "message": "görev", "status": "running", "worker": "ares",
            "created_at": "2026-11-14T10:00:00+00:00", **fields}


def approval(approval_id="a1", **fields):
    return {"approval_id": approval_id, "task_id": "t1", "agent": "obsidian", "action": "create_note",
            "arguments": {"path": "x.md", "content": "ğüş"}, "reason": "onay gerekli",
            "tool_call_id": "call_1", "status": "pending", "created_at": "2026-11-14T10:00:01+00:00",
            "approved_at": None, "rejected_at": None, "executed_at": None, **fields}


# ----------------------------------------------------------------------
# schema / migration
# ----------------------------------------------------------------------

def test_new_database_gets_the_current_schema(store):
    assert store.schema_version() == ts.SCHEMA_VERSION == len(ts.MIGRATIONS) == 10
    tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"schema_version", "tasks", "task_events", "approvals", "sources", "goals"} <= tables
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_reopening_does_not_migrate_again(store):
    store.save_task(task())
    store = reopen(store)
    assert store.schema_version() == ts.SCHEMA_VERSION and store.get_task("t1")["message"] == "görev"
    assert store.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    store.close()


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "savas.db"
    TaskStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = 99")
    conn.commit()
    conn.close()

    with pytest.raises(StoreError, match="newer"):
        TaskStore(path)


def test_failed_migration_leaves_nothing_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "MIGRATIONS", ["CREATE TABLE a (x); CREATE TABLE broken ("])
    path = tmp_path / "savas.db"

    with pytest.raises(StoreError, match="Migration to schema 1"):
        TaskStore(path)

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    conn.close()


def test_migrations_run_in_order(tmp_path, monkeypatch):
    path = tmp_path / "savas.db"
    TaskStore(path).close()
    monkeypatch.setattr(ts, "SCHEMA_VERSION", ts.SCHEMA_VERSION + 1)
    monkeypatch.setattr(ts, "MIGRATIONS", [*ts.MIGRATIONS, "ALTER TABLE tasks ADD COLUMN priority INTEGER;"])

    store = TaskStore(path)
    columns = [row[1] for row in store.conn.execute("PRAGMA table_info(tasks)")]
    assert store.schema_version() == ts.SCHEMA_VERSION and "priority" in columns
    store.close()


# ----------------------------------------------------------------------
# tasks
# ----------------------------------------------------------------------

def test_task_round_trip(store):
    store.save_task(task(daily_memory="workers/ares/daily/x.md", result={"response": "cevap", "n": 1}))
    saved = store.get_task("t1")

    assert saved["message"] == "görev" and saved["status"] == "running" and saved["kind"] == "task"
    assert saved["result"] == {"response": "cevap", "n": 1}
    assert saved["created_at"] == "2026-11-14T10:00:00+00:00" and saved["updated_at"]
    assert saved["parent_id"] is None and saved["completed_at"] is None
    assert saved["has_context"] is False and "context" not in saved


def test_update_writes_only_the_given_fields(store):
    store.save_task(task(daily_memory="d.md"))
    store.save_task({"task_id": "t1", "status": "completed", "completed_at": "2026-11-14T10:05:00+00:00"})

    saved = store.get_task("t1")
    assert saved["status"] == "completed" and saved["completed_at"].startswith("2026-11-14T10:05")
    assert saved["message"] == "görev" and saved["daily_memory"] == "d.md"
    assert saved["created_at"] == "2026-11-14T10:00:00+00:00"


def test_unknown_keys_are_ignored(store):
    store.save_task(task(agent="orchestrator", pending_tools={}))
    assert "agent" not in store.get_task("t1")


@pytest.mark.parametrize("bad,match", [
    ({"task_id": "t9", "message": "x"}, "status"),
    (task(status="done"), "Unknown task status"),
    ({"message": "x", "status": "running"}, "task_id"),
])
def test_invalid_tasks_are_refused(store, bad, match):
    with pytest.raises(StoreError, match=match):
        store.save_task(bad)


def test_missing_task_is_none(store):
    assert store.get_task("yok") is None


def test_worker_tasks_hang_under_their_parent(store):
    store.save_task(task())
    for i, worker in enumerate(("jarvis", "oracle")):
        store.save_task(task(f"w{i}", parent_id="t1", kind="worker", worker=worker,
                             created_at=f"2026-11-14T10:00:0{i + 1}+00:00"))

    children = store.list_tasks(parent_id="t1")
    assert [c["worker"] for c in children] == ["oracle", "jarvis"]
    assert all(c["kind"] == "worker" for c in children)
    assert [t["task_id"] for t in store.list_tasks(kind="task")] == ["t1"]


def test_list_is_newest_first_filtered_and_limited(store):
    for i in range(8):
        store.save_task(task(f"t{i}", status="completed" if i % 2 else "failed",
                             created_at=f"2026-11-14T10:00:0{i}+00:00"))

    assert [t["task_id"] for t in store.list_tasks(limit=3)] == ["t7", "t6", "t5"]
    assert [t["task_id"] for t in store.list_tasks(status="failed")] == ["t6", "t4", "t2", "t0"]
    assert len(store.list_tasks(limit=0)) == 1
    assert len(store.list_tasks(limit=10 ** 6)) == 8


def test_idempotency_key_is_unique_and_can_be_released(store):
    store.save_task(task(idempotency_key="k1"))
    assert store.find_task_by_key("k1")["task_id"] == "t1"

    with pytest.raises(StoreError, match="not saved"):
        store.save_task(task("t2", idempotency_key="k1"))
    assert store.get_task("t2") is None

    assert store.release_idempotency_keys("2026-11-14T09:00:00+00:00") == 0
    assert store.release_idempotency_keys("2026-11-15T10:00:00+00:00") == 1
    assert store.find_task_by_key("k1") is None
    store.save_task(task("t2", idempotency_key="k1"))
    assert store.find_task_by_key("k1")["task_id"] == "t2"


def test_restart_fails_only_the_runs_it_cut(store):
    for task_id, status in (("q", "queued"), ("r", "running"), ("w", "waiting_approval"),
                            ("c", "completed"), ("b", "blocked")):
        store.save_task(task(task_id, status=status))
    store.save_context("r", {"messages": [], "next_iteration": 2})
    store.save_context("w", {"messages": [{"role": "user", "content": "x"}]})

    store = reopen(store)
    assert store.interrupt_tasks() == ["q", "r"]

    for task_id in ("q", "r"):
        cut = store.get_task(task_id)
        assert cut["status"] == "failed" and cut["error"] == ts.INTERRUPTED_ERROR and cut["completed_at"]
        assert cut["has_context"] is False
    assert store.get_task("w")["status"] == "waiting_approval"
    assert store.load_context("w") == {"messages": [{"role": "user", "content": "x"}]}
    assert [store.get_task(t)["status"] for t in ("c", "b")] == ["completed", "blocked"]
    assert store.interrupt_tasks() == []
    store.close()


# ----------------------------------------------------------------------
# context
# ----------------------------------------------------------------------

CONTEXT = {
    "messages": [{"role": "system", "content": "sistem"}, {"role": "user", "content": "görev ğüş"},
                 {"role": "assistant", "content": None,
                  "tool_calls": [{"id": "call_1", "type": "function",
                                  "function": {"name": "create_note", "arguments": "{\"path\": \"x.md\"}"}}]}],
    "pending_tools": {"a1": {"tool_call_id": "call_1", "name": "create_note", "arguments": {"path": "x.md"}}},
    "next_iteration": 3,
    "reads": {("search", "sqlite"), ("note", "a.md", 0), ("note", "a.md", 5976)},
}


def test_context_survives_a_restart_as_it_was(store):
    store.save_task(task(status="waiting_approval"))
    store.save_context("t1", CONTEXT)

    store = reopen(store)
    loaded = store.load_context("t1")
    assert loaded == CONTEXT and isinstance(loaded["reads"], set)
    assert store.get_task("t1")["has_context"] is True
    store.close()


def test_context_without_reads(store):
    store.save_task(task())
    store.save_context("t1", {"messages": [], "pending_tools": {}, "next_iteration": 1})
    assert "reads" not in store.load_context("t1")


def test_context_is_cleared(store):
    store.save_task(task())
    store.save_context("t1", CONTEXT)
    store.clear_context("t1")
    assert store.load_context("t1") is None and store.get_task("t1")["message"] == "görev"


def test_context_needs_its_task(store):
    with pytest.raises(StoreError, match="not found"):
        store.save_context("yok", CONTEXT)
    assert store.load_context("yok") is None


def test_saving_a_task_keeps_its_context(store):
    store.save_task(task())
    store.save_context("t1", CONTEXT)
    store.save_task({"task_id": "t1", "status": "waiting_approval", "context": "yoksayılır"})
    assert store.load_context("t1") == CONTEXT


# ----------------------------------------------------------------------
# events
# ----------------------------------------------------------------------

def test_events_keep_their_details(store):
    first = {"timestamp": "2026-11-14T10:00:00+00:00", "task_id": "t1", "agent": "orchestrator",
             "event": "task", "status": "started", "message": "görev"}
    second = {"timestamp": "2026-11-14T10:00:01+00:00", "task_id": "t1", "agent": "jarvis",
              "event": "worker_report", "status": "parsed", "report_chars": 1000, "nested": {"a": [1, 2]}}
    store.add_event(first)
    store.add_event(second)
    store.add_event({**first, "task_id": "t2"})

    assert store.list_events("t1") == [first, second]
    assert store.list_events("t1", limit=1) == [first]


def test_event_details_that_are_not_json_are_stored_as_text(store):
    store.add_event({"task_id": "t1", "event": "x", "status": "y", "when": __import__("datetime").date(2026, 11, 14)})
    [event] = store.list_events("t1")
    assert event["when"] == "2026-11-14" and event["timestamp"]


@pytest.mark.parametrize("bad", [{"event": "x"}, {"task_id": "t1"}, {"task_id": "", "event": "x"}])
def test_events_need_a_task_and_a_name(store, bad):
    with pytest.raises(StoreError):
        store.add_event(bad)


def test_old_events_are_purged(store):
    for day in ("10", "12", "14"):
        store.add_event({"task_id": "t1", "event": "e", "status": day, "timestamp": f"2026-11-{day}T00:00:00+00:00"})

    assert store.purge_events("2026-11-13T00:00:00+00:00") == 2
    assert [e["status"] for e in store.list_events("t1")] == ["14"]


# ----------------------------------------------------------------------
# approvals
# ----------------------------------------------------------------------

def test_approval_round_trip(store):
    store.save_approval(approval())
    saved = store.get_approval("a1")

    assert saved["arguments"] == {"path": "x.md", "content": "ğüş"}
    assert saved["status"] == "pending" and saved["tool_call_id"] == "call_1"
    assert saved["created_at"] == "2026-11-14T10:00:01+00:00" and saved["approved_at"] is None
    assert set(saved) == set(ts.APPROVAL_FIELDS)


def test_approval_status_changes_survive_a_restart(store):
    store.save_approval(approval())
    store.save_approval({"approval_id": "a1", "status": "approved", "approved_at": "2026-11-14T10:01:00+00:00"})

    store = reopen(store)
    saved = store.get_approval("a1")
    assert saved["status"] == "approved" and saved["approved_at"].startswith("2026-11-14T10:01")
    assert saved["arguments"]["path"] == "x.md" and saved["executed_at"] is None
    store.close()


@pytest.mark.parametrize("status", ts.APPROVAL_STATUSES)
def test_every_approval_status_is_accepted(store, status):
    store.save_approval(approval(status=status))
    assert store.get_approval("a1")["status"] == status


@pytest.mark.parametrize("bad,match", [
    (approval(status="ok"), "Unknown approval status"),
    ({"approval_id": "a9", "task_id": "t1", "status": "pending"}, "action"),
    ({"task_id": "t1", "action": "x", "status": "pending"}, "approval_id"),
])
def test_invalid_approvals_are_refused(store, bad, match):
    with pytest.raises(StoreError, match=match):
        store.save_approval(bad)


def test_approvals_are_listed_oldest_first_and_filtered(store):
    store.save_approval(approval("a2", created_at="2026-11-14T10:00:02+00:00"))
    store.save_approval(approval("a1"))
    store.save_approval(approval("a3", task_id="t2", status="executed", created_at="2026-11-14T10:00:03+00:00"))

    assert [a["approval_id"] for a in store.list_approvals()] == ["a1", "a2", "a3"]
    assert [a["approval_id"] for a in store.list_approvals(status="pending")] == ["a1", "a2"]
    assert [a["approval_id"] for a in store.list_approvals(task_id="t2")] == ["a3"]
    assert store.get_approval("yok") is None


# ----------------------------------------------------------------------
# backup
# ----------------------------------------------------------------------

def test_backup_is_a_readable_copy(store, tmp_path):
    store.save_task(task(status="waiting_approval"))
    store.save_context("t1", CONTEXT)
    store.save_approval(approval())

    copy = TaskStore(store.backup(tmp_path / "backup" / "savas-copy.db"))
    assert copy.get_approval("a1")["status"] == "pending"
    assert copy.load_context("t1") == CONTEXT
    copy.close()
