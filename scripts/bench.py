"""
D-1 token benchmark (roadmap Faz D).

Runs on the host; stdlib only. Sends three fixed tasks to the orchestrator's
/task endpoint and builds the token table from usage.jsonl. It never calls a
model itself and needs no key.

    python3 scripts/bench.py plan                         # no LLM call
    python3 scripts/bench.py run D-1 >> docs/bench.md     # about 9 LLM calls
    python3 scripts/bench.py report D-1 <id-a> <id-b> <id-c>   # no LLM call

plan: prints the tasks and the calls each one is expected to make.
run: one POST /task per entry in TASKS, in order; a refused task (429) does
not stop the others. report: rebuilds the table for task ids from an earlier
run. Output: Markdown on stdout, progress on stderr.

The tasks are fixed so that two runs can be compared; do not edit them
between the first and the last run of a phase.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:18000")
USAGE_FILE = Path(os.getenv(
    "BENCH_USAGE_FILE",
    str(Path.home() / "Obsidian/SavasBrain/SAVAS-AI/usage/usage.jsonl"),
))
# A local-LLM fallback (C-8) can take minutes per call.
TASK_TIMEOUT = 3600

TASKS = [
    {
        "key": "a",
        "name": "tek Ares cevabı",
        "ares_calls": 1,
        "worker_calls": 0,
        "message": (
            "Araç kullanmadan ve kimseye delege etmeden kendin yanıtla: "
            "Bir REST API'de 401 ile 403 durum kodlarının farkını üç cümleyle açıkla."
        ),
    },
    {
        "key": "b",
        "name": "tek worker delegasyonu",
        "ares_calls": 2,
        "worker_calls": 1,
        "message": (
            "Bu görevi JARVIS'e delege et: Python'da bir listenin tekrar eden elemanlarını "
            "sırayı koruyarak kaldıran dedupe(items) fonksiyonunu yazsın ve nasıl çalıştığını "
            "açıklasın. Sonra JARVIS'in cevabını bana ilet."
        ),
    },
    {
        "key": "c",
        "name": "3 worker'lı paralel görev",
        "ares_calls": 2,
        "worker_calls": 3,
        "message": (
            "Şu soruyu delegate_to_workers ile üç worker'a paralel delege et, sonra cevaplarını "
            "tek bir öneride sentezle. Soru: Tek kullanıcılı bir kişisel not uygulamasında "
            "notlar SQLite veritabanında mı yoksa düz Markdown dosyalarında mı saklanmalı? "
            "JARVIS teknik uygulama açısından, Oracle araştırma ve uzun vadeli riskler "
            "açısından, Gordon eleştirel gözden geçirme açısından değerlendirsin. "
            "Vault'ta not arama veya not yazma."
        ),
    },
]


def read_usage(path: Path = USAGE_FILE) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def summarize(records: list[dict]) -> dict:
    ares = [r for r in records if r.get("agent") == "ares"]
    prompt = sum(int(r.get("prompt_tokens") or 0) for r in records)
    completion = sum(int(r.get("completion_tokens") or 0) for r in records)
    return {
        "calls": len(records),
        "ares_calls": len(ares),
        "worker_calls": len(records) - len(ares),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": sum(int(r.get("total_tokens") or 0) for r in records),
        # The last Ares call of a delegated task is the synthesis.
        "ares_last_prompt": int(ares[-1].get("prompt_tokens") or 0) if ares else None,
        "models": sorted({r.get("model") or "?" for r in records}),
        # Records written before C-8.2 have no location: cloud.
        "locations": sorted({r.get("location") or "cloud" for r in records}),
        "failed": sum(1 for r in records if r.get("status") == "failed"),
    }


def pattern_ok(summary: dict, task: dict) -> bool:
    return (
        summary["ares_calls"] == task["ares_calls"]
        and summary["worker_calls"] == task["worker_calls"]
        and summary["failed"] == 0
    )


def post_task(message: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{ORCHESTRATOR_URL}/task",
        data=json.dumps({"message": message}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TASK_TIMEOUT) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"detail": body[:200]}


def health_line() -> str:
    try:
        with urllib.request.urlopen(f"{ORCHESTRATOR_URL}/health", timeout=10) as response:
            health = json.loads(response.read())
    except (OSError, ValueError) as exc:
        return f"`/health` okunamadı: {exc}"
    routes = ", ".join(f"{agent}={model}" for agent, model in sorted(health.get("routes", {}).items()))
    on_local = (health.get("local_llm") or {}).get("agents_on_local") or []
    return f"rotalar: {routes}; local'e yönlenen: {', '.join(on_local) or 'yok'}"


def task_records(usage: list[dict], task_id: str | None) -> list[dict]:
    return [r for r in usage if task_id and r.get("task_id") == task_id]


def run(post=post_task, read_usage=read_usage) -> list[dict]:
    results = []
    for task in TASKS:
        print(f"[{task['key']}] {task['name']} ...", file=sys.stderr)
        started = time.monotonic()
        http, body = post(task["message"])
        seconds = time.monotonic() - started
        task_id = body.get("task_id")
        results.append({
            "key": task["key"],
            "task_id": task_id,
            "http": http,
            "status": str(body.get("status") or body.get("detail") or "?"),
            "seconds": seconds,
            "records": task_records(read_usage(), task_id),
        })
        print(f"[{task['key']}] HTTP {http}, {seconds:.1f} s", file=sys.stderr)
    return results


def report(task_ids: list[str], read_usage=read_usage) -> list[dict]:
    usage = read_usage()
    return [
        {"key": task["key"], "task_id": task_id, "http": None, "status": "kayıttan",
         "seconds": None, "records": task_records(usage, task_id)}
        for task, task_id in zip(TASKS, task_ids)
    ]


def render(label: str, results: list[dict], when: str, note: str = "") -> str:
    tasks = {task["key"]: task for task in TASKS}
    lines = [f"### {label} — {when}", ""]
    if note:
        lines += [f"Çalıştırma öncesi {note}", ""]
    lines += [
        "| Görev | Çağrı (Ares + worker) | Prompt | Completion | Toplam | Ares son prompt | Süre (s) | Konum | Sonuç |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    grand_total = 0
    for result in results:
        task = tasks[result["key"]]
        summary = summarize(result["records"])
        grand_total += summary["total_tokens"]
        ok = pattern_ok(summary, task) and result["http"] in (None, 200)
        outcome = result["status"] if result["http"] in (None, 200) else f"HTTP {result['http']}: {result['status']}"
        seconds = "-" if result["seconds"] is None else f"{result['seconds']:.1f}"
        last_prompt = "-" if summary["ares_last_prompt"] is None else summary["ares_last_prompt"]
        lines.append(
            f"| {task['key']} · {task['name']} | {summary['ares_calls']} + {summary['worker_calls']} "
            f"(beklenen {task['ares_calls']} + {task['worker_calls']}) | {summary['prompt_tokens']} | "
            f"{summary['completion_tokens']} | {summary['total_tokens']} | {last_prompt} | {seconds} | "
            f"{', '.join(summary['locations']) or '-'} | {'✅' if ok else '⚠️'} {outcome} |"
        )
    lines += ["", f"Üç görevin toplamı: **{grand_total}** token.", "",
              "| Çağrı | Agent | Model | Konum | Prompt | Completion | Toplam | ms | Durum |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for result in results:
        for index, r in enumerate(result["records"], start=1):
            lines.append(
                f"| {result['key']}/{index} | {r.get('agent')} | {r.get('model')} | {r.get('location') or 'cloud'} | "
                f"{r.get('prompt_tokens')} | {r.get('completion_tokens')} | {r.get('total_tokens')} | "
                f"{r.get('duration_ms')} | {r.get('status') or 'ok'} |"
            )
    lines += ["", "Görev kimlikleri: " + ", ".join(f"{r['key']}=`{r['task_id']}`" for r in results), ""]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "plan"
    when = datetime.now().strftime("%Y-%m-%d %H:%M")

    if command == "plan":
        for task in TASKS:
            print(f"{task['key']} · {task['name']}: {task['ares_calls']} Ares + "
                  f"{task['worker_calls']} worker çağrısı\n    {task['message']}")
        print(f"Toplam beklenen çağrı: {sum(t['ares_calls'] + t['worker_calls'] for t in TASKS)}")
        return 0

    if command == "run" and len(argv) == 3:
        note = health_line()
        print(render(argv[2], run(), when, note))
        return 0

    if command == "report" and len(argv) == 3 + len(TASKS):
        print(render(argv[2], report(argv[3:]), when))
        return 0

    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
