"""
C-2 model discovery (roadmap Faz C).

Runs inside the orchestrator container (needs OMNIROUTE_URL / OMNIROUTE_KEY);
stdlib only. The key is never printed.

    docker exec -i ai-orchestrator python - catalog gh/ < scripts/discover_models.py
    docker exec -i ai-orchestrator python - smoke gh/a gh/b < scripts/discover_models.py
    docker exec -i ai-orchestrator python - tools gh/a < scripts/discover_models.py
    docker exec -i ai-orchestrator python - code gh/a < scripts/discover_models.py > answers.jsonl
    python3 scripts/discover_models.py grade answers.jsonl

catalog: GET /models, no LLM call. smoke: one chat call per model,
max_tokens=5, at most MAX_SMOKE models. tools: one chat call with a
single tool (C-7), at most MAX_TOOL_CHECK models. code: one chat call per
task in CODE_TASKS (C-7.1), at most MAX_CODE_CHECK models. Output: JSON lines.

grade runs on the host, never in the container: it executes the answers
from "code" against the task cases in a separate, isolated Python process
(import whitelist, timeout, empty working directory). No LLM call.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

MAX_SMOKE = 6
SMOKE_MAX_TOKENS = 5
MAX_TOOL_CHECK = 1
TOOL_MAX_TOKENS = 60
TOOL_NAME = "echo_word"
MAX_CODE_CHECK = 1
CODE_MAX_TOKENS = int(os.environ.get("DISCOVER_CODE_MAX_TOKENS", "500"))  # thinking models spend tokens before the answer
CODE_TIMEOUT = 5
TIMEOUT = float(os.environ.get("DISCOVER_TIMEOUT", "60"))  # local LLM (C-8.1) needs minutes

CODE_SYSTEM = "Reply with exactly one Python code block and nothing else. Standard library only."

# C-7.1 coding tasks. cases: [args, expected]; expected {"raises": name}
# means the call must raise that exception. Arguments must stay unchanged.
CODE_TASKS = [
    {
        "name": "parse_duration",
        "prompt": (
            "`parse_duration(text)` adında bir Python fonksiyonu yaz. Girdi '1h30m', '45s', '2h5s' "
            "gibi bir süre metnidir: saat (h), dakika (m) ve saniye (s) parçaları bu sırayla gelir; "
            "her parça isteğe bağlıdır ama en az biri bulunmalıdır. Toplam süreyi saniye cinsinden "
            "int olarak döndür. Boş ya da bu kurala uymayan metinde ValueError fırlat."
        ),
        "cases": [
            [["1h30m"], 5400], [["45s"], 45], [["2h5s"], 7205], [["1h2m3s"], 3723],
            [["90m"], 5400], [["0s"], 0],
            [[""], {"raises": "ValueError"}], [["abc"], {"raises": "ValueError"}],
            [["30m1h"], {"raises": "ValueError"}], [["1h30"], {"raises": "ValueError"}],
        ],
    },
    {
        "name": "merge_intervals",
        "prompt": (
            "`merge_intervals(intervals)` adında bir Python fonksiyonu yaz. Girdi, [başlangıç, bitiş] "
            "çiftlerinden oluşan sırasız bir listedir (başlangıç <= bitiş). Çakışan ya da birbirine "
            "değen aralıkları (ör. [1, 2] ve [2, 3]) birleştir. Sonucu başlangıca göre sıralı, "
            "[başlangıç, bitiş] listelerinden oluşan yeni bir liste olarak döndür. Girdi listesini "
            "değiştirme."
        ),
        "cases": [
            [[[]], []],
            [[[[1, 3], [2, 6], [8, 10], [15, 18]]], [[1, 6], [8, 10], [15, 18]]],
            [[[[1, 2], [2, 3]]], [[1, 3]]],
            [[[[5, 7], [1, 2]]], [[1, 2], [5, 7]]],
            [[[[1, 10], [2, 3], [4, 5]]], [[1, 10]]],
            [[[[1, 4], [0, 0]]], [[0, 0], [1, 4]]],
            [[[[3, 4], [1, 2], [2, 3]]], [[1, 4]]],
        ],
    },
    {
        "name": "first_index",
        "prompt": (
            "Aşağıdaki fonksiyon, sıralı bir listede `target` değerinin ilk geçtiği indeksi, değer "
            "yoksa -1 döndürmeli; ama hatalı. Hataları düzelt ve fonksiyonun tamamını yeniden yaz. "
            "Girdi listesini değiştirme.\n\n"
            "def first_index(items, target):\n"
            "    lo, hi = 0, len(items)\n"
            "    while lo < hi:\n"
            "        mid = (lo + hi) // 2\n"
            "        if items[mid] < target:\n"
            "            lo = mid\n"
            "        else:\n"
            "            hi = mid\n"
            "    return lo if items[lo] == target else -1\n"
        ),
        "cases": [
            [[[1, 2, 2, 3], 2], 1], [[[], 1], -1], [[[1, 3, 5], 6], -1], [[[1, 3, 5], 0], -1],
            [[[2, 2, 2], 2], 0], [[[1, 3, 5], 5], 2], [[[1, 3, 5], 4], -1],
        ],
    },
]

SAFE_IMPORTS = {"__future__", "bisect", "collections", "dataclasses", "functools", "heapq",
                "itertools", "math", "re", "string", "typing"}
UNSAFE_NAMES = {"__import__", "breakpoint", "compile", "delattr", "eval", "exec", "getattr",
                "globals", "input", "locals", "open", "setattr", "vars"}

# Runs in its own process: job on stdin, list of failure texts on stdout.
CASE_RUNNER = r'''
import contextlib, copy, io, json, sys
job = json.load(sys.stdin)
failures = []
with contextlib.redirect_stdout(io.StringIO()):
    namespace = {}
    exec(job["code"], namespace)
    func = namespace.get(job["function"])
    if not callable(func):
        failures.append("function " + job["function"] + " is not defined")
    else:
        for index, (args, expected) in enumerate(job["cases"]):
            given = copy.deepcopy(args)
            try:
                outcome = json.loads(json.dumps(func(*given)))
            except Exception as exc:
                names = [cls.__name__ for cls in type(exc).__mro__]
                wanted = expected.get("raises") if isinstance(expected, dict) else None
                outcome = {"raises": wanted if wanted in names else names[0]}
            if outcome != expected:
                failures.append(f"case {index} {args!r}: expected {expected!r}, got {outcome!r}")
            elif given != args:
                failures.append(f"case {index} {args!r}: input mutated")
print(json.dumps(failures))
'''


def base_url() -> str:
    return os.environ["OMNIROUTE_URL"].rstrip("/")


def api_key() -> str:
    return os.environ.get("OMNIROUTE_KEY", "")


def clean_error(text: str, key: str | None = None) -> str:
    key = api_key() if key is None else key
    if key:
        text = text.replace(key, "***")
    return " ".join(str(text).split())[:200]


def request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{base_url()}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"error": {"message": body}}
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, {"error": {"message": f"{type(exc).__name__}: {exc}"}}


def catalog_row(model: dict) -> dict:
    caps = model.get("capabilities") or {}
    return {
        "id": model.get("id"),
        "tool_calling": caps.get("tool_calling"),
        "reasoning": caps.get("reasoning"),
        "vision": caps.get("vision"),
        "context_length": model.get("context_length"),
        "max_output_tokens": model.get("max_output_tokens"),
    }


def catalog(prefix: str = "") -> list[dict]:
    status, body = request("GET", "/models")
    if status != 200:
        raise SystemExit(f"/models -> {status}: {clean_error(json.dumps(body))}")
    return [
        catalog_row(m)
        for m in body.get("data", [])
        if str(m.get("id", "")).startswith(prefix)
    ]


def default_post(model_id: str, payload: dict) -> tuple[int, dict]:
    return request("POST", "/chat/completions", payload)


def smoke(model_ids: list[str], post=default_post) -> list[dict]:
    if len(model_ids) > MAX_SMOKE:
        raise SystemExit(f"at most {MAX_SMOKE} models per run (got {len(model_ids)})")

    results = []
    for model_id in model_ids:
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Reply with: ok"}],
            "max_tokens": SMOKE_MAX_TOKENS,
        }
        started = time.monotonic()
        status, body = post(model_id, payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        choices = body.get("choices") or [{}]
        error = body.get("error")
        if isinstance(error, dict):
            error = error.get("message") or json.dumps(error)
        ok = status == 200 and bool(body.get("choices"))

        results.append({
            "id": model_id,
            "ok": ok,
            "status": status,
            "latency_ms": latency_ms,
            "returned_model": body.get("model"),
            "finish_reason": choices[0].get("finish_reason"),
            "total_tokens": (body.get("usage") or {}).get("total_tokens"),
            "error": clean_error(error) if (error and not ok) else ("" if ok else "no choices"),
        })
    return results


def tool_check(model_ids: list[str], post=default_post) -> list[dict]:
    """Does the model answer with a tool call when offered one tool?"""
    if len(model_ids) > MAX_TOOL_CHECK:
        raise SystemExit(f"at most {MAX_TOOL_CHECK} model per run (got {len(model_ids)})")

    results = []
    for model_id in model_ids:
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": f"Call the tool {TOOL_NAME} with word 'ok'."}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": TOOL_NAME,
                    "description": "Echo one word back.",
                    "parameters": {
                        "type": "object",
                        "properties": {"word": {"type": "string"}},
                        "required": ["word"],
                    },
                },
            }],
            "tool_choice": "auto",
            "max_tokens": TOOL_MAX_TOKENS,
        }
        started = time.monotonic()
        status, body = post(model_id, payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        choice = (body.get("choices") or [{}])[0]
        calls = (choice.get("message") or {}).get("tool_calls") or []
        function = (calls[0].get("function") or {}) if calls else {}
        tool_name = function.get("name")
        try:
            arguments_valid = isinstance(json.loads(function.get("arguments") or ""), dict)
        except ValueError:
            arguments_valid = False

        error = body.get("error")
        if isinstance(error, dict):
            error = error.get("message") or json.dumps(error)
        if status != 200:
            error = clean_error(error or f"HTTP {status}")
        elif not calls:
            error = "no tool call in response"
        elif tool_name != TOOL_NAME:
            error = f"unexpected tool: {clean_error(str(tool_name))}"
        elif not arguments_valid:
            error = "tool arguments are not a JSON object"
        else:
            error = ""

        results.append({
            "id": model_id,
            "ok": not error,
            "status": status,
            "latency_ms": latency_ms,
            "returned_model": body.get("model"),
            "finish_reason": choice.get("finish_reason"),
            "tool_name": tool_name,
            "arguments_valid": arguments_valid,
            "total_tokens": (body.get("usage") or {}).get("total_tokens"),
            "error": error,
        })
    return results


def extract_code(text: str) -> str:
    """Body of the first fenced block; the whole text when there is none."""
    match = re.search(r"```[a-zA-Z]*[ \t]*\n(.*?)```", text, re.DOTALL)
    return match.group(1) if match else text


def code_check(model_ids: list[str], post=default_post) -> list[dict]:
    """One answer per CODE_TASKS entry; the code is graded later, on the host."""
    if len(model_ids) > MAX_CODE_CHECK:
        raise SystemExit(f"at most {MAX_CODE_CHECK} model per run (got {len(model_ids)})")

    results = []
    for model_id in model_ids:
        for task in CODE_TASKS:
            payload = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": CODE_SYSTEM},
                    {"role": "user", "content": task["prompt"]},
                ],
                "max_tokens": CODE_MAX_TOKENS,
            }
            started = time.monotonic()
            status, body = post(model_id, payload)
            latency_ms = int((time.monotonic() - started) * 1000)

            choice = (body.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            finish_reason = choice.get("finish_reason")

            error = body.get("error")
            if isinstance(error, dict):
                error = error.get("message") or json.dumps(error)
            if status != 200:
                error = clean_error(error or f"HTTP {status}")
            elif not content.strip():
                error = "empty answer"
            elif finish_reason != "stop":
                error = f"finish_reason {finish_reason}"
            else:
                error = ""

            results.append({
                "id": model_id,
                "task": task["name"],
                "ok": not error,
                "status": status,
                "latency_ms": latency_ms,
                "returned_model": body.get("model"),
                "finish_reason": finish_reason,
                "total_tokens": (body.get("usage") or {}).get("total_tokens"),
                "code": extract_code(content),
                "error": error,
            })
    return results


def unsafe_reason(code: str) -> str:
    """Why this code must not be executed; "" when it may run."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"syntax error: {exc.msg} (line {exc.lineno})"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or "."]
        else:
            modules = []
        for module in modules:
            if getattr(node, "level", 0) or module.split(".")[0] not in SAFE_IMPORTS:
                return f"unsafe: import {module}"
        if isinstance(node, ast.Name) and node.id in UNSAFE_NAMES:
            return f"unsafe: name {node.id}"
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return f"unsafe: attribute {node.attr}"
    return ""


def run_cases(code: str, function: str, cases: list, timeout: float = CODE_TIMEOUT) -> list[str]:
    job = json.dumps({"code": code, "function": function, "cases": cases})
    with tempfile.TemporaryDirectory() as workdir:
        try:
            done = subprocess.run([sys.executable, "-I", "-c", CASE_RUNNER], input=job,
                                  capture_output=True, text=True, timeout=timeout, cwd=workdir)
        except subprocess.TimeoutExpired:
            return [f"timeout after {timeout}s"]
    try:
        return json.loads(done.stdout)
    except ValueError:
        lines = done.stderr.strip().splitlines()
        return [clean_error(lines[-1] if lines else "runner produced no result", key="")]


def grade(rows: list[dict], timeout: float = CODE_TIMEOUT) -> list[dict]:
    """Run each answer from code_check against its task cases."""
    answers = {row.get("task"): row for row in rows}
    results = []
    for task in CODE_TASKS:
        row = answers.get(task["name"]) or {}
        if not row:
            failures = ["no answer for this task"]
        elif not row.get("ok"):
            failures = [f"call failed: {row.get('error')}"]
        else:
            reason = unsafe_reason(row["code"])
            failures = [reason] if reason else run_cases(row["code"], task["name"], task["cases"], timeout)
        results.append({
            "id": row.get("id"),
            "task": task["name"],
            "passed": not failures,
            "cases": len(task["cases"]),
            "failures": failures[:3],
        })
    return results


COMMANDS = ("catalog", "smoke", "tools", "code", "grade")


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in COMMANDS:
        raise SystemExit("usage: catalog [prefix] | smoke MODEL... | tools MODEL | code MODEL | grade FILE")
    if argv[0] == "catalog":
        rows = catalog(argv[1] if len(argv) > 1 else "")
    elif argv[0] == "smoke":
        rows = smoke(argv[1:])
    elif argv[0] == "tools":
        rows = tool_check(argv[1:])
    elif argv[0] == "code":
        rows = code_check(argv[1:])
    else:
        with open(argv[1], encoding="utf-8") as handle:
            rows = grade([json.loads(line) for line in handle if line.strip()])
        passed = sum(row["passed"] for row in rows)
        rows.append({"summary": f"{passed}/{len(rows)} tasks passed", "all_passed": passed == len(rows)})
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
