import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "discover_models.py"


def load():
    spec = importlib.util.spec_from_file_location("discover_models", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_refuses_more_than_six_models():
    dm = load()
    calls = []
    with pytest.raises(SystemExit):
        dm.smoke([f"gh/m{i}" for i in range(7)], post=lambda *a: calls.append(a))
    assert calls == []


def test_error_text_redacts_key_and_truncates():
    dm = load()
    text = dm.clean_error("bad key sk-SECRET123 " + "x" * 500, key="sk-SECRET123")
    assert "sk-SECRET123" not in text
    assert len(text) <= 200


def test_smoke_result_fields():
    dm = load()

    def fake_post(model_id, payload):
        assert payload["max_tokens"] == 5
        if model_id == "gh/bad":
            return 466, {"error": {"message": "client outdated"}}
        return 200, {"model": "gpt-x", "choices": [{"finish_reason": "length"}],
                     "usage": {"total_tokens": 12}}

    ok, bad = dm.smoke(["gh/ok", "gh/bad"], post=fake_post)
    assert ok["ok"] is True and ok["returned_model"] == "gpt-x"
    assert ok["finish_reason"] == "length" and ok["total_tokens"] == 12
    assert bad["ok"] is False and bad["status"] == 466
    assert "client outdated" in bad["error"]
    assert isinstance(ok["latency_ms"], int)


def test_catalog_row_reads_capabilities():
    dm = load()
    row = dm.catalog_row({"id": "gh/a", "context_length": 128000, "max_output_tokens": 4096,
                          "capabilities": {"tool_calling": True, "reasoning": False}})
    assert row == {"id": "gh/a", "tool_calling": True, "reasoning": False,
                   "vision": None, "context_length": 128000, "max_output_tokens": 4096}


def tool_reply(name="echo_word", arguments='{"word": "ok"}'):
    return 200, {"model": "gpt-x", "usage": {"total_tokens": 80}, "choices": [{
        "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": name, "arguments": arguments}}]}}]}


def test_tool_check_sends_one_tool_like_ares_does():
    dm = load()
    sent = []

    def fake_post(model_id, payload):
        sent.append(payload)
        return tool_reply()

    (row,) = dm.tool_check(["gh/a"], post=fake_post)
    (payload,) = sent
    assert payload["model"] == "gh/a" and payload["tool_choice"] == "auto"
    assert [t["function"]["name"] for t in payload["tools"]] == ["echo_word"]
    assert payload["max_tokens"] <= 100
    assert row["ok"] is True and row["tool_name"] == "echo_word" and row["arguments_valid"] is True
    assert row["finish_reason"] == "tool_calls" and row["total_tokens"] == 80


@pytest.mark.parametrize("reply, error_part", [
    ((200, {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}), "no tool call"),
    (tool_reply(name="other"), "unexpected tool"),
    (tool_reply(arguments="{bad"), "arguments"),
    ((400, {"error": {"message": "tools not supported"}}), "tools not supported"),
])
def test_tool_check_failures(reply, error_part):
    dm = load()
    (row,) = dm.tool_check(["gh/a"], post=lambda *a: reply)
    assert row["ok"] is False and error_part in row["error"]


def test_tool_check_refuses_more_than_one_model():
    dm = load()
    calls = []
    with pytest.raises(SystemExit):
        dm.tool_check(["gh/a", "gh/b"], post=lambda *a: calls.append(a))
    assert calls == []


# --- C-7.1 code quality check ---------------------------------------------

GOOD_CODE = {
    "parse_duration": '''
import re

def parse_duration(text):
    match = re.fullmatch(r"(?:(\\d+)h)?(?:(\\d+)m)?(?:(\\d+)s)?", text)
    if not text or not match:
        raise ValueError(text)
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds
''',
    "merge_intervals": '''
def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
''',
    "first_index": '''
def first_index(items, target):
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi) // 2
        if items[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(items) and items[lo] == target else -1
''',
}


def code_rows(**overrides):
    code = {**GOOD_CODE, **overrides}
    return [{"id": "gh/m", "task": name, "ok": True, "code": text, "error": ""}
            for name, text in code.items()]


def graded(rows, **kwargs):
    dm = load()
    return {row["task"]: row for row in dm.grade(rows, **kwargs)}


def code_reply(text, finish_reason="stop"):
    return 200, {"model": "gpt-x", "usage": {"total_tokens": 80},
                 "choices": [{"finish_reason": finish_reason, "message": {"content": text}}]}


def test_code_check_refuses_more_than_one_model():
    dm = load()
    calls = []
    with pytest.raises(SystemExit):
        dm.code_check(["gh/a", "gh/b"], post=lambda *a: calls.append(a))
    assert calls == []


def test_code_check_makes_one_call_per_task():
    dm = load()
    payloads = []

    def fake_post(model_id, payload):
        payloads.append(payload)
        return code_reply("```python\ndef f():\n    return 1\n```")

    rows = dm.code_check(["gh/m"], post=fake_post)
    assert len(payloads) == len(dm.CODE_TASKS) == 3
    assert all(p["model"] == "gh/m" and p["max_tokens"] == dm.CODE_MAX_TOKENS for p in payloads)
    assert [p["messages"][-1]["content"] for p in payloads] == [t["prompt"] for t in dm.CODE_TASKS]
    assert [r["task"] for r in rows] == [t["name"] for t in dm.CODE_TASKS]
    assert rows[0] == {"id": "gh/m", "task": rows[0]["task"], "ok": True, "status": 200,
                       "latency_ms": rows[0]["latency_ms"], "returned_model": "gpt-x",
                       "finish_reason": "stop", "total_tokens": 80,
                       "code": "def f():\n    return 1\n", "error": ""}


@pytest.mark.parametrize("reply, error_part", [
    ((466, {"error": {"message": "client outdated"}}), "client outdated"),
    (code_reply("```python\ndef f():", finish_reason="length"), "length"),
    (code_reply(""), "empty"),
])
def test_code_check_reports_unusable_answers(reply, error_part):
    dm = load()
    rows = dm.code_check(["gh/m"], post=lambda *a: reply)
    assert all(not r["ok"] and error_part in r["error"] for r in rows)


@pytest.mark.parametrize("text, code", [
    ("```python\nx = 1\n```", "x = 1\n"),
    ("Here:\n```py\nx = 1\n```\nDone.", "x = 1\n"),
    ("```\nx = 1\n```", "x = 1\n"),
    ("x = 1", "x = 1"),
])
def test_extract_code(text, code):
    assert load().extract_code(text) == code


def test_grade_passes_correct_solutions():
    rows = graded(code_rows())
    assert sorted(rows) == sorted(GOOD_CODE)
    assert all(r["passed"] and r["failures"] == [] and r["cases"] >= 6 for r in rows.values())


def test_grade_ignores_printed_output():
    noisy = GOOD_CODE["first_index"].replace("lo, hi = 0", "print('x'); lo, hi = 0")
    assert graded(code_rows(first_index=noisy))["first_index"]["passed"]


def test_grade_fails_wrong_answers():
    wrong = "def merge_intervals(intervals):\n    return sorted(intervals)\n"
    row = graded(code_rows(merge_intervals=wrong))["merge_intervals"]
    assert not row["passed"] and row["failures"]


def test_grade_detects_mutated_input():
    mutating = GOOD_CODE["merge_intervals"].replace(
        "    merged = []\n", "    intervals.sort()\n    merged = []\n")
    row = graded(code_rows(merge_intervals=mutating))["merge_intervals"]
    assert not row["passed"] and any("mutated" in f for f in row["failures"])


def test_grade_stops_code_that_never_returns():
    looping = GOOD_CODE["first_index"].replace("lo = mid + 1", "lo = mid")
    row = graded(code_rows(first_index=looping), timeout=1)["first_index"]
    assert not row["passed"] and "timeout" in row["failures"][0]


@pytest.mark.parametrize("code", [
    "import os\ndef first_index(items, target):\n    return -1\n",
    "from subprocess import run\ndef first_index(items, target):\n    return -1\n",
    "def first_index(items, target):\n    open('x', 'w')\n    return -1\n",
    "def first_index(items, target):\n    return ().__class__\n",
    "def first_index(items, target:\n",
])
def test_grade_does_not_run_unsafe_or_broken_code(code, monkeypatch):
    dm = load()
    monkeypatch.setattr(dm.subprocess, "run", lambda *a, **k: pytest.fail("code was executed"))
    answers = [r for r in code_rows(first_index=code) if r["task"] == "first_index"]
    row = {r["task"]: r for r in dm.grade(answers)}["first_index"]
    assert not row["passed"] and row["failures"][0].startswith(("unsafe", "syntax"))


def test_grade_fails_missing_and_failed_calls():
    rows = [r for r in code_rows() if r["task"] != "parse_duration"]
    rows[0].update(ok=False, code="", error="HTTP 500")
    result = graded(rows)
    assert not result["parse_duration"]["passed"] and "no answer" in result["parse_duration"]["failures"][0]
    assert not result[rows[0]["task"]]["passed"] and "HTTP 500" in result[rows[0]["task"]]["failures"][0]
