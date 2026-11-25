import asyncio
import json

import pytest

import task_store as ts

REPORT = json.dumps({"status": "done", "summary": "özet", "findings": [], "artifacts": [],
                     "open_questions": [], "deliverable": ""})


def reply(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return 200, {
        "model": "gh/gpt-4.1",
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def tool_call(name, arguments, call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def script(*ares_replies):
    """LLM handler: Ares gets the given replies in order, every worker returns REPORT."""
    replies = list(ares_replies)

    def handler(url, payload):
        if "tools" not in payload:
            return reply(REPORT)
        return replies.pop(0) if replies else reply("son cevap")

    return handler


DELEGATE = tool_call("delegate_to_workers", {"delegations": [
    {"worker": "jarvis", "task": "kod"}, {"worker": "oracle", "task": "araştır"}]})
CREATE = tool_call("create_note", {"path": "notlar/yeni.md", "content": "içerik"})


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


@pytest.fixture
def db(db_env, orch, monkeypatch):
    """main.py with a task store on a temporary database."""
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    assert orch.task_store is not None
    yield orch
    orch.task_store.close()


@pytest.fixture
def mem(orch, monkeypatch):
    """main.py without a task store (TASK_DB_PATH empty)."""
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    assert orch.task_store is None
    return orch


def submit(orch, message="soru", **fields):
    return asyncio.run(orch.create_task_endpoint(orch.TaskRequest(message=message, **fields)))


def events(orch, task_id):
    return [(e["event"], e["status"]) for e in orch.task_store.list_events(task_id)]


def slow_llm(orch, monkeypatch, gate=None):
    """LLM calls wait until `gate` is set (forever when None)."""
    async def post(self, url, headers=None, json=None, **kwargs):
        type(self).calls.append({"url": url, "json": json})
        await (gate.wait() if gate else asyncio.Event().wait())

    monkeypatch.setattr(orch.fake_client, "post", post)


# ----------------------------------------------------------------------
# task records
# ----------------------------------------------------------------------

def test_task_is_recorded_from_queue_to_result(db):
    db.fake_client.handler = staticmethod(script(reply("cevap")))
    statuses = []
    real_save = db.task_store.save_task
    db.task_store.save_task = lambda task: (statuses.append(task.get("status")), real_save(task))

    result = submit(db, "401 ile 403 farkı")
    saved = db.task_store.get_task(result["task_id"])

    assert result["status"] == "completed" and result["response"] == "cevap"
    assert statuses[0] == "queued" and statuses[1] == "running" and statuses[-1] == "completed"
    assert saved["status"] == "completed" and saved["kind"] == "task" and saved["completed_at"]
    assert saved["message"] == "401 ile 403 farkı" and saved["daily_memory"].startswith("workers/ares/daily/")
    assert saved["result"]["response"] == "cevap" and saved["error"] is None
    assert db.TASKS[result["task_id"]]["result"] == result
    assert ("task", "started") in events(db, result["task_id"]) and ("task", "completed") in events(db, result["task_id"])


def test_new_task_starts_queued(db):
    task_id = db.create_task("x", "ares")
    assert db.TASKS[task_id]["status"] == "queued" == db.task_store.get_task(task_id)["status"]


def test_workers_are_child_tasks(db):
    db.fake_client.handler = staticmethod(script(reply(tool_calls=[DELEGATE]), reply("sentez")))

    result = submit(db)
    children = db.task_store.list_tasks(parent_id=result["task_id"])

    assert result["status"] == "completed"
    assert sorted(c["worker"] for c in children) == ["jarvis", "oracle"]
    for child in children:
        assert child["error"] is None, child["error"]
        assert child["kind"] == "worker" and child["status"] == "completed" and child["completed_at"]
        assert child["result"]["summary"] == "özet"
    assert {c["message"] for c in children} == {"kod", "araştır"}
    # worker events are logged on the parent task
    assert ("worker_report", "parsed") in events(db, result["task_id"])


def test_blocked_worker_child_is_blocked(db, monkeypatch):
    async def refuse(*args, **kwargs):
        raise db.BudgetExceeded("limit")

    monkeypatch.setattr(db.budget_manager, "reserve", refuse)
    result = asyncio.run(db.run_worker("jarvis", "kod", "p1"))

    [child] = db.task_store.list_tasks(parent_id="p1")
    assert result["status"] == child["status"] == "blocked" and "limit" in child["error"]


def test_failed_worker_child_is_failed(db, monkeypatch):
    with pytest.raises(db.WorkerError):
        asyncio.run(db.run_worker("yok", "kod", "p1"))

    [child] = db.task_store.list_tasks(parent_id="p1")
    assert child["status"] == "failed" and "yok" in child["error"]


def test_failed_run_keeps_its_reason(db):
    def handler(url, payload):
        return 500, {"error": "boom"}

    db.fake_client.handler = staticmethod(handler)
    with pytest.raises(db.HTTPException):
        submit(db)

    [saved] = db.task_store.list_tasks(kind="task")
    assert saved["status"] == "failed" and saved["error"]


def test_approval_round_survives_in_the_record(db, monkeypatch):
    async def create_note(*args):
        return {"status": "ok"}

    monkeypatch.setattr(db, "create_note", create_note)
    db.fake_client.handler = staticmethod(script(reply(tool_calls=[CREATE]), reply("not yazıldı")))

    waiting = submit(db)
    task_id = waiting["task_id"]
    assert waiting["status"] == "waiting_approval"
    assert db.task_store.get_task(task_id)["status"] == "waiting_approval"
    assert db.task_store.get_task(task_id)["result"] is None
    assert task_id not in db.RUNNING_TASKS

    [approval] = waiting["pending_approvals"]
    done = asyncio.run(db.approve_endpoint(approval["approval_id"]))

    saved = db.task_store.get_task(task_id)
    assert done["status"] == "completed" and saved["status"] == "completed"
    assert saved["result"]["response"] == "not yazıldı"
    assert ("approval", "approved") in events(db, task_id)


# ----------------------------------------------------------------------
# time limit and cancellation
# ----------------------------------------------------------------------

def test_run_over_the_time_limit_fails(db, monkeypatch):
    monkeypatch.setattr(db, "TASK_TIMEOUT_SECONDS", 0.05)
    slow_llm(db, monkeypatch)

    result = submit(db)
    task_id = result["task_id"]

    assert result == {"status": "failed", "task_id": task_id, "error": "timeout"}
    saved = db.task_store.get_task(task_id)
    assert saved["status"] == "failed" and saved["error"] == "timeout" and saved["completed_at"]
    assert db.TASKS[task_id]["status"] == "failed" and task_id not in db.TASK_CONTEXTS
    # the call's budget reservation was released
    assert db.budget_manager._reserved == {}
    assert ("task", "failed") in events(db, task_id) and task_id not in db.RUNNING_TASKS


def test_waiting_for_approval_is_not_timed(db, monkeypatch):
    monkeypatch.setattr(db, "TASK_TIMEOUT_SECONDS", 0.5)
    db.fake_client.handler = staticmethod(script(reply(tool_calls=[CREATE])))

    task_id = submit(db)["task_id"]
    asyncio.run(asyncio.sleep(0.6))
    assert db.TASKS[task_id]["status"] == "waiting_approval"


def run_and_cancel(orch, message="soru"):
    async def scenario():
        job = asyncio.ensure_future(orch.create_task_endpoint(orch.TaskRequest(message=message)))
        while not orch.RUNNING_TASKS:
            await asyncio.sleep(0.01)
        [task_id] = orch.RUNNING_TASKS
        await asyncio.sleep(0.05)
        answer = await orch.cancel_task_endpoint(task_id)
        return task_id, answer, await job

    return asyncio.run(scenario())


def test_running_task_can_be_cancelled(db, monkeypatch):
    slow_llm(db, monkeypatch)

    task_id, answer, result = run_and_cancel(db)

    assert answer == {"task_id": task_id, "status": "cancelled"}
    assert result == {"status": "cancelled", "task_id": task_id, "error": "cancelled by user"}
    saved = db.task_store.get_task(task_id)
    assert saved["status"] == "cancelled" and saved["completed_at"] and saved["result"]["status"] == "cancelled"
    assert db.budget_manager._reserved == {}
    assert task_id not in db.RUNNING_TASKS and not db.CANCEL_REQUESTED
    assert ("task", "cancelled") in events(db, task_id)


def test_cancelling_a_task_cancels_its_workers(db, monkeypatch):
    gate = asyncio.Event()
    first = {"done": False}
    real_post = db.fake_client.post

    async def post(self, url, headers=None, json=None, **kwargs):
        if not first["done"]:
            first["done"] = True
            return await real_post(self, url, headers=headers, json=json, **kwargs)
        type(self).calls.append({"url": url, "json": json})
        await asyncio.Event().wait()

    db.fake_client.handler = staticmethod(script(reply(tool_calls=[DELEGATE])))
    monkeypatch.setattr(db.fake_client, "post", post)

    task_id, answer, result = run_and_cancel(db)

    children = db.task_store.list_tasks(parent_id=task_id)
    assert result["status"] == "cancelled"
    assert sorted(c["status"] for c in children) == ["cancelled", "cancelled"]
    assert db.budget_manager._reserved == {}
    del gate


def test_cancel_refuses_tasks_that_are_not_running(db):
    with pytest.raises(db.HTTPException) as missing:
        asyncio.run(db.cancel_task_endpoint("yok"))
    assert missing.value.status_code == 404

    db.fake_client.handler = staticmethod(script(reply("cevap")))
    done = submit(db)["task_id"]
    with pytest.raises(db.HTTPException) as finished:
        asyncio.run(db.cancel_task_endpoint(done))
    assert finished.value.status_code == 409 and "completed" in finished.value.detail
    # a task waiting for approval can be cancelled since E-4 (test_approval_persistence.py)


def test_cancel_works_without_a_store(mem, monkeypatch):
    slow_llm(mem, monkeypatch)
    task_id, answer, result = run_and_cancel(mem)
    assert answer["status"] == "cancelled" == mem.TASKS[task_id]["status"]


# ----------------------------------------------------------------------
# idempotency
# ----------------------------------------------------------------------

def test_same_key_returns_the_first_result(db):
    db.fake_client.handler = staticmethod(script(reply("ilk"), reply("ikinci")))

    first = submit(db, "soru", idempotency_key="k-1")
    again = submit(db, "soru", idempotency_key="k-1")

    assert again == {**first, "idempotent_replay": True}
    assert len(db.fake_client.calls) == 1 and len(db.task_store.list_tasks()) == 1


def test_same_key_finds_the_task_after_a_restart(db):
    db.fake_client.handler = staticmethod(script(reply("ilk")))
    first = submit(db, "soru", idempotency_key="k-1")
    db.TASKS.clear()

    again = submit(db, "soru", idempotency_key="k-1")
    assert again["response"] == "ilk" and again["task_id"] == first["task_id"]


def test_same_key_while_waiting_returns_the_status(db):
    db.fake_client.handler = staticmethod(script(reply(tool_calls=[CREATE])))
    first = submit(db, "not yaz", idempotency_key="k-2")

    again = submit(db, "not yaz", idempotency_key="k-2")
    assert again == {"status": "waiting_approval", "task_id": first["task_id"], "idempotent_replay": True}
    assert len(db.APPROVALS) == 1


def test_same_key_with_another_message_is_refused(db):
    db.fake_client.handler = staticmethod(script(reply("ilk")))
    submit(db, "soru", idempotency_key="k-1")

    with pytest.raises(db.HTTPException) as refused:
        submit(db, "başka soru", idempotency_key="k-1")
    assert refused.value.status_code == 409


def test_key_older_than_a_day_opens_a_new_task(db):
    db.fake_client.handler = staticmethod(script(reply("ilk"), reply("yeni")))
    first = submit(db, "soru", idempotency_key="k-1")
    old = "2026-01-01T00:00:00+00:00"
    db.TASKS[first["task_id"]]["created_at"] = old
    db.task_store.save_task({"task_id": first["task_id"], "created_at": old})

    again = submit(db, "soru", idempotency_key="k-1")
    assert again["task_id"] != first["task_id"] and again["response"] == "yeni"
    assert db.task_store.find_task_by_key("k-1")["task_id"] == again["task_id"]


def test_no_key_means_a_new_task_each_time(db):
    db.fake_client.handler = staticmethod(script(reply("a"), reply("b")))
    assert submit(db)["task_id"] != submit(db)["task_id"]
    assert all(t["idempotency_key"] is None for t in db.task_store.list_tasks())


@pytest.mark.parametrize("key", ["  ", ""])
def test_blank_key_is_no_key(db, key):
    db.fake_client.handler = staticmethod(script(reply("a")))
    task_id = submit(db, idempotency_key=key)["task_id"]
    assert db.task_store.get_task(task_id)["idempotency_key"] is None


def test_too_long_key_is_refused(db):
    with pytest.raises(db.HTTPException) as refused:
        submit(db, idempotency_key="k" * 129)
    assert refused.value.status_code == 422 and db.TASKS == {}


def test_idempotency_without_a_store(mem):
    mem.fake_client.handler = staticmethod(script(reply("ilk"), reply("ikinci")))
    first = submit(mem, "soru", idempotency_key="k-1")
    assert submit(mem, "soru", idempotency_key="k-1")["response"] == first["response"] == "ilk"


# ----------------------------------------------------------------------
# start-up
# ----------------------------------------------------------------------

def test_start_up_settles_what_the_last_process_left(db):
    store = db.task_store
    for task_id, status in (("q", "queued"), ("r", "running"), ("w-ctx", "waiting_approval"),
                            ("w-lost", "waiting_approval"), ("c", "completed")):
        store.save_task({"task_id": task_id, "message": "m", "status": status})
    store.save_context("w-ctx", {"messages": [], "next_iteration": 2,
                                 "pending_tools": {"a1": {"tool_call_id": "c1", "name": "create_note", "arguments": {}}}})
    store.save_approval({"approval_id": "a1", "task_id": "w-ctx", "agent": "obsidian", "action": "create_note",
                         "status": "pending"})
    store.add_event({"task_id": "c", "event": "old", "status": "x", "timestamp": "2020-01-01T00:00:00+00:00"})
    store.close()

    store = db.open_task_store(str(store.path))

    statuses = {t["task_id"]: (t["status"], t["error"]) for t in store.list_tasks()}
    assert statuses == {"q": ("failed", ts.INTERRUPTED_ERROR), "r": ("failed", ts.INTERRUPTED_ERROR),
                        "w-ctx": ("waiting_approval", None), "w-lost": ("failed", db.APPROVAL_CONTEXT_LOST),
                        "c": ("completed", None)}
    assert store.load_context("w-ctx") is not None
    assert store.list_events("c") == []
    assert ("task", "failed") in events(db, "r") and ("task", "failed") in events(db, "w-lost")


def test_store_that_cannot_open_stops_the_start_up(db, monkeypatch, tmp_path):
    monkeypatch.setattr(ts, "SCHEMA_VERSION", 0)
    with pytest.raises(ts.StoreError):
        db.open_task_store(str(db.task_store.path))


def test_store_write_failure_does_not_stop_the_task(db, monkeypatch):
    def broken(task):
        raise ts.StoreError("disk dolu")

    monkeypatch.setattr(db.task_store, "save_task", broken)
    db.fake_client.handler = staticmethod(script(reply("cevap")))

    result = submit(db)
    assert result["status"] == "completed"
    failures = [e for e in db.ACTIVITY if e["event"] == "store"]
    assert failures and failures[0]["action"] == "save_task" and "disk dolu" in failures[0]["error"]
    # the failure event itself is not written to the store
    assert all(e["event"] != "store" for e in db.task_store.list_events("system"))
