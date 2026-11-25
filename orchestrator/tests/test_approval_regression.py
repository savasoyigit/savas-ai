"""
Approval Layer regression (handoff §10). Written against the code before
E-4 and kept unchanged after it: persistence must not change a decision.
Every test runs with and without the task store.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest


def reply(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return 200, {
        "model": "gh/gpt-4.1",
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def write_call(path, name="create_note", call_id="call_1"):
    arguments = ({"source": "eski.md", "destination": path} if name == "move_note"
                 else {"path": path, "content": "içerik"})
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


@pytest.fixture(params=["memory", "store"])
def app(request, tmp_path, monkeypatch):
    if request.param == "store":
        monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))
    orch = request.getfixturevalue("orch")
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    orch.writes = []

    async def write(*args):
        orch.writes.append(args[:-1])
        return {"status": "ok"}

    for name in ("create_note", "update_note", "move_note"):
        monkeypatch.setattr(orch, name, write)

    yield orch
    if orch.task_store is not None:
        orch.task_store.close()


def ares(orch, *replies):
    queue = list(replies)

    def handler(url, payload):
        return queue.pop(0) if queue else reply("son cevap")

    orch.fake_client.handler = staticmethod(handler)


def submit(orch, message="not yaz"):
    return asyncio.run(orch.create_task_endpoint(orch.TaskRequest(message=message)))


def policy(orch, paths, minutes=10, actions=("create_note",)):
    return orch.create_auto_approval(orch.AutoApprovalRequest(
        agent="obsidian", actions=list(actions), paths=list(paths), duration_minutes=minutes))


def statuses(orch, event="approval"):
    return [e["status"] for e in orch.ACTIVITY if e["event"] == event]


# ----------------------------------------------------------------------

def test_write_outside_any_policy_waits_for_approval(app):
    ares(app, reply(tool_calls=[write_call("notlar/x.md")]))

    result = submit(app)

    assert result["status"] == "waiting_approval"
    [approval] = result["pending_approvals"]
    assert approval["status"] == "pending" and approval["action"] == "create_note"
    assert approval["arguments"] == {"path": "notlar/x.md", "content": "içerik"}
    assert app.writes == []
    assert app.TASKS[result["task_id"]]["status"] == "waiting_approval"


@pytest.mark.parametrize("path", ["850-Companion/x.md", "proje/850-Companion-notlar/y.md",
                                  "🔮 850-Companion/z.md"])
def test_write_matching_a_policy_is_auto_approved(app, path):
    created = policy(app, ["*850-Companion*/*"])
    ares(app, reply(tool_calls=[write_call(path)]), reply("yazıldı"))

    result = submit(app)

    assert result["status"] == "completed"
    assert app.writes == [(path, "içerik")]
    assert app.APPROVALS == {}
    [auto] = [e for e in app.ACTIVITY if e["status"] == "auto_approved"]
    assert auto["auto_approval_id"] == created["auto_approval_id"] and auto["path"] == path


def test_policy_does_not_cover_other_paths_or_actions(app):
    policy(app, ["*850-Companion*/*"])
    ares(app, reply(tool_calls=[write_call("notlar/x.md"), write_call("850-Companion/a.md", "update_note", "call_2")]))

    result = submit(app)

    assert sorted(a["action"] for a in result["pending_approvals"]) == ["create_note", "update_note"]
    assert app.writes == []


def test_expired_policy_asks_again(app):
    created = policy(app, ["*850-Companion*/*"])
    app.AUTO_APPROVALS[created["auto_approval_id"]]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    ares(app, reply(tool_calls=[write_call("850-Companion/x.md")]))

    result = submit(app)

    assert result["status"] == "waiting_approval" and app.writes == []
    assert app.AUTO_APPROVALS[created["auto_approval_id"]]["status"] == "expired"
    assert "expired" in statuses(app, "auto_approval")


def test_ten_minute_policy_holds_until_it_ends(app):
    created = policy(app, ["*850-Companion*/*"], minutes=10)
    expires = datetime.fromisoformat(created["expires_at"])
    assert timedelta(minutes=9, seconds=59) < expires - datetime.fromisoformat(created["created_at"]) <= timedelta(minutes=10)
    assert app.auto_approval_matches("obsidian", "create_note", "850-Companion/x.md") is not None

    app.AUTO_APPROVALS[created["auto_approval_id"]]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(microseconds=1)).isoformat()
    assert app.auto_approval_matches("obsidian", "create_note", "850-Companion/x.md") is None


def test_disabled_policy_asks_again(app):
    created = policy(app, ["*"])
    app.disable_auto_approval(created["auto_approval_id"])
    ares(app, reply(tool_calls=[write_call("notlar/x.md")]))

    assert submit(app)["status"] == "waiting_approval" and app.writes == []


def test_companion_folder_is_gated_without_a_policy(app):
    ares(app, reply(tool_calls=[write_call("🔮 850-Companion/x.md")]))

    result = submit(app)

    assert result["status"] == "waiting_approval" and app.writes == []


def test_approved_write_runs_once_and_the_task_goes_on(app):
    ares(app, reply(tool_calls=[write_call("notlar/x.md")]), reply("not yazıldı"))
    [approval] = submit(app)["pending_approvals"]

    result = asyncio.run(app.approve_endpoint(approval["approval_id"]))

    assert result["status"] == "completed" and result["response"] == "not yazıldı"
    assert app.writes == [("notlar/x.md", "içerik")]
    saved = asyncio.run(app.get_approval(approval["approval_id"]))
    assert saved["status"] == "executed" and saved["approved_at"] and saved["executed_at"]


def test_rejected_write_is_not_run_and_a_retry_needs_a_new_approval(app):
    ares(app, reply(tool_calls=[write_call("notlar/x.md")]),
         reply(tool_calls=[write_call("notlar/x.md", call_id="call_2")]))
    [first] = submit(app)["pending_approvals"]

    again = asyncio.run(app.reject_endpoint(first["approval_id"]))

    assert again["status"] == "waiting_approval" and app.writes == []
    assert asyncio.run(app.get_approval(first["approval_id"]))["status"] == "rejected"
    [second] = again["pending_approvals"]
    assert second["approval_id"] != first["approval_id"] and second["status"] == "pending"
    # the rejection reached the model
    sent = app.fake_client.calls[-1]["json"]["messages"]
    assert any(m["role"] == "tool" and "rejected" in m["content"] for m in sent)


@pytest.mark.parametrize("decide", ["approve_endpoint", "reject_endpoint"])
def test_resolved_approval_cannot_be_decided_again(app, decide):
    ares(app, reply(tool_calls=[write_call("notlar/x.md")]))
    [approval] = submit(app)["pending_approvals"]
    asyncio.run(app.reject_endpoint(approval["approval_id"]))
    writes = list(app.writes)

    with pytest.raises(app.HTTPException) as refused:
        asyncio.run(getattr(app, decide)(approval["approval_id"]))

    assert refused.value.status_code == 409 and app.writes == writes


def test_unknown_approval_is_404(app):
    with pytest.raises(app.HTTPException) as missing:
        asyncio.run(app.approve_endpoint("yok"))
    assert missing.value.status_code == 404 and app.writes == []


def test_two_writes_in_one_turn_wait_for_both(app):
    ares(app, reply(tool_calls=[write_call("a.md"), write_call("b.md", call_id="call_2")]), reply("ikisi de"))
    first, second = submit(app)["pending_approvals"]

    partial = asyncio.run(app.approve_endpoint(first["approval_id"]))
    assert partial["status"] == "waiting_approval" and app.writes == [("a.md", "içerik")]

    done = asyncio.run(app.reject_endpoint(second["approval_id"]))
    assert done["status"] == "completed" and app.writes == [("a.md", "içerik")]


def test_workers_never_open_approvals(app):
    app.fake_client.handler = staticmethod(lambda url, payload: reply("rapor"))

    asyncio.run(app.run_worker("jarvis", "kod yaz", "p1"))

    worker_payload = app.fake_client.calls[-1]["json"]
    assert "tools" not in worker_payload
    assert app.APPROVALS == {} and app.writes == []
