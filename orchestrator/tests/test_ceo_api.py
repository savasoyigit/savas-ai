"""K-2: read-only data for the CEO Interface and background tasks (docs/plan-k.md)."""

import asyncio
import sys
import types

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


@pytest.fixture
def app(db, orch, monkeypatch):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    yield orch
    orch.task_store.close()


WORKERS_YAML = """workers:

  ares:
    name: Ares
    role: orchestrator

  jarvis:
    name: JARVIS
    role: coding_engineering

  gordon:
    name: "Gordon"
    role: qa_review
"""


# ----------------------------------------------------------------------
# GET /workers
# ----------------------------------------------------------------------

def test_workers_runs_on_the_event_loop(app):
    # A plain def would run in a thread pool: SQLite refuses the store's connection there (seen live, K-3).
    assert asyncio.iscoroutinefunction(app.list_worker_profiles)


def test_profiles_come_from_workers_yaml(app):
    (app.worker_manager.workers_root / "workers.yaml").write_text(WORKERS_YAML)
    assert app.worker_profiles() == {
        "ares": {"name": "Ares", "role": "orchestrator"},
        "jarvis": {"name": "JARVIS", "role": "coding_engineering"},
        "gordon": {"name": "Gordon", "role": "qa_review"},
    }


def test_missing_workers_yaml_is_not_an_error(app):
    assert app.worker_profiles() == {}
    listed = asyncio.run(app.list_worker_profiles())
    assert {w["id"]: w["name"] for w in listed["workers"]} == {
        "ares": "ares", "gordon": "gordon", "jarvis": "jarvis", "oracle": "oracle"}
    assert all(w["role"] is None for w in listed["workers"])


def test_workers_show_model_budget_and_their_active_tasks(app):
    (app.worker_manager.workers_root / "workers.yaml").write_text(WORKERS_YAML)
    app.task_store.save_task({"task_id": "t-run", "parent_id": "top", "kind": "worker", "worker": "jarvis",
                              "message": "kodla", "status": "running", "created_at": "2026-11-16T00:00:00+00:00",
                              "step": None})
    app.task_store.save_task({"task_id": "t-done", "parent_id": "top", "kind": "worker", "worker": "jarvis",
                              "message": "bitti", "status": "completed", "created_at": "2026-11-16T00:00:00+00:00"})
    app.task_store.save_task({"task_id": "top", "kind": "task", "worker": "ares", "message": "iş",
                              "status": "waiting_approval", "created_at": "2026-11-16T00:00:00+00:00"})

    workers = {w["id"]: w for w in asyncio.run(app.list_worker_profiles())["workers"]}
    assert workers["jarvis"]["name"] == "JARVIS" and workers["jarvis"]["role"] == "coding_engineering"
    assert [t["task_id"] for t in workers["jarvis"]["tasks"]] == ["t-run"]
    assert workers["jarvis"]["tasks"][0]["status"] == "running"
    assert [t["status"] for t in workers["ares"]["tasks"]] == ["waiting_approval"]
    assert workers["gordon"]["tasks"] == []
    assert workers["jarvis"]["model"]
    assert workers["ares"]["daily_tokens"] == {"used": 0, "budget": 50000}


# ----------------------------------------------------------------------
# GET /usage/today
# ----------------------------------------------------------------------

def test_usage_counts_cloud_for_the_budget_and_local_apart(app, monkeypatch):
    records = [
        {"agent": "ares", "total_tokens": 1000, "location": "cloud"},
        {"agent": "ares", "total_tokens": 500, "location": "cloud"},
        {"agent": "jarvis", "total_tokens": 700, "location": "local"},
        {"total_tokens": 3, "location": "local"},
    ]
    monkeypatch.setattr(app.budget_manager, "daily_records", lambda: records)
    monkeypatch.setattr(app, "is_cloud", lambda r: r.get("location") == "cloud")

    usage = app.usage_today()
    assert usage["system"] == {"used": 1500, "budget": 200000, "remaining": 198500, "local_tokens": 703}
    assert usage["agents"]["ares"] == {"calls": 2, "cloud_tokens": 1500, "local_tokens": 0, "budget": 50000}
    assert usage["agents"]["jarvis"]["local_tokens"] == 700
    assert usage["agents"]["unknown"]["calls"] == 1
    assert usage["task_budget"] == 100000 and usage["request_budget"] == 50000
    assert len(usage["date"]) == 10


def test_usage_on_an_empty_day_is_zero_not_missing(app, monkeypatch):
    monkeypatch.setattr(app.budget_manager, "daily_records", lambda: [])
    usage = app.usage_today()
    assert usage["system"]["used"] == 0 and usage["agents"] == {}


# ----------------------------------------------------------------------
# POST /task background
# ----------------------------------------------------------------------

def submit(app, **fields):
    async def go():
        answer = await app.create_task_endpoint(app.TaskRequest(message="merhaba", **fields))
        await asyncio.gather(*list(app.BACKGROUND_JOBS))
        return answer

    return asyncio.run(go())


def test_background_answers_with_the_task_id_and_the_task_finishes(app):
    answer = submit(app, background=True)
    assert answer["status"] == "running" and answer["background"] is True
    task = app.TASKS[answer["task_id"]]
    assert task["status"] == "completed" and task["result"]["response"] == "tamam"
    assert app.task_store.get_task(answer["task_id"])["status"] == "completed"
    assert not app.BACKGROUND_JOBS


def test_without_background_the_answer_is_the_result(app):
    answer = submit(app)
    assert answer["status"] == "completed" and answer["response"] == "tamam"


def test_a_background_crash_fails_the_task(app, monkeypatch):
    async def broken(task_id):
        raise RuntimeError("beklenmedik")

    monkeypatch.setattr(app, "run_tracked", broken)
    answer = submit(app, background=True)
    task = app.TASKS[answer["task_id"]]
    assert task["status"] == "failed" and "beklenmedik" in task["error"]


def test_a_background_error_after_the_task_ended_is_logged(app, monkeypatch):
    async def ends_then_raises(task_id):
        app.complete_task(task_id, "failed", error="budget")
        raise app.HTTPException(status_code=429, detail="Daily budget exceeded")

    monkeypatch.setattr(app, "run_tracked", ends_then_raises)
    answer = submit(app, background=True)
    assert app.TASKS[answer["task_id"]]["error"] == "budget"
    [event] = [e for e in app.ACTIVITY if e.get("status") == "background_error"]
    assert "Daily budget" in event["error"]


# ----------------------------------------------------------------------
# /ui
# ----------------------------------------------------------------------

def test_ui_is_served_only_when_its_folder_exists(app, tmp_path, monkeypatch):
    mounted = []
    monkeypatch.setattr(app.app, "mount", lambda path, files, name: mounted.append((path, files, name)),
                        raising=False)
    monkeypatch.setitem(sys.modules, "fastapi.staticfiles", types.SimpleNamespace(
        StaticFiles=lambda directory, html: ("static", directory, html)))

    monkeypatch.setattr(app, "UI_DIR", str(tmp_path / "yok"))
    assert app.mount_ui() is False and mounted == []

    (tmp_path / "ui").mkdir()
    monkeypatch.setattr(app, "UI_DIR", str(tmp_path / "ui"))
    assert app.mount_ui() is True
    assert mounted == [("/ui", ("static", str(tmp_path / "ui"), True), "ui")]
