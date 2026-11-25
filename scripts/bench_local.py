"""
C-8.1 local LLM speed check (roadmap Faz C).

Runs inside the orchestrator container (needs LOCAL_LLM_URL); stdlib only.
No cloud call, nothing goes through OmniRoute.

    docker exec -e LOCAL_LLM_URL=http://ai-llama:8080 -i ai-orchestrator \
        python - MODEL [PROMPT_TOKENS...] < scripts/bench_local.py

One chat call per prompt size (default 500 and 2000 tokens, approximate),
max_tokens=GEN_TOKENS. A random first line defeats the prompt cache, so
every call measures a cold prefill. Speeds are llama-server's own
`timings`, not wall clock. Output: JSON lines.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

GEN_TOKENS = 128
TIMEOUT = 1800
DEFAULT_SIZES = (500, 2000)
TOKENS_PER_LINE = 30  # rough; the real count is reported as prompt_n

SERVICES = ("immich", "nextcloud", "jellyfin", "grafana", "loki", "orchestrator")
LEVELS = ("INFO", "INFO", "INFO", "WARN", "ERROR")


def log_lines(count: int, rng: random.Random) -> str:
    lines = []
    for index in range(count):
        lines.append(
            f"2026-11-14T{rng.randrange(24):02d}:{rng.randrange(60):02d}:{rng.randrange(60):02d}Z "
            f"{rng.choice(LEVELS)} {rng.choice(SERVICES)} request_id={rng.randrange(10**8):08d} "
            f"duration_ms={rng.randrange(5, 9000)} status={rng.choice((200, 200, 200, 404, 500, 503))} line={index}"
        )
    return "\n".join(lines)


def payload(model_id: str, prompt_tokens: int) -> dict:
    rng = random.Random()
    body = log_lines(max(1, prompt_tokens // TOKENS_PER_LINE), rng)
    return {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": f"run {rng.randrange(10**12)}\n{body}\n\n"
                "Explain in detail which services look unhealthy in this log and why.",
            }
        ],
        "max_tokens": GEN_TOKENS,
    }


def post(body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        os.environ["LOCAL_LLM_URL"].rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode(errors="replace")[:300]}
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def bench(model_id: str, sizes: list[int]) -> list[dict]:
    rows = []
    for size in sizes:
        started = time.monotonic()
        status, body = post(payload(model_id, size))
        timings = body.get("timings") or {}
        message = ((body.get("choices") or [{}])[0]).get("message") or {}
        rows.append({
            "id": model_id,
            "status": status,
            "wall_s": round(time.monotonic() - started, 1),
            "prompt_n": timings.get("prompt_n"),
            "cache_n": timings.get("cache_n"),
            "prompt_tps": round(timings.get("prompt_per_second") or 0, 1),
            "gen_n": timings.get("predicted_n"),
            "gen_tps": round(timings.get("predicted_per_second") or 0, 1),
            "reasoning_chars": len(message.get("reasoning_content") or ""),
            "content_chars": len(message.get("content") or ""),
            "error": body.get("error"),
        })
    return rows


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("usage: MODEL [PROMPT_TOKENS...]")
    sizes = [int(arg) for arg in argv[1:]] or list(DEFAULT_SIZES)
    for row in bench(argv[0], sizes):
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
