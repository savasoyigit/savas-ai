import asyncio
import json

from model_router import DEFAULT_REGISTRY_PATH, Selection, load_registry

REPO = load_registry(DEFAULT_REGISTRY_PATH)


def usage_records(orch):
    return [json.loads(line) for line in orch.usage_file.read_text().splitlines()]


def delegate_tools(orch):
    return {t["function"]["name"]: t["function"] for t in orch.TOOLS
            if t["function"]["name"].startswith("delegate_to_worker")}


def test_worker_call_uses_router_not_env_model(orch):
    result = asyncio.run(orch.run_worker("jarvis", "x yaz", "parent-1"))
    assert result["status"] == "completed"
    sent = orch.fake_client.calls[-1]["json"]
    assert sent["model"] == "gh/gpt-4.1" != orch.MODEL
    record = usage_records(orch)[-1]
    assert record["model"] == "gh/gpt-4.1"
    assert record["route_reason"].startswith("jarvis/coding:")


def test_worker_call_follows_selection(orch, monkeypatch):
    monkeypatch.setattr(orch, "route_model", lambda agent, task_type=None, **kwargs: Selection("x/y", "test-reason", ()))
    asyncio.run(orch.run_worker("jarvis", "x yaz", "parent-1"))
    assert orch.fake_client.calls[-1]["json"]["model"] == "x/y"
    record = usage_records(orch)[-1]
    assert (record["model"], record["route_reason"]) == ("x/y", "test-reason")


def test_ares_call_uses_router(orch):
    client = orch.fake_client()
    asyncio.run(orch.call_llm(client, [{"role": "user", "content": "selam"}], "task-1", 1))
    sent = orch.fake_client.calls[-1]["json"]
    assert sent["model"] == "gh/gpt-4.1" and sent["tools"] == orch.TOOLS
    record = usage_records(orch)[-1]
    assert record["agent"] == "ares" and record["route_reason"].startswith("ares/orchestrate:")


def test_delegate_passes_task_type(orch):
    args = {"worker": "jarvis", "task": "kodu incele", "task_type": "review"}
    asyncio.run(orch.execute_worker_tool("delegate_to_worker", args, "parent-1"))
    assert usage_records(orch)[-1]["route_reason"].startswith("jarvis/review:")


def test_delegate_without_task_type_uses_worker_default(orch):
    args = {"worker": "gordon", "task": "kontrol et"}
    asyncio.run(orch.execute_worker_tool("delegate_to_worker", args, "parent-1"))
    assert usage_records(orch)[-1]["route_reason"].startswith("gordon/review:")


def test_delegate_with_disallowed_task_type_falls_back(orch):
    args = {"worker": "oracle", "task": "yaz", "task_type": "coding"}
    result = asyncio.run(orch.execute_worker_tool("delegate_to_worker", args, "parent-1"))
    assert result["status"] == "completed"
    reason = usage_records(orch)[-1]["route_reason"]
    assert reason.startswith("oracle/research:") and "izinli değil" in reason


def test_parallel_delegations_pass_task_type(orch):
    args = {"delegations": [
        {"worker": "jarvis", "task": "a", "task_type": "general"},
        {"worker": "gordon", "task": "b", "task_type": "coding"},
    ]}
    asyncio.run(orch.execute_worker_tool("delegate_to_workers", args, "parent-1"))
    reasons = sorted(r["route_reason"].split(":")[0] for r in usage_records(orch))
    assert reasons == ["gordon/coding", "jarvis/general"]


def test_failed_call_records_route_reason(orch):
    orch.fake_client.handler = staticmethod(lambda url, payload: (500, {"error": "boom"}))
    try:
        asyncio.run(orch.run_worker("jarvis", "x", "parent-1"))
    except Exception:
        pass
    record = usage_records(orch)[-1]
    assert record["status"] == "failed" and record["model"] == "gh/gpt-4.1"
    assert record["route_reason"].startswith("jarvis/coding:")


def test_delegate_schema_has_optional_task_type(orch):
    expected = sorted(set(REPO.task_types) - {"orchestrate"})
    tools = delegate_tools(orch)
    single = tools["delegate_to_worker"]["parameters"]
    item = tools["delegate_to_workers"]["parameters"]["properties"]["delegations"]["items"]
    for schema in (single, item):
        assert sorted(schema["properties"]["task_type"]["enum"]) == expected
        assert "task_type" not in schema["required"]


def test_health_reports_routes(orch):
    health = orch.health()
    assert health["routes"]["ares"] == "gh/gpt-4.1"
    assert health["routes"]["jarvis"] == "gh/gpt-4.1"
    assert health["model"] == orch.MODEL


def test_usage_record_route_reason_defaults_to_none(managers):
    token_manager, _ = managers
    record = token_manager.record_usage(task_id="t", agent="ares", model="m", usage={})
    assert record["route_reason"] is None
