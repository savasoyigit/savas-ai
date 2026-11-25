"""C-8.2: local model routing, budget switch, cloud-only daily totals."""

import asyncio
import copy
import json

import pytest

from budget_manager import BudgetExceeded
from conftest import FakeResponse, ok_handler
from model_router import (
    DEFAULT_REGISTRY_PATH,
    RegistryError,
    Selection,
    load_registry,
    parse_registry,
    select,
)

REPO = load_registry(DEFAULT_REGISTRY_PATH)
PRIMARY = "gh/gpt-4.1"
BACKUP = "gh/gpt-4o"
LOCAL = "gpt-oss-20b-Q4_K_M"
LOCAL_URL = "http://llama:8080"
MESSAGES = [{"role": "user", "content": "selam"}]


def model(id, caps=(), fallback=None, location="cloud", context=None):
    return {"id": id, "provider": "p", "location": location, "capabilities": list(caps),
            "enabled": True, "max_output": 100, "context": context,
            "fallback": fallback, "verified": "2026-11-14", "notes": ""}


BASE = {
    "models": [
        model("m/tool", ["tool_calling"], fallback="m/tool2"),
        model("m/tool2", ["tool_calling"]),
        model("m/plain"),
        model("l/loc", ["tool_calling"], location="local", context=8000),
    ],
    "task_types": {
        "orchestrate": {"requires": ["tool_calling"], "prefer": ["m/tool"]},
        "general": {"requires": [], "prefer": ["l/loc", "m/plain"]},
    },
    "workers": {
        "ares": {"default": "orchestrate", "allowed": ["orchestrate"]},
        "w": {"default": "general", "allowed": ["general"]},
    },
    "default_model": "m/plain",
}


def reg(mutate=None):
    data = copy.deepcopy(BASE)
    if mutate:
        mutate(data)
    return parse_registry(data)


def no_tools_on_local(data):
    data["models"][3]["capabilities"] = []


# --- select(): local is the last candidate --------------------------------

def test_local_is_appended_after_the_cloud_fallback():
    sel = select("ares", registry=reg())
    assert (sel.model, sel.fallbacks, sel.local) == ("m/tool", ("m/tool2", "l/loc"), {"l/loc"})


def test_cloud_wins_even_when_local_is_preferred():
    # general.prefer lists l/loc first
    sel = select("w", registry=reg())
    assert (sel.model, sel.fallbacks) == ("m/plain", ("l/loc",))


def test_local_is_chosen_when_no_cloud_candidate_is_left():
    sel = select("ares", registry=reg(), unavailable={"m/tool", "m/tool2"})
    assert (sel.model, sel.fallbacks, sel.local) == ("l/loc", (), {"l/loc"})
    assert "local" in sel.reason


def test_local_is_ignored_when_not_allowed():
    sel = select("ares", registry=reg(), allow_local=False)
    assert (sel.model, sel.fallbacks, sel.local) == ("m/tool", ("m/tool2",), frozenset())
    down = select("ares", registry=reg(), allow_local=False, unavailable={"m/tool", "m/tool2"}, env_model="env")
    assert down.model == "env"


def test_ares_never_gets_a_local_model_without_tool_calling():
    r = reg(no_tools_on_local)
    assert select("ares", registry=r).fallbacks == ("m/tool2",)
    assert select("ares", registry=r, cloud_budget_percent=150).model == "m/tool"
    assert select("ares", registry=r, unavailable={"m/tool", "m/tool2"}, env_model="env").model == "env"
    # workers without the requirement still get it
    assert select("w", registry=r).fallbacks == ("l/loc",)


# --- select(): context rule ----------------------------------------------

@pytest.mark.parametrize("needed, fallbacks", [
    (None, ("m/tool2", "l/loc")),
    (8000, ("m/tool2", "l/loc")),
    (8001, ("m/tool2",)),
])
def test_local_is_skipped_when_the_request_exceeds_its_context(needed, fallbacks):
    assert select("ares", registry=reg(), needed_tokens=needed).fallbacks == fallbacks


def test_budget_switch_respects_the_context_rule():
    sel = select("ares", registry=reg(), needed_tokens=9000, cloud_budget_percent=150)
    assert (sel.model, sel.local) == ("m/tool", frozenset())


# --- select(): budget switch ---------------------------------------------

@pytest.mark.parametrize("percent, switched", [(None, False), (99.0, False), (100.0, False), (100.5, True), (250, True)])
def test_budget_switch_at_default_threshold(percent, switched):
    sel = select("ares", registry=reg(), cloud_budget_percent=percent)
    if switched:
        assert (sel.model, sel.fallbacks, sel.local) == ("l/loc", (), {"l/loc"})
        assert sel.reason.startswith("ares/orchestrate: budget: local switch (")
    else:
        assert sel.model == "m/tool" and "budget" not in sel.reason


def test_budget_threshold_comes_from_the_registry():
    r = reg(lambda d: d.update(local_switch_percent=80))
    assert select("w", registry=r, cloud_budget_percent=80).model == "m/plain"
    sel = select("w", registry=r, cloud_budget_percent=81)
    assert (sel.model, sel.reason) == ("l/loc", "w/general: budget: local switch (81%)")


def test_budget_switch_without_local_keeps_the_cloud_selection():
    sel = select("ares", registry=reg(), cloud_budget_percent=150, allow_local=False)
    assert (sel.model, sel.fallbacks) == ("m/tool", ("m/tool2",))


# --- registry -------------------------------------------------------------

def test_repo_registry_has_the_measured_local_model():
    entry = REPO.model(LOCAL)
    assert (entry.location, entry.provider, entry.enabled) == ("local", "llama.cpp", True)
    assert entry.capabilities == {"tool_calling", "coding"}
    assert entry.context == 8192 and entry.verified is not None
    assert REPO.local_switch_percent == 100


def test_local_switch_percent_defaults_to_100():
    assert reg().local_switch_percent == 100


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["models"][3].update(context=None), "context"),
    (lambda d: d.update(local_switch_percent=0), "local_switch_percent"),
    (lambda d: d.update(local_switch_percent=101), "local_switch_percent"),
    (lambda d: d.update(local_switch_percent="80"), "local_switch_percent"),
])
def test_invalid_local_settings_are_rejected(mutate, message):
    with pytest.raises(RegistryError, match=message):
        reg(mutate)


# --- budget: daily totals count cloud only -------------------------------

def record(token_manager, tokens, **kwargs):
    return token_manager.record_usage(
        task_id=kwargs.pop("task_id", "t1"), agent="gandalf", model="x",
        usage={"prompt_tokens": tokens, "completion_tokens": 0}, **kwargs)


def test_usage_record_carries_location(managers):
    token_manager, _ = managers
    assert record(token_manager, 10)["location"] == "cloud"
    assert record(token_manager, 10, location="local")["location"] == "local"


def test_daily_totals_ignore_local_usage(managers):
    token_manager, budget_manager = managers
    record(token_manager, 1000)
    record(token_manager, 4000, location="local")
    # a record written before C-8.2 has no location: it is cloud
    with token_manager.usage_file.open("a") as f:
        old = {"timestamp": json.loads(token_manager.usage_file.read_text().splitlines()[0])["timestamp"],
               "task_id": "t1", "agent": "gandalf", "total_tokens": 500}
        f.write(json.dumps(old) + "\n")

    assert budget_manager.agent_used_today("gandalf") == 1500
    assert budget_manager.total_used_today() == 1500
    assert budget_manager.task_used("t1") == 5500


def test_local_reservation_ignores_daily_budget_but_not_task_and_request(managers):
    token_manager, budget_manager = managers  # worker daily 5000, task 10000, request 8000
    record(token_manager, 5000)

    async def scenario():
        with pytest.raises(BudgetExceeded):
            await budget_manager.reserve(agent="gandalf", task_id="t2", requested_tokens=100)
        held = await budget_manager.reserve(agent="gandalf", task_id="t2", requested_tokens=3000, location="local")
        assert set(held["checks"]) == {"task", "request"}
        # a local reservation in flight is not daily cloud usage
        assert budget_manager.check(agent="ares", task_id="t3", requested_tokens=1)["checks"]["system_daily"]["used"] == 5000
        with pytest.raises(BudgetExceeded, match="request"):
            await budget_manager.reserve(agent="gandalf", task_id="t2", requested_tokens=8001, location="local")
        with pytest.raises(BudgetExceeded, match="task"):
            # 3000 held + 7500 > task budget 10000
            await budget_manager.reserve(agent="gandalf", task_id="t2", requested_tokens=7500, location="local")
        budget_manager.release(held["reservation_id"])

    asyncio.run(scenario())


def test_cloud_percent_is_the_projected_daily_usage(managers):
    token_manager, budget_manager = managers  # worker daily 5000, system 50000
    assert budget_manager.cloud_percent(agent="gandalf") == 0
    record(token_manager, 2000)
    record(token_manager, 9000, location="local")
    assert budget_manager.cloud_percent(agent="gandalf") == 40
    assert budget_manager.cloud_percent(agent="gandalf", requested_tokens=3000) == 100
    assert budget_manager.cloud_percent(agent="gandalf", requested_tokens=3500) == 110

    async def with_reservation():
        held = await budget_manager.reserve(agent="gandalf", task_id="t9", requested_tokens=1000)
        assert budget_manager.cloud_percent(agent="gandalf") == 60
        budget_manager.release(held["reservation_id"])

    asyncio.run(with_reservation())
    # the system budget can be the tighter one
    budget_manager.system_daily_budget = 2500
    assert budget_manager.cloud_percent(agent="gandalf") == 80


# --- main.py: call path ---------------------------------------------------

def usage_records(orch):
    if not orch.usage_file.exists():
        return []
    return [json.loads(line) for line in orch.usage_file.read_text().splitlines()]


def calls(orch):
    return [(call["url"], call["json"]["model"]) for call in orch.fake_client.calls]


def respond(orch, failing):
    def handler(url, payload):
        if payload["model"] in failing:
            return 500, {"error": {"message": "upstream 500"}}
        return ok_handler(url, payload)

    orch.fake_client.handler = staticmethod(handler)


def run_ares(orch):
    return asyncio.run(orch.call_llm(orch.fake_client(), MESSAGES, "task-1", 1))


def run_worker(orch, worker="oracle"):
    return asyncio.run(orch.run_worker(worker, "bir iş", "parent-1"))


@pytest.fixture
def local(orch, monkeypatch):
    monkeypatch.setattr(orch, "LOCAL_LLM_URL", LOCAL_URL)
    return orch


def test_local_stays_out_while_url_is_not_configured(orch):
    assert orch.LOCAL_LLM_URL == ""
    sel = orch.route_model("jarvis", requested_tokens=3000)
    assert (sel.model, sel.fallbacks, sel.local) == (PRIMARY, (), frozenset())


def test_budget_exhausted_without_local_is_still_blocked(orch):
    orch.budget_manager.ares_daily_budget = 100
    orch.budget_manager.worker_daily_budget = 100
    with pytest.raises(BudgetExceeded):
        run_ares(orch)
    assert run_worker(orch)["status"] == "blocked"
    assert orch.fake_client.calls == []


def test_worker_chain_is_primary_cloud_backup_then_local(local):
    respond(local, {PRIMARY, BACKUP})
    result = run_worker(local)

    assert result["status"] == "completed"
    cloud_url = f"{local.OMNIROUTE_URL}/chat/completions"
    assert calls(local) == [(cloud_url, PRIMARY), (cloud_url, BACKUP),
                            (f"{LOCAL_URL}/v1/chat/completions", LOCAL)]
    last = local.fake_client.calls[-1]
    assert last["timeout"] == local.LOCAL_LLM_TIMEOUT >= 600
    assert "Authorization" not in last["headers"]
    assert local.fake_client.calls[0]["timeout"] is None

    assert [(r["model"], r["status"], r["location"]) for r in usage_records(local)] == [
        (PRIMARY, "failed", "cloud"), (BACKUP, "failed", "cloud"), (LOCAL, "ok", "local")]
    switch = [(e["from"], e["to"], e["status"]) for e in local.ACTIVITY if e["event"] == "llm_fallback"]
    assert switch == [(PRIMARY, BACKUP, "started"), (PRIMARY, BACKUP, "failed"),
                      (BACKUP, LOCAL, "started"), (BACKUP, LOCAL, "completed")]


def test_ares_chain_reaches_local_with_tools(local):
    respond(local, {PRIMARY, BACKUP})
    data = run_ares(local)

    assert data["model"] == LOCAL
    assert [model for _, model in calls(local)] == [PRIMARY, BACKUP, LOCAL]
    assert local.fake_client.calls[-1]["json"]["tools"] == local.TOOLS
    assert usage_records(local)[-1]["location"] == "local"


def test_all_three_attempts_fail(local):
    respond(local, {PRIMARY, BACKUP, LOCAL})
    with pytest.raises(Exception, match="HTTP 500"):
        run_ares(local)
    assert [model for _, model in calls(local)] == [PRIMARY, BACKUP, LOCAL]
    assert local.budget_manager._reserved == {}


def test_coding_task_has_a_local_backup(local):
    # gh/gpt-4o has no "coding" label: the chain is gpt-4.1 -> local
    respond(local, {PRIMARY})
    result = run_worker(local, "jarvis")
    assert result["status"] == "completed"
    assert [model for _, model in calls(local)] == [PRIMARY, LOCAL]


def test_exhausted_cloud_budget_moves_the_call_to_local(local):
    local.budget_manager.ares_daily_budget = 100
    local.budget_manager.worker_daily_budget = 100

    run_ares(local)
    result = run_worker(local)

    assert result["status"] == "completed"
    assert calls(local) == [(f"{LOCAL_URL}/v1/chat/completions", LOCAL)] * 2
    records = usage_records(local)
    assert [r["location"] for r in records] == ["local", "local"]
    assert all("budget: local switch (" in r["route_reason"] for r in records)
    # local usage is not charged to the daily cloud budget
    assert local.budget_manager.total_used_today() == 0
    assert local.budget_manager.agent_used_today("ares") == 0
    assert local.budget_manager.task_used("task-1") == records[0]["total_tokens"] > 0


def test_request_over_local_context_is_blocked_not_truncated(local):
    local.budget_manager.ares_daily_budget = 100
    big = [{"role": "user", "content": "x" * 4 * 9000}]
    with pytest.raises(BudgetExceeded):
        asyncio.run(local.call_llm(local.fake_client(), big, "task-1", 1))
    assert local.fake_client.calls == []


def test_local_calls_run_one_at_a_time(local, monkeypatch):
    state = {"active": 0, "peak": 0}

    async def post(self, url, headers=None, json=None, **kwargs):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        return FakeResponse(*ok_handler(url, json))

    monkeypatch.setattr(local.fake_client, "post", post)

    async def three_workers():
        return await asyncio.gather(*(local.run_worker(w, "iş", "parent-1") for w in ("oracle", "jarvis", "gordon")))

    # cloud calls overlap (the fake client can show concurrency) ...
    assert all(r["status"] == "completed" for r in asyncio.run(three_workers()))
    assert state["peak"] == 3

    # ... local calls do not
    state["peak"] = 0
    monkeypatch.setattr(local, "route_model",
                        lambda agent, task_type=None, **kwargs: Selection(LOCAL, "test", (), frozenset({LOCAL})))
    assert all(r["status"] == "completed" for r in asyncio.run(three_workers()))
    assert state["peak"] == 1


# --- /health --------------------------------------------------------------

def test_health_without_local(orch):
    assert orch.health()["local_llm"] == {
        "configured": False, "reachable": None, "models": {}, "agents_on_local": []}


def test_health_reports_unreachable_local(local):
    status = local.health()["local_llm"]
    assert (status["configured"], status["reachable"], status["agents_on_local"]) == (True, False, [])
    assert "connection refused" in status["error"]


def test_health_lists_agents_routed_to_local(local):
    seen = []

    def get_handler(url):
        seen.append(url)
        return 200, {"data": [{"id": LOCAL, "status": {"value": "unloaded"}}]}

    local.fake_client.get_handler = staticmethod(get_handler)
    local.budget_manager.worker_daily_budget = 100

    status = local.health()["local_llm"]
    assert seen == [f"{LOCAL_URL}/models"]
    assert (status["reachable"], status["models"]) == (True, {LOCAL: "unloaded"})
    assert status["agents_on_local"] == ["gordon", "jarvis", "oracle"]
    assert local.health()["routes"]["ares"] == PRIMARY
