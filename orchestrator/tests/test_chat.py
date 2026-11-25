"""K-5: direct chat with a worker (docs/plan-k.md karar I)."""

import asyncio

import pytest

import task_store as ts
from task_store import TaskStore


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


@pytest.fixture
def app(db, orch, monkeypatch):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    yield orch
    orch.task_store.close()


@pytest.fixture
def client():
    from conftest import FakeAsyncClient
    return FakeAsyncClient


def say(app, worker, message, conversation_id=None):
    async def go():
        answer = await app.chat_endpoint(worker, app.ChatRequest(message=message, conversation_id=conversation_id))
        await asyncio.gather(*list(app.BACKGROUND_JOBS))
        return answer

    return asyncio.run(go())


def history(app, worker, conversation_id=None):
    return asyncio.run(app.chat_history_endpoint(worker, conversation_id=conversation_id))


def refused(app, worker, message, conversation_id=None):
    with pytest.raises(app.HTTPException) as caught:
        say(app, worker, message, conversation_id)
    return caught.value.status_code


# ----------------------------------------------------------------------
# a turn
# ----------------------------------------------------------------------

def test_a_message_is_answered_and_kept(app, client):
    answer = say(app, "jarvis", "Merhaba JARVIS")
    assert answer["status"] == "running" and answer["conversation_id"]

    shown = history(app, "jarvis", answer["conversation_id"])
    assert shown["conversation_id"] == answer["conversation_id"]
    [turn] = shown["turns"]
    assert turn["status"] == "completed" and turn["message"] == "Merhaba JARVIS" and turn["reply"] == "tamam"
    assert turn["total_tokens"] == 12 and turn["model"]

    stored = app.task_store.get_task(answer["task_id"])
    assert stored["kind"] == "chat" and stored["worker"] == "jarvis" and stored["parent_id"] is None
    assert not app.BACKGROUND_JOBS


def test_the_call_has_no_tools_and_the_worker_context(app, client):
    (app.worker_manager.workers_root / "jarvis" / "jarvis.md").write_text("# JARVIS\nKişilik: titiz mühendis.\n")
    say(app, "jarvis", "Selam")
    payload = client.calls[-1]["json"]
    assert "tools" not in payload and "tool_choice" not in payload
    system = payload["messages"][0]["content"]
    assert "aracın yok" in system and "Mission Console" in system and "titiz mühendis" in system
    assert payload["messages"][-1] == {"role": "user", "content": "Selam"}


def test_usage_counts_for_the_worker(app, client):
    say(app, "gordon", "Bir soru")
    records = app.token_manager.read_usage()
    assert records[-1]["agent"] == "gordon" and records[-1]["total_tokens"] == 12


def test_the_next_turn_gets_the_history(app, client):
    first = say(app, "oracle", "Birinci soru")
    say(app, "oracle", "İkinci soru", first["conversation_id"])
    messages = client.calls[-1]["json"]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "Birinci soru" and messages[2]["content"] == "tamam"
    assert [t["message"] for t in history(app, "oracle", first["conversation_id"])["turns"]] == ["Birinci soru", "İkinci soru"]


def test_history_is_cut_by_turns_and_characters(app, client, monkeypatch):
    monkeypatch.setattr(app, "CHAT_HISTORY_TURNS", 2)
    conversation = say(app, "jarvis", "1")["conversation_id"]
    for n in range(2, 5):
        say(app, "jarvis", str(n), conversation)
    say(app, "jarvis", "son", conversation)
    users = [m["content"] for m in client.calls[-1]["json"]["messages"] if m["role"] == "user"]
    assert users == ["3", "4", "son"]

    monkeypatch.setattr(app, "CHAT_HISTORY_CHARS", 10)
    say(app, "jarvis", "x" * 20, conversation)
    say(app, "jarvis", "kısa", conversation)
    users = [m["content"] for m in client.calls[-1]["json"]["messages"] if m["role"] == "user"]
    assert users == ["kısa"]  # the long turn does not fit: nothing older is sent either


def test_a_new_conversation_starts_without_history(app, client):
    say(app, "jarvis", "eski sohbet")
    say(app, "jarvis", "yeni sohbet")
    assert [m["role"] for m in client.calls[-1]["json"]["messages"]] == ["system", "user"]


def test_failed_turns_are_not_sent_as_history(app, client):
    conversation = say(app, "jarvis", "ilk")["conversation_id"]
    client.handler = staticmethod(lambda url, payload: (400, {"error": "bad request"}))
    say(app, "jarvis", "bozuk", conversation)
    from conftest import ok_handler
    client.handler = staticmethod(ok_handler)
    say(app, "jarvis", "üçüncü", conversation)
    users = [m["content"] for m in client.calls[-1]["json"]["messages"] if m["role"] == "user"]
    assert users == ["ilk", "üçüncü"]


# ----------------------------------------------------------------------
# failures
# ----------------------------------------------------------------------

def test_an_llm_error_fails_the_turn(app, client):
    client.handler = staticmethod(lambda url, payload: (400, {"error": "bad request"}))
    answer = say(app, "jarvis", "Merhaba")
    [turn] = history(app, "jarvis", answer["conversation_id"])["turns"]
    assert turn["status"] == "failed" and turn["error"] and turn["reply"] is None


def test_a_budget_refusal_blocks_the_turn_without_a_call(app, client, monkeypatch):
    async def refuse(**kwargs):
        raise app.BudgetExceeded("Daily budget exceeded")

    monkeypatch.setattr(app.budget_manager, "reserve", refuse)
    answer = say(app, "jarvis", "Merhaba")
    [turn] = history(app, "jarvis", answer["conversation_id"])["turns"]
    assert turn["status"] == "blocked" and "budget" in turn["error"]
    assert client.calls == []


@pytest.mark.parametrize("worker,code", [("ares", 422), ("obsidian", 404), ("yok", 404), ("../x", 404)])
def test_who_one_can_chat_with(app, worker, code):
    assert refused(app, worker, "Merhaba") == code


@pytest.mark.parametrize("message", ["", "   ", "x" * 4001])
def test_message_size(app, message):
    assert refused(app, "jarvis", message) == 422


def test_conversation_rules(app, client):
    assert refused(app, "jarvis", "x", "../../etc") == 422
    theirs = say(app, "gordon", "Gordon'a")["conversation_id"]
    assert refused(app, "jarvis", "x", theirs) == 409

    app.task_store.save_task({"task_id": "busy", "kind": "chat", "worker": "jarvis", "message": "bekle",
                              "status": "running", "conversation_id": "aaaaaaaa-0000"})
    assert refused(app, "jarvis", "x", "aaaaaaaa-0000") == 409


# ----------------------------------------------------------------------
# reading, listing
# ----------------------------------------------------------------------

def test_without_an_id_the_latest_conversation_is_shown(app, client):
    assert history(app, "jarvis") == {"worker": "jarvis", "conversation_id": None, "turns": []}
    say(app, "jarvis", "eski")
    newest = say(app, "jarvis", "yeni")["conversation_id"]
    shown = history(app, "jarvis")
    assert shown["conversation_id"] == newest and [t["message"] for t in shown["turns"]] == ["yeni"]


def test_reading_validates_like_writing(app):
    for worker, cid, code in (("ares", None, 422), ("yok", None, 404), ("jarvis", "../x", 422)):
        with pytest.raises(app.HTTPException) as caught:
            history(app, worker, cid)
        assert caught.value.status_code == code


def test_chats_stay_out_of_the_task_list_but_show_on_the_worker(app, client):
    say(app, "jarvis", "sohbet")
    assert asyncio.run(app.list_tasks())["tasks"] == []
    app.task_store.save_task({"task_id": "live", "kind": "chat", "worker": "jarvis", "message": "yazıyor",
                              "status": "running", "conversation_id": "bbbbbbbb-0000"})
    workers = {w["id"]: w for w in asyncio.run(app.list_worker_profiles())["workers"]}
    assert [(t["task_id"], t["kind"]) for t in workers["jarvis"]["tasks"]] == [("live", "chat")]


# ----------------------------------------------------------------------
# schema v6
# ----------------------------------------------------------------------

def test_v5_database_is_migrated_with_its_tasks(tmp_path, monkeypatch):
    path = tmp_path / "savas.db"
    with monkeypatch.context() as patch:
        patch.setattr(ts, "SCHEMA_VERSION", 5)
        patch.setattr(ts, "MIGRATIONS", ts.MIGRATIONS[:5])
        patch.setattr(ts, "TASK_FIELDS", tuple(f for f in ts.TASK_FIELDS if f != "conversation_id"))
        old = TaskStore(path)
        old.save_task({"task_id": "t1", "message": "eski", "status": "completed"})
        old.close()

    store = TaskStore(path)
    try:
        assert store.schema_version() == ts.SCHEMA_VERSION
        assert store.get_task("t1")["conversation_id"] is None
        store.save_task({"task_id": "c1", "kind": "chat", "worker": "jarvis", "message": "a",
                         "status": "completed", "conversation_id": "cccccccc"})
        assert [t["task_id"] for t in store.list_conversation("cccccccc")] == ["c1"]
        assert store.latest_conversation("jarvis") == "cccccccc" and store.latest_conversation("gordon") is None
    finally:
        store.close()
