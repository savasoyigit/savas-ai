"""G-3 review chain; J-6: the reviewer looks again after each fix, unapproved output needs review."""

import asyncio
import json

import pytest

import context_engine as ce
import delegation as dg


# ----------------------------------------------------------------------
# verdict and helpers
# ----------------------------------------------------------------------

@pytest.mark.parametrize("given,kept", [
    ("approve", "approve"), ("Approved", "approve"), ("revise", "revise"),
    ("changes_requested", "revise"), ("REVISION", "revise"), ("belki", None), (None, None),
])
def test_verdict_is_kept_only_when_known(given, kept):
    report = ce.normalize_report({"status": "ok", "summary": "s", "verdict": given})
    assert report.get("verdict") == kept


def test_review_and_revision_tasks():
    assert dg.review_task("Kod yaz").endswith("\nKod yaz")
    revision = dg.revision_task("Kod yaz")
    assert revision.startswith("Kod yaz\n\nDüzeltme turu") and "draft" in revision and "review" in revision


def test_review_summary():
    done = dg.review_summary({"status": "completed", "worker": "gordon",
                              "report": {"summary": "eksik test", "findings": ["test ekle"]}}, "r-1", "revise")
    assert done == {"worker": "gordon", "status": "completed", "task_id": "r-1", "verdict": "revise",
                    "summary": "eksik test", "findings": ["test ekle"]}
    failed = dg.review_summary({"status": "failed", "worker": "gordon", "error": "down"}, "r-1", None)
    assert failed["error"] == "down" and "summary" not in failed


# ----------------------------------------------------------------------
# chain with a stand-in worker
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
    """calls: every worker call. setup: verdict of the reviewer, workers that fail, call numbers that fail."""
    calls = []
    setup = {"verdict": "approve", "fail_call": set()}

    async def fake(worker_id, task, parent_task_id, task_type=None, report_chars=None, worker_task_id=None,
                   inputs="", instructions=""):
        calls.append({"worker": worker_id, "task": task, "task_id": worker_task_id, "inputs": inputs,
                      "instructions": instructions, "report_chars": report_chars, "task_type": task_type})
        if len(calls) in setup["fail_call"]:
            return {"status": "failed", "worker": worker_id, "error": "model down"}
        report = {"status": "ok", "summary": f"{worker_id} özeti {len(calls)}",
                  "deliverable": f"{worker_id} çıktısı {len(calls)}"}
        if instructions:
            report = {"status": "ok", "summary": "inceleme", "findings": ["hata yönetimi eksik"]}
            verdict = setup["verdicts"].pop(0) if setup.get("verdicts") else setup["verdict"]
            if verdict:
                report["verdict"] = verdict
        return {"status": "completed", "worker": worker_id, "report": report}

    monkeypatch.setattr(app, "run_worker_steps", fake)
    return calls, setup


def chain(app, worker="jarvis", reviewer="gordon"):
    return asyncio.run(app.execute_worker_tool(
        "delegate_to_worker", {"worker": worker, "task": "dedupe yaz", "review_by": reviewer}, "parent-1"))


def review_events(app):
    return [e for e in app.ACTIVITY if e["event"] == "review"]


def stored(app):
    return {c["task_id"]: c for c in app.task_store.list_tasks(None, "parent-1", "worker")}


def test_approved_output_goes_to_ares_with_the_review(app, workers):
    calls, _ = workers
    result = chain(app)

    assert [c["worker"] for c in calls] == ["jarvis", "gordon"]
    draft, review = calls
    assert review["instructions"] == dg.REVIEW_INSTRUCTIONS and review["task_type"] == "review"
    assert f"## draft · jarvis (task {draft['task_id']})" in review["inputs"]
    assert "jarvis çıktısı 1" in review["inputs"]
    assert draft["report_chars"] == ce.report_share(1) - dg.REVIEW_REPORT_CHARS
    assert review["report_chars"] == dg.REVIEW_REPORT_CHARS

    assert result["report"]["deliverable"] == "jarvis çıktısı 1"
    assert result["review"]["verdict"] == "approve" and result["review"]["findings"] == ["hata yönetimi eksik"]
    assert "draft" not in result and "note" not in result and result["review_status"] == "approved"

    rows = stored(app)
    assert rows[review["task_id"]]["step"] == "review" and rows[review["task_id"]]["depends_on"] == [draft["task_id"]]
    assert rows[draft["task_id"]]["step"] is None
    assert review_events(app)[-1]["status"] == "done" and review_events(app)[-1]["revised"] is False


def test_revise_runs_a_fix_round_and_the_reviewer_looks_again(app, workers):
    calls, setup = workers
    setup["verdicts"] = ["revise", "approve"]
    result = chain(app)

    assert [c["worker"] for c in calls] == ["jarvis", "gordon", "jarvis", "gordon"]
    draft, review, revision, second = calls
    assert "jarvis çıktısı 3" in second["inputs"] and second["instructions"] == dg.REVIEW_INSTRUCTIONS
    assert revision["task"].startswith("dedupe yaz\n\nDüzeltme turu")
    assert "jarvis çıktısı 1" in revision["inputs"] and "hata yönetimi eksik" in revision["inputs"]
    assert "## review · gordon" in revision["inputs"] and revision["instructions"] == ""

    assert result["report"]["deliverable"] == "jarvis çıktısı 3"
    assert result["draft"]["task_id"] == draft["task_id"] and "report" not in result["draft"]
    assert result["review"]["verdict"] == "approve" and result["review"]["task_id"] == second["task_id"]
    assert result["review_status"] == "approved" and result["note"] == dg.REVIEWED_AFTER_FIX

    rows = stored(app)
    assert rows[revision["task_id"]]["step"] == "revision"
    assert rows[revision["task_id"]]["depends_on"] == [draft["task_id"], review["task_id"]]
    assert rows[second["task_id"]]["depends_on"] == [revision["task_id"]]
    assert review_events(app)[-1]["status"] == "done" and review_events(app)[-1]["rounds"] == 1


def test_output_never_approved_needs_review(app, workers):
    calls, setup = workers
    setup["verdict"] = "revise"
    result = chain(app)

    assert [c["worker"] for c in calls] == ["jarvis", "gordon"] * (dg.MAX_REVIEW_ROUNDS + 1)
    assert result["report"]["deliverable"] == f"jarvis çıktısı {2 * dg.MAX_REVIEW_ROUNDS + 1}"
    assert result["review_status"] == "needs_review" and result["note"] == dg.NEEDS_REVIEW_NOTE
    assert review_events(app)[-1]["status"] == "needs_review" and review_events(app)[-1]["rounds"] == dg.MAX_REVIEW_ROUNDS


def test_no_verdict_no_fix_round(app, workers):
    calls, setup = workers
    setup["verdict"] = None
    result = chain(app)

    assert len(calls) == 2 and result["review"]["verdict"] is None and result["review_status"] == "needs_review"
    assert [e["status"] for e in review_events(app)] == ["verdict_missing", "needs_review"]


def test_failed_draft_is_not_reviewed(app, workers):
    calls, setup = workers
    setup["fail_call"] = {1}
    result = chain(app)
    assert len(calls) == 1 and result["status"] == "failed"
    assert review_events(app)[-1]["status"] == "not_run"


def test_failed_review_returns_the_draft(app, workers):
    calls, setup = workers
    setup["fail_call"] = {2}
    result = chain(app)
    assert len(calls) == 2 and result["report"]["deliverable"] == "jarvis çıktısı 1"
    assert result["review"]["status"] == "failed" and result["review"]["error"] == "model down"
    assert result["review_status"] == "needs_review"


def test_failed_revision_returns_the_draft_and_says_so(app, workers):
    calls, setup = workers
    setup["verdict"] = "revise"
    setup["fail_call"] = {3}
    result = chain(app)
    assert len(calls) == 3 and result["report"]["deliverable"] == "jarvis çıktısı 1"
    assert result["revision"]["status"] == "failed" and result["revision"]["task_id"] == calls[2]["task_id"]
    assert result["review_status"] == "needs_review"


def test_reviewer_must_be_another_worker(app, workers):
    calls, _ = workers
    assert chain(app, "jarvis", "jarvis") == {"status": "error", "error": "review_by must be another worker."}
    assert calls == []


def test_without_review_by_it_is_one_worker_call(app, workers):
    calls, _ = workers
    result = asyncio.run(app.execute_worker_tool("delegate_to_worker", {"worker": "jarvis", "task": "x"}, "p"))
    assert len(calls) == 1 and calls[0]["instructions"] == "" and "review" not in result


def test_review_by_is_offered_on_the_single_delegation_only(orch):
    tools = {t["function"]["name"]: t["function"] for t in orch.TOOLS}
    single = tools["delegate_to_worker"]["parameters"]["properties"]
    many = tools["delegate_to_workers"]["parameters"]["properties"]["delegations"]["items"]["properties"]
    assert single["review_by"]["enum"] == single["worker"]["enum"] and "review_by" not in many


# ----------------------------------------------------------------------
# end to end through Ares
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


def test_review_chain_end_to_end(app, monkeypatch):
    monkeypatch.setattr(app, "OMNIROUTE_KEY", "test-key")
    call = {"id": "call_r", "type": "function", "function": {"name": "delegate_to_worker", "arguments": json.dumps(
        {"worker": "jarvis", "task": "dedupe fonksiyonu yaz", "review_by": "gordon"})}}
    worker_calls, ares = [], []
    queue = [reply(tool_calls=[call])]

    def handler(url, payload):
        if "tools" not in payload:
            system, task = payload["messages"][0]["content"], payload["messages"][1]["content"]
            worker_calls.append((system, task))
            if dg.REVIEW_INSTRUCTIONS in system:
                body = {"status": "ok", "summary": "sıra korunmuyor", "findings": ["set yerine dict kullan"],
                        "verdict": "revise"}
            else:
                body = {"status": "ok", "summary": f"kod {len(worker_calls)}",
                        "deliverable": f"def dedupe(): ... sürüm {len(worker_calls)}"}
            return reply(json.dumps(body))
        ares.append(payload)
        return queue.pop(0) if queue else reply("tamam")

    app.fake_client.handler = staticmethod(handler)
    done = asyncio.run(app.create_task_endpoint(app.TaskRequest(message="dedupe yaz ve incelet")))

    assert len(worker_calls) == 2 * dg.MAX_REVIEW_ROUNDS + 2
    assert dg.REVIEW_INSTRUCTIONS in worker_calls[1][0] and "sürüm 1" in worker_calls[1][1]
    assert "set yerine dict kullan" in worker_calls[2][1]

    [tool_message] = [m for m in ares[1]["messages"] if m.get("role") == "tool"]
    sent = json.loads(tool_message["content"])
    assert sent["report"]["deliverable"] == "def dedupe(): ... sürüm 5"
    assert sent["review"]["verdict"] == "revise" and sent["draft"]["summary"] == "kod 1"
    assert sent["review_status"] == "needs_review" and sent["note"] == dg.NEEDS_REVIEW_NOTE
    task_id = done["task_id"]
    assert app.TASKS[task_id]["status"] == "needs_review" and app.task_store.get_task(task_id)["status"] == "needs_review"


def test_g4_notes_point_ares_to_depends_on_and_to_the_revised_output(orch):
    tools = {t["function"]["name"]: t["function"] for t in orch.TOOLS}
    assert "send both in one call with depends_on" in tools["delegate_to_workers"]["description"]
    assert dg.REVIEWED_AFTER_FIX.startswith("report is the revised output, which the reviewer approved")
    assert "it is NOT done" in dg.NEEDS_REVIEW_NOTE and "do not present it as finished" in dg.NEEDS_REVIEW_NOTE


# ----------------------------------------------------------------------
# J-6: a review the user starts on a finished task
# ----------------------------------------------------------------------

@pytest.fixture
def answered(app, monkeypatch):
    """A finished task Ares answered without a review; verdicts: what the reviewer says, in order."""
    monkeypatch.setattr(app, "OMNIROUTE_KEY", "test-key")
    seen = {"verdicts": [], "reviews": []}

    def handler(url, payload):
        if "tools" not in payload:
            seen["reviews"].append(payload["messages"][1]["content"])
            verdict = seen["verdicts"].pop(0) if seen["verdicts"] else "approve"
            return reply(json.dumps({"status": "ok", "summary": "inceleme", "findings": ["kaynak yok"],
                                     "verdict": verdict}))
        return reply("İstanbul'un nüfusu 16 milyondur.")

    app.fake_client.handler = staticmethod(handler)
    done = asyncio.run(app.create_task_endpoint(app.TaskRequest(message="İstanbul'un nüfusu?")))
    return done["task_id"], seen


def manual_review(app, task_id):
    async def go():
        started = await app.review_task_endpoint(task_id)
        await asyncio.gather(*list(app.BACKGROUND_JOBS))
        return started
    return asyncio.run(go())


def test_a_finished_task_can_be_reviewed_and_approved(app, answered):
    task_id, seen = answered
    assert app.TASKS[task_id]["status"] == "completed"
    assert manual_review(app, task_id) == {"status": "running", "task_id": task_id}
    assert "İstanbul'un nüfusu 16 milyondur." in seen["reviews"][0] and "İstanbul'un nüfusu?" in seen["reviews"][0]
    stored = app.task_store.get_task(task_id)
    assert stored["status"] == "completed" and stored["result"]["review"]["verdict"] == "approve"
    assert stored["result"]["response"] == "İstanbul'un nüfusu 16 milyondur."
    assert review_events(app)[-1]["status"] == "done" and review_events(app)[-1]["manual"] is True


def test_a_rejected_answer_needs_review_until_a_later_review_approves(app, answered):
    task_id, seen = answered
    seen["verdicts"] = ["revise"]
    manual_review(app, task_id)
    assert app.TASKS[task_id]["status"] == "needs_review"
    assert app.task_store.get_task(task_id)["result"]["review"]["findings"] == ["kaynak yok"]
    assert app.review_open(task_id)
    manual_review(app, task_id)
    assert app.task_store.get_task(task_id)["status"] == "completed" and not app.review_open(task_id)


def test_only_a_finished_top_level_answer_is_reviewed(app, answered):
    task_id, _ = answered
    with pytest.raises(app.HTTPException) as caught:
        manual_review(app, "yok")
    assert caught.value.status_code == 404
    app.task_store.save_task({"task_id": "w-1", "parent_id": task_id, "kind": "worker", "status": "completed",
                              "result": {"response": "x"}})
    app.TASKS[task_id]["status"] = "running"
    for target in ("w-1", task_id):
        with pytest.raises(app.HTTPException) as caught:
            manual_review(app, target)
        assert caught.value.status_code == 409
    app.TASKS[task_id]["status"] = "completed"
    app.REVIEWS_RUNNING.add(task_id)
    with pytest.raises(app.HTTPException) as caught:
        manual_review(app, task_id)
    assert "zaten" in caught.value.detail


# ----------------------------------------------------------------------
# M-3c: claims of the final answer need a trace in the events
# ----------------------------------------------------------------------

FAKE = ("valentine-calendar projesinde validasyon eklendi. Tüm mevcut birim ve entegrasyon testleri "
        "başarıyla geçti. Kod Gordon tarafından incelendi ve onaylandı.")


def test_unsupported_claims_are_found(app):
    claims = app.unsupported_claims("task-x", FAKE, "valentine-calendar: ürün metni ≤ 120")
    assert claims == ["testlerin geçtiği", "incelemenin onaylandığı", "kod değişikliği yapıldığı"]
    # Not about a repository: a "change" in other work is not checked.
    assert app.unsupported_claims("task-x", "Not güncellendi.", "notu düzelt") == []
    app.add_activity(task_id="task-y", agent="orchestrator", event="runner", status="test", passed=True)
    app.add_activity(task_id="task-y", agent="orchestrator", event="review", status="done")
    app.add_activity(task_id="task-y", agent="orchestrator", event="code_result", status="ok", changed=["core.py"])
    assert app.unsupported_claims("task-y", FAKE, "valentine-calendar") == []


def test_invented_success_ends_needs_review(app, monkeypatch):
    monkeypatch.setattr(app, "OMNIROUTE_KEY", "test-key")
    app.fake_client.handler = staticmethod(lambda url, payload: reply(FAKE))
    done = asyncio.run(app.create_task_endpoint(app.TaskRequest(message="valentine-calendar: ürün metni ≤ 120")))
    task = app.TASKS[done["task_id"]]
    assert task["status"] == "needs_review"
    assert "⚠️ Sistem kaydıyla doğrulanamayan iddia: testlerin geçtiği" in task["result"]["response"]


def test_local_answer_gets_a_note(app):
    """L-2c: an Ares answer from the local model says so; a cloud one does not."""
    task_id = app.create_task("merhaba", "ares")
    app.add_activity(task_id=task_id, agent="orchestrator", event="llm_call", status="completed",
                     model="gh/gpt-4.1", location="cloud")
    assert app.local_answer_note(task_id) == ""
    app.add_activity(task_id=task_id, agent="orchestrator", event="llm_call", status="completed",
                     model="gpt-oss-20b-Q4_K_M", location=app.LOCAL)
    assert "yerel modelle yanıtlandı (gpt-oss-20b-Q4_K_M)" in app.local_answer_note(task_id)


def test_cloud_answer_has_no_local_note(app, monkeypatch):
    monkeypatch.setattr(app, "OMNIROUTE_KEY", "test-key")
    app.fake_client.handler = staticmethod(lambda url, payload: reply(FAKE))
    done = asyncio.run(app.create_task_endpoint(app.TaskRequest(message="valentine-calendar: ürün metni ≤ 120")))
    assert "yerel modelle" not in app.TASKS[done["task_id"]]["result"]["response"]
