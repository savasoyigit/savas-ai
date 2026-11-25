"""E-5: retry for Obsidian read calls (plan-e karar I) and the task API."""

import asyncio
import importlib
import json
import sys

import pytest


# ----------------------------------------------------------------------
# read retry
# ----------------------------------------------------------------------

class ReadClient:
    """httpx.AsyncClient stand-in for GET; answers come from `script` in order."""
    script: list = []
    calls: list = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        type(self).calls.append((url, params))
        answer = type(self).script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def reader(orch, monkeypatch):
    from conftest import FakeResponse

    monkeypatch.setattr(orch.httpx, "AsyncClient", ReadClient)
    monkeypatch.setattr(orch, "OBSIDIAN_RETRY_DELAY_SECONDS", 0)
    ReadClient.calls = []
    orch.ok = lambda body: FakeResponse(200, body)
    orch.status = lambda code: FakeResponse(code, {"detail": "x"})
    return orch


def retries(orch):
    return [e for e in orch.ACTIVITY if e["event"] == "tool_call" and e["status"] == "retry"]


@pytest.mark.parametrize("tool,args,body", [
    ("search_notes", ("sqlite",), {"query": "sqlite", "count": 0, "results": []}),
    ("read_note", ("a.md",), {"path": "a.md", "content": "metin"}),
    ("list_folder", ("",), {"path": "", "items": []}),
])
def test_read_is_tried_again_after_a_connection_error(reader, tool, args, body):
    ReadClient.script = [reader.httpx.HTTPError("connection refused"), reader.ok(body)]

    assert asyncio.run(getattr(reader, tool)(*args, "t1")) == body

    assert len(ReadClient.calls) == 2 and ReadClient.calls[0] == ReadClient.calls[1]
    [retry] = retries(reader)
    assert (retry["tool"], retry["attempt"]) == (tool, 1) and "refused" in retry["error"]


def test_read_is_tried_again_after_a_5xx(reader):
    ReadClient.script = [reader.status(503), reader.ok({"path": "a.md", "content": "metin"})]
    assert asyncio.run(reader.read_note("a.md", "t1"))["content"] == "metin"
    assert len(ReadClient.calls) == 2


@pytest.mark.parametrize("code", [400, 404])
def test_4xx_is_not_tried_again(reader, code):
    ReadClient.script = [reader.status(code)]

    with pytest.raises(reader.httpx.HTTPStatusError):
        asyncio.run(reader.read_note("yok.md", "t1"))

    assert len(ReadClient.calls) == 1 and retries(reader) == []


def test_only_one_retry(reader):
    ReadClient.script = [reader.status(502), reader.httpx.HTTPError("timeout")]

    with pytest.raises(reader.httpx.HTTPError):
        asyncio.run(reader.search_notes("q", "t1"))

    assert len(ReadClient.calls) == 2 and len(retries(reader)) == 1
    failed = [e for e in reader.ACTIVITY if e["event"] == "tool_call" and e["status"] == "failed"]
    assert len(failed) == 1


@pytest.mark.parametrize("tool,args", [
    ("create_note", ("x.md", "c")),
    ("update_note", ("x.md", "c")),
    ("move_note", ("a.md", "b.md")),
])
def test_writes_are_never_tried_again(orch, monkeypatch, tool, args):
    calls = []

    class WriteClient(ReadClient):
        async def post(self, url, json=None, **kwargs):
            calls.append(url)
            raise orch.httpx.HTTPError("connection refused")

        put = patch = post

    monkeypatch.setattr(orch.httpx, "AsyncClient", WriteClient)
    monkeypatch.setattr(orch, "OBSIDIAN_RETRY_DELAY_SECONDS", 0)

    with pytest.raises(Exception):
        asyncio.run(getattr(orch, tool)(*args, "t1"))

    assert len(calls) == 1 and retries(orch) == []


def test_llm_calls_are_not_retried_beyond_the_fallback_chain():
    source = (importlib.util.find_spec("main").origin)
    text = open(source, encoding="utf-8").read()
    # obsidian_get is the only retry loop; it is used by the three read tools only
    assert text.count("await obsidian_get(") == 3
    assert text.count("OBSIDIAN_READ_RETRIES + 1") == 1


# ----------------------------------------------------------------------
# task API
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


REPORT = json.dumps({"status": "done", "summary": "özet", "findings": [], "artifacts": [],
                     "open_questions": [], "deliverable": ""})
DELEGATE = {"id": "call_1", "type": "function", "function": {"name": "delegate_to_workers", "arguments": json.dumps(
    {"delegations": [{"worker": "jarvis", "task": "kod"}, {"worker": "oracle", "task": "araştır"}]})}}
CREATE = {"id": "call_1", "type": "function", "function": {"name": "create_note", "arguments": json.dumps(
    {"path": "x.md", "content": "c"})}}


def script(orch, *replies):
    queue = list(replies)

    def handler(url, payload):
        if "tools" not in payload:
            return reply(REPORT)
        return queue.pop(0) if queue else reply("cevap")

    orch.fake_client.handler = staticmethod(handler)


@pytest.fixture(params=["memory", "store"])
def api(request, tmp_path, monkeypatch):
    if request.param == "store":
        monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))
    orch = request.getfixturevalue("orch")
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    yield orch
    if orch.task_store is not None:
        orch.task_store.close()


def submit(orch, message="soru"):
    return asyncio.run(orch.create_task_endpoint(orch.TaskRequest(message=message)))


def test_tasks_are_listed_newest_first_and_filtered(api):
    script(api, reply("a"), reply("b"), reply(tool_calls=[CREATE]))
    ids = [submit(api, f"görev {i}")["task_id"] for i in range(3)]
    for i, task_id in enumerate(ids):
        api.TASKS[task_id]["created_at"] = f"2026-11-15T10:00:0{i}+00:00"

    listed = asyncio.run(api.list_tasks())
    assert [t["task_id"] for t in listed["tasks"]] == ids[::-1] and listed["count"] == 3
    assert [t["task_id"] for t in asyncio.run(api.list_tasks(status="completed"))["tasks"]] == ids[1::-1]
    assert [t["task_id"] for t in asyncio.run(api.list_tasks(status="waiting_approval"))["tasks"]] == [ids[2]]
    assert [t["task_id"] for t in asyncio.run(api.list_tasks(limit=1))["tasks"]] == [ids[2]]
    assert asyncio.run(api.list_tasks(limit=0))["count"] == 1


def test_unknown_status_is_refused(api):
    with pytest.raises(api.HTTPException) as refused:
        asyncio.run(api.list_tasks(status="done"))
    assert refused.value.status_code == 422


def test_task_detail_has_result_events_workers_and_approvals(api):
    script(api, reply(tool_calls=[DELEGATE]), reply(tool_calls=[CREATE]))
    task_id = submit(api)["task_id"]

    detail = asyncio.run(api.get_task(task_id))

    assert detail["task"]["status"] == "waiting_approval"
    assert ("task", "started") in [(e["event"], e["status"]) for e in detail["activity"]]
    [approval] = detail["approvals"]
    assert approval["action"] == "create_note" and approval["status"] == "pending"
    if api.task_store is None:
        assert detail["children"] == []
    else:
        assert sorted(c["worker"] for c in detail["children"]) == ["jarvis", "oracle"]
        assert all(c["status"] == "completed" and c["result"]["summary"] == "özet" for c in detail["children"])


def test_finished_task_carries_its_answer(api):
    script(api, reply("son cevap"))
    task_id = submit(api)["task_id"]
    assert asyncio.run(api.get_task(task_id))["task"]["result"]["response"] == "son cevap"


def test_missing_task_is_404(api):
    with pytest.raises(api.HTTPException) as missing:
        asyncio.run(api.get_task("yok"))
    assert missing.value.status_code == 404


def test_worker_tasks_are_not_in_the_top_level_list(api):
    script(api, reply(tool_calls=[DELEGATE]), reply("sentez"))
    task_id = submit(api)["task_id"]
    assert [t["task_id"] for t in asyncio.run(api.list_tasks())["tasks"]] == [task_id]


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


def test_api_shows_tasks_from_before_a_restart(db_path, orch, monkeypatch):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    script(orch, reply(tool_calls=[DELEGATE]), reply("sentez"))
    task_id = submit(orch)["task_id"]
    orch.task_store.close()

    sys.modules.pop("main", None)
    after = importlib.import_module("main")
    try:
        assert after.TASKS == {}
        [listed] = asyncio.run(after.list_tasks())["tasks"]
        assert listed["task_id"] == task_id and listed["result"]["response"] == "sentez"

        detail = asyncio.run(after.get_task(task_id))
        assert detail["task"]["status"] == "completed" and len(detail["children"]) == 2
        assert ("task", "completed") in [(e["event"], e["status"]) for e in detail["activity"]]

        child = detail["children"][0]["task_id"]
        assert asyncio.run(after.get_task(child))["task"]["parent_id"] == task_id
    finally:
        after.task_store.close()


# ----------------------------------------------------------------------
# thread safety (E-6: FastAPI runs plain "def" endpoints in a thread pool)
# ----------------------------------------------------------------------

def test_store_cannot_be_used_from_another_thread(db_path, orch):
    """The failure E-6 found live: why store endpoints must be async."""
    import threading

    errors = []
    worker = threading.Thread(target=lambda: errors.append(orch.store_write("list_tasks")))
    worker.start()
    worker.join()

    assert errors == [None]
    [failure] = [e for e in orch.ACTIVITY if e["event"] == "store"]
    assert "thread" in failure["error"]
    orch.task_store.close()


def test_every_endpoint_that_uses_the_store_is_async(orch):
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(orch))
    store_helpers = {"store_write", "persist_task", "persist_approval", "checkpoint_task", "add_activity"}

    def calls(node):
        return {c.func.id for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}

    sync_endpoints = [
        node.name for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ("get", "post", "delete")
                for d in node.decorator_list)
    ]
    # Plain "def" endpoints may only log system events (add_activity
    # with task_id="system" is not stored) and must not read the store.
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in sync_endpoints:
            used = calls(node) & (store_helpers - {"add_activity"})
            assert not used, f"{node.name} uses {used} but runs in a thread"

    for name in ("list_tasks", "get_task", "list_approvals", "get_approval", "cancel_task_endpoint",
                 "approve_endpoint", "reject_endpoint", "create_task_endpoint"):
        assert inspect.iscoroutinefunction(getattr(orch, name)), name
