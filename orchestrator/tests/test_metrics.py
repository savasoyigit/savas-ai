"""L-1a: metrics report (docs/plan-l.md). No LLM calls."""

import asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import metrics

TZ = ZoneInfo("Europe/Istanbul")
TODAY = date(2026, 11, 18)


def call(task_id, agent, model, tokens, when="2026-11-18T09:00:00+00:00", status="ok", location="cloud", ms=1000):
    return {"task_id": task_id, "agent": agent, "model": model, "total_tokens": tokens, "timestamp": when,
            "status": status, "location": location, "duration_ms": ms}


def rows(tasks=(), events=(), approvals=()):
    return {"tasks": list(tasks), "events": list(events), "approvals": list(approvals)}


TASKS = [
    {"task_id": "t1", "parent_id": None, "kind": "task", "status": "completed", "message": "dedupe yaz"},
    {"task_id": "w1", "parent_id": "t1", "kind": "worker", "status": "completed", "message": "kod"},
    {"task_id": "t2", "parent_id": None, "kind": "task", "status": "needs_review", "message": "valentine"},
    {"task_id": "p1", "parent_id": None, "kind": "package", "status": "completed", "message": "cv"},
]


def test_empty_data_gives_an_empty_report():
    report = metrics.report(7, [], rows(), TODAY, TZ)
    assert report["tokens"]["calls"] == 0 and len(report["tokens"]["by_day"]) == 7
    assert report["from"] == "2026-11-12" and report["to"] == "2026-11-18"
    assert report["quality"]["models"] == [] and report["errors"]["tasks_by_kind"] == {}


def test_window_cloud_local_and_days():
    usage = [
        call("t1", "ares", "gh/gpt-4.1", 1000),
        call("t1", "jarvis", "gpt-oss", 500, location="local"),
        # 21:30 UTC on the 17th is already the 18th in Istanbul.
        call("t1", "ares", "gh/gpt-4.1", 7, when="2026-11-17T21:30:00+00:00"),
        call("t1", "ares", "gh/gpt-4.1", 99, when="2026-11-17T20:30:00+00:00"),
        # Outside a 1-day window, and a record without a time.
        call("t1", "ares", "gh/gpt-4.1", 5000, when="2026-11-10T09:00:00+00:00"),
        {"total_tokens": 3},
    ]
    one = metrics.report(1, usage, rows(TASKS), TODAY, TZ)["tokens"]
    assert (one["cloud"], one["local"], one["calls"]) == (1007, 500, 3)
    seven = metrics.report(7, usage, rows(TASKS), TODAY, TZ)["tokens"]
    assert seven["by_day"][-2] == {"day": "2026-11-17", "cloud": 99, "local": 0}
    assert seven["by_day"][-1] == {"day": "2026-11-18", "cloud": 1007, "local": 500}
    assert seven["by_agent_model"][0] == {"agent": "ares", "model": "gh/gpt-4.1", "location": "cloud",
                                          "calls": 2 + 1, "tokens": 1106}


def test_sub_tasks_add_up_to_their_top_level_task():
    usage = [call("t1", "ares", "m", 100), call("w1", "jarvis", "m", 400), call("t2", "ares", "m", 50),
             call("p1", "willy", "m", 300), call("gone", "ares", "m", 9)]
    tokens = metrics.report(1, usage, rows(TASKS), TODAY, TZ)["tokens"]
    assert [(t["task_id"], t["tokens"]) for t in tokens["top_tasks"]] == [("t1", 500), ("p1", 300), ("t2", 50)]
    assert tokens["per_kind"]["task"] == {"tasks": 2, "average": 275, "median": 275}
    assert tokens["unlinked_calls"] == 1


def test_errors_by_model_and_task_kind():
    usage = [call("t1", "ares", "a", 1), call("t1", "ares", "a", 1, status="failed"), call("t1", "ares", "b", 1)]
    events = [{"task_id": "t1", "event": "llm_fallback", "status": "started"},
              {"task_id": "t1", "event": "budget_check", "status": "blocked"},
              {"task_id": "t1", "event": "budget_check", "status": "allowed"},
              {"task_id": "t1", "event": "context_budget", "status": "exceeded"}]
    errors = metrics.report(1, usage, rows(TASKS, events), TODAY, TZ)["errors"]
    assert errors["calls_by_model"]["a"] == {"calls": 2, "failed": 1, "failed_percent": 50.0}
    assert errors["tasks_by_kind"]["task"]["total"] == 2 and errors["tasks_by_kind"]["task"]["needs_review"] == 1
    assert "worker" not in errors["tasks_by_kind"]
    assert (errors["fallbacks"], errors["budget_refusals"], errors["context_over_budget"]) == (1, 1, 1)


def test_quality_signals_go_to_the_model_that_did_the_work():
    usage = [call("t1", "ares", "ares-model", 1), call("t1", "jarvis", "coder", 1), call("t1", "jarvis", "coder", 1),
             call("t1", "gordon", "judge", 1), call("t2", "jarvis", "other", 1)]
    events = [
        {"task_id": "t1", "event": "review", "status": "done", "worker": "jarvis", "verdict": "approve"},
        {"task_id": "t1", "event": "review", "status": "needs_review", "worker": "jarvis", "verdict": "revise"},
        {"task_id": "t1", "event": "code_edit", "status": "applied", "worker": "jarvis"},
        {"task_id": "t1", "event": "code_result", "status": "ok"},
        {"task_id": "t1", "event": "claim_check", "status": "unsupported"},
        {"task_id": "x", "event": "review", "status": "done", "worker": "willy", "verdict": "approve"},
        {"task_id": "package-j", "event": "jobs", "status": "package_started"},
        {"task_id": "package-j", "event": "jobs", "status": "package_needs_fix"},
        {"task_id": "package-k", "event": "jobs", "status": "package_ready"},
    ]
    approvals = [{"status": "executed"}, {"status": "rejected"}, {"status": "pending"}]
    quality = metrics.report(1, usage, rows(TASKS, events, approvals), TODAY, TZ)["quality"]
    coder = next(m for m in quality["models"] if m["model"] == "coder")
    assert coder["reviews"] == {"n": 2, "few": True, "approve_percent": 50.0}
    assert coder["code"] == {"n": 1, "few": True, "ok_percent": 100.0}
    assert next(m for m in quality["models"] if m["model"] == "ares-model")["unsupported_claims"] == 1
    assert quality["unlinked_signals"] == 1
    assert quality["packages"]["needs_fix_percent"] == 50.0
    assert quality["approvals"]["n"] == 2 and quality["approvals"]["rejected_percent"] == 50.0


def test_few_samples_are_marked():
    usage = [call("t1", "ares", "m", 1)] * metrics.FEW_SAMPLES
    model = metrics.report(1, usage, rows(TASKS), TODAY, TZ)["quality"]["models"][0]
    assert model["sample"] == {"n": 10, "few": False} and model["reviews"]["few"] is True


# ----------------------------------------------------------------------
# GET /metrics through the store
# ----------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


@pytest.fixture
def app(db, orch):
    yield orch
    if orch.task_store:
        orch.task_store.close()


def test_endpoint_reads_the_store_and_usage(app, monkeypatch):
    task_id = app.create_task("metrik dene", "ares")
    app.add_activity(task_id=task_id, agent="ares", event="claim_check", status="unsupported", claims=["x"])
    app.add_activity(task_id=task_id, agent="ares", event="memory", status="retrieved")
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(app.token_manager, "read_usage", lambda: [call(task_id, "ares", "gh/gpt-4.1", 42, when=now)])

    report = asyncio.run(app.metrics_report(days=1))
    assert report["tokens"]["cloud"] == 42 and report["tokens"]["top_tasks"][0]["message"] == "metrik dene"
    assert report["quality"]["models"][0]["unsupported_claims"] == 1
    with pytest.raises(app.HTTPException):
        asyncio.run(app.metrics_report(days=3))
