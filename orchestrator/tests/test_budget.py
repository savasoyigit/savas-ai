import asyncio

import pytest

from budget_manager import BudgetExceeded


def usage(tokens):
    return {
        "prompt_tokens": tokens,
        "completion_tokens": 0,
        "total_tokens": tokens,
    }


async def fake_worker(token_manager, budget_manager, worker_id, fail=False):
    # Mirrors the production path in main.py:run_worker:
    # reserve -> await LLM call -> record_usage -> release
    budget = await budget_manager.reserve(
        agent="gandalf",
        task_id="parent",
        requested_tokens=3000,
    )

    try:
        await asyncio.sleep(0)

        if fail:
            raise RuntimeError("LLM call failed")

        token_manager.record_usage(
            task_id="parent",
            agent="gandalf",
            worker="gandalf",
            worker_task_id=worker_id,
            model="test",
            usage=usage(3000),
        )
    finally:
        budget_manager.release(budget["reservation_id"])


def run_parallel(token_manager, budget_manager, count):
    async def main():
        return await asyncio.gather(
            *(
                fake_worker(token_manager, budget_manager, f"w{i}")
                for i in range(count)
            ),
            return_exceptions=True,
        )

    return asyncio.run(main())


def test_parallel_workers_respect_budget(managers):
    token_manager, budget_manager = managers

    results = run_parallel(token_manager, budget_manager, 3)

    accepted = [r for r in results if r is None]
    blocked = [r for r in results if isinstance(r, BudgetExceeded)]

    assert len(accepted) == 1
    assert len(blocked) == 2
    assert budget_manager.agent_used_today("gandalf") <= 5000


def test_sequential_blocks(managers):
    token_manager, budget_manager = managers

    run_parallel(token_manager, budget_manager, 1)

    with pytest.raises(BudgetExceeded):
        budget_manager.enforce(
            agent="gandalf",
            task_id="parent",
            requested_tokens=3000,
        )


def test_release_on_exception(managers):
    token_manager, budget_manager = managers

    with pytest.raises(RuntimeError):
        asyncio.run(
            fake_worker(token_manager, budget_manager, "w0", fail=True)
        )

    assert budget_manager._reserved == {}
    # Budget is available again after the failed call.
    assert run_parallel(token_manager, budget_manager, 1) == [None]


def test_day_boundary_istanbul(managers, monkeypatch):
    import json
    from datetime import datetime, timezone

    import budget_manager as budget_module

    token_manager, budget_manager = managers

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # 2026-11-14 00:30 in Europe/Istanbul (UTC+3)
            frozen = datetime(2026, 11, 13, 21, 30, tzinfo=timezone.utc)
            return frozen.astimezone(tz) if tz else frozen

    monkeypatch.setattr(budget_module, "datetime", FrozenDatetime)

    records = [
        # 2026-11-13 23:00 Istanbul -> previous budget day
        ("2026-11-13T20:00:00+00:00", 1000),
        # 2026-11-14 00:15 Istanbul -> current budget day
        ("2026-11-13T21:15:00+00:00", 200),
        # Naive timestamps are treated as UTC (2026-11-14 00:20 Istanbul)
        ("2026-11-13T21:20:00", 30),
    ]

    token_manager.usage_file.parent.mkdir(parents=True, exist_ok=True)

    with token_manager.usage_file.open("w") as handle:
        for timestamp, tokens in records:
            handle.write(json.dumps({
                "timestamp": timestamp,
                "task_id": "t",
                "agent": "gandalf",
                "total_tokens": tokens,
            }) + "\n")

    assert budget_manager.agent_used_today("gandalf") == 230
    assert budget_manager.total_used_today() == 230


def test_failed_usage_counts(managers):
    token_manager, budget_manager = managers

    # Error/timeout path: estimated prompt tokens are recorded
    # as "failed" and still count against the budget.
    record = token_manager.record_usage(
        task_id="parent",
        agent="gandalf",
        model="test",
        usage={"prompt_tokens": 4000, "completion_tokens": 0},
        status="failed",
    )

    assert record["status"] == "failed"
    assert record["total_tokens"] == 4000
    assert budget_manager.agent_used_today("gandalf") == 4000

    with pytest.raises(BudgetExceeded):
        budget_manager.enforce(
            agent="gandalf",
            task_id="other",
            requested_tokens=1500,
        )


def test_usage_status_defaults_to_ok(managers):
    token_manager, _ = managers

    record = token_manager.record_usage(
        task_id="t",
        agent="ares",
        model="test",
        usage=usage(10),
    )

    assert record["status"] == "ok"
