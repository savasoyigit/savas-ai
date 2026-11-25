"""I-3: code tasks on a sandbox copy, edit blocks, tests, at most 3 rounds (plan-i karar D)."""

import asyncio
import json

import pytest

import dev_loop as dl
from context_engine import TRIM_MARK


def block(path, search, replace):
    return f"FILE: {path}\n<<<<<<< SEARCH\n{search}=======\n{replace}>>>>>>> REPLACE\n"


# ----------------------------------------------------------------------
# pure functions
# ----------------------------------------------------------------------

def test_code_argument_is_cleaned():
    assert dl.clean_code(None) is None
    assert dl.clean_code({"repo": "savas-ai", "files": ["/a.py", "a.py", "b.py"], "test": "pytest"}) == {
        "repo": "savas-ai", "files": ["a.py", "b.py"], "test": "pytest", "focus": []}
    assert dl.clean_code({"repo": "savas-ai"}) == {"repo": "savas-ai", "files": [], "test": None, "focus": []}
    focus = [" hafta ", "hafta", "x" * 80] + [f"t{i}" for i in range(6)]
    assert dl.clean_code({"repo": "r", "focus": focus})["focus"] == ["hafta", "x" * 60, "t0", "t1", "t2"]


@pytest.mark.parametrize("value,word", [
    ("savas-ai", "object"), ({"files": []}, "object"), ({"repo": "r", "files": "a.py"}, "list"),
    ({"repo": "r", "files": [f"{i}.py" for i in range(6)]}, "at most 5"),
    ({"repo": "r", "test": "bash -c x"}, "pytest"), ({"repo": "r", "files": ["../x.py"]}, "not allowed"),
    ({"repo": "r", "focus": "hafta"}, "search terms"), ({"repo": "r", "focus": ["", 3]}, "search terms"),
])
def test_unusable_code_arguments(value, word):
    with pytest.raises(dl.CodeError, match=word):
        dl.clean_code(value)


@pytest.mark.parametrize("path", [
    "/etc/passwd", "../x", "a/../../x", "secrets/k", "data/savas.db", "omniroute/.env", "x.env",
    ".git/config", "sandbox/x", ".venv/bin/python", "a" * 201,
])
def test_forbidden_paths(path):
    with pytest.raises(dl.CodeError):
        dl.check_path(path)


def test_blocks_are_parsed_and_a_cut_block_is_left_out():
    text = block("a.py", "x = 1\n", "x = 2\n") + block("new/b.py", "", "print(1)\n")
    assert dl.parse_edits(text) == [
        {"path": "a.py", "search": "x = 1\n", "replace": "x = 2\n"},
        {"path": "new/b.py", "search": "", "replace": "print(1)\n"},
    ]
    cut = text + "FILE: c.py\n<<<<<<< SEARCH\ny = 1\n=====" + TRIM_MARK
    assert len(dl.parse_edits(cut)) == 2
    assert dl.parse_edits(None) == [] and dl.parse_edits("açıklama") == []


@pytest.fixture
def copy(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\ny = 1\ny = 1\n")
    return root


def test_edits_apply_in_order_and_bad_ones_are_reported(copy):
    owners = []
    outcome = dl.apply_edits(copy, [
        {"path": "pkg/a.py", "search": "x = 1\n", "replace": "x = 2\n"},
        {"path": "pkg/a.py", "search": "y = 1\n", "replace": "y = 2\n"},
        {"path": "pkg/a.py", "search": "z\n", "replace": ""},
        {"path": "pkg/b.py", "search": "", "replace": "print(1)\n"},
        {"path": "pkg/new/c.py", "search": "", "replace": "print(2)\n"},
        {"path": "pkg/a.py", "search": "", "replace": "ezme"},
        {"path": "yok.py", "search": "a", "replace": "b"},
        {"path": "secrets/k", "search": "", "replace": "x"},
    ], chown=lambda path, uid, gid: owners.append((path.name, uid, gid)))

    assert outcome["applied"] == ["pkg/a.py", "pkg/b.py"]
    assert [(r["path"], r["error"]) for r in outcome["rejected"]] == [
        ("pkg/a.py", "SEARCH found 2 times"), ("pkg/a.py", "SEARCH not found"),
        ("pkg/new/c.py", "new files only in an existing folder"),
        ("pkg/a.py", "file exists; an empty SEARCH only creates new files"),
        ("yok.py", "file does not exist"), ("secrets/k", "Path not allowed: secrets/k"),
    ]
    assert (copy / "pkg" / "a.py").read_text() == "x = 2\ny = 1\ny = 1\n"
    assert (copy / "pkg" / "b.py").read_text() == "print(1)\n" and not (copy / "pkg" / "new").exists()
    assert owners == [("b.py", 1000, 1000)]
    assert not (copy.parent / "secrets").exists()


def test_link_out_of_the_copy_is_refused(copy, tmp_path):
    (tmp_path / "outside.py").write_text("x\n")
    (copy / "link.py").symlink_to(tmp_path / "outside.py")
    outcome = dl.apply_edits(copy, [{"path": "link.py", "search": "x\n", "replace": "y\n"}], owner=None)
    assert outcome["rejected"] and (tmp_path / "outside.py").read_text() == "x\n"


def test_file_inputs_and_feedback(copy):
    inputs = dl.file_inputs(copy, ["pkg/a.py", "pkg/yeni.py"])
    assert inputs.startswith(dl.FILES_HEADING) and "## pkg/a.py\nx = 1\n" in inputs
    assert "## pkg/yeni.py\n(dosya yok" in inputs
    assert dl.file_inputs(copy, []) == ""


# ----------------------------------------------------------------------
# M-2: big files as a map plus ranges (plan-m karar C)
# ----------------------------------------------------------------------

def big_file(copy, lines=2000):
    body = [f"    x{i} = {i}  # dolgu satırı" for i in range(1, lines + 1)]
    body[99] = "def render_week(days):"
    body[1499] = "    return 'hafta görünümü'"
    body[1799] = '<section id="takvim">'
    body[1899] = "# LOGIN biçimi: yorum satırı"
    body[1949] = '  <div id="giris">'
    (copy / "big.py").write_text("\n".join(body) + "\n")
    return body


def test_small_file_comes_whole_and_big_file_as_map_and_focus(copy):
    body = big_file(copy)
    inputs = dl.file_inputs(copy, ["big.py"], ["HAFTA"])
    assert "(büyük dosya: 2001 satır" in inputs
    outline = inputs.split("### harita (satır ad)\n")[1].split("\n### satır")[0]
    assert outline.split("\n") == ["100 def render_week", "1800 <section>", "1950 #giris"]
    # The focus window: whole lines around line 1500, nothing from far away.
    assert "### satır 1460-1540\n" + body[1459] in inputs and body[1539] in inputs
    assert body[1540] not in inputs and body[999] not in inputs and TRIM_MARK not in inputs
    assert len(inputs) <= dl.FILE_INPUT_CHARS + len(dl.FILES_HEADING) + 10


def test_big_file_without_focus_starts_at_the_top_and_stays_in_its_share(copy):
    body = big_file(copy)
    inputs = dl.file_inputs(copy, ["big.py", "pkg/a.py"])
    assert "### satır 1-" in inputs and body[0] in inputs and body[1999] not in inputs
    assert "## pkg/a.py\nx = 1\n" in inputs

    many = dl.file_inputs(copy, ["big.py"] * 1 + ["pkg/a.py", "x1.py", "x2.py", "x3.py"])
    section = many.split("## pkg/a.py")[0]
    assert len(section) <= dl.TOTAL_INPUT_CHARS // 5 + len(dl.FILES_HEADING) + 10


def test_asked_ranges_come_first(copy):
    body = big_file(copy)
    needs = dl.parse_needs("özet\nNEED: big.py:1900-1910\nNEED: /big.py:5-3\nNEED: başka.py:1-2\n", ["big.py"])
    assert needs == [{"path": "big.py", "start": 1900, "end": 1910}]
    inputs = dl.file_inputs(copy, ["big.py"], ["hafta"], needs)
    assert inputs.index("### satır 1900-1910") < inputs.index("### satır 1460-")
    assert body[1899] in inputs and body[1910] not in inputs


def test_needs_are_capped():
    text = "\n".join(f"NEED: a.py:{i}-{i + 1000}" for i in range(1, 6))
    needs = dl.parse_needs(text, ["a.py"])
    assert len(needs) == dl.MAX_NEEDS and needs[0] == {"path": "a.py", "start": 1, "end": dl.MAX_NEED_LINES}


def test_map_is_capped():
    lines = [f"def f{i}():" for i in range(1000)]
    outline = dl.file_map(lines)
    assert outline.startswith("1 def f0\n") and outline.endswith("\n…") and len(outline) <= dl.MAP_CHARS
    # Definitions win the room over page structure; headings only in Markdown.
    mixed = [f'<div id="d{i}">' for i in range(400)] + ["function late() {"]
    assert "401 late()" in dl.file_map(mixed) and "# Başlık" not in dl.file_map(["# Başlık"])
    assert dl.file_map(["# Başlık", "x"], markdown=True) == "1 # Başlık"


def test_feedback_text():
    text = dl.feedback({"passed": False, "exit_code": 1, "output": "a" * 5000 + "FAILED t"},
                       [{"path": "x.py", "error": "SEARCH not found"}])
    assert text.startswith(dl.REJECTED_HEADING + "\n- x.py: SEARCH not found")
    assert "çıkış kodu 1" in text and text.endswith("FAILED t") and len(text) < dl.TEST_OUTPUT_CHARS + 200
    assert dl.feedback({"passed": True}, []) == ""
    assert "zaman aşımı" in dl.feedback({"passed": False, "timed_out": True, "output": ""}, [])


# ----------------------------------------------------------------------
# the loop through main.py, stand-in worker and runner
# ----------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


@pytest.fixture
def app(db, orch, tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    monkeypatch.setattr(orch, "RUNNER_SOCKET", "/run/savas-runner/run.sock")
    monkeypatch.setattr(orch, "SANDBOX_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setattr(dl, "SANDBOX_OWNER", None)
    monkeypatch.setattr(orch, "apply_edits", lambda root, edits: dl.apply_edits(root, edits, owner=None))
    yield orch
    orch.task_store.close()


@pytest.fixture
def loop(app, tmp_path, monkeypatch):
    """setup: deliverables per round, test results per run. seen: worker inputs, runner calls."""
    setup = {"deliverables": [], "tests": [], "worker_fails": False}
    seen = {"inputs": [], "runner": []}

    async def runner_call(path, body, timeout=60):
        seen["runner"].append(path)
        if path == "/prepare":
            sandbox = tmp_path / "sandbox" / body["task"]
            (sandbox / "repo" / "pkg").mkdir(parents=True)
            (sandbox / "repo" / "pkg" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
            (sandbox / "repo.json").write_text(json.dumps({"repo": body["repo"]}))
            return {"status": "ok", "files": 1}
        if path == "/diff":
            return setup.get("diff") or {"status": "ok", "files": ["pkg/calc.py"], "added": 1, "removed": 1,
                                         "conflicts": [], "patch": "/home/savas/AI/sandbox/t/change.patch",
                                         "diff": "--- a/pkg/calc.py\n+++ b/pkg/calc.py\n-a - b\n+a + b\n"}
        # M-3b: every code task runs the tests; passing unless a test says otherwise.
        return setup["tests"].pop(0) if setup["tests"] else {
            "status": "ok", "run": 1, "exit_code": 0, "passed": True, "timed_out": False, "output": "ok"}

    async def worker(worker_id, task, parent, task_type=None, report_chars=None, worker_task_id=None,
                     depends_on=(), inputs="", step=None, instructions="", search=()):
        seen["inputs"].append({"task": task, "inputs": inputs, "instructions": instructions,
                               "report_chars": report_chars, "step": step, "depends_on": list(depends_on)})
        if setup["worker_fails"]:
            return {"status": "failed", "worker": worker_id, "error": "model down"}
        if instructions == app.REVIEW_INSTRUCTIONS:
            return {"status": "completed", "worker": worker_id,
                    "report": {"status": "ok", "summary": "inceleme", "findings": ["b'yi de kontrol et"],
                               "verdict": setup["verdicts"].pop(0) if setup.get("verdicts")
                               else setup.get("verdict", "approve")}}
        report = {"status": "ok", "summary": f"tur {len(seen['inputs'])}"}
        deliverable = setup["deliverables"].pop(0) if setup["deliverables"] else ""
        if deliverable:
            report["deliverable"] = deliverable
        return {"status": "completed", "worker": worker_id, "report": report}

    monkeypatch.setattr(app, "runner_call", runner_call)
    monkeypatch.setattr(app, "run_worker", worker)
    return setup, seen


FIX = block("pkg/calc.py", "    return a - b\n", "    return a + b\n")
PASS = {"status": "ok", "run": 1, "exit_code": 0, "passed": True, "timed_out": False, "output": "1 passed"}
FAIL = {"status": "ok", "run": 1, "exit_code": 1, "passed": False, "timed_out": False,
        "output": "FAILED test_add - assert 3 == -1"}


def coder(seen):
    """The worker's own rounds; M-3a adds a review to every code task."""
    return [i for i in seen["inputs"] if i["step"] != "review"]


def code_task(app, code, task_id="task-0001", **extra):
    arguments = {"worker": "jarvis", "task": "add'i düzelt", "code": code, **extra}
    return asyncio.run(app.execute_worker_tool("delegate_to_worker", arguments, task_id))


def test_fix_passes_in_one_round(app, loop, tmp_path):
    setup, seen = loop
    setup["deliverables"] = [FIX]
    setup["tests"] = [PASS]
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"})

    assert result["status"] == "completed" and "deliverable" not in result["report"]
    patch = result["sandbox"].pop("patch")
    assert result["sandbox"] == {"repo": "savas-ai", "task": "task-0001", "rounds": 1,
                                 "changed_files": ["pkg/calc.py"], "tests": "passed",
                                 "rejected_blocks": 0, "note": dl.NOTHING_LIVE}
    assert patch["apply"] == "scripts/apply_patch.sh task-0001" and patch["files"] == ["pkg/calc.py"]
    assert result["review_status"] == "approved" and result["review"]["worker"] == "gordon"
    first = seen["inputs"][0]
    assert first["instructions"] == dl.EDIT_INSTRUCTIONS and first["report_chars"] == dl.CODE_REPORT_CHARS
    assert "return a - b" in first["inputs"] and first["step"] == "code"
    assert "return a + b" in (tmp_path / "sandbox" / "task-0001" / "repo" / "pkg" / "calc.py").read_text()
    assert seen["runner"] == ["/prepare", "/test", "/diff"]


def test_failed_test_goes_back_and_the_second_round_passes(app, loop):
    setup, seen = loop
    setup["deliverables"] = [block("pkg/calc.py", "def add(a, b):\n", "def add(a, b):  # x\n"), FIX]
    setup["tests"] = [FAIL, PASS]
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"})

    assert result["sandbox"]["rounds"] == 2 and result["sandbox"]["tests"] == "passed"
    second = seen["inputs"][1]
    assert "assert 3 == -1" in second["inputs"] and "def add(a, b):  # x" in second["inputs"]
    assert second["depends_on"] and second["depends_on"] != []


def test_at_most_three_rounds(app, loop):
    setup, seen = loop
    setup["deliverables"] = [FIX, FIX, FIX, FIX]
    setup["tests"] = [FAIL, FAIL, FAIL, FAIL]
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"})
    assert len(coder(seen)) == dl.MAX_ROUNDS and result["sandbox"]["tests"] == "failed"
    assert result["sandbox"]["rejected_blocks"] == 2


def test_rejected_block_without_tests_gets_one_more_round(app, loop):
    setup, seen = loop
    setup["deliverables"] = [block("pkg/calc.py", "yok\n", "x\n"), FIX]
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"]})
    assert len(coder(seen)) == 2 and "SEARCH not found" in seen["inputs"][1]["inputs"]
    assert result["sandbox"]["tests"] == "passed" and result["sandbox"]["changed_files"] == ["pkg/calc.py"]


def test_need_without_edits_gets_another_round(app, loop):
    setup, seen = loop
    setup["deliverables"] = ["NEED: pkg/calc.py:1-2", FIX]
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "focus": ["add"]})
    assert len(coder(seen)) == 2 and result["sandbox"]["changed_files"] == ["pkg/calc.py"]
    events = [e for e in app.task_events("task-0001") if e.get("event") == "code_need"]
    assert events and events[0]["ranges"] == ["pkg/calc.py:1-2"]


def test_runner_error_stops_the_loop(app, loop):
    setup, seen = loop
    setup["deliverables"] = [FIX]
    setup["tests"] = [{"status": "error", "error": "at most 5 test runs per task"}]
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"})
    assert len(coder(seen)) == 1 and result["sandbox"]["tests"] == "error"
    assert "at most 5" in result["sandbox"]["test_error"]


def test_failed_worker_returns_the_error(app, loop):
    setup, _ = loop
    setup["worker_fails"] = True
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"})
    assert result["status"] == "failed" and result["error"] == "model down" and result["sandbox"]["rounds"] == 1


def test_bad_combinations_and_arguments(app, loop):
    _, seen = loop
    assert "search" in code_task(app, {"repo": "savas-ai"}, search=["x"])["error"]
    assert "another worker" in code_task(app, {"repo": "savas-ai", "files": ["a.py"]}, review_by="jarvis")["error"]
    assert code_task(app, {"repo": "savas-ai"})["error"] == app.CODE_FILES_REQUIRED
    assert "not allowed" in code_task(app, {"repo": "savas-ai", "files": ["secrets/x"]})["error"]
    assert code_task(app, {"repo": "/home"})["status"] == "error"
    assert seen["inputs"] == []


def test_code_is_offered_on_the_single_delegation_only(orch):
    tools = {t["function"]["name"]: t["function"] for t in orch.TOOLS}
    single = tools["delegate_to_worker"]["parameters"]["properties"]
    many = tools["delegate_to_workers"]["parameters"]["properties"]["delegations"]["items"]["properties"]
    assert single["code"] == orch.CODE_SCHEMA and "code" not in many
    assert orch.CODE_SCHEMA["properties"]["test"]["enum"] == ["pytest"]



# ----------------------------------------------------------------------
# I-4: diff, review; J-6: fix rounds reviewed again
# ----------------------------------------------------------------------

def test_reviewer_gets_the_diff_and_approves(app, loop):
    setup, seen = loop
    setup["deliverables"] = [FIX]
    setup["tests"] = [PASS]
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"}, review_by="gordon")

    review_call = seen["inputs"][1]
    assert review_call["instructions"] == app.REVIEW_INSTRUCTIONS and review_call["step"] == "review"
    assert review_call["inputs"].startswith(dl.DIFF_HEADING) and "+a + b" in review_call["inputs"]
    assert "Testler geçti." in review_call["inputs"]
    assert result["review"]["verdict"] == "approve" and "note" not in result and result["review_status"] == "approved"
    assert result["sandbox"]["rounds"] == 1 and seen["runner"] == ["/prepare", "/test", "/diff"]


def test_revise_runs_a_fix_round_a_new_diff_and_a_new_review(app, loop):
    setup, seen = loop
    setup["verdicts"] = ["revise", "approve"]
    setup["deliverables"] = [FIX, block("pkg/calc.py", "def add(a, b):\n", "def add(a: int, b: int):\n")]
    setup["tests"] = [PASS, PASS]
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"}, review_by="gordon")

    fix = seen["inputs"][2]
    assert fix["step"] == "revision" and dl.REVIEW_HEADING in fix["inputs"] and "b'yi de kontrol et" in fix["inputs"]
    assert len(fix["depends_on"]) == 2
    assert result["sandbox"]["rounds"] == 2 and result["note"] == app.REVIEWED_AFTER_FIX
    assert result["review_status"] == "approved" and result["review"]["verdict"] == "approve"
    assert seen["runner"] == ["/prepare", "/test", "/diff", "/test", "/diff"]
    assert len(seen["inputs"]) == 4 and seen["inputs"][3]["step"] == "review"
    assert len(seen["inputs"][3]["depends_on"]) == 1 and "+a + b" in seen["inputs"][3]["inputs"]


def test_a_change_the_reviewer_never_approves_needs_review(app, loop):
    setup, seen = loop
    setup["verdict"] = "revise"
    setup["deliverables"] = [FIX, FIX, FIX]
    setup["tests"] = [PASS, PASS, PASS]
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"}, review_by="gordon")
    assert result["sandbox"]["rounds"] == 1 + app.MAX_REVIEW_ROUNDS and len(seen["inputs"]) == 6
    assert result["review_status"] == "needs_review" and result["note"] == app.NEEDS_REVIEW_NOTE
    assert [e for e in app.ACTIVITY if e["event"] == "review"][-1]["status"] == "needs_review"


def test_no_change_no_diff_no_review(app, loop):
    setup, seen = loop
    setup["tests"] = [PASS]
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"}, review_by="gordon")
    assert "patch" not in result["sandbox"] and "review" not in result and len(seen["inputs"]) == 1


def test_diff_error_is_reported(app, loop):
    setup, _ = loop
    setup["deliverables"] = [FIX]
    setup["tests"] = [PASS]
    setup["diff"] = {"status": "error", "error": "sandbox has no manifest or unknown repo; prepare it again"}
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"}, review_by="gordon")
    assert "prepare it again" in result["sandbox"]["patch_error"] and "review" not in result


def test_review_input_and_findings_text():
    text = dl.diff_input({"files": ["a.py"], "diff": "x\n" * 5000}, {"status": "ok", "passed": False})
    assert "Dosyalar: a.py\nTestler geçmedi." in text and len(text) < dl.DIFF_INPUT_CHARS + 200
    assert "koşulmadı" in dl.diff_input({"files": [], "diff": ""}, None)
    notes = dl.review_findings({"report": {"summary": "eksik", "findings": ["test ekle"]}})
    assert notes == dl.REVIEW_HEADING + "\neksik\n- test ekle"


# ----------------------------------------------------------------------
# M-3a: files required, review always, facts from the orchestrator
# ----------------------------------------------------------------------

def test_gordon_reviews_unasked_and_gandalf_reviews_gordon(app, loop):
    setup, seen = loop
    setup["deliverables"] = [FIX]
    setup["tests"] = [PASS]
    code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"})
    assert [i["step"] for i in seen["inputs"]] == ["code", "review"]

    setup["deliverables"] = [FIX]
    setup["tests"] = [PASS]
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"},
                       task_id="task-0002", worker="gordon")
    assert result["review"]["worker"] == "gandalf"


def test_need_may_name_another_file(app, loop):
    setup, seen = loop
    setup["deliverables"] = ["NEED: pkg/other.py:1-5", FIX]
    code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"]})
    assert "## pkg/other.py" in coder(seen)[1]["inputs"]
    assert dl.parse_needs("NEED: secrets/k:1-2\nNEED: x.py:1-2") == [{"path": "x.py", "start": 1, "end": 2}]


def test_facts_and_status_come_from_the_events(app, loop):
    setup, _ = loop
    setup["deliverables"] = [FIX]
    setup["tests"] = [PASS]
    code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"})
    assert app.code_fact_lines("task-0001") == (
        "🔧 Kod sonucu (savas-ai, sistemden): değişen dosya: pkg/calc.py · testler: geçti · "
        "gordon onayladı · ✅")
    assert app.code_open("task-0001") is False

    # Nothing applied: not done, whatever the model says.
    setup["deliverables"] = [block("pkg/calc.py", "yok\n", "x\n")] * 3
    result = code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"]}, task_id="task-0003")
    assert result["note"] == app.NO_CHANGE_NOTE and app.code_open("task-0003") is True
    assert "değişen dosya: yok · testler: geçti · gordon incelemedi · ⚠️ İNCELENMELİ" in app.code_fact_lines("task-0003")


def test_unapproved_change_is_not_done(app, loop):
    setup, _ = loop
    setup["deliverables"] = [FIX, FIX, FIX]
    setup["tests"] = [PASS, PASS, PASS]
    setup["verdict"] = "revise"
    code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"], "test": "pytest"})
    assert app.code_open("task-0001") is True
    assert "gordon onaylamadı (revise)" in app.code_fact_lines("task-0001")


# ----------------------------------------------------------------------
# M-3b: real paths, existing folders, tests always, reviewer sees the repo
# ----------------------------------------------------------------------

def test_invented_path_goes_back_with_the_real_file_list(app, loop):
    setup, seen = loop
    result = code_task(app, {"repo": "savas-ai", "files": ["backend/routes/list.js", "pkg/calc.py"]})
    assert result["status"] == "error" and "Not in savas-ai: backend/routes/list.js" in result["error"]
    assert result["repo_files"] == ["pkg/calc.py"] and seen["inputs"] == []


def test_tests_run_unasked_and_reviewer_gets_the_file_list(app, loop):
    setup, seen = loop
    setup["deliverables"] = [FIX]
    code_task(app, {"repo": "savas-ai", "files": ["pkg/calc.py"]})
    assert "/test" in seen["runner"]
    review = [i for i in seen["inputs"] if i["step"] == "review"][0]["inputs"]
    assert "Projedeki dosyalar: pkg/calc.py" in review and dl.REVIEW_CODE_RULES in review


def test_repo_files_skips_images_and_caches(tmp_path):
    (tmp_path / "icons").mkdir()
    (tmp_path / "icons" / "a.png").write_bytes(b"x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "m.pyc").write_bytes(b"x")
    (tmp_path / "core.py").write_text("x\n")
    assert dl.repo_files(tmp_path) == ["core.py"]
    assert dl.missing_files(tmp_path, ["core.py", "yok.py", "../x"]) == ["yok.py", "../x"]


def test_direct_code_task_without_ares(app, loop):
    setup, seen = loop
    setup["deliverables"] = [FIX]
    task_id = app.create_task("[kod · savas-ai] add'i düzelt", "jarvis")
    request = app.CodeTaskRequest(repo="savas-ai", task="add'i düzelt", files=["pkg/calc.py"])
    asyncio.run(app.run_direct_code(task_id, request))
    task = app.TASKS[task_id]
    assert task["status"] == "completed" and "gordon onayladı · ✅" in task["result"]["response"]
    assert [i["step"] for i in seen["inputs"]] == ["code", "review"]

    with pytest.raises(app.HTTPException):
        asyncio.run(app.create_code_task(app.CodeTaskRequest(repo="baska", task="x", files=["a.py"])))
