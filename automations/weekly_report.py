#!/usr/bin/env python3
"""
Weekly report (J-2, plan-j L). No LLM.

The last 7 local days from the daily snapshots (daily_summary.py) and the
task list -> data/automations/reports/YYYY-Www.md, a push with the
totals, and the same text proposed as an Obsidian note (written only when
the user approves it; plan-j L, karar b).
"""

from datetime import date, timedelta

import lib

NAME = "weekly-report"
TASK_LIMIT = 500
NOTE_DIR = "SAVAS-AI/Raporlar"


def week_days(today: date) -> list[str]:
    return [(today - timedelta(days=n)).isoformat() for n in range(6, -1, -1)]


def report(days: list[str], snapshots: dict, tasks: list[dict]) -> tuple[str, dict]:
    week = [t for t in tasks if lib.local_date(t.get("created_at")) in days]
    failed = [t for t in week if t["status"] == "failed"]
    cloud = sum(s["cloud_tokens"] for s in snapshots.values())
    local = sum(s["local_tokens"] for s in snapshots.values())

    agents = {}
    for s in snapshots.values():
        for name, a in s.get("agents", {}).items():
            total = agents.setdefault(name, {"calls": 0, "cloud_tokens": 0, "local_tokens": 0})
            for key in total:
                total[key] += a.get(key, 0)

    lines = [
        f"# SAVAS AI haftalık rapor · {days[0]} – {days[-1]}",
        "",
        "Otomatik üretildi (LLM yok). Token sayıları günlük anlık görüntülerden; anlık görüntüsü olmayan gün \"–\".",
        "",
        "## Günler",
        "",
        "| Gün | Görev | Başarısız | Bulut token | Yerel token |",
        "|---|---|---|---|---|",
    ]
    for day in days:
        day_tasks = [t for t in week if lib.local_date(t.get("created_at")) == day]
        s = snapshots.get(day)
        lines.append(f"| {day} | {len(day_tasks)} | {sum(t['status'] == 'failed' for t in day_tasks)} | "
                     f"{s['cloud_tokens'] if s else '–'} | {s['local_tokens'] if s else '–'} |")
    lines += ["", f"**Toplam:** {len(week)} görev, {len(failed)} başarısız, {cloud} bulut + {local} yerel token "
                  f"({len(snapshots)}/7 gün ölçüldü).", "", "## Ajanlar", "",
              "| Ajan | Çağrı | Bulut token | Yerel token |", "|---|---|---|---|"]
    for name, a in sorted(agents.items(), key=lambda item: -item[1]["cloud_tokens"]):
        lines.append(f"| {name} | {a['calls']} | {a['cloud_tokens']} | {a['local_tokens']} |")
    lines += ["", "## Başarısız görevler", ""]
    lines += [f"- {lib.local_date(t['created_at'])} · {' '.join((t.get('message') or '').split())[:100]} — "
              f"{' '.join((t.get('error') or '').split())[:120]}" for t in failed] or ["Yok."]

    totals = {"tasks": len(week), "failed": len(failed), "cloud_tokens": cloud, "local_tokens": local,
              "measured_days": len(snapshots)}
    return "\n".join(lines) + "\n", totals


def run():
    today = lib.now_local().date()
    days = week_days(today)
    snapshots = {d: s for d in days if (s := lib.State(f"daily/{d}").load())}
    tasks = lib.api(f"/tasks?limit={TASK_LIMIT}").get("tasks", [])
    text, totals = report(days, snapshots, tasks)

    year, week, _ = today.isocalendar()
    path = lib.STATE_DIR / "reports" / f"{year}-W{week:02d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

    body = (f"{totals['tasks']} görev ({totals['failed']} başarısız) · "
            f"{totals['cloud_tokens']:,} bulut token".replace(",", "."))

    # One proposal per week: a second run (a retry, Persistent=true) does not ask again.
    state = lib.State(NAME)
    url = "/ui/"
    if state.load().get("proposed") != path.stem:
        lib.propose_note(f"{NOTE_DIR}/Haftalık {path.stem}.md", text, f"Haftalık rapor {path.stem}")
        state.save({"proposed": path.stem})
        body, url = body + " · not onay bekliyor", "/ui/#approvals"
    lib.notify("SAVAS AI · Haftalık rapor", body, url, "weekly-report")
    lib.log(NAME, f"{path.name}: {body}")


if __name__ == "__main__":
    lib.main(NAME, run)
