import ast
from pathlib import Path

from worker_manager import WorkerManager

APP = Path(__file__).resolve().parents[1] / "app"

MESSAGE = "benzersiz-gorev-metni-42"


def make_manager(tmp_path):
    manager = WorkerManager(tmp_path)
    worker_dir = tmp_path / "workers" / "ares"
    worker_dir.mkdir(parents=True)
    (worker_dir / "ares.md").write_text("# Ares\n", encoding="utf-8")
    manager.create_daily_task("ares", "t1", MESSAGE)
    return manager


def test_context_can_omit_task_text(tmp_path):
    manager = make_manager(tmp_path)

    context = manager.build_worker_context(
        "ares",
        "t1",
        include_task=False,
    )

    assert MESSAGE not in context
    # The rest of the daily memory is still there.
    assert "## Status" in context
    assert "## Work Log" in context


def test_context_includes_task_by_default(tmp_path):
    manager = make_manager(tmp_path)

    assert MESSAGE in manager.build_worker_context("ares", "t1")


def test_main_sends_task_text_once():
    tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "build_worker_context"
    ]

    assert len(calls) == 3  # Ares, worker, K-5 chat

    for call in calls:
        flags = {
            keyword.arg: keyword.value
            for keyword in call.keywords
        }

        assert isinstance(flags.get("include_task"), ast.Constant)
        assert flags["include_task"].value is False

    source = (APP / "main.py").read_text(encoding="utf-8")

    assert "DELEGATED TASK" not in source


BACKSLASH_TEXTS = [
    're.match(r"\\d+", s)',
    'print("a\\nb")',
    'json: "\\u00f6"',
    'grup: \\1 \\g<0>',
]


def test_daily_section_keeps_backslashes(tmp_path):
    manager = make_manager(tmp_path)

    for text in BACKSLASH_TEXTS:
        manager.update_daily_section("ares", "t1", "Current State", text)
        daily = manager.daily_task_path("ares", "t1").read_text(encoding="utf-8")
        assert f"## Current State\n\n{text}\n\n" in daily


def test_appended_section_keeps_backslashes(tmp_path):
    manager = make_manager(tmp_path)

    for text in BACKSLASH_TEXTS:
        manager.append_daily_section("ares", "t1", "Findings", text)

    daily = manager.daily_task_path("ares", "t1").read_text(encoding="utf-8")
    assert all(text in daily for text in BACKSLASH_TEXTS)
