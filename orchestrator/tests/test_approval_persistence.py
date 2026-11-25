"""
E-4: approvals survive a restart (plan-e). A restart is simulated by
closing the store and importing main.py again on the same database.
"""

import asyncio
import importlib
import json
import sys

import pytest

import task_store as ts


def reply(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return 200, {
        "model": "gh/gpt-4.1",
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def write_call(path, call_id="call_1"):
    return {"id": call_id, "type": "function",
            "function": {"name": "create_note", "arguments": json.dumps({"path": path, "content": "içerik"})}}


WRITES = []


def prepare(orch, monkeypatch, *replies):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")

    async def write(*args):
        WRITES.append(args[:-1])
        return {"status": "ok"}

    for name in ("create_note", "update_note", "move_note"):
        monkeypatch.setattr(orch, name, write)

    queue = list(replies)
    orch.fake_client.handler = staticmethod(lambda url, payload: queue.pop(0) if queue else reply("son cevap"))
    return orch


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))
    WRITES.clear()
    return tmp_path


@pytest.fixture
def first(db, orch):
    """The process before the restart."""
    yield orch
    if orch.task_store is not None:
        orch.task_store.close()


@pytest.fixture
def restart(first, monkeypatch):
    """restart(*replies) -> a fresh main.py on the same database."""
    started = []

    def run(*replies):
        current = started[-1] if started else first
        current.task_store.close()
        sys.modules.pop("main", None)
        main = importlib.import_module("main")
        main.fake_client = first.fake_client
        started.append(main)
        return prepare(main, monkeypatch, *replies)

    yield run
    for main in started:
        main.task_store.close()


def submit(orch, message="not yaz"):
    return asyncio.run(orch.create_task_endpoint(orch.TaskRequest(message=message)))


def wait_for_one(first, monkeypatch, path="notlar/x.md"):
    prepare(first, monkeypatch, reply(tool_calls=[write_call(path)]))
    result = submit(first)
    assert result["status"] == "waiting_approval"
    return result["task_id"], result["pending_approvals"][0]["approval_id"]


def stored_task(orch, task_id):
    return orch.task_store.get_task(task_id)


# ----------------------------------------------------------------------
# restore
# ----------------------------------------------------------------------

def test_pending_approval_is_still_there_after_a_restart(first, restart, monkeypatch):
    task_id, approval_id = wait_for_one(first, monkeypatch)

    after = restart()

    listed = asyncio.run(after.list_approvals(status="pending"))
    assert [a["approval_id"] for a in listed["approvals"]] == [approval_id]
    assert asyncio.run(after.get_approval(approval_id))["arguments"] == {"path": "notlar/x.md", "content": "içerik"}
    assert after.TASKS[task_id]["status"] == "waiting_approval"
    assert after.TASK_CONTEXTS[task_id]["pending_tools"][approval_id]["name"] == "create_note"
    assert any(e["event"] == "task" and e["status"] == "restored" for e in after.ACTIVITY)


def test_approving_after_a_restart_runs_the_write_once_and_finishes(first, restart, monkeypatch):
    task_id, approval_id = wait_for_one(first, monkeypatch)
    after = restart(reply("not yazıldı"))

    result = asyncio.run(after.approve_endpoint(approval_id))

    assert result["status"] == "completed" and result["response"] == "not yazıldı"
    assert WRITES == [("notlar/x.md", "içerik")]
    assert stored_task(after, task_id)["status"] == "completed"
    assert after.task_store.get_approval(approval_id)["status"] == "executed"
    # the model saw the whole conversation from before the restart
    sent = after.fake_client.calls[-1]["json"]["messages"]
    assert sent[1]["content"] == "not yaz" and sent[-1]["role"] == "tool"


def test_rejecting_after_a_restart_writes_nothing_and_finishes(first, restart, monkeypatch):
    task_id, approval_id = wait_for_one(first, monkeypatch)
    after = restart(reply("vazgeçildi"))

    result = asyncio.run(after.reject_endpoint(approval_id))

    assert result["status"] == "completed" and WRITES == []
    assert after.task_store.get_approval(approval_id)["status"] == "rejected"
    assert stored_task(after, task_id)["status"] == "completed"
    assert stored_task(after, task_id)["has_context"] is False


def test_history_is_listed_after_a_restart(first, restart, monkeypatch):
    task_id, approval_id = wait_for_one(first, monkeypatch)
    prepare(first, monkeypatch, reply("tamam"))
    asyncio.run(first.reject_endpoint(approval_id))

    after = restart()

    assert after.APPROVALS == {}
    assert [a["status"] for a in asyncio.run(after.list_approvals())["approvals"]] == ["rejected"]
    assert asyncio.run(after.get_approval(approval_id))["status"] == "rejected"
    with pytest.raises(after.HTTPException) as refused:
        asyncio.run(after.approve_endpoint(approval_id))
    assert refused.value.status_code == 404 and WRITES == []


def test_second_approval_of_a_turn_comes_back_alone(first, restart, monkeypatch):
    prepare(first, monkeypatch, reply(tool_calls=[write_call("a.md"), write_call("b.md", "call_2")]))
    task_id = submit(first)["task_id"]
    one, two = (a["approval_id"] for a in first.get_pending_approvals_for_task(task_id))
    asyncio.run(first.approve_endpoint(one))

    after = restart(reply("ikisi tamam"))

    assert list(after.TASK_CONTEXTS[task_id]["pending_tools"]) == [two]
    result = asyncio.run(after.approve_endpoint(two))
    assert result["status"] == "completed"
    assert WRITES == [("a.md", "içerik"), ("b.md", "içerik")]


def test_auto_approval_policies_do_not_survive(first, restart, monkeypatch):
    prepare(first, monkeypatch)
    first.create_auto_approval(first.AutoApprovalRequest(
        agent="obsidian", actions=["create_note"], paths=["*"], duration_minutes=60))

    after = restart(reply(tool_calls=[write_call("notlar/x.md")]))

    assert after.AUTO_APPROVALS == {}
    assert submit(after)["status"] == "waiting_approval" and WRITES == []


def test_reads_record_comes_back_as_a_set(first, restart, monkeypatch):
    task_id, _ = wait_for_one(first, monkeypatch)
    first.TASK_CONTEXTS[task_id]["reads"].add(("note", "a.md", 0))
    first.checkpoint_task(task_id)

    after = restart()
    assert after.TASK_CONTEXTS[task_id]["reads"] == {("note", "a.md", 0)}


# ----------------------------------------------------------------------
# what cannot come back
# ----------------------------------------------------------------------

def test_approved_write_cut_by_a_restart_is_never_run_again(first, restart, monkeypatch):
    task_id, approval_id = wait_for_one(first, monkeypatch)
    # approved, then the process died before the write ran
    first.task_store.save_approval({"approval_id": approval_id, "status": "approved"})

    after = restart()

    assert after.task_store.get_approval(approval_id)["status"] == "interrupted"
    saved = stored_task(after, task_id)
    assert saved["status"] == "failed" and saved["error"] == after.APPROVAL_INTERRUPTED
    assert task_id not in after.TASKS and approval_id not in after.APPROVALS
    assert WRITES == []
    with pytest.raises(after.HTTPException):
        asyncio.run(after.approve_endpoint(approval_id))
    assert WRITES == []


def test_half_resolved_turn_fails_and_its_rest_is_cancelled(first, restart, monkeypatch):
    prepare(first, monkeypatch, reply(tool_calls=[write_call("a.md"), write_call("b.md", "call_2")]))
    task_id = submit(first)["task_id"]
    one, two = (a["approval_id"] for a in first.get_pending_approvals_for_task(task_id))
    # the first write ran, the process died before the context was saved again
    first.task_store.save_approval({"approval_id": one, "status": "executed"})

    after = restart()

    saved = stored_task(after, task_id)
    assert saved["status"] == "failed" and saved["error"] == after.APPROVAL_ROUND_INCOMPLETE
    assert after.task_store.get_approval(two)["status"] == "cancelled"
    assert after.task_store.get_approval(one)["status"] == "executed"
    assert task_id not in after.TASKS and WRITES == []


def test_approval_without_its_checkpoint_is_cancelled(first, restart, monkeypatch):
    prepare(first, monkeypatch)
    task_id = first.create_task("not yaz", "ares")
    first.complete_task(task_id, "running")
    approval = first.create_approval(task_id, "obsidian", "create_note", {"path": "x.md", "content": "c"},
                                     "onay", "call_1")

    after = restart()

    assert stored_task(after, task_id)["status"] == "failed"
    assert after.task_store.get_approval(approval["approval_id"])["status"] == "cancelled"
    assert after.APPROVALS == {}


def test_waiting_task_without_a_context_fails(first, restart, monkeypatch):
    task_id, approval_id = wait_for_one(first, monkeypatch)
    first.task_store.clear_context(task_id)

    after = restart()

    assert stored_task(after, task_id)["error"] == after.APPROVAL_CONTEXT_LOST
    assert after.task_store.get_approval(approval_id)["status"] == "cancelled"


# ----------------------------------------------------------------------
# cancelling a task that waits for approval
# ----------------------------------------------------------------------

def test_waiting_task_can_be_cancelled(first, monkeypatch):
    task_id, approval_id = wait_for_one(first, monkeypatch)

    answer = asyncio.run(first.cancel_task_endpoint(task_id))

    assert answer == {"task_id": task_id, "status": "cancelled"}
    assert first.APPROVALS[approval_id]["status"] == "cancelled"
    assert first.task_store.get_approval(approval_id)["status"] == "cancelled"
    saved = stored_task(first, task_id)
    assert saved["status"] == "cancelled" and saved["has_context"] is False
    assert task_id not in first.TASK_CONTEXTS

    with pytest.raises(first.HTTPException) as refused:
        asyncio.run(first.approve_endpoint(approval_id))
    assert refused.value.status_code == 409 and WRITES == []


def test_restored_task_can_be_cancelled(first, restart, monkeypatch):
    task_id, approval_id = wait_for_one(first, monkeypatch)
    after = restart()

    assert asyncio.run(after.cancel_task_endpoint(task_id))["status"] == "cancelled"
    assert after.task_store.get_approval(approval_id)["status"] == "cancelled"


def test_cancel_without_a_store(orch, monkeypatch):
    assert orch.task_store is None
    prepare(orch, monkeypatch, reply(tool_calls=[write_call("x.md")]))
    result = submit(orch)

    asyncio.run(orch.cancel_task_endpoint(result["task_id"]))
    assert orch.TASKS[result["task_id"]]["status"] == "cancelled"
    assert orch.APPROVALS[result["pending_approvals"][0]["approval_id"]]["status"] == "cancelled"


# ----------------------------------------------------------------------
# one transaction
# ----------------------------------------------------------------------

def test_checkpoint_is_all_or_nothing(first, monkeypatch):
    task_id, approval_id = wait_for_one(first, monkeypatch)
    store = first.task_store
    before = store.load_context(task_id)

    def broken(approval):
        raise ts.StoreError("disk dolu")

    monkeypatch.setattr(store, "save_approval", broken)
    first.TASKS[task_id]["status"] = "running"
    first.TASK_CONTEXTS[task_id]["next_iteration"] = 99

    first.checkpoint_task(task_id)

    assert stored_task(first, task_id)["status"] == "waiting_approval"
    assert store.load_context(task_id) == before
    assert any(e["event"] == "store" and e["action"] == "checkpoint" for e in first.ACTIVITY)
