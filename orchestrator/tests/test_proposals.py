"""J-2: note proposals from host automations go through the approval layer (plan-j L, karar b)."""

import asyncio
import importlib
import sys

import pytest

PATH = "SAVAS-AI/Raporlar/Haftalık 2026-W47.md"
WRITES = []


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))
    WRITES.clear()


@pytest.fixture
def app(db, orch, monkeypatch):
    started = [patched(orch, monkeypatch)]
    yield started
    for main in started:
        if main.task_store is not None:
            main.task_store.close()


def patched(main, monkeypatch):
    async def create_note(path, content, task_id):
        if path.endswith("var.md"):
            raise RuntimeError("Note already exists")
        WRITES.append((path, content))
        return {"status": "created", "path": path}

    monkeypatch.setattr(main, "create_note", create_note)
    return main


def propose(main, path=PATH, content="# rapor\n", reason="Haftalık rapor"):
    return asyncio.run(main.propose_note(main.NoteProposal(path=path, content=content, reason=reason)))


def stored(main, task_id):
    return main.task_store.get_task(task_id)


def test_a_proposal_waits_for_approval_and_writes_nothing(app):
    main = app[0]
    answer = propose(main)
    assert answer["status"] == "waiting_approval" and WRITES == []
    approval = main.APPROVALS[answer["approval_id"]]
    assert approval["agent"] == "obsidian" and approval["action"] == "create_note"
    assert approval["arguments"] == {"path": PATH, "content": "# rapor\n"} and approval["reason"] == "Haftalık rapor"
    task = stored(main, answer["task_id"])
    assert task["kind"] == "proposal" and task["status"] == "waiting_approval"
    assert [e["status"] for e in main.ACTIVITY if e["event"] == "approval"] == ["pending"]


def test_approve_writes_the_note_without_an_llm(app):
    main = app[0]
    answer = propose(main)
    calls_before = len(main.fake_client.calls) if hasattr(main.fake_client, "calls") else None
    result = asyncio.run(main.approve_endpoint(answer["approval_id"]))
    assert result["status"] == "executed" and WRITES == [(PATH, "# rapor\n")]
    assert main.APPROVALS[answer["approval_id"]]["status"] == "executed"
    assert stored(main, answer["task_id"])["status"] == "completed"
    if calls_before is not None:
        assert len(main.fake_client.calls) == calls_before


def test_reject_writes_nothing(app):
    main = app[0]
    answer = propose(main)
    result = asyncio.run(main.reject_endpoint(answer["approval_id"]))
    assert result["status"] == "rejected" and WRITES == []
    task = stored(main, answer["task_id"])
    assert task["status"] == "cancelled" and task["error"] == "rejected by user"
    with pytest.raises(main.HTTPException) as caught:
        asyncio.run(main.approve_endpoint(answer["approval_id"]))
    assert caught.value.status_code == 409


def test_a_failed_write_fails_the_task(app):
    main = app[0]
    answer = propose(main, path="SAVAS-AI/Raporlar/var.md")
    result = asyncio.run(main.approve_endpoint(answer["approval_id"]))
    assert result["status"] == "error" and "already exists" in result["error"]
    assert main.APPROVALS[answer["approval_id"]]["status"] == "failed"
    assert stored(main, answer["task_id"])["status"] == "failed"
    assert any(e["event"] == "task" and e["status"] == "failed" for e in main.ACTIVITY)  # -> gateway alert


def test_an_auto_approval_rule_applies_as_for_agents(app):
    main = app[0]
    main.create_auto_approval(main.AutoApprovalRequest(agent="obsidian", actions=["create_note"],
                                                       paths=["SAVAS-AI/Raporlar/*"], duration_minutes=5))
    answer = propose(main)
    assert answer["status"] == "completed" and WRITES == [(PATH, "# rapor\n")]
    assert not [a for a in main.APPROVALS.values() if a["task_id"] == answer["task_id"]]


@pytest.mark.parametrize("path", [
    "notlar/x.md", "SAVAS-AI/workers/ares/ares.md", "SAVAS-AI/Raporlar/x.txt", "SAVAS-AI/Raporlar/../x.md",
    "SAVAS-AI/Raporlar/.gizli.md", "SAVAS-AI/Raporlar//x.md", "/SAVAS-AI/Raporlar/x.md", "SAVAS-AI/Raporlar/a\\b.md",
])
def test_paths_outside_the_report_folder_are_refused(app, path):
    with pytest.raises(app[0].HTTPException) as caught:
        propose(app[0], path=path)
    assert caught.value.status_code == 422 and WRITES == []


@pytest.mark.parametrize("content,reason", [("", "x"), ("  \n", "x"), ("x" * 20001, "x"), ("x", "r" * 201)])
def test_bad_content_is_refused(app, content, reason):
    with pytest.raises(app[0].HTTPException):
        propose(app[0], content=content, reason=reason)


def test_a_waiting_proposal_survives_a_restart(app, monkeypatch):
    first = app[0]
    answer = propose(first)
    first.task_store.close()
    sys.modules.pop("main", None)
    second = patched(importlib.import_module("main"), monkeypatch)
    app.append(second)

    assert second.APPROVALS[answer["approval_id"]]["status"] == "pending"
    assert stored(second, answer["task_id"])["status"] == "waiting_approval"
    assert asyncio.run(second.approve_endpoint(answer["approval_id"]))["status"] == "executed"
    assert WRITES == [(PATH, "# rapor\n")]
