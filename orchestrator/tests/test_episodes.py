"""F-2: episodes of finished tasks (plan-f karar B: no LLM call)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import memory
import task_store as ts
from task_store import StoreError, TaskStore

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


def task(**fields):
    return {"task_id": "t1", "message": "SQLite mi Markdown mı?", "status": "completed", "worker": "ares",
            "error": None, "result": {"status": "completed", "response": "Markdown öneriyorum."}, **fields}


# ----------------------------------------------------------------------
# build_episode
# ----------------------------------------------------------------------

def test_episode_of_a_simple_task():
    episode = memory.build_episode(task(), now=NOW)

    assert episode == {
        "task_id": "t1", "worker": "ares", "workers": [], "status": "completed",
        "title": "SQLite mi Markdown mı?",
        "summary": "Sonuç: completed\nCevap: Markdown öneriyorum.",
        "created_at": "2026-11-15T10:00:00+00:00",
        "expires_at": (NOW + timedelta(days=180)).isoformat(),
    }


def test_episode_lists_workers_and_their_summaries():
    children = [
        {"worker": "oracle", "status": "completed", "created_at": "2", "result": {"summary": "Uzun vadede\nMarkdown."}},
        {"worker": "jarvis", "status": "completed", "created_at": "1", "result": {"summary": "SQLite hızlı."}},
        {"worker": "gordon", "status": "blocked", "created_at": "3", "result": None},
    ]
    episode = memory.build_episode(task(), children, now=NOW)

    assert episode["workers"] == ["gordon", "jarvis", "oracle"]
    assert episode["summary"].splitlines()[2:] == [
        "jarvis: SQLite hızlı.", "oracle: Uzun vadede Markdown.", "gordon: blocked"]


def test_episode_records_what_happened_to_writes():
    approvals = [
        {"status": "executed", "action": "create_note", "arguments": {"path": "a.md"}, "created_at": "1"},
        {"status": "rejected", "action": "update_note", "arguments": {"path": "b.md"}, "created_at": "2"},
        {"status": "executed", "action": "move_note", "arguments": {"source": "c.md", "destination": "d/c.md"},
         "created_at": "3"},
        {"status": "pending", "action": "create_note", "arguments": {"path": "e.md"}, "created_at": "4"},
    ]
    events = [{"event": "approval", "status": "auto_approved", "action": "create_note", "path": "850-Companion/x.md"},
              {"event": "task", "status": "completed"}]

    lines = memory.build_episode(task(), approvals=approvals, events=events, now=NOW)["summary"].splitlines()

    assert lines[2:] == ["Yazıldı: create_note a.md", "Reddedildi: update_note b.md",
                         "Yazıldı: move_note c.md → d/c.md",
                         "Otomatik onayla yazıldı: create_note 850-Companion/x.md"]


def test_failed_task_keeps_its_reason():
    episode = memory.build_episode(task(status="failed", error="timeout", result={"status": "failed"}), now=NOW)
    assert episode["summary"] == "Sonuç: failed (timeout)" and episode["status"] == "failed"


def test_long_texts_are_clipped():
    episode = memory.build_episode(task(message="soru " * 100, result={"response": "cevap. " * 200}), now=NOW)

    assert len(episode["title"]) <= memory.EPISODE_TITLE_CHARS
    answer = episode["summary"].splitlines()[1]
    assert len(answer) <= len("Cevap: ") + memory.EPISODE_ANSWER_CHARS and answer.endswith("[kırpıldı]")


# ----------------------------------------------------------------------
# store
# ----------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    store = TaskStore(tmp_path / "savas.db")
    yield store
    store.close()


def fts(store, query):
    return [row[0] for row in store.conn.execute(
        "SELECT rowid FROM episodes_fts WHERE episodes_fts MATCH ? ORDER BY rowid", (query,))]


def test_episode_round_trip_and_index(store):
    episode_id = store.save_episode(memory.build_episode(task(), now=NOW))
    saved = store.get_episode("t1")

    assert saved["episode_id"] == episode_id and saved["workers"] == []
    assert saved["summary"].endswith("Markdown öneriyorum.")
    assert fts(store, "markdown") == [episode_id]
    assert fts(store, '"öneriyor"*') == [episode_id]


def test_saving_again_replaces_the_episode_and_its_index(store):
    first = store.save_episode(memory.build_episode(task(), now=NOW))
    again = store.save_episode(memory.build_episode(
        task(status="failed", result={"response": "Vazgeçildi."}), now=NOW))

    assert first == again and store.get_episode("t1")["status"] == "failed"
    assert fts(store, "vazgeçildi") == [first] and fts(store, "öneriyorum") == []
    assert len(store.list_episodes()) == 1


def test_episodes_are_listed_newest_first_and_by_worker(store):
    for i, (worker, workers) in enumerate((("ares", []), ("ares", ["jarvis"]), ("jarvis", []))):
        store.save_episode({**memory.build_episode(task(task_id=f"t{i}", worker=worker), now=NOW + timedelta(minutes=i)),
                            "workers": workers})

    assert [e["task_id"] for e in store.list_episodes()] == ["t2", "t1", "t0"]
    assert [e["task_id"] for e in store.list_episodes(limit=1)] == ["t2"]
    assert [e["task_id"] for e in store.list_episodes(worker="jarvis")] == ["t2", "t1"]


def test_expired_episodes_are_purged_with_their_index(store):
    old = store.save_episode(memory.build_episode(task(task_id="old"), now=NOW - timedelta(days=200)))
    new = store.save_episode(memory.build_episode(task(task_id="new"), now=NOW))

    assert store.purge_episodes(NOW.isoformat()) == 1
    assert [e["task_id"] for e in store.list_episodes()] == ["new"]
    assert fts(store, "markdown") == [new] and old != new


@pytest.mark.parametrize("bad", [{"status": "completed", "title": "x", "summary": ""},
                                 {"task_id": "t", "title": "x", "summary": ""},
                                 {"task_id": "t", "status": "completed", "summary": ""},
                                 {"task_id": "t", "status": "completed", "title": "x"}])
def test_incomplete_episode_is_refused(store, bad):
    with pytest.raises(StoreError):
        store.save_episode(bad)


def test_v1_database_is_migrated_to_the_current_schema_with_its_data(tmp_path, monkeypatch):
    path = tmp_path / "savas.db"
    with monkeypatch.context() as patch:
        patch.setattr(ts, "SCHEMA_VERSION", 1)
        patch.setattr(ts, "MIGRATIONS", ts.MIGRATIONS[:1])
        old = TaskStore(path)
        old.save_task({"task_id": "t1", "message": "eski", "status": "completed"})
        old.save_approval({"approval_id": "a1", "task_id": "t1", "action": "create_note", "status": "executed"})
        assert old.schema_version() == 1
        old.close()

    store = TaskStore(path)
    try:
        assert store.schema_version() == ts.SCHEMA_VERSION
        assert store.get_task("t1")["message"] == "eski" and store.get_approval("a1")["status"] == "executed"
        store.save_episode(memory.build_episode(task(), now=NOW))
        assert store.list_episodes()[0]["task_id"] == "t1"
    finally:
        store.close()


# ----------------------------------------------------------------------
# main.py
# ----------------------------------------------------------------------

def reply(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return 200, {
        "model": "gh/gpt-4.1",
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


REPORT = json.dumps({"status": "done", "summary": "Rapor özeti.", "findings": [], "artifacts": [],
                     "open_questions": [], "deliverable": ""})
DELEGATE = {"id": "call_1", "type": "function", "function": {"name": "delegate_to_workers", "arguments": json.dumps(
    {"delegations": [{"worker": "jarvis", "task": "kod"}, {"worker": "oracle", "task": "araştır"}]})}}
CREATE = {"id": "call_1", "type": "function", "function": {"name": "create_note", "arguments": json.dumps(
    {"path": "notlar/x.md", "content": "c"})}}


def script(orch, *replies):
    queue = list(replies)

    def handler(url, payload):
        if "tools" not in payload:
            return reply(REPORT)
        return queue.pop(0) if queue else reply("son cevap")

    orch.fake_client.handler = staticmethod(handler)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))
    return tmp_path


@pytest.fixture
def app(db, orch, monkeypatch):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")

    async def write(*args):
        return {"status": "ok"}

    monkeypatch.setattr(orch, "create_note", write)
    yield orch
    orch.task_store.close()


def submit(orch, message="soru"):
    return asyncio.run(orch.create_task_endpoint(orch.TaskRequest(message=message)))


def episode_of(orch, task_id):
    return orch.task_store.get_episode(task_id)


def test_finished_task_leaves_an_episode(app):
    script(app, reply("Kısa cevap."))
    task_id = submit(app, "401 ile 403 farkı")["task_id"]

    episode = episode_of(app, task_id)
    assert episode["title"] == "401 ile 403 farkı" and episode["status"] == "completed"
    assert episode["summary"] == "Sonuç: completed\nCevap: Kısa cevap."
    assert any(e["event"] == "memory" and e["status"] == "episode_saved" for e in app.ACTIVITY)
    assert len(app.fake_client.calls) == 1  # no LLM call for the episode


def test_delegated_task_episode_names_its_workers(app):
    script(app, reply(tool_calls=[DELEGATE]), reply("Sentez."))
    task_id = submit(app)["task_id"]

    episode = episode_of(app, task_id)
    assert episode["workers"] == ["jarvis", "oracle"]
    assert "jarvis: Rapor özeti." in episode["summary"] and "oracle: Rapor özeti." in episode["summary"]


def test_waiting_task_has_no_episode_until_it_ends(app):
    script(app, reply(tool_calls=[CREATE]), reply("Vazgeçildi."))
    waiting = submit(app)
    assert episode_of(app, waiting["task_id"]) is None

    asyncio.run(app.reject_endpoint(waiting["pending_approvals"][0]["approval_id"]))

    episode = episode_of(app, waiting["task_id"])
    assert "Reddedildi: create_note notlar/x.md" in episode["summary"]
    assert "Cevap: Vazgeçildi." in episode["summary"]


def test_approved_write_is_in_the_episode(app):
    script(app, reply(tool_calls=[CREATE]), reply("Yazdım."))
    waiting = submit(app)
    asyncio.run(app.approve_endpoint(waiting["pending_approvals"][0]["approval_id"]))
    assert "Yazıldı: create_note notlar/x.md" in episode_of(app, waiting["task_id"])["summary"]


def test_cancelled_waiting_task_leaves_an_episode(app):
    script(app, reply(tool_calls=[CREATE]))
    task_id = submit(app)["task_id"]

    asyncio.run(app.cancel_task_endpoint(task_id))

    episode = episode_of(app, task_id)
    assert episode["status"] == "cancelled" and "İptal edildi: create_note notlar/x.md" in episode["summary"]


def test_timed_out_task_leaves_an_episode(app, monkeypatch):
    async def never(self, url, headers=None, json=None, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "TASK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(app.fake_client, "post", never)
    task_id = submit(app)["task_id"]

    assert episode_of(app, task_id)["summary"] == "Sonuç: failed (timeout)"


def test_failed_task_leaves_an_episode(app):
    app.fake_client.handler = staticmethod(lambda url, payload: (500, {"error": "boom"}))
    with pytest.raises(app.HTTPException):
        submit(app)

    [listed] = app.task_store.list_episodes()
    assert listed["status"] == "failed" and listed["summary"].startswith("Sonuç: failed (")


def test_episodes_endpoint(app):
    script(app, reply("a"), reply(tool_calls=[DELEGATE]), reply("b"))
    first = submit(app, "birinci")["task_id"]
    second = submit(app, "ikinci")["task_id"]

    listed = asyncio.run(app.list_episodes())
    assert [e["task_id"] for e in listed["episodes"]] == [second, first] and listed["count"] == 2
    assert [e["task_id"] for e in asyncio.run(app.list_episodes(worker="jarvis"))["episodes"]] == [second]
    assert asyncio.run(app.list_episodes(limit=1))["count"] == 1


def test_start_up_purges_expired_episodes(app):
    app.task_store.save_episode(memory.build_episode(task(task_id="old"), now=NOW - timedelta(days=400)))
    path = str(app.task_store.path)
    app.task_store.close()

    store = app.open_task_store(path)
    assert store.get_episode("old") is None


def test_without_a_store_no_episode_is_kept(orch, monkeypatch):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    script(orch, reply("cevap"))
    task_id = submit(orch)["task_id"]

    assert orch.record_episode(task_id) is None
    assert asyncio.run(orch.list_episodes()) == {"count": 0, "episodes": []}


def test_episodes_endpoint_is_async(orch):
    import inspect
    assert inspect.iscoroutinefunction(orch.list_episodes)
