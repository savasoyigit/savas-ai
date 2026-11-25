import ast
from pathlib import Path

import budget_manager
import token_manager

APP = Path(__file__).resolve().parents[1] / "app"


def parse(name):
    return ast.parse((APP / name).read_text(encoding="utf-8"))


def method_names(tree, class_name):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return [
                item.name
                for item in node.body
                if isinstance(item, ast.FunctionDef)
            ]

    raise AssertionError(f"class not found: {class_name}")


def test_budget_exceeded_type_unique():
    assert not hasattr(token_manager, "BudgetExceeded")
    assert issubclass(budget_manager.BudgetExceeded, Exception)


def test_no_dead_check_budget():
    names = {
        node.name
        for node in ast.walk(parse("token_manager.py"))
        if isinstance(node, ast.FunctionDef)
    }

    assert "check_budget" not in names


def test_update_daily_section_defined_once():
    names = method_names(parse("worker_manager.py"), "WorkerManager")

    assert names.count("update_daily_section") == 1


def test_health_tools_derived_from_tools():
    # /health must not carry a hand-written tool list.
    for node in ast.walk(parse("main.py")):
        if isinstance(node, ast.FunctionDef) and node.name == "health":
            for child in ast.walk(node):
                if isinstance(child, ast.Dict):
                    for key, value in zip(child.keys, child.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "tools"
                        ):
                            assert not isinstance(value, ast.List)
                            return

    raise AssertionError("health() 'tools' key not found")


def test_chat_payloads_send_max_tokens():
    # Every /chat/completions body (Ares + worker + K-5 chat) must cap
    # completion length with what the budget already holds: Ares the
    # reserve, a worker or a chat its per-call limit (D-3: cloud by
    # report share or CHAT_COMPLETION_TOKENS, local the reserve).
    payloads = []

    for node in ast.walk(parse("main.py")):
        if not isinstance(node, ast.Dict):
            continue

        keys = {
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant)
        }

        if {"model", "messages"} <= keys:
            payloads.append(node)

    assert len(payloads) == 3

    names = []

    for payload in payloads:
        values = {
            key.value: value
            for key, value in zip(payload.keys, payload.values)
            if isinstance(key, ast.Constant)
        }

        assert isinstance(values.get("max_tokens"), ast.Name)
        names.append(values["max_tokens"].id)

    assert sorted(names) == ["completion_limit", "completion_limit", "completion_reserve"]


def test_blocked_worker_result_says_do_not_retry():
    # decisions.md: a blocked worker result tells Ares not to retry,
    # so the tool loop does not burn iterations on the same refusal.
    for node in ast.walk(parse("main.py")):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_worker_steps":
            for child in ast.walk(node):
                if not isinstance(child, ast.Dict):
                    continue

                values = {
                    key.value: value
                    for key, value in zip(child.keys, child.values)
                    if isinstance(key, ast.Constant)
                }

                status = values.get("status")

                if isinstance(status, ast.Constant) and status.value == "blocked":
                    message = values.get("message")

                    assert isinstance(message, ast.Constant)
                    assert "tekrar deneme" in message.value.lower()
                    return

    raise AssertionError("blocked result not found in run_worker_steps")
