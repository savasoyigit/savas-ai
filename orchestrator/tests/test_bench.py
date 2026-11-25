import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bench.py"


def load():
    spec = importlib.util.spec_from_file_location("bench", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rec(task_id, agent, prompt, completion, status="ok", location="cloud", model="gh/gpt-4.1", ms=1000):
    return {"task_id": task_id, "agent": agent, "model": model, "location": location,
            "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion, "duration_ms": ms, "status": status}


PARALLEL = [
    rec("t-c", "ares", 1500, 100),
    rec("t-c", "jarvis", 700, 900),
    rec("t-c", "oracle", 720, 950),
    rec("t-c", "gordon", 710, 800),
    rec("t-c", "ares", 6100, 400),
]


def test_three_fixed_tasks_with_expected_call_patterns():
    bench = load()
    assert [task["key"] for task in bench.TASKS] == ["a", "b", "c"]
    assert [(task["ares_calls"], task["worker_calls"]) for task in bench.TASKS] == [(1, 0), (2, 1), (2, 3)]
    assert all(task["message"].strip() for task in bench.TASKS)


def test_summarize_totals_and_synthesis_prompt():
    bench = load()
    summary = bench.summarize(PARALLEL)
    assert summary["calls"] == 5
    assert (summary["ares_calls"], summary["worker_calls"]) == (2, 3)
    assert summary["prompt_tokens"] == 1500 + 700 + 720 + 710 + 6100
    assert summary["completion_tokens"] == 100 + 900 + 950 + 800 + 400
    assert summary["total_tokens"] == summary["prompt_tokens"] + summary["completion_tokens"]
    # the last Ares call is the synthesis
    assert summary["ares_last_prompt"] == 6100
    assert summary["models"] == ["gh/gpt-4.1"]
    assert summary["locations"] == ["cloud"]
    assert summary["failed"] == 0


def test_summarize_counts_failed_calls_and_old_records_as_cloud():
    bench = load()
    old = rec("t", "ares", 10, 2)
    del old["location"], old["status"]
    summary = bench.summarize([old, rec("t", "ares", 30, 0, status="failed", location="local", model="local-m")])
    assert summary["failed"] == 1
    assert summary["locations"] == ["cloud", "local"]
    assert summary["models"] == ["gh/gpt-4.1", "local-m"]


def test_summarize_empty_task():
    bench = load()
    summary = bench.summarize([])
    assert summary["calls"] == 0 and summary["total_tokens"] == 0 and summary["ares_last_prompt"] is None


def test_pattern_matches_only_the_expected_call_shape():
    bench = load()
    parallel = bench.TASKS[2]
    assert bench.pattern_ok(bench.summarize(PARALLEL), parallel)
    assert not bench.pattern_ok(bench.summarize(PARALLEL[:4]), parallel)
    assert not bench.pattern_ok(bench.summarize(PARALLEL), bench.TASKS[0])


def test_run_posts_each_task_once_in_order_and_reads_usage_by_task_id():
    bench = load()
    posted = []

    def post(message):
        posted.append(message)
        return 200, {"status": "completed", "task_id": f"t-{len(posted)}"}

    usage = [rec("t-1", "ares", 1600, 50), rec("other", "ares", 9, 9), *[{**r, "task_id": "t-3"} for r in PARALLEL]]
    results = bench.run(post=post, read_usage=lambda: usage)

    assert posted == [task["message"] for task in bench.TASKS]
    assert [r["task_id"] for r in results] == ["t-1", "t-2", "t-3"]
    assert [len(r["records"]) for r in results] == [1, 0, 5]
    assert all(r["http"] == 200 and r["status"] == "completed" for r in results)


def test_run_keeps_going_after_a_refused_task():
    bench = load()
    answers = iter([(200, {"status": "completed", "task_id": "t-1"}),
                    (429, {"detail": "Token budget exceeded: task: 0 remaining"}),
                    (200, {"status": "completed", "task_id": "t-3"})])
    results = bench.run(post=lambda message: next(answers), read_usage=lambda: [])
    assert [r["http"] for r in results] == [200, 429, 200]
    assert results[1]["task_id"] is None and "budget" in results[1]["status"]


def test_render_is_a_markdown_table_with_totals_and_calls():
    bench = load()
    results = [
        {"key": "a", "task_id": "t-1", "http": 200, "status": "completed", "seconds": 3.21,
         "records": [rec("t-1", "ares", 1600, 50)]},
        {"key": "b", "task_id": None, "http": 429, "status": "Token budget exceeded", "seconds": 0.1, "records": []},
        {"key": "c", "task_id": "t-3", "http": 200, "status": "completed", "seconds": 40.0, "records": PARALLEL},
    ]
    text = bench.render("D-1", results, when="2026-11-14 18:30")

    assert "D-1" in text and "2026-11-14 18:30" in text
    lines = text.splitlines()
    row_c = next(line for line in lines if line.startswith("| c "))
    assert f"| {sum(r['total_tokens'] for r in PARALLEL)} |" in row_c
    assert "| 6100 |" in row_c and "✅" in row_c
    row_b = next(line for line in lines if line.startswith("| b "))
    assert "429" in row_b and "⚠️" in row_b
    # grand total over all tasks
    assert f"**{1650 + sum(r['total_tokens'] for r in PARALLEL)}**" in text
    # per-call rows: 1 + 0 + 5
    assert sum(1 for line in lines if line.startswith("| a/") or line.startswith("| c/")) == 6


def test_report_reuses_recorded_task_ids_without_posting():
    bench = load()
    usage = [rec("t-1", "ares", 1600, 50), *[{**r, "task_id": "t-3"} for r in PARALLEL]]
    results = bench.report(["t-1", "t-2", "t-3"], read_usage=lambda: usage)
    assert [r["key"] for r in results] == ["a", "b", "c"]
    assert [len(r["records"]) for r in results] == [1, 0, 5]
