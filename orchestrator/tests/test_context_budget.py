import asyncio
import json

import pytest

import context_engine as ce
from worker_manager import WorkerManager

RULES = (
    "# Ares\n\n"
    "## Identity\n\nOrchestrator.\n\n"
    "## Core Rules\n\n- Kural bir.\n- Kural iki.\n\n"
)
EMPTY_MEMORY = "## Important Decisions\n\n## Learned Principles\n\n## Things To Remember\n"
IDENTITY = RULES + EMPTY_MEMORY

MESSAGE = "benzersiz-gorev-metni-42"


def identity_with(decisions=(), principles=(), remember=()):
    def section(heading, items):
        return f"## {heading}\n\n" + "".join(f"- {item}\n" for item in items) + ("\n" if items else "")

    return (RULES + section("Important Decisions", decisions) + section("Learned Principles", principles)
            + section("Things To Remember", remember))


def daily_file(**sections):
    """A daily task file as create_daily_task writes it, with the given sections filled."""
    filled = {"Worker": "ares", "Date": "2026-11-14", "Task": MESSAGE, "Status": "In Progress",
              "Work Log": "", "Findings": "", "Decisions": "", "Current State": "", "Next Step": ""}
    filled.update({name.replace("_", " "): body for name, body in sections.items()})
    return "# Task t1\n\n" + "".join(f"## {name}\n\n{body}\n\n" if body else f"## {name}\n\n"
                                      for name, body in filled.items())


def daily_part(context):
    return context.split(ce.DAILY_HEADING, 1)[1] if ce.DAILY_HEADING in context else ""


# ----------------------------------------------------------------------
# split_sections
# ----------------------------------------------------------------------

@pytest.mark.parametrize("text", [IDENTITY, daily_file(), "", "başlıksız metin", "## Tek\n", "a\n### alt başlık\nb"])
def test_sections_join_back_to_the_text(text):
    assert "".join(chunk for _, chunk in ce.split_sections(text)) == text


def test_sections_are_cut_at_second_level_headings_only():
    sections = ce.split_sections("# Üst\n\n## Bir\n\na\n### Alt\nb\n## İki \n")
    assert [heading for heading, _ in sections] == [None, "Bir", "İki"]
    assert sections[1][1] == "## Bir\n\na\n### Alt\nb\n"


# ----------------------------------------------------------------------
# identity: rules and persistent memory
# ----------------------------------------------------------------------

def test_empty_memory_sections_are_left_out_and_rules_stay_as_written():
    assert ce.fit_identity(IDENTITY, 6000) == RULES.rstrip()


def test_filled_memory_is_kept_when_it_fits():
    identity = identity_with(decisions=["karar bir", "karar iki"], remember=["hatırla"])
    fitted = ce.fit_identity(identity, 6000)

    assert fitted.startswith(RULES)
    assert "## Important Decisions\n\n- karar bir\n- karar iki\n" in fitted
    assert "## Things To Remember\n\n- hatırla" in fitted
    assert "Learned Principles" not in fitted and "çıkarıldı" not in fitted


def test_memory_over_the_room_loses_its_oldest_items():
    decisions = [f"karar {i:02d} " + "x" * 40 for i in range(20)]
    identity = identity_with(decisions=decisions, remember=["hatırla"])
    room = len(RULES) + 500

    fitted = ce.fit_identity(identity, room)

    assert len(fitted) <= room
    assert fitted.startswith(RULES)
    kept = [item for item in decisions if item in fitted]
    assert 0 < len(kept) < 20 and kept == decisions[-len(kept):]
    assert f"({20 - len(kept)} eski madde çıkarıldı)\n- {kept[0]}" in fitted
    # the small section was not touched
    assert "## Things To Remember\n\n- hatırla" in fitted


def test_the_largest_memory_section_is_cut_first():
    identity = identity_with(decisions=["kısa"], principles=[f"ilke {i} " + "y" * 60 for i in range(10)])
    fitted = ce.fit_identity(identity, len(RULES) + 400)

    assert "- kısa" in fitted and "ilke 9" in fitted and "ilke 0" not in fitted


def test_rules_are_never_cut():
    identity = identity_with(decisions=["karar bir", "karar iki"])
    fitted = ce.fit_identity(identity, 10)

    assert fitted.startswith(RULES.rstrip())
    assert "karar" not in fitted and "(2 eski madde çıkarıldı)" in fitted


def test_text_without_headings_is_all_rules():
    assert ce.fit_identity("# ares\n", 3) == "# ares"


# ----------------------------------------------------------------------
# daily memory
# ----------------------------------------------------------------------

def test_new_daily_file_keeps_only_the_date():
    assert ce.fit_daily(daily_file(), 6000) == "## Date\n\n2026-11-14"


def test_filled_daily_sections_are_kept_in_order():
    daily = daily_file(Work_Log="- başladı", Findings="- bulgu", Next_Step="devam")
    assert ce.fit_daily(daily, 6000) == (
        "## Date\n\n2026-11-14\n\n## Work Log\n\n- başladı\n\n## Findings\n\n- bulgu\n\n## Next Step\n\ndevam")


def test_status_is_kept_once_it_says_something():
    assert "## Status" not in ce.fit_daily(daily_file(), 6000)
    assert "## Status\n\nWaiting Approval" in ce.fit_daily(daily_file(Status="Waiting Approval"), 6000)


def test_task_text_is_kept_only_on_request():
    assert MESSAGE not in ce.fit_daily(daily_file(), 6000)
    assert f"## Task\n\n{MESSAGE}" in ce.fit_daily(daily_file(), 6000, include_task=True)
    assert "## Worker" not in ce.fit_daily(daily_file(), 6000, include_task=True)


def test_work_log_is_cut_to_its_last_lines_first():
    log = "\n".join(f"- adım {i:02d} " + "z" * 50 for i in range(12))
    state = "durum " * 40
    daily = daily_file(Work_Log=log, Findings="- bulgu", Current_State=state)
    whole = ce.fit_daily(daily, 6000)

    fitted = ce.fit_daily(daily, len(whole) - 100)

    assert "(7 eski satır çıkarıldı)\n- adım 07" in fitted
    assert "adım 06" not in fitted and fitted.count("- adım") == ce.WORK_LOG_LINES
    # that was enough: the state is whole
    assert state.strip() in fitted and "kırpıldı" not in fitted
    assert "## Findings\n\n- bulgu" in fitted


def test_current_state_is_cut_to_its_last_part_next():
    log = "\n".join(f"- adım {i}" for i in range(3))
    state = "\n".join(f"durum satırı {i:03d} " + "s" * 40 for i in range(40))
    daily = daily_file(Work_Log=log, Current_State=state)
    room = 1200

    fitted = ce.fit_daily(daily, room)

    assert len(fitted) <= room
    # a short log is not touched
    assert "eski satır" not in fitted and "- adım 0" in fitted
    cut = fitted.split("## Current State\n\n", 1)[1]
    assert cut.startswith("[kırpıldı]\ndurum satırı ")
    assert cut.endswith(state[-200:]) and "durum satırı 000" not in cut


def test_current_state_keeps_a_minimum():
    state = "s" * 2000
    cut = ce.fit_daily(daily_file(Current_State=state), 0).split("## Current State\n\n", 1)[1]
    assert cut == "[kırpıldı]\n" + "s" * ce.MIN_STATE_CHARS


def test_other_daily_sections_are_not_cut():
    daily = daily_file(Findings="b" * 3000, Decisions="k" * 3000)
    fitted = ce.fit_daily(daily, 100)
    assert "b" * 3000 in fitted and "k" * 3000 in fitted


# ----------------------------------------------------------------------
# build_context: priority order
# ----------------------------------------------------------------------

def test_context_layout():
    context = ce.build_context(IDENTITY, daily_file(Findings="- bulgu"), 6000)
    assert context == (
        "# WORKER IDENTITY\n\n" + RULES.rstrip()
        + "\n\n# CURRENT DAILY WORK MEMORY\n\n## Date\n\n2026-11-14\n\n## Findings\n\n- bulgu")


def test_context_without_a_daily_file():
    assert ce.build_context(IDENTITY, None, 6000) == "# WORKER IDENTITY\n\n" + RULES.rstrip()


def test_daily_block_is_left_out_when_nothing_remains():
    daily = "# Task t1\n\n## Worker\n\nares\n\n## Status\n\nIn Progress\n\n## Work Log\n\n"
    assert ce.DAILY_HEADING not in ce.build_context(IDENTITY, daily, 6000)


def test_daily_memory_gives_way_before_persistent_memory():
    decisions = [f"karar {i:02d} " + "x" * 40 for i in range(10)]
    identity = identity_with(decisions=decisions)
    state = "\n".join(f"durum satırı {i:03d} " + "s" * 40 for i in range(60))
    daily = daily_file(Current_State=state)
    budget = len(identity) + 1000

    context = ce.build_context(identity, daily, budget)

    assert len(context) <= budget
    assert all(item in context for item in decisions) and "eski madde" not in context
    assert "[kırpıldı]" in daily_part(context)


def test_task_text_takes_room_from_the_daily_memory_only():
    decisions = [f"karar {i:02d} " + "x" * 40 for i in range(10)]
    identity = identity_with(decisions=decisions)
    state = "\n".join(f"durum satırı {i:03d} " + "s" * 40 for i in range(60))
    daily = daily_file(Current_State=state)
    budget = len(identity) + len(ce.fit_daily(daily, 10 ** 6)) + 200

    without_task = ce.build_context(identity, daily, budget)
    with_task = ce.build_context(identity, daily, budget, task_chars=1500)

    assert "[kırpıldı]" not in without_task
    assert "[kırpıldı]" in daily_part(with_task)
    assert len(with_task) + 1500 <= budget
    # persistent memory outranks the task: nothing was dropped from it
    assert all(item in with_task for item in decisions)
    assert ce.context_over_budget(with_task, budget, 1500) == 0


def test_over_budget_is_reported_not_enforced():
    context = ce.build_context(IDENTITY, daily_file(), 50)

    assert RULES.rstrip() in context
    assert ce.context_over_budget(context, 50) == len(context) - 50
    assert ce.context_over_budget(context, 50, task_chars=30) == len(context) - 20
    assert ce.context_over_budget(context, 6000, task_chars=100) == 0


# ----------------------------------------------------------------------
# WorkerManager.build_worker_context
# ----------------------------------------------------------------------

@pytest.fixture
def manager(tmp_path):
    manager = WorkerManager(tmp_path)
    (tmp_path / "workers" / "ares").mkdir(parents=True)
    (tmp_path / "workers" / "ares" / "ares.md").write_text(IDENTITY, encoding="utf-8")
    manager.create_daily_task("ares", "t1", MESSAGE)
    return manager


def test_without_a_budget_the_files_are_included_whole(manager):
    context = manager.build_worker_context("ares", "t1")

    assert context.startswith("# WORKER IDENTITY\n\n" + IDENTITY)
    assert "## Things To Remember" in context
    assert f"## Task\n\n{MESSAGE}" in context
    for heading in ("# Task t1", "## Worker", "## Status", "## Work Log", "## Next Step"):
        assert heading in context

    omitted = manager.build_worker_context("ares", "t1", include_task=False)
    assert MESSAGE not in omitted and "(Görev metni kullanıcı mesajında.)" in omitted


def test_with_a_budget_the_skeleton_is_left_out(manager):
    context = manager.build_worker_context("ares", "t1", include_task=False, budget_chars=6000)

    assert context.startswith("# WORKER IDENTITY\n\n" + RULES.rstrip() + ce.DAILY_HEADING + "## Date\n\n")
    assert daily_part(context).count("## ") == 1
    assert MESSAGE not in context and "Important Decisions" not in context
    assert len(context) < len(manager.build_worker_context("ares", "t1", include_task=False))


def test_budgeted_context_follows_the_daily_file(manager):
    manager.append_daily_section("ares", "t1", "Findings", "- bulgu bir")
    manager.update_daily_section("ares", "t1", "Status", "Waiting Approval")

    daily = daily_part(manager.build_worker_context("ares", "t1", include_task=False, budget_chars=6000))

    assert "## Findings\n\n- bulgu bir" in daily and "## Status\n\nWaiting Approval" in daily
    assert "## Work Log" not in daily


def test_budgeted_context_without_a_task(manager):
    assert manager.build_worker_context("ares", budget_chars=6000) == "# WORKER IDENTITY\n\n" + RULES.rstrip()
    assert ce.DAILY_HEADING not in manager.build_worker_context("ares", "yok", budget_chars=6000)


# ----------------------------------------------------------------------
# main.py harness
# ----------------------------------------------------------------------

def budget_warnings(orch):
    return [event for event in orch.ACTIVITY if event["event"] == "context_budget"]


def write_identity(orch, worker, text):
    orch.worker_manager.worker_memory_path(worker).write_text(text, encoding="utf-8")


def test_ares_context_is_built_within_its_budget(orch):
    write_identity(orch, "ares", IDENTITY)
    task_id = orch.create_task(MESSAGE, "ares")

    context = orch.get_worker_context(task_id)

    assert context.startswith("# WORKER IDENTITY\n\n" + RULES.rstrip() + ce.DAILY_HEADING + "## Date\n\n")
    assert MESSAGE not in context
    for heading in ("# Task", "## Worker", "## Status", "## Work Log", "## Important Decisions"):
        assert heading not in context
    assert budget_warnings(orch) == []


def test_ares_context_over_budget_is_logged(orch):
    rules = RULES + "## Decision Principles\n\n" + "- ilke\n" * 1200
    write_identity(orch, "ares", rules)
    task_id = orch.create_task(MESSAGE, "ares")

    context = orch.get_worker_context(task_id)

    assert rules.rstrip() in context
    [warning] = budget_warnings(orch)
    assert (warning["task_id"], warning["agent"], warning["status"]) == (task_id, "ares", "exceeded")
    assert warning["budget_chars"] == ce.ARES_CONTEXT_CHARS == 6000
    assert warning["context_chars"] == len(context) and warning["task_chars"] == len(MESSAGE)
    assert warning["over_chars"] == len(context) + len(MESSAGE) - 6000


def test_long_task_text_counts_against_the_budget(orch):
    task_id = orch.create_task("g" * 6500, "ares")
    orch.get_worker_context(task_id)

    [warning] = budget_warnings(orch)
    assert warning["task_chars"] == 6500 and warning["context_chars"] < 100


def test_worker_prompt_carries_the_budgeted_context(orch):
    write_identity(orch, "jarvis", IDENTITY.replace("# Ares", "# JARVIS"))
    asyncio.run(orch.run_worker("jarvis", MESSAGE, "parent-1"))

    messages = orch.fake_client.calls[-1]["json"]["messages"]
    system = messages[0]["content"]

    assert "# WORKER IDENTITY\n\n# JARVIS" in system and "- Kural iki." in system
    assert daily_part(system).startswith("## Date\n\n") and daily_part(system).count("## ") == 1
    for heading in ("## Worker", "## Status", "## Work Log", "## Things To Remember"):
        assert heading not in system
    assert system.count(MESSAGE) == 0 and messages[1]["content"].count(MESSAGE) == 1
    assert budget_warnings(orch) == []


def test_worker_context_over_budget_is_logged_on_the_parent_task(orch):
    write_identity(orch, "jarvis", "# JARVIS\n\n## Coding Standards\n\n" + "- standart\n" * 500)
    asyncio.run(orch.run_worker("jarvis", MESSAGE, "parent-1"))

    [warning] = budget_warnings(orch)
    assert (warning["task_id"], warning["agent"]) == ("parent-1", "jarvis")
    assert warning["budget_chars"] == ce.WORKER_CONTEXT_CHARS == 4000
    assert "- standart\n" * 499 in orch.fake_client.calls[-1]["json"]["messages"][0]["content"]


# ----------------------------------------------------------------------
# fixed prompt size (decision E)
# ----------------------------------------------------------------------

def test_fixed_prompt_stays_small(orch):
    # Measured after the D-5 tightening: 900 + 4,568 (6,500 before it);
    # F-3 remember tool: TOOLS 5,092; F-7 remember rule: SYSTEM_PROMPT 1,032;
    # F-7 key rule in the remember description: TOOLS 5,203; G-2 id + depends_on: 5,412;
    # G-3 review_by: 5,578. 2026-11-16 web rule (current facts -> oracle search): SYSTEM_PROMPT 1396.
    # M-2 code.focus: TOOLS 6,926 (code description shortened). M-3 yigit-calendar repo: approval wording shortened.
    assert len(orch.SYSTEM_PROMPT) <= 1400
    assert len(json.dumps(orch.TOOLS, ensure_ascii=False)) <= 6930


def test_tightened_prompt_keeps_what_the_model_must_know(orch):
    for phrase in ("Approval Layer", "Onay bekleyen bir write tool'u kendin tekrar deneme",
                   "search_notes", "read_note", "list_folder", "delegate"):
        assert phrase in orch.SYSTEM_PROMPT

    tools = {tool["function"]["name"]: tool["function"] for tool in orch.TOOLS}
    for name in ("create_note", "update_note", "move_note"):
        assert "requires approval" in tools[name]["description"]

    workers = tools["delegate_to_worker"]["parameters"]["properties"]["worker"]
    for worker in workers["enum"]:
        assert f"{worker}:" in workers["description"]
