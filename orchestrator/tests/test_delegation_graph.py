"""G-2: dependency-aware delegation (plan-g karar A, B, D)."""

import asyncio
import json

import pytest

import context_engine as ce
import delegation as dg
import task_store as ts
from task_store import TaskStore


def items(*specs):
    """specs: (worker, id, depends_on) tuples."""
    return [{"worker": w, "task": f"{w} işi", **({"id": i} if i else {}), **({"depends_on": d} if d else {})}
            for w, i, d in specs]


# ----------------------------------------------------------------------
# graph
# ----------------------------------------------------------------------

def test_without_depends_on_everything_is_one_layer():
    assert dg.plan_layers(items(("jarvis", None, None), ("oracle", "b", None), ("gordon", None, None))) == [[0, 1, 2]]


def test_chain_and_diamond_layers():
    assert dg.plan_layers(items(("oracle", "a", None), ("willy", "b", ["a"]), ("gordon", "c", ["b"]))) == \
        [[0], [1], [2]]
    assert dg.plan_layers(items(("oracle", "a", None), ("jarvis", "b", ["a"]), ("willy", "c", ["a"]),
                                ("gordon", "d", ["b", "c"]))) == [[0], [1, 2], [3]]


def test_order_in_the_list_does_not_matter():
    assert dg.plan_layers(items(("willy", "b", ["a"]), ("oracle", "a", None))) == [[1], [0]]


@pytest.mark.parametrize("delegations,word", [
    (items(("oracle", "a", None), ("willy", "a", None)), "Duplicate id"),
    (items(("oracle", "a", None), ("willy", "b", ["x"])), "unknown id"),
    (items(("oracle", "a", ["a"]), ("willy", "b", None)), "itself"),
    (items(("oracle", "a", ["b"]), ("willy", "b", ["a"])), "cycle"),
    (items(("oracle", "a", None), ("willy", "b", ["a"]), ("jarvis", "c", ["b"]), ("gordon", "d", ["c"])),
     "at most 3"),
    ([{"worker": "oracle", "task": "x", "id": "a"}, {"worker": "willy", "task": "y", "depends_on": "a"}],
     "list of ids"),
    ([{"worker": "oracle", "task": "x", "id": "a" * 33}, {"worker": "willy", "task": "y"}], "at most 32"),
])
def test_graphs_that_cannot_run_are_refused(delegations, word):
    with pytest.raises(dg.GraphError) as refused:
        dg.plan_layers(delegations)
    assert word in str(refused.value)


def test_dependents_are_the_items_whose_result_goes_on():
    assert dg.dependents(items(("oracle", "a", None), ("jarvis", "b", ["a"]), ("willy", "c", ["a", "b"]))) == {0, 1}
    assert dg.dependents(items(("oracle", None, None), ("willy", None, None))) == set()


# ----------------------------------------------------------------------
# inputs and references
# ----------------------------------------------------------------------

REPORT = {"status": "ok", "summary": "Kısa özet.", "findings": ["bulgu 1", "bulgu 2"], "artifacts": [],
          "open_questions": [], "deliverable": "Asıl çıktı metni."}


def test_input_block_shows_source_and_puts_the_deliverable_first():
    block = dg.input_block([{"id": "a", "worker": "oracle", "worker_task_id": "w-1", "report": REPORT}])
    assert block.startswith(dg.INPUT_HEADING + "\n\n## a · oracle (task w-1)\n")
    assert block.index("Asıl çıktı metni.") < block.index("Kısa özet.") < block.index("- bulgu 1")


def test_inputs_share_the_limit():
    long = {**REPORT, "deliverable": "uzun cümle. " * 600}
    sources = [{"id": i, "worker": "oracle", "worker_task_id": f"w-{i}", "report": long} for i in "ab"]
    block = dg.input_block(sources)
    assert len(block) <= dg.INPUT_CHARS + len(dg.INPUT_HEADING) + 4
    assert block.count("## ") == 2
    assert dg.input_block([]) == ""


def test_reference_is_short():
    ref = dg.reference("a", {"status": "completed", "worker": "oracle",
                             "report": {**REPORT, "summary": "özet " * 200}}, "w-1")
    assert set(ref) == {"id", "worker", "status", "task_id", "summary", "note"}
    assert ref["task_id"] == "w-1" and len(ref["summary"]) <= dg.REFERENCE_SUMMARY_CHARS
    assert "id" not in dg.reference(None, {"status": "completed", "worker": "oracle", "report": REPORT}, "w-1")


# ----------------------------------------------------------------------
# main.py with a stand-in worker
# ----------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


@pytest.fixture
def app(db, orch):
    yield orch
    orch.task_store.close()


@pytest.fixture
def workers(app, monkeypatch):
    """run_worker_steps stand-in: records calls; outcome per worker from `fail`."""
    calls, fail = [], set()

    async def fake(worker_id, task, parent_task_id, task_type=None, report_chars=None, worker_task_id=None,
                   inputs="", instructions=""):
        calls.append({"worker": worker_id, "task_id": worker_task_id, "inputs": inputs, "report_chars": report_chars})
        await asyncio.sleep(0)
        if worker_id in fail:
            return {"status": "failed", "worker": worker_id, "error": "model down"}
        report = {**REPORT, "summary": f"{worker_id} özeti", "deliverable": f"{worker_id} çıktısı"}
        return {"status": "completed", "worker": worker_id, "report": report}

    monkeypatch.setattr(app, "run_worker_steps", fake)
    return calls, fail


def run(app, delegations):
    return asyncio.run(app.run_workers_parallel(delegations, "parent-1"))


def events(app, status):
    return [e for e in app.ACTIVITY if e["event"] == "delegation" and e["status"] == status]


def test_independent_items_run_as_before(app, workers):
    calls, _ = workers
    result = run(app, items(("jarvis", None, None), ("oracle", None, None)))

    assert all(set(d) == {"status", "worker", "report"} for d in result["delegations"])
    assert all(c["inputs"] == "" and c["report_chars"] == ce.report_share(2) for c in calls)
    assert events(app, "planned") == []


def test_chain_passes_the_report_on_and_returns_a_reference(app, workers):
    calls, _ = workers
    result = run(app, items(("oracle", "a", None), ("willy", "b", ["a"])))

    assert [c["worker"] for c in calls] == ["oracle", "willy"]
    oracle, willy = calls
    assert "## a · oracle (task " + oracle["task_id"] + ")" in willy["inputs"]
    assert "oracle çıktısı" in willy["inputs"]
    assert willy["report_chars"] == ce.report_share(1)

    first, last = result["delegations"]
    assert first["id"] == "a" and first["task_id"] == oracle["task_id"] and "report" not in first
    assert first["summary"] == "oracle özeti"
    assert last["id"] == "b" and last["report"]["deliverable"] == "willy çıktısı"
    [planned] = events(app, "planned")
    assert planned["layers"] == [["oracle"], ["willy"]]


def test_children_record_their_dependencies(app, workers):
    calls, _ = workers
    run(app, items(("oracle", "a", None), ("jarvis", "b", None), ("willy", "c", ["a", "b"])))

    children = {c["worker"]: c for c in app.task_store.list_tasks(None, "parent-1", "worker")}
    by_worker = {c["worker"]: c["task_id"] for c in calls}
    assert children["willy"]["depends_on"] == [by_worker["oracle"], by_worker["jarvis"]]
    assert children["oracle"]["depends_on"] == [] and children["oracle"]["step"] is None


def test_item_after_a_failed_one_is_skipped(app, workers):
    calls, fail = workers
    fail.add("oracle")
    result = run(app, items(("oracle", "a", None), ("willy", "b", ["a"]), ("jarvis", None, None)))

    assert sorted(c["worker"] for c in calls) == ["jarvis", "oracle"]
    first, skipped, independent = result["delegations"]
    assert first["status"] == "failed" and "report" not in first and first["id"] == "a"
    assert skipped == {"id": "b", "status": "skipped", "worker": "willy",
                       "error": dg.SKIPPED_ERROR.format(ids="a")}
    assert independent["status"] == "completed"

    stored = {c["worker"]: c for c in app.task_store.list_tasks(None, "parent-1", "worker")}
    assert stored["willy"]["status"] == "skipped" and len(stored["willy"]["depends_on"]) == 1
    [event] = events(app, "skipped")
    assert event["worker"] == "willy"


def test_skip_goes_down_the_chain(app, workers):
    _, fail = workers
    fail.add("oracle")
    result = run(app, items(("oracle", "a", None), ("willy", "b", ["a"]), ("gordon", "c", ["b"])))
    assert [d["status"] for d in result["delegations"]] == ["failed", "skipped", "skipped"]


def test_invalid_graph_runs_nothing(app, workers):
    calls, _ = workers
    result = run(app, items(("oracle", "a", ["b"]), ("willy", "b", ["a"])))
    assert result == {"status": "error", "error": "depends_on has a cycle."}
    assert calls == [] and len(events(app, "invalid")) == 1


def test_without_a_store_the_chain_still_runs(orch, monkeypatch):
    seen = []

    async def fake(worker_id, task, parent_task_id, task_type=None, report_chars=None, worker_task_id=None,
                   inputs="", instructions=""):
        seen.append(inputs)
        return {"status": "completed", "worker": worker_id, "report": REPORT}

    monkeypatch.setattr(orch, "run_worker_steps", fake)
    result = asyncio.run(orch.run_workers_parallel(items(("oracle", "a", None), ("willy", "b", ["a"])), "p"))
    assert seen[0] == "" and "Asıl çıktı metni." in seen[1]
    assert result["delegations"][1]["status"] == "completed"


# ----------------------------------------------------------------------
# end to end: the worker really gets the inputs in its user message
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


def test_ares_chain_end_to_end(app, monkeypatch):
    # The test vault has ares, jarvis, gordon and oracle (conftest).
    monkeypatch.setattr(app, "OMNIROUTE_KEY", "test-key")
    chain = {"id": "call_g", "type": "function", "function": {"name": "delegate_to_workers", "arguments": json.dumps(
        {"delegations": [{"worker": "oracle", "task": "Konuyu araştır", "id": "a"},
                         {"worker": "jarvis", "task": "Araştırmadan yazı yaz", "id": "b", "depends_on": ["a"]}]})}}
    worker_messages, ares = [], []
    queue = [reply(tool_calls=[chain])]

    def handler(url, payload):
        if "tools" not in payload:
            task = payload["messages"][1]["content"]
            worker_messages.append(task)
            name = "oracle" if task.startswith("Konuyu") else "jarvis"
            return reply(json.dumps({"status": "done", "summary": f"{name} özeti", "findings": [],
                                     "artifacts": [], "open_questions": [], "deliverable": f"{name} metni"}))
        ares.append(payload)
        return queue.pop(0) if queue else reply("tamam")

    app.fake_client.handler = staticmethod(handler)
    asyncio.run(app.create_task_endpoint(app.TaskRequest(message="Araştır ve yaz")))

    assert worker_messages[0] == "Konuyu araştır"
    assert worker_messages[1].startswith("Araştırmadan yazı yaz\n\n" + dg.INPUT_HEADING)
    assert "oracle metni" in worker_messages[1]

    [tool_message] = [m for m in ares[1]["messages"] if m.get("role") == "tool"]
    sent = json.loads(tool_message["content"])["delegations"]
    assert "report" not in sent[0] and sent[0]["summary"] == "oracle özeti"
    assert sent[1]["report"]["deliverable"] == "jarvis metni"


def test_tool_schema_offers_id_and_depends_on(orch):
    tool = next(t["function"] for t in orch.TOOLS if t["function"]["name"] == "delegate_to_workers")
    properties = tool["parameters"]["properties"]["delegations"]["items"]["properties"]
    assert properties["depends_on"]["type"] == "array" and properties["id"]["type"] == "string"
    assert "depends_on" in tool["description"] and "needs another worker" not in tool["description"]


# ----------------------------------------------------------------------
# schema v4
# ----------------------------------------------------------------------

def test_v3_database_is_migrated_with_its_tasks(tmp_path, monkeypatch):
    path = tmp_path / "savas.db"
    with monkeypatch.context() as patch:
        patch.setattr(ts, "SCHEMA_VERSION", 3)
        patch.setattr(ts, "MIGRATIONS", ts.MIGRATIONS[:3])
        patch.setattr(ts, "TASK_FIELDS", tuple(f for f in ts.TASK_FIELDS if f not in ("depends_on", "step")))
        patch.setattr(ts, "JSON_TASK_FIELDS", ("result",))
        old = TaskStore(path)
        old.save_task({"task_id": "t1", "message": "eski", "status": "completed"})
        old.close()

    store = TaskStore(path)
    try:
        assert store.schema_version() == ts.SCHEMA_VERSION
        task = store.get_task("t1")
        assert task["message"] == "eski" and task["depends_on"] == [] and task["step"] is None
        store.save_task({"task_id": "t2", "status": "skipped", "depends_on": ["t1"]})
        assert store.get_task("t2")["depends_on"] == ["t1"]
        assert "skipped" in ts.FINAL_TASK_STATUSES
    finally:
        store.close()
