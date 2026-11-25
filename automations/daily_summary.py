#!/usr/bin/env python3
"""
Evening summary (J-2, plan-j L). No LLM.

  daily_summary.py             push today's summary once a day, keep a snapshot
  daily_summary.py --snapshot  only keep the snapshot (late run, no push)

Snapshots (data/automations/daily/YYYY-MM-DD.json) feed weekly_report.py.
"""

import sys

import lib

NAME = "daily-summary"
TASK_LIMIT = 200


def summary(usage: dict, tasks: list[dict], pending: int, day: str) -> dict:
    today = [t for t in tasks if lib.local_date(t.get("created_at")) == day]
    by_status = {}
    for task in today:
        by_status[task["status"]] = by_status.get(task["status"], 0) + 1
    return {
        "date": day,
        "tasks": len(today),
        "by_status": by_status,
        "cloud_tokens": usage["system"]["used"],
        "cloud_budget": usage["system"]["budget"],
        "local_tokens": usage["system"]["local_tokens"],
        "agents": {name: {"calls": a["calls"], "cloud_tokens": a["cloud_tokens"], "local_tokens": a["local_tokens"]}
                   for name, a in usage.get("agents", {}).items()},
        "pending_approvals": pending,
    }


def message(s: dict) -> str:
    failed = s["by_status"].get("failed", 0)
    parts = [f"{s['tasks']} görev" + (f" ({failed} başarısız)" if failed else ""),
             f"{s['cloud_tokens']:,}/{s['cloud_budget']:,} bulut token".replace(",", ".")]
    if s["local_tokens"]:
        parts.append(f"{s['local_tokens']:,} yerel".replace(",", "."))
    if s["pending_approvals"]:
        parts.append(f"{s['pending_approvals']} onay bekliyor")
    return " · ".join(parts)


def run(snapshot_only: bool):
    usage = lib.api("/usage/today")
    day = usage["date"]  # the budget day (local), the same day the numbers belong to
    tasks = lib.api(f"/tasks?limit={TASK_LIMIT}").get("tasks", [])
    pending = lib.api("/approvals?status=pending").get("count", 0)
    s = summary(usage, tasks, pending, day)

    snapshot = lib.State(f"daily/{day}")
    snapshot.save(s)

    state = lib.State(NAME)
    if snapshot_only or state.load().get("sent") == day:
        lib.log(NAME, f"snapshot {day} saved")
        return
    url = "/ui/#tasks" if s["by_status"].get("failed") else "/ui/"
    lib.notify("SAVAS AI · Günün özeti", message(s), url, "daily-summary")
    state.save({"sent": day})
    lib.log(NAME, f"sent {day}: {message(s)}")


if __name__ == "__main__":
    lib.main(NAME, lambda: run("--snapshot" in sys.argv[1:]))
