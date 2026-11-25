import json
import os
from datetime import datetime, timezone
from pathlib import Path


USAGE_ROOT = Path(
    os.getenv(
        "TOKEN_USAGE_ROOT",
        "/mnt/vault/SAVAS-AI/usage",
    )
).resolve()

class TokenManager:

    def __init__(self, root: str | Path = USAGE_ROOT):
        self.root = Path(root).resolve()
        self.root.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.usage_file = self.root / "usage.jsonl"

    def record_usage(
        self,
        *,
        task_id: str,
        agent: str,
        model: str,
        usage: dict | None = None,
        worker: str | None = None,
        worker_task_id: str | None = None,
        duration_ms: int | None = None,
        status: str = "ok",
        route_reason: str | None = None,
        location: str = "cloud",
    ) -> dict:

        usage = usage or {}

        prompt_tokens = int(
            usage.get("prompt_tokens", 0) or 0
        )

        completion_tokens = int(
            usage.get("completion_tokens", 0) or 0
        )

        total_tokens = int(
            usage.get("total_tokens", 0)
            or (
                prompt_tokens
                + completion_tokens
            )
        )

        record = {
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),

            "task_id": task_id,

            "agent": agent,

            "worker": worker,

            "worker_task_id": worker_task_id,

            "model": model,

            "prompt_tokens": prompt_tokens,

            "completion_tokens": completion_tokens,

            "total_tokens": total_tokens,

            "duration_ms": duration_ms,

            "status": status,

            "route_reason": route_reason,

            # "cloud" | "local"; daily budgets count cloud only.
            "location": location,
        }

        with self.usage_file.open(
            "a",
            encoding="utf-8",
        ) as f:
            f.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                )
                + "\n"
            )

        return record

    def read_usage(self) -> list[dict]:

        if not self.usage_file.exists():
            return []

        records = []

        with self.usage_file.open(
            "r",
            encoding="utf-8",
        ) as f:

            for line in f:
                line = line.strip()

                if not line:
                    continue

                try:
                    records.append(
                        json.loads(line)
                    )
                except json.JSONDecodeError:
                    continue

        return records

    def total_usage(
        self,
        records: list[dict] | None = None,
    ) -> dict:

        records = (
            self.read_usage()
            if records is None
            else records
        )

        prompt_tokens = sum(
            r.get("prompt_tokens", 0)
            for r in records
        )

        completion_tokens = sum(
            r.get("completion_tokens", 0)
            for r in records
        )

        total_tokens = sum(
            r.get("total_tokens", 0)
            for r in records
        )

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    def usage_by_agent(self) -> dict:

        records = self.read_usage()

        result = {}

        for record in records:

            agent = (
                record.get("agent")
                or "unknown"
            )

            if agent not in result:
                result[agent] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                }

            result[agent][
                "prompt_tokens"
            ] += record.get(
                "prompt_tokens",
                0,
            )

            result[agent][
                "completion_tokens"
            ] += record.get(
                "completion_tokens",
                0,
            )

            result[agent][
                "total_tokens"
            ] += record.get(
                "total_tokens",
                0,
            )

        return result
