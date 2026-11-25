"""
L-1a metrics report: token use, errors and model success over the last
days. Pure functions over what is already recorded (plan-l A): usage.jsonl
records for tokens and calls, task rows and events for quality signals.
Nothing is stored or estimated; what cannot be linked is counted apart.
"""

from datetime import date, datetime, timedelta, timezone
from statistics import median

LOCAL = "local"
DAY_CHOICES = (1, 7, 30)
TOP_TASKS = 10
# Fewer samples than this get a "few data" mark (plan-l D).
FEW_SAMPLES = 10
# Events read for quality signals and error counts.
EVENTS = ("review", "code_edit", "code_result", "claim_check", "llm_fallback",
          "budget_check", "context_budget", "jobs")
TASK_STATUSES = ("completed", "needs_review", "failed", "blocked", "cancelled", "rejected", "running", "queued")


def window_start(days: int, today: date, tz) -> datetime:
    """Midnight (budget time zone) of the first day in the window, as UTC."""
    first = today - timedelta(days=days - 1)
    return datetime(first.year, first.month, first.day, tzinfo=tz).astimezone(timezone.utc)


def parse_time(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # usage.jsonl timestamps are UTC; naive ones are treated as UTC too.
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def is_cloud(record: dict) -> bool:
    return record.get("location") != LOCAL


def tokens(record: dict) -> int:
    return int(record.get("total_tokens", 0) or 0)


def enough(count: int) -> dict:
    return {"n": count, "few": count < FEW_SAMPLES}


def rate(part: int, whole: int) -> float | None:
    return round(part * 100 / whole, 1) if whole else None


def root_of(task_id: str, parents: dict) -> str:
    seen = set()
    while parents.get(task_id) and task_id not in seen:
        seen.add(task_id)
        task_id = parents[task_id]
    return task_id


def token_section(usage: list[dict], tasks: list[dict], days: list[str], day_of) -> dict:
    by_day = {day: {"day": day, "cloud": 0, "local": 0} for day in days}
    by_pair = {}
    for record in usage:
        where = "cloud" if is_cloud(record) else "local"
        by_day[day_of(record)][where] += tokens(record)
        key = (record.get("agent") or "?", record.get("model") or "?", where)
        pair = by_pair.setdefault(key, {"agent": key[0], "model": key[1], "location": where, "calls": 0, "tokens": 0})
        pair["calls"] += 1
        pair["tokens"] += tokens(record)

    # A top-level task counts its own calls and its sub-tasks' (parent_id).
    known = {task["task_id"]: task for task in tasks}
    parents = {task["task_id"]: task.get("parent_id") for task in tasks}
    per_root, unlinked = {}, 0
    for record in usage:
        task_id = record.get("task_id")
        if task_id not in known:
            unlinked += 1
            continue
        root = root_of(task_id, parents)
        per_root[root] = per_root.get(root, 0) + tokens(record)

    per_kind = {}
    for root, used in per_root.items():
        per_kind.setdefault(known[root].get("kind") or "?", []).append(used)

    top = sorted(per_root.items(), key=lambda item: item[1], reverse=True)[:TOP_TASKS]
    return {
        "cloud": sum(tokens(r) for r in usage if is_cloud(r)),
        "local": sum(tokens(r) for r in usage if not is_cloud(r)),
        "calls": len(usage),
        "by_day": list(by_day.values()),
        "by_agent_model": sorted(by_pair.values(), key=lambda x: x["tokens"], reverse=True),
        "per_kind": {
            kind: {"tasks": len(used), "average": round(sum(used) / len(used)), "median": round(median(used))}
            for kind, used in sorted(per_kind.items())
        },
        "top_tasks": [
            {"task_id": root, "kind": known[root].get("kind"), "status": known[root].get("status"),
             "message": known[root].get("message") or "", "tokens": used}
            for root, used in top
        ],
        "unlinked_calls": unlinked,
    }


def error_section(usage: list[dict], tasks: list[dict], events: list[dict]) -> dict:
    calls = {}
    for record in usage:
        model = calls.setdefault(record.get("model") or "?", {"calls": 0, "failed": 0})
        model["calls"] += 1
        model["failed"] += record.get("status", "ok") != "ok"
    for model in calls.values():
        model["failed_percent"] = rate(model["failed"], model["calls"])

    kinds = {}
    for task in tasks:
        if task.get("parent_id"):
            continue
        row = kinds.setdefault(task.get("kind") or "?", {"total": 0, **{s: 0 for s in TASK_STATUSES}})
        row["total"] += 1
        if task.get("status") in row:
            row[task["status"]] += 1

    def count(event: str, status: str | None = None) -> int:
        return sum(e["event"] == event and (status is None or e.get("status") == status) for e in events)

    return {
        "calls_by_model": dict(sorted(calls.items())),
        "tasks_by_kind": dict(sorted(kinds.items())),
        "fallbacks": count("llm_fallback", "started"),
        "budget_refusals": count("budget_check", "blocked"),
        "context_over_budget": count("context_budget", "exceeded"),
    }


def model_section(usage: list[dict], events: list[dict], approvals: list[dict]) -> dict:
    models = {}
    for record in usage:
        model = models.setdefault(record.get("model") or "?", {
            "calls": 0, "ok": 0, "tokens": 0, "duration_ms": 0, "timed": 0,
            "review_approve": 0, "review_other": 0, "code_ok": 0, "code_open": 0, "unsupported_claims": 0,
        })
        model["calls"] += 1
        model["ok"] += record.get("status", "ok") == "ok"
        model["tokens"] += tokens(record)
        if record.get("duration_ms"):
            model["duration_ms"] += int(record["duration_ms"])
            model["timed"] += 1

    # Which model did an agent's work in a task: the one it used most there.
    used = {}
    for record in usage:
        key = (record.get("task_id"), record.get("agent") or record.get("worker"))
        counter = used.setdefault(key, {})
        counter[record.get("model") or "?"] = counter.get(record.get("model") or "?", 0) + 1

    def model_of(task_id, agent):
        counter = used.get((task_id, agent))
        return max(counter, key=counter.get) if counter else None

    coder = {e["task_id"]: e.get("worker") for e in events if e["event"] == "code_edit"}
    unlinked = 0
    for event in events:
        if event["event"] == "review" and event.get("status") in ("done", "needs_review") and event.get("worker"):
            target, field = model_of(event["task_id"], event["worker"]), (
                "review_approve" if event.get("verdict") == "approve" else "review_other")
        elif event["event"] == "code_result":
            target, field = model_of(event["task_id"], coder.get(event["task_id"])), (
                "code_ok" if event.get("status") == "ok" else "code_open")
        elif event["event"] == "claim_check" and event.get("status") == "unsupported":
            target, field = model_of(event["task_id"], "ares"), "unsupported_claims"
        else:
            continue
        if target in models:
            models[target][field] += 1
        else:
            unlinked += 1

    rows = []
    for name, model in sorted(models.items()):
        reviews = model["review_approve"] + model["review_other"]
        codes = model["code_ok"] + model["code_open"]
        rows.append({
            "model": name, "calls": model["calls"], "ok_percent": rate(model["ok"], model["calls"]),
            "average_ms": round(model["duration_ms"] / model["timed"]) if model["timed"] else None,
            "tokens_per_call": round(model["tokens"] / model["calls"]),
            "reviews": {**enough(reviews), "approve_percent": rate(model["review_approve"], reviews)},
            "code": {**enough(codes), "ok_percent": rate(model["code_ok"], codes)},
            "unsupported_claims": model["unsupported_claims"],
            "sample": enough(model["calls"]),
        })

    packages = [e for e in events if e["event"] == "jobs" and (e.get("status") or "").startswith("package_")]
    done = [e for e in packages if e["status"] not in ("package_started",)]
    fixes = sum(e["status"] == "package_needs_fix" for e in done)
    decided = [a for a in approvals if a.get("status") in ("approved", "executed", "failed", "rejected")]
    rejected = sum(a["status"] == "rejected" for a in decided)
    return {
        "models": rows,
        "unlinked_signals": unlinked,
        "packages": {**enough(len(done)), "needs_fix": fixes, "needs_fix_percent": rate(fixes, len(done))},
        "approvals": {**enough(len(decided)), "rejected": rejected, "rejected_percent": rate(rejected, len(decided))},
    }


def report(days: int, usage: list[dict], rows: dict, today: date, tz) -> dict:
    """
    usage: usage.jsonl records (any dates); rows: task_store.metric_rows()
    for the same window. Days follow the budget time zone.
    """
    start = window_start(days, today, tz)
    inside = []
    for record in usage:
        moment = parse_time(record.get("timestamp"))
        if moment and moment >= start:
            inside.append({**record, "_day": moment.astimezone(tz).date().isoformat()})
    day_names = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
    return {
        "days": days,
        "from": day_names[0],
        "to": day_names[-1],
        "tokens": token_section(inside, rows["tasks"], day_names, lambda r: r["_day"]),
        "errors": error_section(inside, rows["tasks"], rows["events"]),
        "quality": model_section(inside, rows["events"], rows["approvals"]),
        "note": "Kalite sinyalleri sistemin kendi yargısıdır (Gordon bozuk işi onaylayabildi, M-3); "
                f"{FEW_SAMPLES}'dan az örnek 'az veri' sayılır.",
    }
