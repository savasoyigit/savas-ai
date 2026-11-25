import asyncio
import os
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


LOCAL = "local"


class BudgetExceeded(Exception):
    pass


def is_cloud(entry):
    # Usage records written before C-8.2 have no location.
    return entry.get("location") != LOCAL


class BudgetManager:

    def __init__(self, token_manager):
        self.token_manager = token_manager

        # In-flight reservations. Valid only for a single
        # uvicorn process (see docs/plan-b.md §0).
        self._lock = asyncio.Lock()
        self._reserved = {}

        self.ares_daily_budget = int(
            os.getenv("ARES_DAILY_TOKEN_BUDGET", "20000")
        )

        self.worker_daily_budget = int(
            os.getenv("WORKER_DAILY_TOKEN_BUDGET", "15000")
        )

        self.system_daily_budget = int(
            os.getenv("SYSTEM_DAILY_TOKEN_BUDGET", "50000")
        )

        self.task_budget = int(
            os.getenv("TASK_TOKEN_BUDGET", "10000")
        )

        self.request_budget = int(
            os.getenv("REQUEST_TOKEN_BUDGET", "8000")
        )

        # Budget day resets at local midnight (decisions.md).
        self.budget_tz = ZoneInfo(
            os.getenv("BUDGET_TZ", "Europe/Istanbul")
        )

    def local_today(self):
        return datetime.now(self.budget_tz).date()

    def daily_records(self):
        today = self.local_today()

        records = self.token_manager.read_usage()

        result = []

        for record in records:
            timestamp = record.get("timestamp")

            if not timestamp:
                continue

            try:
                record_time = datetime.fromisoformat(
                    timestamp
                )
            except ValueError:
                continue

            # usage.jsonl timestamps are UTC; naive ones are
            # treated as UTC too.
            if record_time.tzinfo is None:
                record_time = record_time.replace(
                    tzinfo=timezone.utc
                )

            record_date = record_time.astimezone(
                self.budget_tz
            ).date()

            if record_date == today:
                result.append(record)

        return result

    # Daily totals count cloud usage only: local calls are not
    # charged to the cloud budget (C-8.2).
    def total_used_today(self):
        return sum(
            int(record.get("total_tokens", 0) or 0)
            for record in self.daily_records()
            if is_cloud(record)
        )

    def agent_used_today(self, agent):
        return sum(
            int(record.get("total_tokens", 0) or 0)
            for record in self.daily_records()
            if record.get("agent") == agent
            and is_cloud(record)
        )

    def task_used(self, task_id):
        return sum(
            int(record.get("total_tokens", 0) or 0)
            for record in self.token_manager.read_usage()
            if record.get("task_id") == task_id
        )

    def budget_for_agent(self, agent):
        if agent == "ares":
            return self.ares_daily_budget

        return self.worker_daily_budget

    def daily_checks(self, agent):
        reservations = [
            r for r in self._reserved.values()
            if is_cloud(r)
        ]

        daily_system_used = self.total_used_today() + sum(
            r["tokens"] for r in reservations
        )
        daily_agent_used = self.agent_used_today(agent) + sum(
            r["tokens"] for r in reservations
            if r["agent"] == agent
        )

        agent_budget = self.budget_for_agent(agent)

        return {
            "system_daily": {
                "used": daily_system_used,
                "budget": self.system_daily_budget,
                "remaining": max(
                    self.system_daily_budget
                    - daily_system_used,
                    0,
                ),
            },
            "agent_daily": {
                "used": daily_agent_used,
                "budget": agent_budget,
                "remaining": max(
                    agent_budget
                    - daily_agent_used,
                    0,
                ),
            },
        }

    def cloud_percent(
        self,
        *,
        agent,
        requested_tokens=0,
    ):
        """Daily cloud usage if this request were made, as a percentage
        of the tighter of the agent and system budgets. Above 100 the
        cloud reservation would be refused."""
        requested_tokens = max(
            int(requested_tokens or 0),
            0,
        )

        return max(
            (
                (check["used"] + requested_tokens)
                * 100
                / check["budget"]
            )
            if check["budget"] > 0
            else float("inf")
            for check in self.daily_checks(agent).values()
        )

    def check(
        self,
        *,
        agent,
        task_id,
        requested_tokens,
        location="cloud",
    ):
        requested_tokens = max(
            int(requested_tokens or 0),
            0,
        )

        task_used = self.task_used(task_id) + sum(
            r["tokens"] for r in self._reserved.values()
            if r["task_id"] == task_id
        )

        # Local calls skip the daily budgets; the task and request
        # limits still guard against runaway loops.
        checks = {
            **(
                {}
                if location == LOCAL
                else self.daily_checks(agent)
            ),
            "task": {
                "used": task_used,
                "budget": self.task_budget,
                "remaining": max(
                    self.task_budget
                    - task_used,
                    0,
                ),
            },
            "request": {
                "used": 0,
                "budget": self.request_budget,
                "remaining": self.request_budget,
            },
        }

        allowed = all(
            requested_tokens
            <= check["remaining"]
            for check in checks.values()
        )

        return {
            "allowed": allowed,
            "agent": agent,
            "task_id": task_id,
            "requested_tokens": requested_tokens,
            "checks": checks,
        }

    def enforce(
        self,
        *,
        agent,
        task_id,
        requested_tokens,
        location="cloud",
    ):
        result = self.check(
            agent=agent,
            task_id=task_id,
            requested_tokens=requested_tokens,
            location=location,
        )

        if not result["allowed"]:
            failed = []

            for name, check in result["checks"].items():
                if (
                    result["requested_tokens"]
                    > check["remaining"]
                ):
                    failed.append(
                        f"{name}: "
                        f"{check['remaining']} remaining"
                    )

            raise BudgetExceeded(
                "Token budget exceeded: "
                + ", ".join(failed)
            )

        return result

    async def reserve(
        self,
        *,
        agent,
        task_id,
        requested_tokens,
        location="cloud",
    ):
        """Check the budget and hold requested_tokens until release().

        Caller must record actual usage before calling release(), and
        must call release() on every path (use try/finally).
        """
        async with self._lock:
            result = self.enforce(
                agent=agent,
                task_id=task_id,
                requested_tokens=requested_tokens,
                location=location,
            )

            reservation_id = uuid.uuid4().hex

            self._reserved[reservation_id] = {
                "agent": agent,
                "task_id": task_id,
                "tokens": result["requested_tokens"],
                "location": location,
            }

        return {
            **result,
            "reservation_id": reservation_id,
        }

    def release(self, reservation_id):
        self._reserved.pop(reservation_id, None)
