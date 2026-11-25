import asyncio
import json

import pytest

from conftest import FakeHTTPError, ok_handler
from model_router import CircuitBreaker, Selection

PRIMARY = "gh/gpt-4.1"
BACKUP = "gh/gpt-4o"
MESSAGES = [{"role": "user", "content": "selam"}]


def usage_records(orch):
    if not orch.usage_file.exists():
        return []
    return [json.loads(line) for line in orch.usage_file.read_text().splitlines()]


def called_models(orch):
    return [call["json"]["model"] for call in orch.fake_client.calls]


def events(orch, name):
    return [event for event in orch.ACTIVITY if event["event"] == name]


def respond(orch, outcomes):
    """outcomes: {model: status | (status, headers) | exception}; default 200."""
    def handler(url, payload):
        outcome = outcomes.get(payload["model"], 200)
        if isinstance(outcome, Exception):
            raise outcome
        status, headers = outcome if isinstance(outcome, tuple) else (outcome, {})
        if status == 200:
            return ok_handler(url, payload)
        return status, {"error": {"message": f"upstream {status}"}}, headers

    orch.fake_client.handler = staticmethod(handler)


def run_oracle(orch):
    # oracle/research has no capability requirement -> gpt-4.1 with gpt-4o as fallback
    return asyncio.run(orch.run_worker("oracle", "araştır", "parent-1"))


def run_ares(orch):
    return asyncio.run(orch.call_llm(orch.fake_client(), MESSAGES, "task-1", 1))


@pytest.fixture
def ares_with_fallback(orch, monkeypatch):
    # Fixed selection, so these tests do not depend on the repo registry.
    monkeypatch.setattr(orch, "route_model", lambda agent, task_type=None, **kwargs: Selection("m/a", "test", ("m/b",)))
    return orch


@pytest.fixture
def clock(orch, monkeypatch):
    class Clock:
        now = 1000.0

        def __call__(self):
            return self.now

    clock = Clock()
    monkeypatch.setattr(orch, "circuit", CircuitBreaker(clock=clock))
    return clock


# --- worker path ----------------------------------------------------------

@pytest.mark.parametrize("outcome", [466, 429, 500, 503, FakeHTTPError("timeout")])
def test_worker_falls_back_on_transient_failure(orch, outcome):
    respond(orch, {PRIMARY: outcome})
    result = run_oracle(orch)

    assert result["status"] == "completed"
    assert called_models(orch) == [PRIMARY, BACKUP]

    failed, ok = usage_records(orch)
    assert (failed["model"], failed["status"]) == (PRIMARY, "failed")
    assert (ok["model"], ok["status"]) == (BACKUP, "ok")
    assert ok["route_reason"].startswith("oracle/research:") and "fallback" in ok["route_reason"]

    switch = events(orch, "llm_fallback")
    assert [(e["from"], e["to"], e["status"]) for e in switch] == [
        (PRIMARY, BACKUP, "started"),
        (PRIMARY, BACKUP, "completed"),
    ]


def test_fallback_call_reserves_budget_again(orch, monkeypatch):
    reserved, released = [], []
    real_reserve, real_release = orch.budget_manager.reserve, orch.budget_manager.release

    async def reserve(**kwargs):
        # the first reservation is already closed when the fallback reserves
        assert orch.budget_manager._reserved == {}
        budget = await real_reserve(**kwargs)
        reserved.append(budget["reservation_id"])
        return budget

    def release(reservation_id):
        released.append(reservation_id)
        real_release(reservation_id)

    monkeypatch.setattr(orch.budget_manager, "reserve", reserve)
    monkeypatch.setattr(orch.budget_manager, "release", release)
    respond(orch, {PRIMARY: 500})
    run_oracle(orch)

    assert len(set(reserved)) == 2 and released == reserved
    assert orch.budget_manager._reserved == {}
    assert len([e for e in events(orch, "budget_check") if e["status"] == "allowed"]) == 2


@pytest.mark.parametrize("status", [400, 401])
def test_worker_does_not_fall_back_on_config_errors(orch, status):
    respond(orch, {PRIMARY: status})
    with pytest.raises(FakeHTTPError):
        run_oracle(orch)

    assert called_models(orch) == [PRIMARY]
    assert events(orch, "llm_fallback") == []
    assert [(e["model"], e["http_status"], e["kind"]) for e in events(orch, "model_alert")] == [
        (PRIMARY, status, "fatal")]
    assert orch.circuit.unavailable() == frozenset()
    assert orch.budget_manager._reserved == {}


def test_budget_block_does_not_fall_back(orch, monkeypatch):
    async def reserve(**kwargs):
        raise orch.BudgetExceeded("limit")

    monkeypatch.setattr(orch.budget_manager, "reserve", reserve)
    result = run_oracle(orch)

    assert result["status"] == "blocked"
    assert called_models(orch) == [] and events(orch, "llm_fallback") == []


def test_fallback_blocked_when_its_reservation_exceeds_budget(orch, monkeypatch):
    real_reserve, count = orch.budget_manager.reserve, []

    async def reserve(**kwargs):
        count.append(1)
        if len(count) == 2:
            raise orch.BudgetExceeded("limit")
        return await real_reserve(**kwargs)

    monkeypatch.setattr(orch.budget_manager, "reserve", reserve)
    respond(orch, {PRIMARY: 500})
    result = run_oracle(orch)

    assert result["status"] == "blocked" and "tekrar deneme" in result["message"]
    assert called_models(orch) == [PRIMARY]
    assert [e["status"] for e in events(orch, "llm_fallback")] == ["started", "blocked"]
    assert orch.budget_manager._reserved == {}


def test_at_most_one_fallback(orch):
    respond(orch, {PRIMARY: 500, BACKUP: 503})
    with pytest.raises(FakeHTTPError):
        run_oracle(orch)

    assert called_models(orch) == [PRIMARY, BACKUP]
    assert [r["status"] for r in usage_records(orch)] == ["failed", "failed"]
    assert [e["status"] for e in events(orch, "llm_fallback")] == ["started", "failed"]
    assert [e["status"] for e in events(orch, "worker_completed")] == ["failed"]
    assert orch.budget_manager._reserved == {}


def test_worker_without_fallback_model_fails_as_before(orch):
    # jarvis/coding: gpt-4o has no verified "coding" capability
    respond(orch, {PRIMARY: 500})
    with pytest.raises(FakeHTTPError):
        asyncio.run(orch.run_worker("jarvis", "x yaz", "parent-1"))
    assert called_models(orch) == [PRIMARY] and events(orch, "llm_fallback") == []


def test_worker_events_stay_paired_across_fallback(orch):
    respond(orch, {PRIMARY: 500})
    run_oracle(orch)
    assert len(events(orch, "worker_started")) == 1
    assert [e["status"] for e in events(orch, "worker_completed")] == ["completed"]


# --- alerts: errors that need the user, without stopping the work ---------

def test_403_falls_back_and_raises_an_alert(orch):
    respond(orch, {PRIMARY: 403})
    result = run_oracle(orch)

    assert result["status"] == "completed"
    assert called_models(orch) == [PRIMARY, BACKUP]
    assert [e["status"] for e in events(orch, "llm_fallback")] == ["started", "completed"]

    alerts = events(orch, "model_alert")
    assert [(e["status"], e["model"], e["http_status"], e["kind"]) for e in alerts] == [
        ("warning", PRIMARY, 403, "access")]
    assert alerts[0]["task_id"] == "parent-1"

    health = orch.health()["model_alerts"]
    assert health["count"] == 1
    assert health["recent"][-1]["model"] == PRIMARY and health["recent"][-1]["http_status"] == 403


def test_403_without_fallback_still_fails_with_alert(orch):
    # jarvis/coding has no fallback model
    respond(orch, {PRIMARY: 403})
    with pytest.raises(FakeHTTPError):
        asyncio.run(orch.run_worker("jarvis", "x yaz", "parent-1"))
    assert len(events(orch, "model_alert")) == 1


def test_repeated_403_disables_the_model(orch, clock):
    respond(orch, {PRIMARY: 403})
    run_oracle(orch)
    run_oracle(orch)
    assert orch.circuit.unavailable() == {PRIMARY}

    orch.fake_client.calls.clear()
    run_oracle(orch)
    assert called_models(orch) == [BACKUP]
    assert len(events(orch, "model_alert")) == 2


@pytest.mark.parametrize("outcome", [429, 466, 500, FakeHTTPError("timeout")])
def test_no_alert_for_limit_or_transient_failures(orch, outcome):
    respond(orch, {PRIMARY: outcome})
    run_oracle(orch)
    assert events(orch, "model_alert") == []
    assert orch.health()["model_alerts"] == {"count": 0, "recent": []}


def test_ares_403_falls_back_with_alert(ares_with_fallback):
    orch = ares_with_fallback
    respond(orch, {"m/a": 403})
    assert run_ares(orch)["model"] == "m/b"
    assert [(e["model"], e["task_id"]) for e in events(orch, "model_alert")] == [("m/a", "task-1")]


# --- Ares path ------------------------------------------------------------

def test_ares_falls_back_and_keeps_tools(ares_with_fallback):
    orch = ares_with_fallback
    respond(orch, {"m/a": 500})
    data = run_ares(orch)

    assert data["model"] == "m/b"
    assert called_models(orch) == ["m/a", "m/b"]
    assert orch.fake_client.calls[-1]["json"]["tools"] == orch.TOOLS
    assert [(r["model"], r["status"], r["agent"]) for r in usage_records(orch)] == [
        ("m/a", "failed", "ares"), ("m/b", "ok", "ares")]
    assert [e["status"] for e in events(orch, "llm_fallback")] == ["started", "completed"]
    assert orch.budget_manager._reserved == {}


def test_ares_does_not_fall_back_on_400(ares_with_fallback):
    orch = ares_with_fallback
    respond(orch, {"m/a": 400})
    with pytest.raises(FakeHTTPError):
        run_ares(orch)
    assert called_models(orch) == ["m/a"] and events(orch, "llm_fallback") == []


def test_ares_budget_block_does_not_fall_back(ares_with_fallback, monkeypatch):
    orch = ares_with_fallback

    async def reserve(**kwargs):
        raise orch.BudgetExceeded("limit")

    monkeypatch.setattr(orch.budget_manager, "reserve", reserve)
    with pytest.raises(orch.BudgetExceeded):
        run_ares(orch)
    assert called_models(orch) == [] and events(orch, "llm_fallback") == []


def test_ares_fallback_blocked_when_its_reservation_exceeds_budget(ares_with_fallback, monkeypatch):
    orch = ares_with_fallback
    real_reserve, count = orch.budget_manager.reserve, []

    async def reserve(**kwargs):
        count.append(1)
        if len(count) == 2:
            raise orch.BudgetExceeded("limit")
        return await real_reserve(**kwargs)

    monkeypatch.setattr(orch.budget_manager, "reserve", reserve)
    respond(orch, {"m/a": 500})
    with pytest.raises(orch.BudgetExceeded):
        run_ares(orch)
    assert called_models(orch) == ["m/a"]
    assert [e["status"] for e in events(orch, "llm_fallback")] == ["started", "blocked"]
    assert orch.budget_manager._reserved == {}


def test_ares_in_repo_registry_falls_back_to_gpt_4o(orch):
    respond(orch, {PRIMARY: 500})
    data = run_ares(orch)
    assert data["model"] == BACKUP and called_models(orch) == [PRIMARY, BACKUP]
    assert orch.fake_client.calls[-1]["json"]["tools"] == orch.TOOLS


# --- health (circuit breaker) --------------------------------------------

def test_two_failures_disable_model_for_ten_minutes(orch, clock):
    respond(orch, {PRIMARY: 500})
    run_oracle(orch)
    assert events(orch, "model_circuit") == []
    run_oracle(orch)

    opened = events(orch, "model_circuit")
    assert [(e["status"], e["model"], e["kind"]) for e in opened] == [("open", PRIMARY, "transient")]
    assert opened[0]["task_id"] == "parent-1" and opened[0]["seconds"] == 600
    assert orch.health()["unavailable_models"] == [PRIMARY]

    # third call: the disabled model is not tried at all
    orch.fake_client.calls.clear()
    result = run_oracle(orch)
    assert result["status"] == "completed" and called_models(orch) == [BACKUP]
    assert "fallback" not in usage_records(orch)[-1]["route_reason"]

    # after ten minutes it is used again
    clock.now += 600
    respond(orch, {})
    orch.fake_client.calls.clear()
    run_oracle(orch)
    assert called_models(orch) == [PRIMARY]
    assert [e["status"] for e in events(orch, "model_circuit")] == ["open", "closed"]
    assert orch.health()["unavailable_models"] == []


def test_success_between_failures_keeps_model_enabled(orch, clock):
    respond(orch, {PRIMARY: 500})
    run_oracle(orch)
    respond(orch, {})
    run_oracle(orch)
    respond(orch, {PRIMARY: 500})
    run_oracle(orch)
    assert events(orch, "model_circuit") == []


def test_limit_error_disables_model_at_once_for_retry_after(orch, clock):
    respond(orch, {PRIMARY: (429, {"Retry-After": "30"})})
    assert run_oracle(orch)["status"] == "completed"

    opened = events(orch, "model_circuit")
    assert [(e["status"], e["kind"], e["seconds"], e["http_status"]) for e in opened] == [("open", "limit", 30, 429)]
    # only the failing model is disabled, not the whole provider
    assert orch.circuit.unavailable() == {PRIMARY}
    assert orch.route_model("oracle").model == BACKUP

    clock.now += 30
    assert orch.route_model("oracle").model == PRIMARY


def test_limit_error_without_retry_after_uses_ten_minutes(orch, clock):
    respond(orch, {PRIMARY: 429})
    run_oracle(orch)
    assert events(orch, "model_circuit")[0]["seconds"] == 600


def test_ares_uses_backup_while_its_model_is_disabled(orch, clock):
    respond(orch, {PRIMARY: 429})
    run_oracle(orch)
    assert orch.route_model("ares").model == BACKUP


def test_coding_uses_last_resort_while_its_model_is_disabled(orch, clock):
    respond(orch, {PRIMARY: 429})
    run_oracle(orch)
    selection = orch.route_model("jarvis")
    assert selection.model == orch.MODEL and "env MODEL" in selection.reason


def test_fallback_model_failures_count_too(orch, clock):
    respond(orch, {PRIMARY: 500, BACKUP: 429})
    with pytest.raises(FakeHTTPError):
        run_oracle(orch)
    assert orch.circuit.unavailable() == {BACKUP}
