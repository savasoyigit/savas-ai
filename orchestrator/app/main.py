import fnmatch
import json
import os
import re
import time
import uuid
import asyncio
import contextvars
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from urllib.parse import quote
from pydantic import BaseModel, Field

from cv_layout import SCHEMA as CV_SCHEMA, cv_markdown, parse_cv
from docx_export import cv_docx, to_docx
from worker_manager import WorkerManager, WorkerError
from token_manager import TokenManager
from budget_manager import BudgetManager, BudgetExceeded, is_cloud
import metrics
from context_engine import (
    ARES_CONTEXT_CHARS,
    ARES_RETRIEVED_NOTE,
    MAX_NOTE_CHARS,
    MAX_SEARCH_RESULTS,
    WORKER_CONTEXT_CHARS,
    compact_note,
    compact_search,
    context_over_budget,
    note_offset,
    parse_worker_report,
    repeated_note,
    repeated_search,
    report_instructions,
    report_share,
    serialized_length,
    worker_completion_tokens,
)
from delegation import (
    MAX_REVIEW_ROUNDS,
    NEEDS_REVIEW_NOTE,
    REVIEWED_AFTER_FIX,
    REVIEW_INSTRUCTIONS,
    REVIEW_REPORT_CHARS,
    GraphError,
    SKIPPED_ERROR,
    dependents,
    input_block,
    item_id,
    plan_layers,
    reference,
    review_summary,
    review_task,
    revision_task,
)
from memory import (
    ARES_RETRIEVAL_CHARS,
    DUPLICATE_SEARCH_SLACK,
    FACT_PURGE_DAYS,
    MAX_RETRIEVED_EPISODES,
    MAX_RETRIEVED_FACTS,
    WORKER_RETRIEVAL_CHARS,
    build_episode,
    fts_query,
    later_expiry,
    near_duplicate,
    new_fact,
    retrieved_lines,
    same_fact_value,
)
from research import (
    CITATION_INSTRUCTIONS,
    MAX_SEARCH_QUERIES,
    RESULTS_PER_QUERY,
    SEARCH_DEPTH,
    SEARCH_TIMEOUT_SECONDS,
    TAVILY_URL,
    SearchError,
    cited_numbers,
    clean_queries,
    merge_results,
    research_note,
    research_note_path,
    source_list,
    sources_block,
    tavily_results,
)
from dev_loop import (
    CODE_REPORT_CHARS,
    EDIT_INSTRUCTIONS,
    MAX_ROUNDS,
    NOTHING_LIVE,
    TEST_PROFILES,
    CodeError,
    apply_edits,
    clean_code,
    diff_input,
    feedback,
    file_inputs,
    parse_edits,
    missing_files,
    parse_needs,
    repo_files,
    review_findings,
)
from repo_tools import (
    REPO_COPY,
    RepoError,
    check_repo_name,
    list_files,
    read_file,
    sandbox_repo,
)
from task_store import (
    FACT_STATUSES,
    FINAL_TASK_STATUSES,
    INTERRUPTED_ERROR,
    JOB_STATUSES,
    TASK_STATUSES,
    TaskStore,
)
from model_router import (
    ACCESS,
    FATAL,
    LOCAL,
    TRANSIENT,
    CircuitBreaker,
    Selection,
    classify_failure,
    parse_retry_after,
    select as select_model,
)


app = FastAPI(
    title="Savas AI - Orchestrator",
    version="0.6.0",
)


OMNIROUTE_URL = os.getenv(
    "OMNIROUTE_URL",
    "http://omniroute:20128/v1",
)

OMNIROUTE_KEY = os.getenv(
    "OMNIROUTE_KEY",
    "",
)

OBSIDIAN_AGENT_URL = os.getenv(
    "OBSIDIAN_AGENT_URL",
    "http://obsidian-agent:8000",
)

# Web search (Faz H): Tavily free plan; empty = search unavailable.
TAVILY_API_KEY = os.getenv(
    "TAVILY_API_KEY",
    "",
)

# Faz I (plan-i karar A): the host runner, reached only through its
# unix socket, and its sandbox copies (read-only here in I-2).
# Empty = repository tools unavailable.
RUNNER_SOCKET = os.getenv(
    "RUNNER_SOCKET",
    "",
)
SANDBOX_ROOT = os.getenv(
    "SANDBOX_ROOT",
    "",
)
RUNNER_TIMEOUT_SECONDS = 60
# A test run may take the runner's 300 s limit plus container start.
RUNNER_TEST_TIMEOUT_SECONDS = 360

# Local LLM (C-8.2): llama-server reached directly, not through
# OmniRoute. Empty = no local model is ever selected.
LOCAL_LLM_URL = os.getenv(
    "LOCAL_LLM_URL",
    "",
).rstrip("/")

# CPU-only server: a call at the local context limit takes about
# 10 minutes (estimate from the C-8.1 speeds).
LOCAL_LLM_TIMEOUT = float(
    os.getenv(
        "LOCAL_LLM_TIMEOUT",
        "900",
    )
)

# Last-resort default only; models are chosen by route_model().
MODEL = os.getenv(
    "MODEL",
    "gh/gpt-4.1",
)

# Task types Ares may pass when delegating (config/models.json
# task_types without "orchestrate").
WORKER_TASK_TYPES = [
    "coding",
    "research",
    "review",
    "writing",
    "general",
]

TASK_TYPE_SCHEMA = {
    "type": "string",
    "enum": WORKER_TASK_TYPES,
    "description": (
        "Optional kind of work; selects the model. "
        "Omit to use the worker's speciality."
    ),
}

# Repositories in runner/repos.json (the runner checks; this names them for Ares).
KNOWN_REPOS = ("savas-ai", "valentine-calendar")

CODE_FILES_REQUIRED = (
    "code.files is required: list the files to change "
    "(find them with repo_read first), then delegate again."
)

CODE_SCHEMA = {
    "type": "object",
    "description": (
        "Sandbox code task; isolated tests; not live. "
        "focus: big-file search terms."
    ),
    "properties": {
        "repo": {"type": "string", "description": ", ".join(KNOWN_REPOS)},
        "files": {"type": "array", "items": {"type": "string"}},
        "focus": {"type": "array", "items": {"type": "string"}},
        "test": {"type": "string", "enum": list(TEST_PROFILES)},
    },
    "required": ["repo"],
}

SEARCH_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "maxItems": MAX_SEARCH_QUERIES,
    "description": "Web searches; results go to the worker as numbered sources.",
}


# Model health (C-6). In-memory, single uvicorn process.
circuit = CircuitBreaker()

# The local server has one slot; parallel delegations wait here
# instead of piling up on the CPU.
local_llm_lock = asyncio.Lock()


def route_model(
    agent: str,
    task_type: str | None = None,
    requested_tokens: int | None = None,
    context_tokens: int | None = None,
) -> Selection:
    for model in circuit.expire():
        add_activity(
            task_id=None,
            agent="model_router",
            event="model_circuit",
            status="closed",
            model=model,
        )

    return select_model(
        agent,
        task_type,
        env_model=MODEL,
        unavailable=circuit.unavailable(),
        # A worker call is larger on the local model than on
        # cloud (D-3); the context check uses the larger size.
        needed_tokens=context_tokens or requested_tokens,
        allow_local=bool(LOCAL_LLM_URL),
        # Daily cloud budget used up -> local model, chosen
        # before the call (a budget refusal is never retried).
        cloud_budget_percent=(
            budget_manager.cloud_percent(
                agent=agent,
                requested_tokens=requested_tokens,
            )
            if LOCAL_LLM_URL
            else None
        ),
    )


async def post_chat(
    client: httpx.AsyncClient,
    location: str,
    payload: dict,
):
    if location == LOCAL:
        # The timeout starts after the wait for the slot.
        async with local_llm_lock:
            return await client.post(
                f"{LOCAL_LLM_URL}/v1/chat/completions",
                headers={
                    "Content-Type": (
                        "application/json"
                    ),
                },
                json=payload,
                timeout=LOCAL_LLM_TIMEOUT,
            )

    return await client.post(
        f"{OMNIROUTE_URL}/chat/completions",
        headers={
            "Authorization": (
                f"Bearer {OMNIROUTE_KEY}"
            ),
            "Content-Type": (
                "application/json"
            ),
        },
        json=payload,
    )


def local_llm_status() -> dict:
    status = {
        "configured": bool(LOCAL_LLM_URL),
        "reachable": None,
        "models": {},
    }

    if not LOCAL_LLM_URL:
        return status

    try:
        # Listing models does not load one.
        response = httpx.get(
            f"{LOCAL_LLM_URL}/models",
            timeout=2,
        )

        response.raise_for_status()

        status["models"] = {
            entry.get("id"): (
                entry.get("status") or {}
            ).get("value")
            for entry in response.json().get("data", [])
        }

        status["reachable"] = True

    except Exception as exc:
        status["reachable"] = False
        status["error"] = str(exc)

    return status

WORKERS_ROOT = os.getenv(
    "WORKERS_ROOT",
    "/mnt/vault/SAVAS-AI",
)

DEFAULT_WORKER = os.getenv(
    "DEFAULT_WORKER",
    "ares",
)

WORKER_RETENTION_DAYS = int(
    os.getenv(
        "WORKER_RETENTION_DAYS",
        "4",
    )
)

worker_manager = WorkerManager(
    WORKERS_ROOT,
    retention_days=WORKER_RETENTION_DAYS,
)

token_manager = TokenManager()

budget_manager = BudgetManager(
    token_manager
)


# --------------------------------------------------
# In-memory storage
# --------------------------------------------------

TASKS = {}
ACTIVITY = []
APPROVALS = {}
AUTO_APPROVALS = {}

# Runtime context for tasks waiting for approval.
TASK_CONTEXTS = {}

# Run segments in progress: task_id -> asyncio.Task (run_tracked).
RUNNING_TASKS = {}
CANCEL_REQUESTED = set()

MAX_ACTIVITY_EVENTS = 1000
MAX_TOOL_ITERATIONS = 5

# Task store (Faz E, docs/plan-e.md). Empty TASK_DB_PATH: nothing is
# persisted and the orchestrator behaves as before. The SQLite
# connection belongs to the event-loop thread: an endpoint that uses
# the store must be "async def" (FastAPI runs plain "def" endpoints in
# a thread pool).
TASK_DB_PATH = os.getenv("TASK_DB_PATH", "").strip()
TASK_TIMEOUT_SECONDS = float(
    os.getenv("TASK_TIMEOUT_SECONDS", "3600")
)
IDEMPOTENCY_TTL_SECONDS = 24 * 3600
MAX_IDEMPOTENCY_KEY_CHARS = 128
EVENT_RETENTION_DAYS = 30
APPROVAL_CONTEXT_LOST = "approval context lost on restart"
APPROVAL_INTERRUPTED = "approval interrupted by restart"
APPROVAL_ROUND_INCOMPLETE = "approval round incomplete at restart"

task_store = None


# --------------------------------------------------
# General helpers
# --------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_datetime(value: str):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


def add_activity(
    task_id: str,
    agent: str,
    event: str,
    status: str,
    **details,
):
    activity = {
        "timestamp": utc_now(),
        "task_id": task_id,
        "agent": agent,
        "event": event,
        "status": status,
        **details,
    }

    ACTIVITY.append(activity)

    if len(ACTIVITY) > MAX_ACTIVITY_EVENTS:
        del ACTIVITY[:-MAX_ACTIVITY_EVENTS]

    # A failed store write is itself an event; it is not stored.
    if task_id != "system" and event != "store":
        store_write("add_event", activity)

    return activity


def store_write(action: str, *args):
    """
    Call task_store.<action>(*args). A failure is logged and the task
    goes on: the in-memory state stays the working copy.
    """
    if task_store is None:
        return None

    try:
        return getattr(task_store, action)(*args)

    except Exception as exc:
        add_activity(
            task_id="system",
            agent="orchestrator",
            event="store",
            status="failed",
            action=action,
            error=str(exc),
        )
        return None


def persist_task(task_id: str):
    task = TASKS.get(task_id)

    if task:
        store_write("save_task", task)


def persist_approval(approval: dict):
    store_write("save_approval", approval)


def checkpoint_task(task_id: str):
    """Task waiting for approval: status, context and its pending approvals in one write."""
    task = TASKS.get(task_id)
    context = TASK_CONTEXTS.get(task_id)

    if task and context:
        store_write(
            "checkpoint",
            task,
            context,
            get_pending_approvals_for_task(task_id),
        )


def record_usage_safely(**kwargs):
    # A usage.jsonl write error must not fail the LLM call
    # it describes; it is logged to activity instead.
    try:
        token_manager.record_usage(**kwargs)

    except Exception as exc:
        add_activity(
            task_id=kwargs["task_id"],
            agent=kwargs["agent"],
            event="usage_record",
            status="failed",
            error=str(exc),
        )


def note_llm_failure(
    exc: Exception,
    model: str,
    task_id: str,
    agent: str,
) -> str:
    """
    Report a failed LLM call to the circuit breaker and
    return the failure kind (limit / transient / access / fatal).
    """

    status = None
    kind = TRANSIENT
    retry_after = None

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code

        kind = classify_failure(
            status,
            exc.response.text,
        )

        retry_after = parse_retry_after(
            exc.response.headers.get("retry-after"),
            now=circuit.clock(),
        )

    if kind in (ACCESS, FATAL):
        # Needs the user (permission, key, bad request). The alert
        # never blocks the call path; /health lists recent ones.
        add_activity(
            task_id=task_id,
            agent=agent,
            event="model_alert",
            status="warning",
            model=model,
            kind=kind,
            http_status=status,
            error=str(exc),
        )

    open_until = circuit.record_failure(
        model,
        kind,
        retry_after,
    )

    if open_until is not None:
        add_activity(
            task_id=task_id,
            agent=agent,
            event="model_circuit",
            status="open",
            model=model,
            kind=kind,
            http_status=status,
            seconds=round(open_until - circuit.clock()),
            open_until=datetime.fromtimestamp(
                open_until,
                timezone.utc,
            ).isoformat(),
        )

    return kind


def note_llm_success(
    model: str,
    task_id: str,
    agent: str,
):
    if circuit.record_success(model):
        add_activity(
            task_id=task_id,
            agent=agent,
            event="model_circuit",
            status="closed",
            model=model,
        )


MODEL_ALERT_FIELDS = (
    "timestamp",
    "task_id",
    "agent",
    "model",
    "kind",
    "http_status",
)


def recent_model_alerts(limit: int = 5):
    alerts = [
        event
        for event in ACTIVITY
        if event["event"] == "model_alert"
    ]

    return {
        "count": len(alerts),
        "recent": [
            {
                field: event.get(field)
                for field in MODEL_ALERT_FIELDS
            }
            for event in alerts[-limit:]
        ],
    }


def add_fallback_activity(
    task_id: str,
    agent: str,
    status: str,
    from_model: str,
    to_model: str,
    **details,
):
    add_activity(
        task_id=task_id,
        agent=agent,
        event="llm_fallback",
        status=status,
        **{"from": from_model, "to": to_model},
        **details,
    )


def create_task(
    message: str,
    worker_id: str = DEFAULT_WORKER,
    idempotency_key: str | None = None,
):
    task_id = str(uuid.uuid4())

    if not worker_manager.worker_exists(worker_id):
        raise HTTPException(
            status_code=400,
            detail=f"Worker not found: {worker_id}",
        )

    TASKS[task_id] = {
        "task_id": task_id,
        "message": message,
        "status": "queued",
        "created_at": utc_now(),
        "completed_at": None,
        "agent": "orchestrator",
        "worker": worker_id,
        "daily_memory": None,
        "error": None,
        "result": None,
        "idempotency_key": idempotency_key,
    }

    daily_path = worker_manager.create_daily_task(
        worker_id,
        task_id,
        message,
    )

    TASKS[task_id]["daily_memory"] = str(
        daily_path.relative_to(worker_manager.root)
    )

    persist_task(task_id)

    return task_id


def complete_task(
    task_id: str,
    status: str,
    error: str | None = None,
):
    task = TASKS.get(task_id)

    if not task:
        return

    task["status"] = status

    if error is not None:
        task["error"] = error

    if status in (
        "completed",
        "needs_review",
        "failed",
        "rejected",
        "blocked",
        "cancelled",
    ):
        task["completed_at"] = utc_now()

        update_worker_daily_memory(
            task_id,
            "Status",
            status,
            append=True
        )

    persist_task(task_id)

    if status in FINAL_TASK_STATUSES:
        store_write("clear_context", task_id)

def update_worker_daily_memory(
    task_id: str,
    section: str,
    content: str,
    append: bool = False,
):
    task = TASKS.get(task_id)

    if not task:
        return

    worker_id = task.get("worker")

    if not worker_id:
        return

    try:
        if append:
            worker_manager.append_daily_section(
                worker_id,
                task_id,
                section,
                content,
            )
        else:
            worker_manager.update_daily_section(
                worker_id,
                task_id,
                section,
                content,
            )

    except WorkerError as exc:
        add_activity(
            task_id=task_id,
            agent="orchestrator",
            event="worker_memory",
            status="failed",
            error=str(exc),
        )

def get_pending_approvals_for_task(
    task_id: str,
):
    return [
        approval
        for approval in APPROVALS.values()
        if (
            approval["task_id"] == task_id
            and approval["status"] == "pending"
        )
    ]


# --------------------------------------------------
# Approval helpers
# --------------------------------------------------

def cleanup_expired_auto_approvals():
    now = datetime.now(timezone.utc)

    for policy_id, policy in list(
        AUTO_APPROVALS.items()
    ):
        if policy["status"] != "active":
            continue

        expires_at = parse_datetime(
            policy["expires_at"]
        )

        if expires_at <= now:
            policy["status"] = "expired"
            policy["expired_at"] = utc_now()

            add_activity(
                task_id="system",
                agent="orchestrator",
                event="auto_approval",
                status="expired",
                auto_approval_id=policy_id,
            )


def path_matches(
    path: str,
    patterns: list[str],
):
    return any(
        fnmatch.fnmatchcase(path, pattern)
        for pattern in patterns
    )


def auto_approval_matches(
    agent: str,
    action: str,
    path: str | None,
):
    cleanup_expired_auto_approvals()

    for policy in AUTO_APPROVALS.values():

        if policy["status"] != "active":
            continue

        if (
            policy["agent"] != "*"
            and policy["agent"] != agent
        ):
            continue

        if (
            "*" not in policy["actions"]
            and action not in policy["actions"]
        ):
            continue

        allowed_paths = policy.get(
            "paths",
            ["*"],
        )

        if path is not None:
            if not path_matches(
                path,
                allowed_paths,
            ):
                continue
        elif allowed_paths != ["*"]:
            continue

        return policy

    return None


def create_approval(
    task_id: str,
    agent: str,
    action: str,
    arguments: dict,
    reason: str,
    tool_call_id: str,
):
    approval_id = str(uuid.uuid4())

    approval = {
        "approval_id": approval_id,
        "task_id": task_id,
        "agent": agent,
        "action": action,
        "arguments": arguments,
        "reason": reason,
        "tool_call_id": tool_call_id,
        "status": "pending",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "approved_at": None,
        "rejected_at": None,
        "executed_at": None,
    }

    APPROVALS[approval_id] = approval
    persist_approval(approval)

    add_activity(
        task_id=task_id,
        agent=agent,
        event="approval",
        status="pending",
        approval_id=approval_id,
        action=action,
        arguments=arguments,
        reason=reason,
    )

    return approval


# --------------------------------------------------
# Tool definitions
# --------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": (
                "Search the user's Obsidian vault for Markdown notes "
                "matching a query. Use it when the answer may be in "
                "the user's notes. "
                f"Returns at most {MAX_SEARCH_RESULTS} note paths."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Text to search for."
                        ),
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": (
                "Read a Markdown note from the vault. "
                f"A note longer than {MAX_NOTE_CHARS} "
                "characters is returned in parts: when the result "
                "has truncated=true, call read_note again with "
                "offset=next_offset to continue."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path of the note inside the vault."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "next_offset of the previous result; "
                            "omit to read from the start."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_folder",
            "description": (
                "List the direct contents of a folder in the vault."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative folder path inside the vault; "
                            "empty string for the vault root."
                        ),
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repo_read",
            "description": (
                "Read-only code repository (copy without secrets): "
                "reads path, or lists files matching pattern."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": ", ".join(KNOWN_REPOS)},
                    "path": {"type": "string"},
                    "pattern": {
                        "type": "string",
                        "description": "Glob, e.g. orchestrator/app/*.py",
                    },
                    "offset": {"type": "integer"},
                },
                "required": ["repo"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": (
                "Create a new Markdown note in the vault. "
                "Write: requires approval unless auto-approved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative Markdown path inside the vault."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Complete Markdown content of the note."
                        ),
                    },
                    "from_task": {
                        "type": "string",
                        "description": (
                            "task_id of a research result: the note is "
                            "built from its report and sources; omit "
                            "content (path optional)."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_note",
            "description": (
                "Update an existing Markdown note in the vault. "
                "Write: requires approval unless auto-approved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative Markdown path inside the vault."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Complete replacement Markdown content."
                        ),
                    },
                },
                "required": [
                    "path",
                    "content",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_note",
            "description": (
                "Move an existing Markdown note to another location "
                "in the vault. Write: requires approval unless auto-approved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "Current relative Markdown path."
                        ),
                    },
                    "destination": {
                        "type": "string",
                        "description": (
                            "Destination relative Markdown path."
                        ),
                    },
                },
                "required": [
                    "source",
                    "destination",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save a lasting fact, preference or decision the "
                "user wants kept. No approval. Write a short key "
                "and the value in the user's language; if retrieved "
                "memory has the same topic, reuse its key."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "importance": {
                        "type": "integer",
                        "description": "1: 30 days, 2: 180, 3: forever",
                    },
                    "scope": {"type": "string", "description": "global or worker id"},
                    "expires_in_days": {"type": "integer"},
                },
                "required": ["key", "value", "importance"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_worker",
            "description": (
                "Delegate a task that needs a specialist to "
                "another SAVAS AI worker. Do not delegate simple "
                "tasks you can complete yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "worker": {
                        "type": "string",
                        "enum": [
                            "gandalf",
                            "jarvis",
                            "oracle",
                            "willy",
                            "gordon",
                        ],
                        "description": (
                            "gandalf: strategy, jarvis: coding, "
                            "oracle: research, willy: content, "
                            "gordon: QA and review."
                        ),
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "A clear, self-contained description "
                            "of the task."
                        ),
                    },
                    "task_type": TASK_TYPE_SCHEMA,
                    "search": SEARCH_SCHEMA,
                    "code": CODE_SCHEMA,
                    "review_by": {
                        "type": "string",
                        "enum": [
                            "gandalf",
                            "jarvis",
                            "oracle",
                            "willy",
                            "gordon",
                        ],
                        "description": (
                            "Reviewer; re-checks each fix; "
                            "unapproved = not done."
                        ),
                    },
                },
                "required": [
                    "worker",
                    "task",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_workers",
            "description": (
                "Delegate tasks to several SAVAS AI workers. "
                "Independent tasks run at once; a task with "
                "depends_on waits for those tasks and receives "
                "their results. When one task needs another's "
                "result, send both in one call with depends_on. "
                "Do not use it for simple tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "delegations": {
                        "type": "array",
                        "description": (
                            "Independent tasks that run concurrently."
                        ),
                        "minItems": 2,
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "worker": {
                                    "type": "string",
                                    "enum": [
                                        "gandalf",
                                        "jarvis",
                                        "oracle",
                                        "willy",
                                        "gordon",
                                    ],
                                    "description": (
                                        "Worker ID, as in "
                                        "delegate_to_worker."
                                    ),
                                },
                                "task": {
                                    "type": "string",
                                    "description": (
                                        "A clear, self-contained "
                                        "description of the task."
                                    ),
                                },
                                "task_type": TASK_TYPE_SCHEMA,
                                "search": SEARCH_SCHEMA,
                                "id": {
                                    "type": "string",
                                    "description": "Short id, e.g. a.",
                                },
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Ids whose results this task "
                                        "needs; it runs after them and "
                                        "gets their reports."
                                    ),
                                },
                            },
                            "required": [
                                "worker",
                                "task",
                            ],
                        },
                    },
                },
                "required": [
                    "delegations",
                ],
            },
        },
    },
]


WRITE_ACTIONS = {
    "create_note",
    "update_note",
    "move_note",
}


# --------------------------------------------------
# Read-only Obsidian tools
# --------------------------------------------------

OBSIDIAN_READ_RETRIES = 1
OBSIDIAN_RETRY_DELAY_SECONDS = 0.5


async def obsidian_get(
    endpoint: str,
    params: dict,
    task_id: str,
    tool: str,
):
    """
    GET for a read tool. A connection error, timeout or 5xx gets one
    more try (plan-e karar I); a 4xx is final. Write tools never go
    through here: a write is not repeated.
    """
    for attempt in range(OBSIDIAN_READ_RETRIES + 1):
        try:
            async with httpx.AsyncClient(
                timeout=30
            ) as client:
                response = await client.get(
                    f"{OBSIDIAN_AGENT_URL}{endpoint}",
                    params=params,
                )

            response.raise_for_status()

            return response

        except httpx.HTTPStatusError as exc:
            if (
                exc.response.status_code < 500
                or attempt == OBSIDIAN_READ_RETRIES
            ):
                raise

            error = exc

        except httpx.HTTPError as exc:
            if attempt == OBSIDIAN_READ_RETRIES:
                raise

            error = exc

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="retry",
            tool=tool,
            attempt=attempt + 1,
            error=str(error),
        )

        await asyncio.sleep(OBSIDIAN_RETRY_DELAY_SECONDS)


async def search_notes(
    query: str,
    task_id: str,
):
    add_activity(
        task_id=task_id,
        agent="obsidian",
        event="tool_call",
        status="started",
        tool="search_notes",
        query=query,
    )

    start = time.perf_counter()

    try:
        response = await obsidian_get(
            "/search",
            {"q": query},
            task_id,
            "search_notes",
        )

        result = response.json()

        duration_ms = round(
            (time.perf_counter() - start) * 1000
        )

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="completed",
            tool="search_notes",
            query=query,
            duration_ms=duration_ms,
            result_count=result.get("count"),
        )

        return result

    except Exception as exc:
        duration_ms = round(
            (time.perf_counter() - start) * 1000
        )

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="failed",
            tool="search_notes",
            query=query,
            duration_ms=duration_ms,
            error=str(exc),
        )

        raise


async def read_note(
    path: str,
    task_id: str,
):
    add_activity(
        task_id=task_id,
        agent="obsidian",
        event="tool_call",
        status="started",
        tool="read_note",
        path=path,
    )

    start = time.perf_counter()

    try:
        response = await obsidian_get(
            "/note",
            {"path": path},
            task_id,
            "read_note",
        )

        result = response.json()

        duration_ms = round(
            (time.perf_counter() - start) * 1000
        )

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="completed",
            tool="read_note",
            path=path,
            duration_ms=duration_ms,
        )

        return result

    except Exception as exc:
        duration_ms = round(
            (time.perf_counter() - start) * 1000
        )

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="failed",
            tool="read_note",
            path=path,
            duration_ms=duration_ms,
            error=str(exc),
        )

        raise


async def list_folder(
    path: str,
    task_id: str,
):
    add_activity(
        task_id=task_id,
        agent="obsidian",
        event="tool_call",
        status="started",
        tool="list_folder",
        path=path,
    )

    start = time.perf_counter()

    try:
        response = await obsidian_get(
            "/folder",
            {"path": path},
            task_id,
            "list_folder",
        )

        result = response.json()

        duration_ms = round(
            (time.perf_counter() - start) * 1000
        )

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="completed",
            tool="list_folder",
            path=path,
            duration_ms=duration_ms,
            item_count=len(
                result.get("items", [])
            ),
        )

        return result

    except Exception as exc:
        duration_ms = round(
            (time.perf_counter() - start) * 1000
        )

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="failed",
            tool="list_folder",
            path=path,
            duration_ms=duration_ms,
            error=str(exc),
        )

        raise


# --------------------------------------------------
# Obsidian write execution
# --------------------------------------------------

async def create_note(
    path: str,
    content: str,
    task_id: str,
):
    add_activity(
        task_id=task_id,
        agent="obsidian",
        event="tool_call",
        status="started",
        tool="create_note",
        path=path,
    )

    start = time.perf_counter()

    try:
        async with httpx.AsyncClient(
            timeout=30
        ) as client:
            response = await client.post(
                f"{OBSIDIAN_AGENT_URL}/note",
                json={
                    "path": path,
                    "content": content,
                },
            )

        response.raise_for_status()

        result = response.json()

        duration_ms = round(
            (time.perf_counter() - start) * 1000
        )

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="completed",
            tool="create_note",
            path=path,
            duration_ms=duration_ms,
        )

        return result

    except Exception as exc:
        duration_ms = round(
            (time.perf_counter() - start) * 1000
        )

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="failed",
            tool="create_note",
            path=path,
            duration_ms=duration_ms,
            error=str(exc),
        )

        raise


async def update_note(
    path: str,
    content: str,
    task_id: str,
):
    add_activity(
        task_id=task_id,
        agent="obsidian",
        event="tool_call",
        status="started",
        tool="update_note",
        path=path,
    )

    start = time.perf_counter()

    try:
        async with httpx.AsyncClient(
            timeout=30
        ) as client:
            response = await client.put(
                f"{OBSIDIAN_AGENT_URL}/note",
                json={
                    "path": path,
                    "content": content,
                },
            )

        response.raise_for_status()

        result = response.json()

        duration_ms = round(
            (time.perf_counter() - start) * 1000
        )

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="completed",
            tool="update_note",
            path=path,
            duration_ms=duration_ms,
        )

        return result

    except Exception as exc:
        duration_ms = round(
            (time.perf_counter() - start) * 1000
        )

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="failed",
            tool="update_note",
            path=path,
            duration_ms=duration_ms,
            error=str(exc),
        )

        raise


async def move_note(
    source: str,
    destination: str,
    task_id: str,
):
    add_activity(
        task_id=task_id,
        agent="obsidian",
        event="tool_call",
        status="started",
        tool="move_note",
        source=source,
        destination=destination,
    )

    start = time.perf_counter()

    try:
        async with httpx.AsyncClient(
            timeout=30
        ) as client:
            response = await client.post(
                f"{OBSIDIAN_AGENT_URL}/move",
                json={
                    "source": source,
                    "destination": destination,
                },
            )

        response.raise_for_status()

        result = response.json()

        duration_ms = round(
            (time.perf_counter() - start) * 1000
        )

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="completed",
            tool="move_note",
            source=source,
            destination=destination,
            duration_ms=duration_ms,
        )

        return result

    except Exception as exc:
        duration_ms = round(
            (time.perf_counter() - start) * 1000
        )

        add_activity(
            task_id=task_id,
            agent="obsidian",
            event="tool_call",
            status="failed",
            tool="move_note",
            source=source,
            destination=destination,
            duration_ms=duration_ms,
            error=str(exc),
        )

        raise


# --------------------------------------------------
# Tool execution
# --------------------------------------------------

async def execute_worker_tool(
    name: str,
    arguments: dict,
    task_id: str,
):
    if name == "delegate_to_worker":
        try:
            search = clean_queries(arguments.get("search"))
            code = clean_code(arguments.get("code"))
        except (SearchError, CodeError) as exc:
            return {"status": "error", "error": str(exc)}

        if code:
            if search:
                return {
                    "status": "error",
                    "error": "code cannot be combined with search.",
                }

            # M-3a: the worker cannot guess paths; Ares finds them first.
            if not code["files"]:
                return {"status": "error", "error": CODE_FILES_REQUIRED}

            # M-3b: the repo's tests run on every code task.
            code["test"] = code["test"] or TEST_PROFILES[0]

            # M-3a: every code change is reviewed, asked for or not.
            reviewer = arguments.get("review_by") or (
                "gandalf" if arguments["worker"] == "gordon" else "gordon"
            )
            return await run_code_task(
                arguments["worker"],
                arguments["task"],
                task_id,
                arguments.get("task_type"),
                code,
                reviewer,
            )

        if arguments.get("review_by"):
            return await run_reviewed_worker(
                arguments["worker"],
                arguments["task"],
                task_id,
                arguments.get("task_type"),
                arguments["review_by"],
                search=search,
            )

        return await run_worker(
            arguments["worker"],
            arguments["task"],
            task_id,
            arguments.get("task_type"),
            search=search,
        )

    if name == "delegate_to_workers":
        return await run_workers_parallel(
            arguments["delegations"],
            task_id,
        )

    raise ValueError(
        f"Unknown worker tool: {name}"
    )

async def execute_write_tool(
    name: str,
    arguments: dict,
    task_id: str,
):
    if name == "create_note":
        result = await create_note(
            arguments["path"],
            arguments["content"],
            task_id,
        )

        forget_reads(task_id, arguments["path"])

        return result

    if name == "update_note":
        result = await update_note(
            arguments["path"],
            arguments["content"],
            task_id,
        )

        forget_reads(task_id, arguments["path"])

        return result

    if name == "move_note":
        result = await move_note(
            arguments["source"],
            arguments["destination"],
            task_id,
        )

        forget_reads(
            task_id,
            arguments["source"],
            arguments["destination"],
        )

        return result

    raise ValueError(
        f"Unknown write tool: {name}"
    )


def task_reads(
    task_id: str,
) -> set:
    """
    Searches and note parts already answered in this task.
    Kept with the task context; a task without one keeps
    no record.
    """

    context = TASK_CONTEXTS.get(task_id)

    if context is None:
        return set()

    return context.setdefault("reads", set())


def forget_reads(
    task_id: str,
    *paths: str,
):
    """
    After a write the written notes may be read again, and
    any search may return something new.
    """

    reads = task_reads(task_id)

    reads.difference_update(
        {
            key
            for key in reads
            if key[0] == "search" or key[1] in paths
        }
    )


async def execute_read_tool(
    name: str,
    arguments: dict,
    task_id: str,
):
    # Results are compacted before they reach the model, and
    # a read repeated within the task is not answered twice.
    reads = task_reads(task_id)

    if name == "search_notes":
        query = arguments["query"]
        key = ("search", query.strip().casefold())

        if key in reads:
            add_activity(
                task_id=task_id,
                agent="orchestrator",
                event="tool_result",
                status="repeat",
                tool=name,
                query=query,
            )

            return repeated_search(query)

        result = compact_search(
            await search_notes(
                query,
                task_id,
            )
        )

        reads.add(key)

        if "note" in result:
            add_activity(
                task_id=task_id,
                agent="orchestrator",
                event="tool_result",
                status="truncated",
                tool=name,
                query=query,
                result_count=result["count"],
                returned=len(result["results"]),
            )

        return result

    if name == "read_note":
        path = arguments["path"]
        offset = note_offset(arguments.get("offset"))
        key = ("note", path, offset)

        if key in reads:
            add_activity(
                task_id=task_id,
                agent="orchestrator",
                event="tool_result",
                status="repeat",
                tool=name,
                path=path,
                offset=offset,
            )

            return repeated_note(path, offset)

        result = compact_note(
            await read_note(
                path,
                task_id,
            ),
            offset,
        )

        reads.add(key)

        if result.get("truncated"):
            add_activity(
                task_id=task_id,
                agent="orchestrator",
                event="tool_result",
                status="truncated",
                tool=name,
                path=path,
                offset=offset,
                total_chars=result["total_chars"],
                next_offset=result["next_offset"],
            )

        return result

    if name == "list_folder":
        return await list_folder(
            arguments["path"],
            task_id,
        )

    if name == "repo_read":
        return await execute_repo_tool(
            name,
            arguments,
            task_id,
            reads,
        )

    raise ValueError(
        f"Unknown read tool: {name}"
    )


async def runner_call(
    path: str,
    body: dict,
    timeout: float = RUNNER_TIMEOUT_SECONDS,
) -> dict:
    """POST to the host runner over its unix socket (plan-i karar A)."""
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=RUNNER_SOCKET),
        timeout=timeout,
    ) as client:
        response = await client.post(f"http://runner{path}", json=body)

    try:
        result = response.json()
    except ValueError:
        result = {"status": "error", "error": f"HTTP {response.status_code}"}

    return result


async def ensure_sandbox(task_id: str, repo: str):
    """
    The read-only copy of `repo` for this task, made by the runner on
    first use (I-2). One repository per task. Raises RepoError.
    """
    if not RUNNER_SOCKET or not SANDBOX_ROOT:
        raise RepoError("Repository tools are not configured.")

    sandbox = Path(SANDBOX_ROOT) / task_id
    existing = sandbox_repo(sandbox)

    if existing is not None and existing != repo:
        raise RepoError(
            f"This task already reads {existing}; one repository per task."
        )

    if existing is None:
        started = time.monotonic()
        try:
            result = await runner_call(
                "/prepare",
                {"task": task_id, "repo": repo},
            )
        except httpx.HTTPError as exc:
            result = {"status": "error", "error": type(exc).__name__}

        add_activity(
            task_id=task_id,
            agent="orchestrator",
            event="runner",
            status="prepare" if result.get("status") == "ok" else "failed",
            repo=repo,
            files=result.get("files"),
            skipped=result.get("skipped"),
            ms=int((time.monotonic() - started) * 1000),
            error=result.get("error"),
        )

        if result.get("status") != "ok":
            raise RepoError(
                f"The repository copy could not be made: {result.get('error')}"
            )

    return sandbox / REPO_COPY


async def run_tests(task_id: str, profile: str) -> dict:
    """One test run of the task's sandbox copy by the runner (I-3)."""
    started = time.monotonic()
    try:
        result = await runner_call(
            "/test",
            # M-3: "auto" = the repo's own profile (runner/repos.json).
            {"task": task_id, "profile": "auto" if profile else profile},
            timeout=RUNNER_TEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        result = {"status": "error", "error": type(exc).__name__}

    add_activity(
        task_id=task_id,
        agent="orchestrator",
        event="runner",
        status="test" if result.get("status") == "ok" else "failed",
        profile=profile,
        run=result.get("run"),
        exit_code=result.get("exit_code"),
        passed=result.get("passed"),
        timed_out=result.get("timed_out"),
        ms=int((time.monotonic() - started) * 1000),
        error=result.get("error"),
    )
    return result


async def run_diff(task_id: str) -> dict:
    """The task's change as a patch file, made by the runner (I-4)."""
    try:
        result = await runner_call("/diff", {"task": task_id})
    except httpx.HTTPError as exc:
        result = {"status": "error", "error": type(exc).__name__}

    add_activity(
        task_id=task_id,
        agent="orchestrator",
        event="runner",
        status="diff" if result.get("status") == "ok" else "failed",
        files=result.get("files"),
        added=result.get("added"),
        removed=result.get("removed"),
        conflicts=result.get("conflicts"),
        error=result.get("error"),
    )
    return result


async def run_code_task(
    worker_id: str,
    task: str,
    parent_task_id: str,
    task_type: str | None,
    code: dict,
    reviewer_id: str | None = None,
) -> dict:
    """
    Code loop (Faz I-3, plan-i karar D): the worker gets the named
    files of the sandbox copy and answers with search / replace
    blocks; the orchestrator applies them, the runner tests the copy,
    and a failure goes back to the worker. At most MAX_ROUNDS rounds.

    I-4: the change ends as a patch file (runner diff). With
    reviewer_id the reviewer gets the diff; on revise the worker gets
    fix rounds, each reviewed again (J-6, at most MAX_REVIEW_ROUNDS). The
    user applies the patch (scripts/apply_patch.sh, plan-i karar E).

    Ares gets the worker's summary and what changed, never the blocks.
    """
    if reviewer_id == worker_id:
        return {"status": "error", "error": "review_by must be another worker."}

    try:
        root = await ensure_sandbox(parent_task_id, check_repo_name(code["repo"]))
    except RepoError as exc:
        return {"status": "error", "error": str(exc)}

    # M-3b: named files must exist; Ares gets the real list instead.
    missing = missing_files(root, code["files"])
    if missing:
        return {
            "status": "error",
            "error": (
                f"Not in {code['repo']}: {', '.join(missing)}. Pick the files to change "
                "from this list and delegate again (new files: let the worker create them)."
            ),
            "repo_files": repo_files(root),
        }

    state = {
        "changed": [],
        "previous": [],
        "rejected": 0,
        "test": None,
        "result": {},
        "rounds": 0,
        "needs": [],
    }

    async def one_round(notes: str, step: str) -> tuple[bool, str]:
        """One worker round; (go on, notes for the next round)."""
        state["rounds"] += 1
        worker_task_id = str(uuid.uuid4())
        files = code["files"] + [f for f in state["changed"] if f not in code["files"]]
        # M-3a: a NEED may name another file of the copy.
        files += [n["path"] for n in state["needs"] if n["path"] not in files]
        inputs = file_inputs(root, files, code.get("focus"), state["needs"])
        if notes:
            inputs = f"{inputs}\n\n{notes}"

        try:
            result = await run_worker(
                worker_id,
                task,
                parent_task_id,
                task_type,
                CODE_REPORT_CHARS,
                worker_task_id=worker_task_id,
                depends_on=state["previous"],
                inputs=inputs,
                step=step,
                instructions=EDIT_INSTRUCTIONS,
            )
        except Exception as exc:
            result = {"status": "failed", "worker": worker_id, "error": str(exc)}

        state["previous"] = [worker_task_id]
        state["result"] = result

        if result.get("status") != "completed":
            return False, ""

        deliverable = (result.get("report") or {}).get("deliverable")
        edits = parse_edits(deliverable)
        # M-2: ranges of big files the worker asked to see next round.
        state["needs"] = parse_needs(deliverable)
        outcome = apply_edits(root, edits)
        state["rejected"] += len(outcome["rejected"])
        state["changed"] += [p for p in outcome["applied"] if p not in state["changed"]]

        add_activity(
            task_id=parent_task_id,
            agent="orchestrator",
            event="code_edit",
            status="applied" if not outcome["rejected"] else "partial",
            worker=worker_id,
            worker_task_id=worker_task_id,
            round=state["rounds"],
            blocks=len(edits),
            applied=outcome["applied"],
            rejected=outcome["rejected"],
        )

        if state["needs"]:
            add_activity(
                task_id=parent_task_id,
                agent="orchestrator",
                event="code_need",
                status="asked",
                worker=worker_id,
                round=state["rounds"],
                ranges=[f"{n['path']}:{n['start']}-{n['end']}" for n in state["needs"]],
            )

        if not edits and state["rounds"] == 1 and not code["test"] and not state["needs"]:
            return False, ""

        if code["test"]:
            state["test"] = await run_tests(parent_task_id, code["test"])
            if state["test"].get("status") != "ok":
                return False, ""

        notes = feedback(state["test"], outcome["rejected"])
        return bool(notes) or bool(state["needs"]), notes

    notes = ""
    for _ in range(MAX_ROUNDS):
        go_on, notes = await one_round(notes, "code")
        if not go_on:
            break

    diff = None
    review = None
    review_id = None
    verdict = None
    revisions = 0

    if state["result"].get("status") == "completed" and state["changed"]:
        diff = await run_diff(parent_task_id)

    if reviewer_id and diff and diff.get("status") == "ok" and diff.get("files"):
        # J-6: the reviewer looks again after each fix round, at most MAX_REVIEW_ROUNDS.
        while True:
            review_id = str(uuid.uuid4())
            last_id = state["previous"][0]
            try:
                review = await run_worker(
                    reviewer_id,
                    review_task(task),
                    parent_task_id,
                    "review",
                    REVIEW_REPORT_CHARS,
                    worker_task_id=review_id,
                    depends_on=[last_id],
                    inputs=diff_input(diff, state["test"], repo_files(root)),
                    step="review",
                    instructions=REVIEW_INSTRUCTIONS,
                )
            except Exception as exc:
                review = {"status": "failed", "worker": reviewer_id, "error": str(exc)}

            verdict = None
            if review.get("status") == "completed":
                verdict = (review.get("report") or {}).get("verdict")

            if verdict != "revise" or revisions == MAX_REVIEW_ROUNDS:
                break

            state["previous"] = [last_id, review_id]
            await one_round(review_findings(review), "revision")
            revisions += 1
            if state["result"].get("status") != "completed":
                break
            diff = await run_diff(parent_task_id)
            if not (diff and diff.get("status") == "ok" and diff.get("files")):
                break

        approved = verdict == "approve" and state["result"].get("status") == "completed"
        add_activity(
            task_id=parent_task_id,
            agent="orchestrator",
            event="review",
            status="done" if approved else "needs_review",
            worker=worker_id,
            reviewer=reviewer_id,
            verdict=verdict,
            revised=revisions > 0,
            rounds=revisions,
        )

    result, test = state["result"], state["test"]
    report = {
        key: value
        for key, value in (result.get("report") or {}).items()
        if key != "deliverable"
    }

    if test is None:
        tests = "not_run"
    elif test.get("status") != "ok":
        tests = "error"
    else:
        tests = "passed" if test.get("passed") else "failed"

    sandbox = {
        "repo": code["repo"],
        "task": parent_task_id,
        "rounds": state["rounds"],
        "changed_files": state["changed"],
        "tests": tests,
        "rejected_blocks": state["rejected"],
        "note": NOTHING_LIVE,
    }
    if tests == "error":
        sandbox["test_error"] = test.get("error")

    if diff is not None:
        if diff.get("status") == "ok":
            sandbox["patch"] = {
                "files": diff.get("files"),
                "added": diff.get("added"),
                "removed": diff.get("removed"),
                "conflicts": diff.get("conflicts"),
                "path": diff.get("patch"),
                "apply": f"scripts/apply_patch.sh {parent_task_id}",
            }
        else:
            sandbox["patch_error"] = diff.get("error")

    outcome = {
        "status": result.get("status", "failed"),
        "worker": worker_id,
        **({"report": report} if report else {}),
        **({"error": result["error"]} if result.get("error") else {}),
        "sandbox": sandbox,
    }

    approved = False
    if review is not None:
        outcome["review"] = review_summary(review, review_id, verdict)
        approved = verdict == "approve" and outcome["status"] == "completed"
        outcome["review_status"] = "approved" if approved else "needs_review"
        if not approved:
            outcome["note"] = NEEDS_REVIEW_NOTE
        elif revisions:
            outcome["note"] = REVIEWED_AFTER_FIX

    # M-3a: the facts of the change, written by the orchestrator; the
    # final answer carries them and the task is not done unless all hold.
    done = bool(state["changed"]) and tests == "passed" and approved
    if not state["changed"]:
        outcome["note"] = NO_CHANGE_NOTE
    add_activity(
        task_id=parent_task_id,
        agent="orchestrator",
        event="code_result",
        status="ok" if done else "needs_review",
        repo=code["repo"],
        changed=state["changed"],
        tests=tests,
        reviewer=reviewer_id,
        verdict=verdict if review is not None else None,
    )

    return outcome


async def execute_repo_tool(
    name: str,
    arguments: dict,
    task_id: str,
    reads: set,
) -> dict:
    """
    repo_read (I-2, read-only): a file with path, else a file list.
    Errors go back to the model.
    """
    try:
        repo = check_repo_name(arguments.get("repo"))
        listing = not arguments.get("path")

        if not listing:
            offset = note_offset(arguments.get("offset"))
            key = ("repo", repo, str(arguments.get("path")), offset)

            if key in reads:
                return {
                    **repeated_note(arguments.get("path"), offset),
                    "note": "Dosyanın bu kısmı bu görevde zaten okundu.",
                }

        root = await ensure_sandbox(task_id, repo)

        if listing:
            return {"repo": repo, **list_files(root, arguments.get("pattern"))}

        result = compact_note(read_file(root, arguments.get("path")), offset)
        reads.add(key)

        if result.get("truncated"):
            result["note"] = (
                f"Dosya kırpıldı; devamı için repo_read(offset="
                f"{result['next_offset']})."
            )

        return {"repo": repo, **result}

    except RepoError as exc:
        return {"status": "error", "error": str(exc)}


# --------------------------------------------------
# Tool path extraction
# --------------------------------------------------

def get_tool_path(
    name: str,
    arguments: dict,
):
    if name in (
        "create_note",
        "update_note",
    ):
        return arguments.get("path")

    if name == "move_note":
        return arguments.get("destination")

    return None


# --------------------------------------------------
# API models
# --------------------------------------------------

class TaskRequest(BaseModel):
    message: str
    # Same key + same message within 24 h: the existing task is
    # returned instead of a new run (plan-e karar H).
    idempotency_key: str | None = None
    # K-2 (plan-k karar J): answer with the task id at once; the task
    # runs on in this process and the page follows it through /tasks.
    background: bool = False


class ResolveFactRequest(BaseModel):
    # keep_old: the flagged (new) fact is superseded.
    # use_new: it becomes active, the old one is superseded.
    choice: str


class AutoApprovalRequest(BaseModel):
    agent: str = "obsidian"
    actions: list[str] = Field(
        min_length=1
    )
    paths: list[str] = Field(
        default_factory=lambda: ["*"]
    )
    duration_minutes: int = Field(
        gt=0,
        le=10080,
    )

def get_worker_context(
    task_id: str,
):
    task = TASKS.get(task_id)

    if not task:
        raise WorkerError(
            f"Task not found: {task_id}"
        )

    worker_id = task.get("worker")

    if not worker_id:
        raise WorkerError(
            f"Task has no worker: {task_id}"
        )

    task_chars = len(task.get("message") or "")

    # The task text is sent as the user message; do not repeat it.
    context = worker_manager.build_worker_context(
        worker_id,
        task_id,
        include_task=False,
        budget_chars=ARES_CONTEXT_CHARS,
        task_chars=task_chars,
        retrieved=retrieve_memory(
            task_id,
            task.get("message") or "",
            worker_id,
            with_episodes=True,
        ),
        retrieved_chars=ARES_RETRIEVAL_CHARS,
        retrieved_note=ARES_RETRIEVED_NOTE,
    )

    warn_context_budget(
        task_id,
        worker_id,
        context,
        task_chars,
        ARES_CONTEXT_CHARS,
    )

    return context


def retrieve_memory(
    task_id: str,
    text: str,
    worker_id: str,
    with_episodes: bool = False,
) -> list[str]:
    """
    Memory lines for a task context (Faz F, plan-f karar G): active
    facts of scope global + this worker, and for Ares the episodes of
    earlier tasks, best match first. Without a store or a match: [].
    """
    query = fts_query(text)

    if task_store is None or query is None:
        return []

    now = utc_now()

    facts = store_write(
        "search_facts",
        query,
        ["global", worker_id],
        now,
        MAX_RETRIEVED_FACTS,
    ) or []

    episodes = []
    duplicates = 0

    if with_episodes:
        found = store_write(
            "search_episodes",
            query,
            now,
            MAX_RETRIEVED_EPISODES + DUPLICATE_SEARCH_SLACK,
            task_id,
        ) or []
        episodes = [
            episode for episode in found
            if not near_duplicate(episode["title"], text)
        ]
        duplicates = len(found) - len(episodes)
        episodes = episodes[:MAX_RETRIEVED_EPISODES]

    if facts or episodes or duplicates:
        add_activity(
            task_id=task_id,
            agent=worker_id,
            event="memory",
            status="retrieved",
            facts=[fact["fact_id"] for fact in facts],
            episodes=[episode["task_id"] for episode in episodes],
            duplicates_skipped=duplicates,
        )

    return retrieved_lines(facts, episodes)


def warn_context_budget(
    task_id: str,
    agent: str,
    context: str,
    task_chars: int,
    budget_chars: int,
):
    """
    Rules and the task text are never cut, so a context can end up
    over its budget. That is not an error; it is made visible.
    """
    over_chars = context_over_budget(
        context,
        budget_chars,
        task_chars,
    )

    if over_chars:
        add_activity(
            task_id=task_id,
            agent=agent,
            event="context_budget",
            status="exceeded",
            context_chars=len(context),
            task_chars=task_chars,
            budget_chars=budget_chars,
            over_chars=over_chars,
        )

# --------------------------------------------------
# LLM execution
# --------------------------------------------------

async def run_worker(
    worker_id: str,
    task: str,
    parent_task_id: str,
    task_type: str | None = None,
    report_chars: int | None = None,
    worker_task_id: str | None = None,
    depends_on: list[str] = (),
    inputs: str = "",
    step: str | None = None,
    instructions: str = "",
    search: list[str] = (),
):
    """
    run_worker_steps() recorded as a child task of parent_task_id
    in the task store (kind "worker"). depends_on: worker task ids
    whose reports are in `inputs` (Faz G). step: "review" or
    "revision" in a review chain (G-3). search: queries run before
    the call; the results go to the worker as sources (Faz H).
    """
    worker_task_id = worker_task_id or str(uuid.uuid4())
    record = {
        "task_id": worker_task_id,
        "parent_id": parent_task_id,
        "kind": "worker",
        "worker": worker_id,
        "message": task,
        "status": "running",
        "created_at": utc_now(),
        "depends_on": list(depends_on),
        "step": step,
    }
    store_write("save_task", record)

    def finish(status: str, error=None, result=None):
        store_write(
            "save_task",
            {
                "task_id": worker_task_id,
                "status": status if status in TASK_STATUSES else "failed",
                "error": error,
                "result": result,
                "completed_at": utc_now(),
            },
        )

    sources = []

    try:
        if search:
            sources = await run_search(
                search,
                parent_task_id,
                worker_id,
                worker_task_id,
            )
            block = sources_block(sources, failed=not sources)
            inputs = f"{inputs}\n\n{block}" if inputs else block
            instructions = (
                f"{instructions}\n\n{CITATION_INSTRUCTIONS}"
                if instructions
                else CITATION_INSTRUCTIONS
            )

        result = await run_worker_steps(
            worker_id,
            task,
            parent_task_id,
            task_type,
            report_chars,
            worker_task_id=worker_task_id,
            inputs=inputs,
            instructions=instructions,
        )

    except asyncio.CancelledError:
        finish("cancelled", "cancelled with the parent task")
        raise

    except Exception as exc:
        finish("failed", str(exc))
        raise

    if sources and result.get("report"):
        attach_sources(
            result,
            sources,
            parent_task_id,
            worker_id,
            worker_task_id,
        )

    finish(
        result.get("status", "failed"),
        result.get("error"),
        result.get("report"),
    )

    return result


async def run_search(
    queries: list[str],
    parent_task_id: str,
    worker_id: str,
    worker_task_id: str,
) -> list[dict]:
    """
    Run the queries at once (Faz H, plan-h karar 3) and keep the
    numbered sources of the worker task. A failed query is logged and
    left out; [] when nothing was found or search is unavailable.
    """

    async def one(query: str) -> list[dict]:
        started = time.monotonic()

        def note(status: str, **details):
            add_activity(
                task_id=parent_task_id,
                agent="orchestrator",
                event="search",
                status=status,
                worker=worker_id,
                worker_task_id=worker_task_id,
                query=query,
                ms=int((time.monotonic() - started) * 1000),
                **details,
            )

        if not TAVILY_API_KEY:
            note("unavailable", error="TAVILY_API_KEY is not set")
            return []

        try:
            async with httpx.AsyncClient(
                timeout=SEARCH_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(
                    TAVILY_URL,
                    headers={
                        "Authorization": f"Bearer {TAVILY_API_KEY}",
                    },
                    json={
                        "query": query,
                        "max_results": RESULTS_PER_QUERY,
                        "search_depth": SEARCH_DEPTH,
                    },
                )

            response.raise_for_status()
            results = tavily_results(response.json())

        except httpx.HTTPStatusError as exc:
            note("failed", error=f"HTTP {exc.response.status_code}")
            return []

        except (httpx.HTTPError, ValueError) as exc:
            note("failed", error=type(exc).__name__)
            return []

        note("done", results=len(results))
        return results

    found = await asyncio.gather(*(one(query) for query in queries))
    sources = merge_results(list(zip(queries, found)))

    if sources:
        store_write("save_sources", worker_task_id, sources)

    return sources


def attach_sources(
    result: dict,
    sources: list[dict],
    parent_task_id: str,
    worker_id: str,
    worker_task_id: str,
):
    """
    Citation check (plan-h karar 5): [n] that match no source are
    reported, the report is kept. The cited sources go with the
    report, so Ares and dependent tasks see what [n] points to.
    """
    report = result["report"]
    cited = cited_numbers(report)
    unknown = [n for n in cited if n > len(sources)]

    if unknown:
        add_activity(
            task_id=parent_task_id,
            agent="orchestrator",
            event="citation_unknown",
            status="warning",
            worker=worker_id,
            worker_task_id=worker_task_id,
            numbers=unknown,
            sources=len(sources),
        )

    report["sources"] = source_list(sources, cited)
    # H-3: create_note(from_task=...) saves it as a research note.
    result["task_id"] = worker_task_id


def prepare_create_note(arguments: dict) -> dict:
    """
    create_note arguments before the approval (H-3). With from_task
    the note is built from that worker task's report and sources, so
    the approval holds the exact text that will be written. Raises
    ValueError; the message is for the model.
    """
    if "from_task" not in arguments:
        if not arguments.get("path") or "content" not in arguments:
            raise ValueError("create_note needs path and content, or from_task.")
        return arguments

    if "content" in arguments:
        raise ValueError("Give content or from_task, not both.")

    task = store_write("get_task", arguments["from_task"])

    if (
        not task
        or task.get("kind") != "worker"
        or task.get("status") != "completed"
        or not isinstance(task.get("result"), dict)
    ):
        raise ValueError(
            "from_task must be the task_id of a completed worker task."
        )

    day = utc_now()[:10]
    sources = store_write("list_sources", task["task_id"]) or []

    return {
        "path": arguments.get("path") or research_note_path(task.get("message"), day),
        "content": research_note(task, sources, day),
    }


async def run_worker_steps(
    worker_id: str,
    task: str,
    parent_task_id: str,
    task_type: str | None = None,
    report_chars: int | None = None,
    worker_task_id: str | None = None,
    inputs: str = "",
    instructions: str = "",
):
    """
    Run a delegated worker using its persistent identity
    and a disposable daily task memory.

    Ares receives the worker's report cut to report_chars
    (default: the whole delegation budget); the full output
    stays in the worker's daily file.

    inputs: reports of the tasks this one depends on (Faz G),
    sent after the task text; counted as task text.
    instructions: added after the report schema (review calls).
    """

    report_chars = report_chars or report_share(1)

    if not worker_manager.worker_exists(worker_id):
        raise WorkerError(
            f"Worker does not exist: {worker_id}"
        )

    worker_task_id = worker_task_id or str(uuid.uuid4())

    worker_manager.create_daily_task(
        worker_id,
        worker_task_id,
        task,
    )

    def update_worker_memory(
        section: str,
        content: str,
        append: bool = False,
    ):
        try:
            if append:
                worker_manager.append_daily_section(
                    worker_id,
                    worker_task_id,
                    section,
                    content,
                )
            else:
                worker_manager.update_daily_section(
                    worker_id,
                    worker_task_id,
                    section,
                    content,
                )

        except WorkerError as exc:
            add_activity(
                task_id=parent_task_id,
                agent=worker_id,
                event="worker_memory",
                status="failed",
                worker=worker_id,
                worker_task_id=worker_task_id,
                error=str(exc),
            )

    user_message = f"{task}\n\n{inputs}" if inputs else task

    # The task text is sent as the user message; do not repeat it.
    worker_context = worker_manager.build_worker_context(
        worker_id,
        worker_task_id,
        include_task=False,
        budget_chars=WORKER_CONTEXT_CHARS,
        task_chars=len(user_message),
        retrieved=retrieve_memory(
            parent_task_id,
            task,
            worker_id,
        ),
        retrieved_chars=WORKER_RETRIEVAL_CHARS,
    )

    warn_context_budget(
        parent_task_id,
        worker_id,
        worker_context,
        len(user_message),
        WORKER_CONTEXT_CHARS,
    )

    update_worker_memory(
        "Work Log",
        (
            f"Delegated task started at {utc_now()}.\n"
            f"Parent task: {parent_task_id}"
        ),
        append=True,
    )

    worker_prompt = (
        "Sen SAVAS AI içindeki uzman bir workersın.\n\n"
        "Aşağıdaki persistent worker context senin kimliğini, "
        "kişiliğini, kurallarını ve kalıcı bilgilerini tanımlar. "
        "Bu kurallara uy.\n\n"
        "Görevi bağımsız ve uzman bir şekilde analiz et. "
        "Sonucunu Orchestrator olan Ares'e rapor olarak "
        "döndür.\n\n"
        f"{report_instructions(report_chars)}\n\n"
        f"{instructions + chr(10) + chr(10) if instructions else ''}"
        "========================================\n"
        "PERSISTENT WORKER CONTEXT\n"
        "========================================\n\n"
        f"{worker_context}"
    )

    messages = [
        {
            "role": "system",
            "content": worker_prompt,
        },
        {
            "role": "user",
            "content": user_message,
        },
    ]

    # --------------------------------------------------
    # Worker budget check
    # --------------------------------------------------

    serialized_messages = json.dumps(
        messages,
        ensure_ascii=False,
    )

    estimated_prompt_tokens = max(
        len(serialized_messages) // 4,
        1,
    )

    completion_reserve = int(
        os.getenv(
            "COMPLETION_TOKEN_RESERVE",
            "2000",
        )
    )

    # A cloud call is capped to what a report of this size
    # needs. The local model thinks before it answers and
    # keeps the full reserve.
    cloud_completion_limit = worker_completion_tokens(
        report_chars,
        completion_reserve,
    )

    selection = route_model(
        worker_id,
        task_type,
        requested_tokens=(
            estimated_prompt_tokens
            + cloud_completion_limit
        ),
        context_tokens=(
            estimated_prompt_tokens
            + completion_reserve
        ),
    )

    # Primary model, then at most one cloud fallback and the
    # local model (C-6, C-8.2).
    models = [selection.model, *selection.fallbacks[:2]]
    route_reason = selection.reason

    for attempt, model in enumerate(models):
        is_fallback = attempt > 0

        location = (
            LOCAL
            if model in selection.local
            else "cloud"
        )

        completion_limit = (
            completion_reserve
            if location == LOCAL
            else cloud_completion_limit
        )

        # What is reserved is what the call is capped to.
        requested_tokens = (
            estimated_prompt_tokens
            + completion_limit
        )

        # Every attempt reserves again. A budget refusal is
        # never retried on another model.
        try:
            budget = await budget_manager.reserve(
                agent=worker_id,
                task_id=parent_task_id,
                requested_tokens=requested_tokens,
                location=location,
            )

        except BudgetExceeded as exc:
            # Blocked workers return a result instead of raising,
            # so Ares sees the refusal and the parent task continues.
            if is_fallback:
                add_fallback_activity(
                    parent_task_id,
                    worker_id,
                    "blocked",
                    models[attempt - 1],
                    model,
                )

            update_worker_memory(
                "Status",
                "blocked",
            )

            add_activity(
                task_id=parent_task_id,
                agent=worker_id,
                event="budget_check",
                status="blocked",
                worker=worker_id,
                worker_task_id=worker_task_id,
                requested_tokens=requested_tokens,
                error=str(exc),
            )

            return {
                "status": "blocked",
                "worker": worker_id,
                "worker_task_id": worker_task_id,
                "error": str(exc),
                "message": (
                    "Token bütçesi aşıldı. Bu worker'ı bu görevde "
                    "tekrar deneme; kullanıcıya bütçe sınırını bildir."
                ),
            }

        try:
            add_activity(
                task_id=parent_task_id,
                agent=worker_id,
                event="budget_check",
                status="allowed",
                worker=worker_id,
                worker_task_id=worker_task_id,
                requested_tokens=requested_tokens,
                estimated_prompt_tokens=estimated_prompt_tokens,
                completion_reserve=completion_limit,
                budget=budget,
            )

            worker_start = time.perf_counter()

            if not is_fallback:
                add_activity(
                    task_id=parent_task_id,
                    agent=worker_id,
                    event="worker_started",
                    status="started",
                    worker=worker_id,
                    worker_task_id=worker_task_id,
                    model=model,
                    route_reason=route_reason,
                )

            async with httpx.AsyncClient(
                timeout=120
            ) as client:

                response = await post_chat(
                    client,
                    location,
                    {
                        "model": model,
                        "messages": messages,
                        "max_tokens": completion_limit,
                    },
                )

                response.raise_for_status()

                data = response.json()

            note_llm_success(
                model,
                parent_task_id,
                worker_id,
            )

            if is_fallback:
                add_fallback_activity(
                    parent_task_id,
                    worker_id,
                    "completed",
                    models[attempt - 1],
                    model,
                )

            worker_response = (
                data["choices"][0]["message"].get(
                    "content",
                    "",
                )
            )

            duration_ms = round(
                (time.perf_counter() - worker_start) * 1000
            )

            update_worker_memory(
                "Current State",
                worker_response or "(No worker response)",
            )

            update_worker_memory(
                "Status",
                "completed",
            )

            update_worker_memory(
                "Work Log",
                (
                    f"Delegated task completed at {utc_now()}.\n"
                    f"Duration: {duration_ms} ms."
                ),
                append=True,
            )

            add_activity(
                task_id=parent_task_id,
                agent=worker_id,
                event="worker_completed",
                status="completed",
                worker=worker_id,
                worker_task_id=worker_task_id,
                duration_ms=duration_ms,
                prompt_tokens=data.get(
                    "usage",
                    {},
                ).get("prompt_tokens"),
                completion_tokens=data.get(
                    "usage",
                    {},
                ).get("completion_tokens"),
                total_tokens=data.get(
                    "usage",
                    {},
                ).get("total_tokens"),
            )

            record_usage_safely(
                task_id=parent_task_id,
                agent=worker_id,
                worker=worker_id,
                worker_task_id=worker_task_id,
                model=model,
                route_reason=route_reason,
                usage=data.get("usage", {}),
                duration_ms=duration_ms,
                location=location,
            )

            # Ares gets the compact report, not the raw output.
            report, parsed = parse_worker_report(
                worker_response,
                report_chars,
            )

            add_activity(
                task_id=parent_task_id,
                agent=worker_id,
                event="worker_report",
                status="parsed" if parsed else "fallback",
                worker=worker_id,
                worker_task_id=worker_task_id,
                report_chars=report_chars,
                response_chars=len(worker_response or ""),
                result_chars=serialized_length(report),
                finish_reason=data["choices"][0].get(
                    "finish_reason"
                ),
            )

            return {
                "status": "completed",
                "worker": worker_id,
                "report": report,
            }

        except Exception as exc:

            fallback_to = None

            if isinstance(exc, httpx.HTTPError):
                # Tokens may have been spent; record the estimate.
                record_usage_safely(
                    task_id=parent_task_id,
                    agent=worker_id,
                    worker=worker_id,
                    worker_task_id=worker_task_id,
                    model=model,
                    route_reason=route_reason,
                    usage={
                        "prompt_tokens": estimated_prompt_tokens,
                        "completion_tokens": 0,
                    },
                    status="failed",
                    location=location,
                )

                kind = note_llm_failure(
                    exc,
                    model,
                    parent_task_id,
                    worker_id,
                )

                if is_fallback:
                    add_fallback_activity(
                        parent_task_id,
                        worker_id,
                        "failed",
                        models[attempt - 1],
                        model,
                        error=str(exc),
                    )

                if kind != FATAL and attempt + 1 < len(models):
                    fallback_to = models[attempt + 1]

            if fallback_to:
                add_fallback_activity(
                    parent_task_id,
                    worker_id,
                    "started",
                    model,
                    fallback_to,
                    kind=kind,
                    error=str(exc),
                )

                route_reason = (
                    f"{selection.reason} → fallback ({kind})"
                )

                continue

            update_worker_memory(
                "Status",
                "failed",
            )

            update_worker_memory(
                "Current State",
                f"Worker failed: {exc}",
            )

            update_worker_memory(
                "Work Log",
                f"Delegated task failed at {utc_now()}.",
                append=True,
            )

            add_activity(
                task_id=parent_task_id,
                agent=worker_id,
                event="worker_completed",
                status="failed",
                worker=worker_id,
                worker_task_id=worker_task_id,
                error=str(exc),
            )

            raise

        finally:
            budget_manager.release(budget["reservation_id"])

async def run_workers_parallel(
    delegations: list[dict],
    parent_task_id: str,
):
    """
    Run several delegated workers (Faz G, plan-g): items without
    depends_on run concurrently as before; an item with depends_on
    runs after those items and gets their reports as inputs.

    Each delegation:
        {"worker": "<worker_id>", "task": "<task>",
         "id": "<optional>", "depends_on": ["<id>", ...]}

    Ares gets the full report of items no other item depends on;
    an item whose result went on gets a reference. An item whose
    dependency did not complete is not run ("skipped").
    """
    try:
        layers = plan_layers(delegations)
        searches = [
            clean_queries(item.get("search"))
            for item in delegations
        ]

    except (GraphError, SearchError) as exc:
        add_activity(
            task_id=parent_task_id,
            agent="orchestrator",
            event="delegation",
            status="invalid",
            error=str(exc),
        )
        return {
            "status": "error",
            "error": str(exc),
        }

    feeds = dependents(delegations)
    index_of = {
        item_id(item): index
        for index, item in enumerate(delegations)
        if item_id(item)
    }
    worker_task_ids = [str(uuid.uuid4()) for _ in delegations]
    results: list[dict | None] = [None] * len(delegations)

    # The delegation budget is split between the reports Ares gets.
    leaf_chars = report_share(len(delegations) - len(feeds))

    if len(layers) > 1:
        add_activity(
            task_id=parent_task_id,
            agent="orchestrator",
            event="delegation",
            status="planned",
            layers=[
                [delegations[index]["worker"] for index in layer]
                for layer in layers
            ],
        )

    async def run_one(index: int, report_chars: int):
        delegation = delegations[index]
        worker_id = delegation["worker"]
        needed = delegation.get("depends_on") or []

        not_done = [
            name for name in needed
            if results[index_of[name]].get("status") != "completed"
        ]

        if not_done:
            error = SKIPPED_ERROR.format(ids=", ".join(not_done))
            store_write(
                "save_task",
                {
                    "task_id": worker_task_ids[index],
                    "parent_id": parent_task_id,
                    "kind": "worker",
                    "worker": worker_id,
                    "message": delegation["task"],
                    "status": "skipped",
                    "error": error,
                    "depends_on": [worker_task_ids[index_of[name]] for name in needed],
                    "created_at": utc_now(),
                    "completed_at": utc_now(),
                },
            )
            add_activity(
                task_id=parent_task_id,
                agent="orchestrator",
                event="delegation",
                status="skipped",
                worker=worker_id,
                id=item_id(delegation),
                error=error,
            )
            return {
                "status": "skipped",
                "worker": worker_id,
                "error": error,
            }

        inputs = input_block([
            {
                "id": name,
                "worker": delegations[index_of[name]]["worker"],
                "worker_task_id": worker_task_ids[index_of[name]],
                "report": results[index_of[name]].get("report") or {},
            }
            for name in needed
        ])

        try:
            return await run_worker(
                worker_id,
                delegation["task"],
                parent_task_id,
                delegation.get("task_type"),
                report_chars,
                worker_task_id=worker_task_ids[index],
                depends_on=[worker_task_ids[index_of[name]] for name in needed],
                inputs=inputs,
                search=searches[index],
            )

        except Exception as exc:
            return {
                "status": "failed",
                "worker": worker_id,
                "error": str(exc),
            }

    for layer in layers:
        # A report that goes on to another worker shares the layer's
        # budget; one that goes to Ares shares the delegation budget.
        layer_chars = report_share(len(layer))
        outcomes = await asyncio.gather(
            *(
                run_one(index, layer_chars if index in feeds else leaf_chars)
                for index in layer
            )
        )
        for index, outcome in zip(layer, outcomes):
            results[index] = outcome

    returned = []
    for index, delegation in enumerate(delegations):
        result = results[index]
        name = item_id(delegation)

        if index in feeds and result.get("status") == "completed":
            returned.append(reference(name, result, worker_task_ids[index]))
        elif name:
            returned.append({"id": name, **result})
        else:
            returned.append(result)

    return {
        "status": "completed",
        "delegations": returned,
    }

async def run_reviewed_worker(
    worker_id: str,
    task: str,
    parent_task_id: str,
    task_type: str | None,
    reviewer_id: str,
    search: list[str] = (),
):
    """
    Review chain (Faz G-3, plan-g karar E; J-6): the worker's output
    goes to the reviewer, who answers approve or revise. On revise the
    worker runs again with its last output and the review as inputs,
    and the reviewer looks again: at most MAX_REVIEW_ROUNDS fix rounds.
    Output not approved in the end (revise, no verdict, a failed
    review or revision) carries review_status "needs_review"; the
    task then ends needs_review, not completed.

    Ares gets the final output, the last review and, after a revision,
    a reference to the draft.
    """
    if reviewer_id == worker_id:
        return {
            "status": "error",
            "error": "review_by must be another worker.",
        }

    output_chars = report_share(1) - REVIEW_REPORT_CHARS
    draft_id = str(uuid.uuid4())

    def as_input(name: str, result: dict, worker_task_id: str) -> dict:
        return {
            "id": name,
            "worker": result.get("worker"),
            "worker_task_id": worker_task_id,
            "report": result.get("report") or {},
        }

    def note(status: str, **details):
        add_activity(
            task_id=parent_task_id,
            agent="orchestrator",
            event="review",
            status=status,
            worker=worker_id,
            reviewer=reviewer_id,
            **details,
        )

    async def attempt(*args, **kwargs) -> dict:
        try:
            return await run_worker(*args, **kwargs)
        except Exception as exc:
            return {
                "status": "failed",
                "worker": args[0],
                "error": str(exc),
            }

    draft = await attempt(
        worker_id,
        task,
        parent_task_id,
        task_type,
        output_chars,
        worker_task_id=draft_id,
        search=search,
    )

    if draft.get("status") != "completed":
        note("not_run", error="the draft did not complete")
        return draft

    current, current_id, revisions = draft, draft_id, 0
    error = None
    while True:
        review_id = str(uuid.uuid4())
        review = await attempt(
            reviewer_id,
            review_task(task),
            parent_task_id,
            "review",
            REVIEW_REPORT_CHARS,
            worker_task_id=review_id,
            depends_on=[current_id],
            inputs=input_block([as_input("draft", current, current_id)]),
            step="review",
            instructions=REVIEW_INSTRUCTIONS,
        )

        verdict = None
        if review.get("status") == "completed":
            verdict = (review.get("report") or {}).get("verdict")
            if verdict is None:
                note("verdict_missing")

        if verdict != "revise" or revisions == MAX_REVIEW_ROUNDS:
            break

        revision_id = str(uuid.uuid4())
        revision = await attempt(
            worker_id,
            revision_task(task),
            parent_task_id,
            task_type,
            output_chars,
            worker_task_id=revision_id,
            depends_on=[current_id, review_id],
            inputs=input_block([
                as_input("draft", current, current_id),
                as_input("review", review, review_id),
            ]),
            step="revision",
        )
        if revision.get("status") != "completed":
            error = {"status": revision.get("status"), "task_id": revision_id, "error": revision.get("error")}
            break
        current, current_id = revision, revision_id
        revisions += 1

    approved = verdict == "approve"
    note("done" if approved else "needs_review", verdict=verdict, revised=revisions > 0,
         rounds=revisions, **({"error": error["error"]} if error else {}))

    result = {
        **current,
        "review": review_summary(review, review_id, verdict),
        "review_status": "approved" if approved else "needs_review",
    }
    if revisions:
        result["draft"] = reference("draft", draft, draft_id)
    if error:
        result["revision"] = error
    if not approved:
        result["note"] = NEEDS_REVIEW_NOTE
    elif revisions:
        result["note"] = REVIEWED_AFTER_FIX
    return result


SYSTEM_PROMPT = (
    "Sen SAVAS AI'ın Orchestrator ajanısın. "
    "Aşağıdaki persistent worker context senin kimliğini, "
    "kişiliğini, kurallarını ve hafızanı tanımlar; bunlara uy. "
    "Kullanıcının isteğini analiz et ve gerektiğinde araçları "
    "kullan: notlardaki bilgi için önce search_notes, belirli "
    "bir not için read_note, klasör yapısı için list_folder, "
    "not oluşturma, güncelleme veya taşıma için ilgili write "
    "tool'u. "
    "read_note sonucu truncated ise notun yalnızca bir kısmını "
    "gördün; devamını offset ile okumadan o notu update_note "
    "ile yeniden yazma. "
    "Uzmanlık gerektiren görevleri uygun worker'a delegate et; "
    "delegation sonucunu değerlendir ve kullanıcıya nihai "
    "cevabı sen ver. "
    "Write işlemleri Approval Layer tarafından kontrol edilir. "
    "Onay bekleyen bir write tool'u kendin tekrar deneme. "
    "Daily work memory'deki bulguları, kararları ve mevcut "
    "durumu dikkate al. "
    "Araçlardan gelen sonuçları kullanıcıya doğru ve "
    "anlaşılır şekilde aktar. "
    "Bir bilgiyi kaydettiğini yalnızca remember aracını çağırıp "
    "sonucunu aldıysan söyle; çelişki sonucu gelirse bunu "
    "kullanıcıya bildir. "
    "Bir worker'ın çalıştığını yalnızca bu görevde delege edip "
    "sonucunu aldıysan söyle. "
    "Güncel ya da doğrulanması gereken bilgide (CVE, sürüm, haber, "
    "fiyat, mevzuat, kayıt numarası) kendi bilginle "
    "cevap verme: oracle'a search sorgularıyla delege et, kaynaklara "
    "dayan. Bilginin bir kesim tarihi var; bir şeyin olmadığını "
    "ondan çıkarma, aramada da bulunmazsa bunu söyle."
)


async def call_llm(
    client: httpx.AsyncClient,
    messages: list,
    task_id: str,
    iteration: int,
):
    # --------------------------------------------------
    # Budget estimation
    # --------------------------------------------------

    # Rough token estimate.
    # ~4 characters/token is a practical approximation
    # for mixed Turkish/English prompts.
    serialized_messages = json.dumps(
        messages,
        ensure_ascii=False,
    )

    estimated_prompt_tokens = max(
        len(serialized_messages) // 4,
        1,
    )

    completion_reserve = int(
        os.getenv(
            "COMPLETION_TOKEN_RESERVE",
            "2000",
        )
    )

    requested_tokens = (
        estimated_prompt_tokens
        + completion_reserve
    )

    selection = route_model(
        "ares",
        requested_tokens=requested_tokens,
    )

    # Primary model, then at most one cloud fallback and the
    # local model (C-6, C-8.2).
    models = [selection.model, *selection.fallbacks[:2]]
    route_reason = selection.reason

    for attempt, model in enumerate(models):
        is_fallback = attempt > 0

        location = (
            LOCAL
            if model in selection.local
            else "cloud"
        )

        # Every attempt reserves again. A budget refusal is
        # never retried on another model.
        try:
            budget = await budget_manager.reserve(
                agent="ares",
                task_id=task_id,
                requested_tokens=requested_tokens,
                location=location,
            )

        except BudgetExceeded:
            if is_fallback:
                add_fallback_activity(
                    task_id,
                    "orchestrator",
                    "blocked",
                    models[attempt - 1],
                    model,
                )

            raise

        try:
            add_activity(
                task_id=task_id,
                agent="orchestrator",
                event="budget_check",
                status="allowed",
                model=model,
                iteration=iteration,
                requested_tokens=requested_tokens,
                estimated_prompt_tokens=estimated_prompt_tokens,
                completion_reserve=completion_reserve,
                budget=budget,
            )

            # --------------------------------------------------
            # LLM call
            # --------------------------------------------------

            llm_start = time.perf_counter()

            add_activity(
                task_id=task_id,
                agent="orchestrator",
                event="llm_call",
                status="started",
                model=model,
                route_reason=route_reason,
                iteration=iteration,
            )

            response = await post_chat(
                client,
                location,
                {
                    "model": model,
                    "messages": messages,
                    "tools": TOOLS,
                    "tool_choice": "auto",
                    "max_tokens": completion_reserve,
                },
            )

            response.raise_for_status()

            data = response.json()

            note_llm_success(
                model,
                task_id,
                "orchestrator",
            )

            if is_fallback:
                add_fallback_activity(
                    task_id,
                    "orchestrator",
                    "completed",
                    models[attempt - 1],
                    model,
                )

            llm_duration_ms = round(
                (
                    time.perf_counter()
                    - llm_start
                ) * 1000
            )

            usage = data.get(
                "usage",
                {},
            )

            add_activity(
                task_id=task_id,
                agent="orchestrator",
                event="llm_call",
                status="completed",
                model=model,
                location=location,
                iteration=iteration,
                duration_ms=llm_duration_ms,
                prompt_tokens=usage.get(
                    "prompt_tokens"
                ),
                completion_tokens=usage.get(
                    "completion_tokens"
                ),
                total_tokens=usage.get(
                    "total_tokens"
                ),
            )

            # --------------------------------------------------
            # Token accounting
            # --------------------------------------------------

            record_usage_safely(
                task_id=task_id,
                agent="ares",
                model=model,
                route_reason=route_reason,
                usage=usage,
                duration_ms=llm_duration_ms,
                location=location,
            )

            return data

        except httpx.HTTPError as exc:
            # Tokens may have been spent; record the estimate.
            record_usage_safely(
                task_id=task_id,
                agent="ares",
                model=model,
                route_reason=route_reason,
                usage={
                    "prompt_tokens": estimated_prompt_tokens,
                    "completion_tokens": 0,
                },
                status="failed",
                location=location,
            )

            kind = note_llm_failure(
                exc,
                model,
                task_id,
                "orchestrator",
            )

            if is_fallback:
                add_fallback_activity(
                    task_id,
                    "orchestrator",
                    "failed",
                    models[attempt - 1],
                    model,
                    error=str(exc),
                )

            if kind == FATAL or attempt + 1 == len(models):
                raise

            add_fallback_activity(
                task_id,
                "orchestrator",
                "started",
                model,
                models[attempt + 1],
                kind=kind,
                error=str(exc),
            )

            route_reason = (
                f"{selection.reason} → fallback ({kind})"
            )

        finally:
            budget_manager.release(budget["reservation_id"])


async def run_task(
    task_id: str,
):
    context = TASK_CONTEXTS.get(task_id)

    if not context:
        raise RuntimeError(
            "Task context not found"
        )

    messages = context["messages"]

    try:
        async with httpx.AsyncClient(
            timeout=120
        ) as client:

            for iteration in range(
                context.get(
                    "next_iteration",
                    1,
                ),
                MAX_TOOL_ITERATIONS + 1,
            ):
                context["next_iteration"] = (
                    iteration + 1
                )

                data = await call_llm(
                    client,
                    messages,
                    task_id,
                    iteration,
                )

                assistant_message = data[
                    "choices"
                ][0]["message"]

                tool_calls = assistant_message.get(
                    "tool_calls"
                )

                if not tool_calls:

                    final_response = (
                        assistant_message.get(
                            "content",
                            "",
                        )
                    )

                    update_worker_daily_memory(
                        task_id,
                        "Current State",
                        final_response or "(No final response)",
                        append=True,
                    )

                    update_worker_daily_memory(
                        task_id,
                        "Work Log",
                        f"Task completed at {utc_now()}.",
                        append=True,
                    )

                    # M-3a: the code facts come from the orchestrator,
                    # never from the model's own account.
                    facts = code_fact_lines(task_id)
                    # M-3c: a claim with no event behind it is flagged, not believed.
                    unproven = unsupported_claims(
                        task_id, final_response, (TASKS.get(task_id) or {}).get("message", "")
                    )
                    if unproven:
                        add_activity(task_id=task_id, agent="orchestrator", event="claim_check",
                                     status="unsupported", claims=unproven)
                        facts = "\n".join(x for x in (facts, (
                            "⚠️ Sistem kaydıyla doğrulanamayan iddia: " + "; ".join(unproven)
                            + ". Bu görevde bunlara ait kayıt yok; İNCELENMELİ."
                        )) if x)
                    facts = "\n".join(x for x in (facts, local_answer_note(task_id)) if x)
                    if facts:
                        final_response = f"{final_response or ''}\n\n{facts}".strip()

                    # J-6: a review left open ends the task needs_review.
                    # M-3a: so does a code task without an approved change.
                    status = (
                        "needs_review"
                        if review_open(task_id) or code_open(task_id) or unproven
                        else "completed"
                    )

                    complete_task(
                        task_id,
                        status,
                    )

                    add_activity(
                        task_id=task_id,
                        agent="orchestrator",
                        event="task",
                        status=status,
                    )

                    TASK_CONTEXTS.pop(
                        task_id,
                        None,
                    )

                    return {
                        "status": status,
                        "task_id": task_id,
                        "model": data.get(
                            "model"
                        ),
                        "response": final_response,
                        "usage": data.get(
                            "usage",
                            {},
                        ),
                    }

                messages.append(
                    assistant_message
                )

                pending_created = False

                for tool_call in tool_calls:

                    function = tool_call[
                        "function"
                    ]

                    name = function[
                        "name"
                    ]

                    try:
                        arguments = json.loads(
                            function.get(
                                "arguments",
                                "{}",
                            )
                        )

                    except json.JSONDecodeError:

                        add_activity(
                            task_id=task_id,
                            agent="orchestrator",
                            event="tool_call",
                            status="failed",
                            tool=name,
                            error=(
                                "Invalid JSON arguments "
                                "from model"
                            ),
                        )

                        raise HTTPException(
                            status_code=502,
                            detail=(
                                "Invalid tool arguments "
                                "from model"
                            ),
                        )

                    # ------------------------------
                    # Memory (Faz F: no approval, SQLite only)
                    # ------------------------------

                    if name == "remember":

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": (
                                    tool_call["id"]
                                ),
                                "content": json.dumps(
                                    execute_memory_tool(
                                        arguments,
                                        task_id,
                                    ),
                                    ensure_ascii=False,
                                ),
                            }
                        )

                        continue

                    # ------------------------------
                    # Worker delegation
                    # ------------------------------

                    if name in (
                        "delegate_to_worker",
                        "delegate_to_workers",
                    ):

                        if name == "delegate_to_worker":
                            activity_worker = arguments.get(
                                "worker"
                            )

                            activity_event = "delegation"

                        else:
                            activity_worker = "parallel"

                            activity_event = (
                                "parallel_delegation"
                            )

                        add_activity(
                            task_id=task_id,
                            agent="orchestrator",
                            event=activity_event,
                            status="started",
                            worker=activity_worker,
                        )

                        try:
                            result = (
                                await execute_worker_tool(
                                    name,
                                    arguments,
                                    task_id,
                                )
                            )

                        except Exception as exc:
                            add_activity(
                                task_id=task_id,
                                agent="orchestrator",
                                event=activity_event,
                                status="failed",
                                worker=activity_worker,
                                error=str(exc),
                            )

                            raise

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": (
                                    tool_call["id"]
                                ),
                                "content": json.dumps(
                                    result,
                                    ensure_ascii=False,
                                ),
                            }
                        )

                        add_activity(
                            task_id=task_id,
                            agent="orchestrator",
                            event=activity_event,
                            status="completed",
                            worker=activity_worker,
                        )

                        continue

                    # ------------------------------
                    # Write operation
                    # ------------------------------

                    if name == "create_note":
                        try:
                            arguments = prepare_create_note(arguments)

                        except ValueError as exc:
                            add_activity(
                                task_id=task_id,
                                agent="orchestrator",
                                event="tool_call",
                                status="invalid",
                                tool=name,
                                error=str(exc),
                            )
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call["id"],
                                    "content": json.dumps(
                                        {"status": "error", "error": str(exc)},
                                        ensure_ascii=False,
                                    ),
                                }
                            )
                            continue

                    if name in WRITE_ACTIONS:

                        path = get_tool_path(
                            name,
                            arguments,
                        )

                        auto_policy = (
                            auto_approval_matches(
                                agent="obsidian",
                                action=name,
                                path=path,
                            )
                        )

                        if auto_policy:

                            add_activity(
                                task_id=task_id,
                                agent="orchestrator",
                                event="approval",
                                status="auto_approved",
                                action=name,
                                auto_approval_id=(
                                    auto_policy[
                                        "auto_approval_id"
                                    ]
                                ),
                                path=path,
                            )

                            result = (
                                await execute_write_tool(
                                    name,
                                    arguments,
                                    task_id,
                                )
                            )

                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": (
                                        tool_call["id"]
                                    ),
                                    "content": json.dumps(
                                        result,
                                        ensure_ascii=False,
                                    ),
                                }
                            )

                            continue

                        # --------------------------
                        # Manual approval required
                        # --------------------------

                        approval = create_approval(
                            task_id=task_id,
                            agent="obsidian",
                            action=name,
                            arguments=arguments,
                            reason=(
                                f"Obsidian write operation "
                                f"'{name}' requires approval."
                            ),
                            tool_call_id=(
                                tool_call["id"]
                            ),
                        )

                        context[
                            "pending_tools"
                        ][approval[
                            "approval_id"
                        ]] = {
                            "tool_call_id": (
                                tool_call["id"]
                            ),
                            "name": name,
                            "arguments": arguments,
                        }

                        pending_created = True

                        continue

                    # ------------------------------
                    # Read operation
                    # ------------------------------

                    result = await execute_read_tool(
                        name,
                        arguments,
                        task_id,
                    )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": (
                                tool_call["id"]
                            ),
                            "content": json.dumps(
                                result,
                                ensure_ascii=False,
                            ),
                        }
                    )

                if pending_created:

                    update_worker_daily_memory(
                        task_id,
                        "Current State",
                        (
                            "Task is waiting for user approval.\n"
                            f"Pending approvals: "
                            f"{len(get_pending_approvals_for_task(task_id))}"
                        ),
                    )

                    complete_task(
                        task_id,
                        "waiting_approval",
                    )

                    checkpoint_task(task_id)

                    add_activity(
                        task_id=task_id,
                        agent="orchestrator",
                        event="task",
                        status="waiting_approval",
                        pending_approvals=len(
                            get_pending_approvals_for_task(
                                task_id
                            )
                        ),
                    )

                    return {
                        "status": "waiting_approval",
                        "task_id": task_id,
                        "pending_approvals": (
                            get_pending_approvals_for_task(
                                task_id
                            )
                        ),
                    }

            complete_task(
                task_id,
                "failed",
            )

            add_activity(
                task_id=task_id,
                agent="orchestrator",
                event="task",
                status="failed",
                error=(
                    "Maximum tool-calling "
                    "iterations exceeded"
                ),
            )

            return {
                "status": "failed",
                "task_id": task_id,
                "error": (
                    "Maximum tool-calling "
                    "iterations exceeded"
                ),
            }

    except BudgetExceeded as exc:

        complete_task(
            task_id,
            "blocked",
        )

        add_activity(
            task_id=task_id,
            agent="orchestrator",
            event="task",
            status="blocked",
            error=str(exc),
        )

        raise HTTPException(
            status_code=429,
            detail=str(exc),
        )

    except httpx.HTTPStatusError as exc:

        complete_task(
            task_id,
            "failed",
        )

        add_activity(
            task_id=task_id,
            agent="orchestrator",
            event="task",
            status="failed",
            error=(
                "OmniRoute or Obsidian Agent "
                "returned an HTTP error"
            ),
            status_code=exc.response.status_code,
        )

        raise HTTPException(
            status_code=502,
            detail={
                "message": (
                    "OmniRoute or Obsidian Agent "
                    "returned an error"
                ),
                "status_code": (
                    exc.response.status_code
                ),
                "response": exc.response.text,
            },
        )

    except HTTPException:
        raise

    except Exception as exc:

        complete_task(
            task_id,
            "failed",
        )

        add_activity(
            task_id=task_id,
            agent="orchestrator",
            event="task",
            status="failed",
            error=str(exc),
        )

        raise HTTPException(
            status_code=502,
            detail=f"Orchestrator error: {exc}",
        )


# --------------------------------------------------
# Run segments: time limit, cancellation, result
# --------------------------------------------------

def task_events(task_id: str) -> list[dict]:
    events = store_write("list_events", task_id)
    if events is None:
        events = [event for event in ACTIVITY if event["task_id"] == task_id]
    return events


NO_CHANGE_NOTE = (
    "No change was applied to the copy: the task is NOT done. "
    "Say so plainly; do not claim a change, passing tests or a review."
)
CODE_TESTS_TEXT = {
    "passed": "geçti", "failed": "geçmedi", "not_run": "koşulmadı", "error": "koşulamadı",
}


def code_results(task_id: str) -> list[dict]:
    return [e for e in task_events(task_id) if e.get("event") == "code_result"]


def code_fact_lines(task_id: str) -> str:
    """M-3a: what the code tasks of this task really did, from the events."""
    lines = []
    for e in code_results(task_id):
        changed = ", ".join(e.get("changed") or []) or "yok"
        if e.get("verdict") == "approve":
            review = f"{e.get('reviewer')} onayladı"
        elif e.get("verdict"):
            review = f"{e.get('reviewer')} onaylamadı ({e['verdict']})"
        else:
            review = f"{e.get('reviewer') or 'inceleme'} incelemedi"
        tests = CODE_TESTS_TEXT.get(e.get("tests"), str(e.get("tests")))
        mark = "✅" if e.get("status") == "ok" else "⚠️ İNCELENMELİ"
        lines.append(
            f"🔧 Kod sonucu ({e.get('repo')}, sistemden): değişen dosya: {changed} · "
            f"testler: {tests} · {review} · {mark}"
        )
    return "\n".join(lines)


def code_open(task_id: str) -> bool:
    return any(e.get("status") != "ok" for e in code_results(task_id))


# M-3c: what the final answer claims must have a trace in the task's events.
CLAIMS = {
    "tests": (
        re.compile(r"test\w*[^.\n]{0,40}?\b(geçti|geçiyor|başarıyla|başarılı|passed|sorunsuz)", re.I),
        "testlerin geçtiği",
        lambda events: any(e.get("event") == "runner" and e.get("status") == "test" and e.get("passed")
                           for e in events),
    ),
    "review": (
        re.compile(r"(gordon|gandalf|incelem\w*|incelen\w*)[^.\n]{0,60}?(onay\w*|uygun bul\w*|approve\w*)", re.I),
        "incelemenin onaylandığı",
        lambda events: any(e.get("event") == "review" and e.get("status") == "done" for e in events),
    ),
    "change": (
        re.compile(r"(değişiklik\w*|düzenleme\w*|validasyon\w*|kontrol\w*|kod\w*)[^.\n]{0,60}?"
                   r"\b(yapıldı|eklendi|uygulandı|güncellendi)", re.I),
        "kod değişikliği yapıldığı",
        lambda events: any(e.get("event") == "code_result" and e.get("changed") for e in events),
    ),
}


def unsupported_claims(task_id: str, answer: str, message: str = "") -> list[str]:
    """
    Claims of the final answer with no event behind them. A change claim
    is only checked when the request names a code repository.
    """
    events = task_events(task_id)
    about_code = any(repo in (message or "").lower() for repo in KNOWN_REPOS)
    found = []
    for key, (pattern, label, proven) in CLAIMS.items():
        if key == "change" and not about_code:
            continue
        if pattern.search(answer or "") and not proven(events):
            found.append(label)
    return found


def local_answer_note(task_id: str) -> str:
    """L-2c: an Ares answer written (even in part) by the local model says so."""
    models = sorted({e.get("model") or "?" for e in task_events(task_id)
                     if e.get("event") == "llm_call" and e.get("status") == "completed" and e.get("location") == LOCAL})
    if not models:
        return ""
    return ("ℹ️ Bu cevap yerel modelle yanıtlandı (" + ", ".join(models) + "): bulut bütçesi doldu ya da "
            "bulut modeli yanıt vermedi. Yerel model daha zayıftır; sonucu kontrol et.")


def review_open(task_id: str) -> bool:
    """J-6: a review of this task ended unapproved (and no later manual review approved it)."""
    state = None
    for event in task_events(task_id):
        if event.get("event") == "review" and event.get("status") in ("done", "needs_review"):
            state = event["status"]
    return state == "needs_review"


def stop_task(
    task_id: str,
    status: str,
    error: str,
):
    complete_task(task_id, status, error=error)

    add_activity(
        task_id=task_id,
        agent="orchestrator",
        event="task",
        status=status,
        error=error,
    )

    update_worker_daily_memory(
        task_id,
        "Current State",
        f"Task stopped: {error}.",
    )

    TASK_CONTEXTS.pop(task_id, None)

    return {
        "status": status,
        "task_id": task_id,
        "error": error,
    }


def execute_memory_tool(
    arguments: dict,
    task_id: str,
) -> dict:
    """
    remember(): save a lasting fact (plan-f karar D, E, F, J).

    Same key + scope with the same value -> one more confirmation.
    With a different value -> the new fact is saved as "flagged", the
    old one stays active; the user decides (F-5).
    """
    if task_store is None:
        return {
            "status": "unavailable",
            "error": "Memory store is not available; nothing was saved.",
        }

    try:
        fact = new_fact(arguments, task_id, "ares")
    except ValueError as exc:
        add_activity(
            task_id=task_id,
            agent="ares",
            event="memory",
            status="invalid",
            error=str(exc),
        )
        return {"status": "error", "error": str(exc)}

    existing = store_write(
        "find_active_fact",
        fact["key_norm"],
        fact["scope"],
        fact["created_at"],
    )

    if existing and same_fact_value(existing["value"], fact["value"]):
        expires_at = later_expiry(existing["expires_at"], fact["expires_at"])
        store_write("confirm_fact", existing["fact_id"], expires_at)
        result = {
            "status": "confirmed",
            "fact_id": existing["fact_id"],
            "confirmations": existing["confirmations"] + 1,
            "expires_at": expires_at,
        }

    else:
        # The same different value given again: one more confirmation
        # of the fact already waiting for the user, not a second one.
        waiting = None

        if existing:
            fact["status"] = "flagged"
            waiting = next(
                (
                    flagged
                    for flagged in store_write(
                        "find_flagged_facts",
                        fact["key_norm"],
                        fact["scope"],
                    ) or []
                    if same_fact_value(flagged["value"], fact["value"])
                ),
                None,
            )

        if waiting:
            fact_id = waiting["fact_id"]
            store_write(
                "confirm_fact",
                fact_id,
                later_expiry(waiting["expires_at"], fact["expires_at"]),
            )
        else:
            fact_id = store_write("save_fact", fact)

        if fact_id is None:
            return {
                "status": "error",
                "error": "The fact could not be saved.",
            }

        if existing:
            result = {
                "status": "conflict",
                "fact_id": fact_id,
                "existing": {
                    "fact_id": existing["fact_id"],
                    "value": existing["value"],
                },
                "note": (
                    "A different value is already remembered. Both are "
                    "kept; tell the user so they can choose."
                ),
            }
        else:
            result = {
                "status": "saved",
                "fact_id": fact_id,
                "expires_at": fact["expires_at"],
            }

    add_activity(
        task_id=task_id,
        agent="ares",
        event="memory",
        status=result["status"],
        fact_id=result["fact_id"],
        key=fact["key"],
        scope=fact["scope"],
    )

    return result


def record_episode(
    task_id: str,
):
    """
    Finished top-level task -> episode in the store (Faz F, plan-f
    karar B: no LLM call). Without a store nothing is kept.
    """
    task = TASKS.get(task_id)

    if (
        task_store is None
        or not task
        or task["status"] not in FINAL_TASK_STATUSES
    ):
        return None

    approvals = {
        approval["approval_id"]: approval
        for approval in store_write(
            "list_approvals", None, task_id
        ) or []
    }

    for approval in APPROVALS.values():
        if approval["task_id"] == task_id:
            approvals[approval["approval_id"]] = approval

    episode = build_episode(
        task,
        store_write(
            "list_tasks", None, task_id, "worker", MAX_TASK_LIST_LIMIT
        ) or [],
        list(approvals.values()),
        store_write("list_events", task_id) or [],
    )

    episode_id = store_write("save_episode", episode)

    if episode_id is not None:
        add_activity(
            task_id=task_id,
            agent="orchestrator",
            event="memory",
            status="episode_saved",
            episode_id=episode_id,
        )

    return episode_id


async def run_tracked(
    task_id: str,
):
    """
    One run segment of a task (POST /task, or the rest of it after
    its approvals): run_task() within TASK_TIMEOUT_SECONDS,
    cancellable through POST /tasks/{id}/cancel. Time spent waiting
    for approval is not counted. The final result is kept on the
    task.
    """
    complete_task(task_id, "running")

    job = asyncio.ensure_future(run_task(task_id))
    RUNNING_TASKS[task_id] = job

    try:
        result = await asyncio.wait_for(
            job,
            TASK_TIMEOUT_SECONDS,
        )

    except asyncio.TimeoutError:
        result = stop_task(task_id, "failed", "timeout")

    except asyncio.CancelledError:
        if task_id not in CANCEL_REQUESTED:
            raise

        result = stop_task(task_id, "cancelled", "cancelled by user")

    except HTTPException as exc:
        # run_task has already set the status; keep why.
        task = TASKS.get(task_id)

        if task and task["status"] in FINAL_TASK_STATUSES:
            task["error"] = str(exc.detail)[:1000]
            persist_task(task_id)
            record_episode(task_id)

        raise

    finally:
        RUNNING_TASKS.pop(task_id, None)
        CANCEL_REQUESTED.discard(task_id)

    task = TASKS.get(task_id)

    if task and result.get("status") in FINAL_TASK_STATUSES:
        task["result"] = result
        task["error"] = task.get("error") or result.get("error")
        persist_task(task_id)
        record_episode(task_id)

    return result


# Background runs (K-2). A strong reference keeps the job alive.
BACKGROUND_JOBS = set()


async def run_in_background(
    task_id: str,
):
    """
    run_tracked() with nobody waiting for the answer: an error is
    kept on the task instead of going to a closed request.
    """
    try:
        await run_tracked(task_id)

    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
        task = TASKS.get(task_id)

        if task and task["status"] not in FINAL_TASK_STATUSES:
            stop_task(task_id, "failed", str(detail)[:1000])
        else:
            add_activity(
                task_id=task_id,
                agent="orchestrator",
                event="task",
                status="background_error",
                error=str(detail)[:1000],
            )


def find_idempotent_task(
    idempotency_key: str,
):
    """The task opened with this key in the last 24 hours, if any."""
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(seconds=IDEMPOTENCY_TTL_SECONDS)
    ).isoformat()

    for task in TASKS.values():
        if (
            task.get("idempotency_key") == idempotency_key
            and task["created_at"] >= cutoff
        ):
            return task

    store_write("release_idempotency_keys", cutoff)

    return store_write("find_task_by_key", idempotency_key)


# --------------------------------------------------
# Approval continuation
# --------------------------------------------------

async def continue_after_approval(
    approval_id: str,
):
    approval = APPROVALS.get(
        approval_id
    )

    if not approval:
        raise HTTPException(
            status_code=404,
            detail="Approval not found",
        )

    task_id = approval["task_id"]

    if is_proposal(task_id):
        return await finish_proposal(approval)

    context = TASK_CONTEXTS.get(task_id)

    if not context:
        raise HTTPException(
            status_code=409,
            detail=(
                "Task execution context is no longer available. "
                "The orchestrator may have been restarted."
            ),
        )

    pending_tool = context[
        "pending_tools"
    ].pop(
        approval_id,
        None,
    )

    if not pending_tool:
        raise HTTPException(
            status_code=409,
            detail=(
                "Pending tool context not found."
            ),
        )

    if approval["status"] == "approved":

        try:
            result = await execute_write_tool(
                pending_tool["name"],
                pending_tool["arguments"],
                task_id,
            )

            approval["status"] = "executed"
            approval["executed_at"] = utc_now()
            approval["updated_at"] = utc_now()
            persist_approval(approval)

            add_activity(
                task_id=task_id,
                agent="orchestrator",
                event="approval",
                status="executed",
                approval_id=approval_id,
                action=approval["action"],
            )

        except Exception as exc:

            approval["status"] = "failed"
            approval["updated_at"] = utc_now()
            persist_approval(approval)

            add_activity(
                task_id=task_id,
                agent="orchestrator",
                event="approval",
                status="execution_failed",
                approval_id=approval_id,
                action=approval["action"],
                error=str(exc),
            )

            result = {
                "status": "error",
                "error": str(exc),
            }

    else:

        result = {
            "status": "rejected",
            "message": (
                "The user rejected this operation."
            ),
        }

        add_activity(
            task_id=task_id,
            agent="orchestrator",
            event="approval",
            status="rejection_sent_to_llm",
            approval_id=approval_id,
            action=approval["action"],
        )

    context["messages"].append(
        {
            "role": "tool",
            "tool_call_id": pending_tool[
                "tool_call_id"
            ],
            "content": json.dumps(
                result,
                ensure_ascii=False,
            ),
        }
    )

    remaining = get_pending_approvals_for_task(
        task_id
    )

    if remaining:
        checkpoint_task(task_id)

        return {
            "status": "waiting_approval",
            "task_id": task_id,
            "approval_id": approval_id,
            "remaining_approvals": remaining,
        }
   
    update_worker_daily_memory(
        task_id,
        "Work Log",
        (
            f"Approval {approval_id} resolved with status "
            f"'{approval['status']}'."
        ),
    )

    # All approvals for this tool-call batch
    # are now resolved. Continue the LLM loop.
    complete_task(
        task_id,
        "running",
    )

    add_activity(
        task_id=task_id,
        agent="orchestrator",
        event="task",
        status="resumed",
    )

    update_worker_daily_memory(
        task_id,
        "Current State",
        "All pending approvals for the current tool batch were resolved. "
        "Task execution resumed.",
    )

    return await run_tracked(
        task_id
    )


# --------------------------------------------------
# Task store start-up
# --------------------------------------------------

def fail_stored_task(task_id: str, error: str):
    task_store.save_task(
        {
            "task_id": task_id,
            "status": "failed",
            "error": error,
            "completed_at": utc_now(),
        }
    )
    task_store.clear_context(task_id)

    add_activity(
        task_id=task_id,
        agent="orchestrator",
        event="task",
        status="failed",
        error=error,
    )


def set_stored_approval(approval: dict, status: str):
    task_store.save_approval(
        {"approval_id": approval["approval_id"], "status": status}
    )

    add_activity(
        task_id=approval["task_id"],
        agent=approval["agent"],
        event="approval",
        status=status,
        approval_id=approval["approval_id"],
        action=approval["action"],
    )


def open_task_store(path: str):
    """
    Open the store and settle what the last process left behind
    (plan-e, E-4):
    - runs it cut become failed (karar B);
    - an approved write that may or may not have run is never run
      again: the approval becomes interrupted, its task failed
      (karar C);
    - a task waiting for approval comes back with its context and
      approvals when every approval its context waits for is still
      pending; otherwise it fails;
    - pending approvals of tasks that did not come back are
      cancelled. Auto-approval policies are not kept (karar D).
    A store that cannot be opened stops the start-up.
    """
    global task_store

    task_store = TaskStore(path)

    for task_id in task_store.interrupt_tasks():
        add_activity(
            task_id=task_id,
            agent="orchestrator",
            event="task",
            status="failed",
            error=INTERRUPTED_ERROR,
        )

    for approval in task_store.list_approvals(status="approved"):
        set_stored_approval(approval, "interrupted")

        task = task_store.get_task(approval["task_id"])

        if task and task["status"] not in FINAL_TASK_STATUSES:
            fail_stored_task(task["task_id"], APPROVAL_INTERRUPTED)

    for task in task_store.list_tasks(status="waiting_approval", limit=500):
        task_id = task["task_id"]

        if task.get("kind") == "proposal":
            # A note proposal needs no LLM context: its approval holds the note.
            pending = task_store.list_approvals(status="pending", task_id=task_id)
            if not pending:
                fail_stored_task(task_id, APPROVAL_ROUND_INCOMPLETE)
                continue
            for approval in pending:
                APPROVALS[approval["approval_id"]] = approval
            add_activity(task_id=task_id, agent="orchestrator", event="task",
                         status="restored", pending_approvals=len(pending))
            continue

        context = task_store.load_context(task_id)
        waiting_for = list((context or {}).get("pending_tools", {}))
        approvals = [task_store.get_approval(i) for i in waiting_for]

        if not context:
            fail_stored_task(task_id, APPROVAL_CONTEXT_LOST)
            continue

        if not approvals or not all(
            approval and approval["status"] == "pending"
            for approval in approvals
        ):
            fail_stored_task(task_id, APPROVAL_ROUND_INCOMPLETE)
            continue

        TASKS[task_id] = {
            "task_id": task_id,
            "message": task["message"],
            "status": "waiting_approval",
            "created_at": task["created_at"],
            "completed_at": None,
            "agent": "orchestrator",
            "worker": task["worker"],
            "daily_memory": task["daily_memory"],
            "error": None,
            "result": None,
            "idempotency_key": task["idempotency_key"],
        }
        context.setdefault("reads", set())
        TASK_CONTEXTS[task_id] = context

        for approval in approvals:
            APPROVALS[approval["approval_id"]] = approval

        add_activity(
            task_id=task_id,
            agent="orchestrator",
            event="task",
            status="restored",
            pending_approvals=len(approvals),
        )

    for approval in task_store.list_approvals(status="pending"):
        if approval["approval_id"] not in APPROVALS:
            set_stored_approval(approval, "cancelled")

    task_store.interrupt_packages()
    task_store.interrupt_goals()

    now = datetime.now(timezone.utc)
    task_store.purge_events(
        (now - timedelta(days=EVENT_RETENTION_DAYS)).isoformat()
    )
    task_store.purge_episodes(now.isoformat())
    task_store.purge_facts(
        (now - timedelta(days=FACT_PURGE_DAYS)).isoformat()
    )
    task_store.release_idempotency_keys(
        (now - timedelta(seconds=IDEMPOTENCY_TTL_SECONDS)).isoformat()
    )

    return task_store


if TASK_DB_PATH:
    open_task_store(TASK_DB_PATH)


# --------------------------------------------------
# Health
# --------------------------------------------------

try:
    deleted_worker_files = (
        worker_manager.cleanup_old_daily_files()
    )

    if deleted_worker_files:
        print(
            "Worker daily memory cleanup removed "
            f"{len(deleted_worker_files)} file(s)."
        )

except Exception as exc:
    print(
        "Worker daily memory cleanup failed: "
        f"{exc}"
    )

@app.get("/health")
def health():
    cleanup_expired_auto_approvals()

    # Routes for a minimal request, so a used-up cloud budget shows.
    routes = {
        worker: route_model(
            worker,
            requested_tokens=int(
                os.getenv(
                    "COMPLETION_TOKEN_RESERVE",
                    "2000",
                )
            ),
        )
        for worker in worker_manager.list_workers()
    }

    return {
        "status": "ok",
        "service": "orchestrator",
        "model": MODEL,
        "routes": {
            worker: selection.model
            for worker, selection in routes.items()
        },
        "local_llm": {
            **local_llm_status(),
            "agents_on_local": sorted(
                worker
                for worker, selection in routes.items()
                if selection.model in selection.local
            ),
        },
        "unavailable_models": sorted(circuit.unavailable()),
        "model_alerts": recent_model_alerts(),
        "tools": [
            tool["function"]["name"]
            for tool in TOOLS
        ],
        "active_tasks": sum(
            1
            for task in TASKS.values()
            if task["status"] == "running"
        ),
        "waiting_approval_tasks": sum(
            1
            for task in TASKS.values()
            if task["status"]
            == "waiting_approval"
        ),
        "pending_approvals": sum(
            1
            for approval in APPROVALS.values()
            if approval["status"]
            == "pending"
        ),
        "active_auto_approvals": sum(
            1
            for policy in AUTO_APPROVALS.values()
            if policy["status"]
            == "active"
        ),
        "activity_events": len(ACTIVITY),
        "workers": {
            "default": DEFAULT_WORKER,
            "available": worker_manager.list_workers(),
            "retention_days": WORKER_RETENTION_DAYS,
        },
    }


# --------------------------------------------------
# Task / Activity endpoints
# --------------------------------------------------

DEFAULT_TASK_LIST_LIMIT = 50
MAX_TASK_LIST_LIMIT = 500


@app.get("/tasks")
async def list_tasks(
    status: str | None = None,
    limit: int = DEFAULT_TASK_LIST_LIMIT,
):
    """
    Top-level tasks, newest first. With a store this includes tasks
    from before a restart; the in-memory copy wins for the ones this
    process knows. Worker tasks are listed under GET /tasks/{id}.
    """
    if status is not None and status not in TASK_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown task status: {status}",
        )

    limit = max(1, min(int(limit), MAX_TASK_LIST_LIMIT))

    tasks = {
        task["task_id"]: task
        for task in store_write(
            "list_tasks", status, None, "task", limit
        ) or []
    }

    for task in TASKS.values():
        if status is None or task["status"] == status:
            tasks[task["task_id"]] = {
                **tasks.get(task["task_id"], {}),
                **task,
            }

    tasks = sorted(
        tasks.values(),
        key=lambda task: task["created_at"],
        reverse=True,
    )[:limit]

    return {
        "count": len(tasks),
        "tasks": tasks,
    }


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """
    A task with its events, worker tasks and approvals. Works for a
    worker task id too, and, with a store, after a restart.
    """
    stored = store_write("get_task", task_id)
    task = TASKS.get(task_id)

    if task is None and stored is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    task = {**(stored or {}), **(task or {})}

    task_activity = store_write("list_events", task_id)

    if task_activity is None:
        task_activity = [
            event
            for event in ACTIVITY
            if event["task_id"] == task_id
        ]

    children = store_write(
        "list_tasks", None, task_id, None, MAX_TASK_LIST_LIMIT
    ) or []

    # Faz H: search results a worker task got as sources.
    for item in [task, *children]:
        sources = store_write("list_sources", item.get("task_id"))
        if sources:
            item["sources"] = sources

    approvals = {
        approval["approval_id"]: approval
        for approval in store_write(
            "list_approvals", None, task_id
        ) or []
    }

    for approval in APPROVALS.values():
        if approval["task_id"] == task_id:
            approvals[approval["approval_id"]] = approval

    return {
        "task": task,
        "activity": task_activity,
        "children": list(reversed(children)),
        "approvals": sorted(
            approvals.values(),
            key=lambda approval: approval["created_at"],
        ),
    }


# --------------------------------------------------
# CEO Interface data (K-2, docs/plan-k.md)
# Read-only; the /ui page shows only what these return.
# --------------------------------------------------

ACTIVE_TASK_STATUSES = ("queued", "running", "waiting_approval")
WORKER_PROFILE_LINE = re.compile(r"    (name|role):\s*(.+?)\s*")
WORKER_ID_LINE = re.compile(r"  ([a-z0-9_-]+):\s*")


def worker_profiles() -> dict:
    """name / role per worker from workers.yaml (read only; two levels)."""
    try:
        lines = (worker_manager.workers_root / "workers.yaml").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}

    profiles, current = {}, None
    for line in lines:
        if match := WORKER_ID_LINE.fullmatch(line):
            current = profiles.setdefault(match[1], {})
        elif (match := WORKER_PROFILE_LINE.fullmatch(line)) and current is not None:
            current[match[1]] = match[2].strip("\"'")
    return profiles


def active_tasks() -> list[dict]:
    """Queued, running and waiting tasks, worker tasks included."""
    if task_store is None:
        return [t for t in TASKS.values() if t["status"] in ACTIVE_TASK_STATUSES]

    tasks = []
    for status in ACTIVE_TASK_STATUSES:
        tasks += store_write("list_tasks", status) or []
    return tasks


@app.get("/workers")
async def list_worker_profiles():
    # async: FastAPI runs a plain def in a thread pool, and the task
    # store's SQLite connection belongs to the event loop thread.
    profiles = worker_profiles()
    tasks = active_tasks()
    reserve = int(os.getenv("COMPLETION_TOKEN_RESERVE", "2000"))

    workers = []
    for worker in worker_manager.list_workers():
        profile = profiles.get(worker, {})
        workers.append({
            "id": worker,
            "name": profile.get("name") or worker,
            "role": profile.get("role"),
            "model": route_model(worker, requested_tokens=reserve).model,
            "daily_tokens": {
                "used": budget_manager.agent_used_today(worker),
                "budget": budget_manager.budget_for_agent(worker),
            },
            "tasks": [
                {
                    key: task.get(key)
                    for key in ("task_id", "parent_id", "kind", "status", "step", "created_at")
                }
                for task in tasks
                if task.get("worker") == worker
            ],
        })

    return {"count": len(workers), "workers": workers}


# ----------------------------------------------------------------------
# K-5: direct chat with a worker (plan-k karar I). One call, the worker's
# own context, no tools: it cannot write notes, search or run code, so the
# Approval Layer is not involved. Each turn is a task row (kind 'chat').
# ----------------------------------------------------------------------

CHAT_MESSAGE_CHARS = 4000
CHAT_HISTORY_TURNS = int(os.getenv("CHAT_HISTORY_TURNS", "6"))
CHAT_HISTORY_CHARS = int(os.getenv("CHAT_HISTORY_CHARS", "6000"))
CHAT_COMPLETION_TOKENS = int(os.getenv("CHAT_COMPLETION_TOKENS", "800"))
CHAT_ID = re.compile(r"[0-9a-f-]{8,64}")
CHAT_PROMPT = (
    "Sen SAVAS AI içindeki uzman bir workersın. Kullanıcı (CEO) seninle "
    "arayüzden doğrudan sohbet ediyor.\n\n"
    "Bu sohbette aracın yok: not okuyamaz/yazamaz, web araması yapamaz, kod "
    "çalıştıramaz, başka worker'a iş veremezsin. Böyle bir iş istenirse "
    "yapmış gibi davranma; kullanıcıya bu işi Mission Console'dan Ares'e "
    "vermesini söyle.\n\n"
    "Aşağıdaki persistent worker context senin kimliğini, kişiliğini ve "
    "kurallarını tanımlar; kendi kişiliğinle, kısa ve Türkçe yanıt ver."
)


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


def chat_worker(worker_id: str) -> str:
    """A worker one can chat with: not Ares (Mission Console) and not Obsidian."""
    if worker_id == "ares":
        raise HTTPException(
            status_code=422,
            detail="Ares'le sohbet Mission Console'dan (POST /task) yapılır.",
        )

    try:
        worker_manager.validate_worker_id(worker_id)
    except WorkerError:
        raise HTTPException(status_code=404, detail="Worker not found")

    if not worker_manager.worker_exists(worker_id):
        raise HTTPException(status_code=404, detail="Worker not found")

    return worker_id


def chat_turn(task: dict) -> dict:
    result = task.get("result") or {}
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "message": task.get("message") or "",
        "reply": result.get("reply"),
        "model": result.get("model"),
        "total_tokens": (result.get("usage") or {}).get("total_tokens"),
        "error": task.get("error"),
        "created_at": task.get("created_at"),
        "completed_at": task.get("completed_at"),
    }


def chat_history(conversation_id: str, current_task_id: str) -> list[dict]:
    """The last answered turns as messages, newest kept first within CHAT_HISTORY_CHARS."""
    turns = store_write("list_conversation", conversation_id, CHAT_HISTORY_TURNS + 1) or []
    kept, used = [], 0

    for turn in reversed(turns):
        reply = (turn.get("result") or {}).get("reply")
        if turn["task_id"] == current_task_id or turn["status"] != "completed" or not reply:
            continue
        size = len(turn["message"]) + len(reply)
        if len(kept) >= CHAT_HISTORY_TURNS or used + size > CHAT_HISTORY_CHARS:
            break
        kept.append(turn)
        used += size

    messages = []
    for turn in reversed(kept):
        messages.append({"role": "user", "content": turn["message"]})
        messages.append({"role": "assistant", "content": turn["result"]["reply"]})
    return messages


class CloudOnly(Exception):
    pass


# L-3a (plan-l3 C): a goal's steps never fall back to the local model.
CLOUD_ONLY = contextvars.ContextVar("cloud_only", default=False)
CLOUD_ONLY_ERROR = "bulut bütçesi ya da bulut modeli yok; hedef yerel modele düşmez"


async def chat_call(worker_id: str, task_id: str, messages: list, completion_tokens: int = CHAT_COMPLETION_TOKENS,
                    timeout: float = 120) -> dict:
    """One answer with the worker's model and fallbacks; budget and usage as for a worker call."""
    estimated_prompt_tokens = max(len(json.dumps(messages, ensure_ascii=False)) // 4, 1)
    completion_reserve = int(os.getenv("COMPLETION_TOKEN_RESERVE", "2000"))

    selection = route_model(
        worker_id,
        requested_tokens=estimated_prompt_tokens + completion_tokens,
        context_tokens=estimated_prompt_tokens + completion_reserve,
    )
    models = [selection.model, *selection.fallbacks[:2]]
    route_reason = selection.reason

    for attempt, model in enumerate(models):
        location = LOCAL if model in selection.local else "cloud"
        # The local model thinks before it answers and keeps the full reserve.
        completion_limit = completion_reserve if location == LOCAL else completion_tokens
        if location == LOCAL and CLOUD_ONLY.get():
            raise CloudOnly(CLOUD_ONLY_ERROR)

        # BudgetExceeded goes to the caller: a budget refusal is never retried.
        budget = await budget_manager.reserve(
            agent=worker_id,
            task_id=task_id,
            requested_tokens=estimated_prompt_tokens + completion_limit,
            location=location,
        )

        try:
            start = time.perf_counter()

            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await post_chat(
                    client,
                    location,
                    {"model": model, "messages": messages, "max_tokens": completion_limit},
                )
                response.raise_for_status()
                data = response.json()

            note_llm_success(model, task_id, worker_id)
            duration_ms = round((time.perf_counter() - start) * 1000)
            usage = data.get("usage") or {}

            record_usage_safely(
                task_id=task_id,
                agent=worker_id,
                worker=worker_id,
                model=model,
                route_reason=route_reason,
                usage=usage,
                duration_ms=duration_ms,
                location=location,
            )

            choice = data["choices"][0]
            return {
                "reply": choice["message"].get("content") or "",
                "model": model,
                "usage": usage,
                "duration_ms": duration_ms,
                "finish_reason": choice.get("finish_reason"),
            }

        except httpx.HTTPError as exc:
            # Tokens may have been spent; record the estimate.
            record_usage_safely(
                task_id=task_id,
                agent=worker_id,
                worker=worker_id,
                model=model,
                route_reason=route_reason,
                usage={"prompt_tokens": estimated_prompt_tokens, "completion_tokens": 0},
                status="failed",
                location=location,
            )
            kind = note_llm_failure(exc, model, task_id, worker_id)

            if kind == FATAL or attempt + 1 >= len(models):
                raise

            add_fallback_activity(task_id, worker_id, "started", model, models[attempt + 1], kind=kind, error=str(exc))
            route_reason = f"{selection.reason} → fallback ({kind})"

        finally:
            budget_manager.release(budget["reservation_id"])


async def run_chat(task_id: str, worker_id: str, conversation_id: str, message: str):
    def finish(status: str, error=None, result=None):
        store_write("save_task", {
            "task_id": task_id, "status": status, "error": error, "result": result, "completed_at": utc_now(),
        })

    add_activity(task_id=task_id, agent=worker_id, event="chat", status="started", worker=worker_id)

    try:
        # The message is the user turn; no daily file, nothing repeated.
        context = worker_manager.build_worker_context(
            worker_id,
            include_task=False,
            budget_chars=WORKER_CONTEXT_CHARS,
            task_chars=len(message),
        )
        messages = [
            {
                "role": "system",
                "content": (
                    f"{CHAT_PROMPT}\n\n"
                    "========================================\n"
                    "PERSISTENT WORKER CONTEXT\n"
                    "========================================\n\n"
                    f"{context}"
                ),
            },
            *chat_history(conversation_id, task_id),
            {"role": "user", "content": message},
        ]
        result = await chat_call(worker_id, task_id, messages)

    except asyncio.CancelledError:
        finish("cancelled", "cancelled")
        raise

    except BudgetExceeded as exc:
        finish("blocked", str(exc))
        add_activity(task_id=task_id, agent=worker_id, event="chat", status="blocked", worker=worker_id, error=str(exc))
        return

    except Exception as exc:
        error = str(getattr(exc, "detail", None) or exc or type(exc).__name__)[:1000]
        finish("failed", error)
        add_activity(task_id=task_id, agent=worker_id, event="chat", status="failed", worker=worker_id, error=error)
        return

    finish("completed", result=result)
    add_activity(
        task_id=task_id,
        agent=worker_id,
        event="chat",
        status="completed",
        worker=worker_id,
        model=result["model"],
        total_tokens=result["usage"].get("total_tokens"),
        duration_ms=result["duration_ms"],
    )


@app.post("/workers/{worker_id}/chat")
async def chat_endpoint(worker_id: str, request: ChatRequest):
    """A message to a worker; answers at once, the reply comes through GET (karar I, J)."""
    worker_id = chat_worker(worker_id)

    if task_store is None:
        raise HTTPException(status_code=503, detail="Task store is not available")

    message = (request.message or "").strip()
    if not message or len(message) > CHAT_MESSAGE_CHARS:
        raise HTTPException(status_code=422, detail=f"message must be 1-{CHAT_MESSAGE_CHARS} characters")

    conversation_id = request.conversation_id
    if conversation_id:
        if not CHAT_ID.fullmatch(conversation_id):
            raise HTTPException(status_code=422, detail="Bad conversation_id")
        last = store_write("list_conversation", conversation_id, 1) or []
        if last and last[-1]["worker"] != worker_id:
            raise HTTPException(status_code=409, detail="Conversation belongs to another worker")
        if last and last[-1]["status"] == "running":
            raise HTTPException(status_code=409, detail="The previous answer is still being written")
    else:
        conversation_id = str(uuid.uuid4())

    task_id = str(uuid.uuid4())
    store_write("save_task", {
        "task_id": task_id,
        "kind": "chat",
        "worker": worker_id,
        "message": message,
        "status": "running",
        "created_at": utc_now(),
        "conversation_id": conversation_id,
    })
    if not store_write("get_task", task_id):
        raise HTTPException(status_code=503, detail="The message could not be saved")

    job = asyncio.create_task(run_chat(task_id, worker_id, conversation_id, message))
    BACKGROUND_JOBS.add(job)
    job.add_done_callback(BACKGROUND_JOBS.discard)

    return {"status": "running", "task_id": task_id, "conversation_id": conversation_id}


@app.get("/workers/{worker_id}/chat")
async def chat_history_endpoint(worker_id: str, conversation_id: str | None = None, limit: int = 30):
    """A conversation's turns, oldest first; without an id the worker's latest one."""
    worker_id = chat_worker(worker_id)

    if conversation_id is not None and not CHAT_ID.fullmatch(conversation_id):
        raise HTTPException(status_code=422, detail="Bad conversation_id")

    conversation_id = conversation_id or store_write("latest_conversation", worker_id)
    if not conversation_id:
        return {"worker": worker_id, "conversation_id": None, "turns": []}

    limit = max(1, min(int(limit), 100))
    turns = [
        chat_turn(task)
        for task in store_write("list_conversation", conversation_id, limit) or []
        if task["worker"] == worker_id
    ]
    return {"worker": worker_id, "conversation_id": conversation_id, "turns": turns}


# --------------------------------------------------
# Note proposals (J-2, plan-j L, karar b)
# --------------------------------------------------

# A host automation proposes a note; nothing is written until the user
# approves it, the same approval (and auto-approval rules) as an agent's
# create_note. No LLM. Local only: the gateway does not pass /notes/.
# Kariyer/: the filter rules note (J-3) and application packages (J-5).
PROPOSAL_PREFIXES = ("SAVAS-AI/Raporlar/", "Kariyer/")
PROPOSAL_CHARS = 20000
PROPOSAL_REASON_CHARS = 200
PROPOSAL_SEGMENT = re.compile(r"[^/\\\x00-\x1f.][^/\\\x00-\x1f]{0,119}")


class NoteProposal(BaseModel):
    path: str
    content: str
    reason: str = ""


def check_proposal(request: NoteProposal) -> str:
    path = request.path
    if not path.startswith(PROPOSAL_PREFIXES) or not path.endswith(".md"):
        raise HTTPException(status_code=422, detail=f"path must be under {', '.join(PROPOSAL_PREFIXES)} and end in .md")
    if not all(PROPOSAL_SEGMENT.fullmatch(part) for part in path.split("/")):
        raise HTTPException(status_code=422, detail="Bad path")
    if not request.content.strip() or len(request.content) > PROPOSAL_CHARS:
        raise HTTPException(status_code=422, detail=f"content must be 1-{PROPOSAL_CHARS} characters")
    if len(request.reason) > PROPOSAL_REASON_CHARS:
        raise HTTPException(status_code=422, detail=f"reason must be at most {PROPOSAL_REASON_CHARS} characters")
    return path


def is_proposal(task_id: str) -> bool:
    task = store_write("get_task", task_id)
    return bool(task) and task.get("kind") == "proposal"


async def write_proposal(task_id: str, arguments: dict) -> dict:
    """Write the approved note; the task ends completed or failed."""
    try:
        result = await execute_write_tool("create_note", arguments, task_id)
    except Exception as exc:
        fail_stored_task(task_id, f"note write failed: {exc}")
        raise
    store_write("save_task", {"task_id": task_id, "status": "completed", "completed_at": utc_now(),
                              "result": {"path": arguments["path"]}})
    add_activity(task_id=task_id, agent="orchestrator", event="task", status="completed")
    return result


async def finish_proposal(approval: dict) -> dict:
    task_id = approval["task_id"]

    if approval["status"] != "approved":
        store_write("save_task", {"task_id": task_id, "status": "cancelled", "completed_at": utc_now(),
                                  "error": "rejected by user"})
        add_activity(task_id=task_id, agent="orchestrator", event="task", status="cancelled",
                     error="rejected by user")
        return {"status": "rejected", "task_id": task_id, "approval_id": approval["approval_id"]}

    try:
        result = await write_proposal(task_id, approval["arguments"])
    except Exception as exc:
        approval["status"] = "failed"
        approval["updated_at"] = utc_now()
        persist_approval(approval)
        add_activity(task_id=task_id, agent="orchestrator", event="approval", status="execution_failed",
                     approval_id=approval["approval_id"], action=approval["action"], error=str(exc))
        return {"status": "error", "task_id": task_id, "error": str(exc)}

    approval["status"] = "executed"
    approval["executed_at"] = utc_now()
    approval["updated_at"] = utc_now()
    persist_approval(approval)
    add_activity(task_id=task_id, agent="orchestrator", event="approval", status="executed",
                 approval_id=approval["approval_id"], action=approval["action"])
    return {"status": "executed", "task_id": task_id, "result": result}


@app.post("/notes/propose")
async def propose_note(request: NoteProposal):
    """A note for the user's approval (plan-j L). Returns at once; the note is written on approve."""
    path = check_proposal(request)
    arguments = {"path": path, "content": request.content}
    reason = request.reason or "Otomasyon not önerisi"

    task_id = str(uuid.uuid4())
    store_write("save_task", {
        "task_id": task_id,
        "kind": "proposal",
        "worker": DEFAULT_WORKER,
        "message": f"Not önerisi: {path}",
        "status": "waiting_approval",
        "created_at": utc_now(),
    })
    if not store_write("get_task", task_id):
        raise HTTPException(status_code=503, detail="The proposal could not be saved")
    add_activity(task_id=task_id, agent="orchestrator", event="task", status="proposed", path=path)

    auto_policy = auto_approval_matches(agent="obsidian", action="create_note", path=path)
    if auto_policy:
        add_activity(task_id=task_id, agent="orchestrator", event="approval", status="auto_approved",
                     action="create_note", auto_approval_id=auto_policy["auto_approval_id"], path=path)
        try:
            await write_proposal(task_id, arguments)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Note write failed: {exc}") from exc
        return {"status": "completed", "task_id": task_id}

    approval = create_approval(task_id, "obsidian", "create_note", arguments, reason, "proposal")
    return {"status": "waiting_approval", "task_id": task_id, "approval_id": approval["approval_id"]}


# --------------------------------------------------
# Job listings (J-3, plan-j karar C)
# --------------------------------------------------

# Host automations (automations/jobs/) import; the UI reads. No LLM here.
JOB_SOURCES = ("linkedin", "kariyer", "remotive", "mail")
JOB_IMPORT_MAX = 200
JOB_LIMITS = {"external_id": 200, "url": 1000, "title": 300, "company": 200, "location": 200,
              "language": 10, "description": 20000, "posted_at": 40, "filter_reason": 200}


class JobImport(BaseModel):
    jobs: list


def check_job(job) -> dict:
    """One imported listing; raises ValueError with the reason."""
    if not isinstance(job, dict):
        raise ValueError("a job must be an object")
    if job.get("source") not in JOB_SOURCES:
        raise ValueError(f"source must be one of {', '.join(JOB_SOURCES)}")
    if job.get("status", "new") not in ("new", "filtered"):
        raise ValueError("status must be new or filtered")
    clean = {"job_id": str(uuid.uuid4()), "source": job["source"], "status": job.get("status", "new"),
             "remote": bool(job.get("remote"))}
    for field, limit in JOB_LIMITS.items():
        value = job.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"{field} must be a string of at most {limit} characters")
        clean[field] = value
    for field in ("external_id", "url", "title"):
        if not clean.get(field, "").strip():
            raise ValueError(f"{field} is required")
    # Opened from the UI: only a web page, never javascript: or the like.
    if not clean["url"].startswith("https://"):
        raise ValueError("url must start with https://")
    return clean


@app.post("/jobs/import")
async def import_jobs(request: JobImport):
    """New listings from the collector; known ones (source + id, or url) are skipped."""
    if not isinstance(request.jobs, list) or len(request.jobs) > JOB_IMPORT_MAX:
        raise HTTPException(status_code=422, detail=f"jobs must be a list of at most {JOB_IMPORT_MAX}")
    try:
        jobs = [check_job(job) for job in request.jobs]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = store_write("import_jobs", jobs, utc_now())
    if result is None:
        raise HTTPException(status_code=503, detail="Job store is not available")

    added = set(result["job_ids"])
    result["new"] = sum(1 for job in jobs if job["job_id"] in added and job["status"] == "new")
    if result["added"]:
        add_activity(task_id="system", agent="orchestrator", event="jobs", status="imported",
                     added=result["added"], duplicates=result["duplicates"], new=result["new"])
    return result


@app.get("/jobs")
async def list_jobs(status: str | None = None, limit: int = 100):
    if status is not None and status not in JOB_STATUSES:
        raise HTTPException(status_code=422, detail=f"Unknown status: {status}")
    jobs = store_write("list_jobs", status, max(1, min(int(limit), 500))) or []
    # The list leaves the long text out; GET /jobs/{id} has it.
    return {"count": len(jobs), "counts": store_write("count_jobs") or {},
            "jobs": [{k: v for k, v in job.items() if k != "description"} for job in jobs]}


# --------------------------------------------------
# Job scoring (J-4, plan-j karar E, revised: own endpoint, not the chat)
# --------------------------------------------------

# Oracle rates new listings against the CV the collector sends (Kariyer/CV.md).
# Same model path as a chat turn: routing, fallbacks, Oracle's budget, usage.
SCORE_WORKER = "oracle"
SCORE_BATCH = 10
SCORE_MAX_JOBS = 30
SCORE_CV_CHARS = 12000
SCORE_PREFS_CHARS = 3000
SCORE_DESCRIPTION_CHARS = 1200
SCORE_COMPLETION_TOKENS = 1500
SCORE_TRACKS = ("dev", "pm_ba")
SCORE_LOCK = asyncio.Lock()
SCORE_PROMPT = (
    "You rate job listings for one candidate, for the candidate's own job search.\n"
    "Judge only from the CV and preferences given; never assume a skill or experience the CV does not show.\n"
    "score 0-100: how well the candidate fits and would be a credible applicant (role, seniority, "
    "skills, location / remote, language). 70+ = worth applying, 40-69 = possible, below 40 = poor fit.\n"
    "Location: a role outside Turkey that is on-site or hybrid scores at most 40. Outside Turkey with "
    "no remote/hybrid/on-site information and no description: at most 65, and say the work mode is unclear. "
    "Roles in Turkey (any mode) and remote roles open to the candidate are scored normally.\n"
    "track: \"dev\" for software development roles, \"pm_ba\" for project management / business analysis roles.\n"
    "reason: one short sentence in Turkish naming the main match or gap. A listing from an alert may have "
    "no description: rate it by title, company and location and say so.\n"
    "Answer with a JSON array only, one object per listing, no other text: "
    '[{"n": 1, "score": 72, "track": "dev", "reason": "..."}]'
)


# The location caps of SCORE_PROMPT, enforced: the model does not always keep them.
# Abroad = a known place in the user's target region (Turkey's own places, districts
# included, are never on the list); "remote" in the location lifts it.
ABROAD_PLACES = (
    "bulgaria", "sofia", "plovdiv", "varna", "czechia", "czech", "prague", "praha", "brno", "ostrava",
    "poland", "warsaw", "warszawa", "krakow", "kraków", "wroclaw", "wrocław", "gdansk", "gdańsk", "poznan",
    "poznań", "lodz", "łódź", "lublin", "katowice", "szczecin", "bialystok", "białystok", "rzeszow", "rzeszów",
    "mielec", "romania", "bucharest", "bucurești", "cluj", "iasi", "iași", "timisoara", "timișoara", "hungary",
    "budapest", "slovakia", "bratislava", "serbia", "belgrade", "croatia", "zagreb", "slovenia", "ljubljana",
    "estonia", "tallinn", "latvia", "riga", "lithuania", "vilnius", "greece", "athens", "germany", "berlin",
    "munich", "austria", "vienna", "netherlands", "amsterdam",
)
ON_SITE_WORDS = ("hybrid", "on-site", "onsite", "on site", "hibrit", "yerinde", "ofisten")
SCORE_CAP_ON_SITE_ABROAD = 40
SCORE_CAP_UNCLEAR_ABROAD = 65


def cap_score(job: dict, score: int) -> tuple[int, str | None]:
    """(score, why it was lowered) after the location caps."""
    location = (job.get("location") or "").lower()
    words = set(re.findall(r"[\w-]+", location))
    abroad = any((place in words) if " " not in place else (place in location) for place in ABROAD_PLACES)
    if not abroad or "remote" in location or "uzaktan" in location:
        return score, None
    if any(word in location for word in ON_SITE_WORDS):
        if score > SCORE_CAP_ON_SITE_ABROAD:
            return SCORE_CAP_ON_SITE_ABROAD, "yurt dışı hibrit/yerinde"
    elif not (job.get("description") or "").strip() and score > SCORE_CAP_UNCLEAR_ABROAD:
        return SCORE_CAP_UNCLEAR_ABROAD, "yurt dışı, çalışma şekli belirsiz"
    return score, None


class ScoreRequest(BaseModel):
    cv: str
    preferences: str = ""
    max_jobs: int = SCORE_MAX_JOBS
    # After a change to the prompt or the CV: score the new listings again.
    rescore: bool = False


def score_message(cv: str, preferences: str, jobs: list[dict]) -> str:
    lines = [f"CANDIDATE CV:\n{cv}", f"CANDIDATE PREFERENCES:\n{preferences or '-'}", "LISTINGS:"]
    for n, job in enumerate(jobs, 1):
        head = " | ".join(x for x in (job["title"], job["company"], job["location"],
                                       "remote" if job["remote"] else "", job["source"]) if x)
        description = " ".join((job.get("description") or "").split())[:SCORE_DESCRIPTION_CHARS]
        lines.append(f"{n}. {head}\n   {description or '(no description)'}")
    return "\n\n".join(lines)


def parse_scores(reply: str, count: int) -> dict[int, dict]:
    """n -> {score, track, reason}; anything malformed is left out (scored next time)."""
    start, end = reply.find("["), reply.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        items = json.loads(reply[start:end + 1])
    except ValueError:
        return {}
    out = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        n, score = item.get("n"), item.get("score")
        if not isinstance(n, int) or not 1 <= n <= count or isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        track = item.get("track") if item.get("track") in SCORE_TRACKS else None
        out[n] = {"score": max(0, min(100, round(score))), "track": track,
                  "reason": " ".join(str(item.get("reason") or "").split())[:300]}
    return out


@app.post("/jobs/score")
async def score_jobs(request: ScoreRequest):
    """
    Rate up to max_jobs unscored new listings, SCORE_BATCH per call. Local
    only (the gateway does not pass it); the collector calls it after an
    import. Stops at Oracle's budget: the rest waits for the next run.
    """
    cv = (request.cv or "").strip()
    if not cv or len(cv) > SCORE_CV_CHARS:
        raise HTTPException(status_code=422, detail=f"cv must be 1-{SCORE_CV_CHARS} characters")
    if len(request.preferences or "") > SCORE_PREFS_CHARS:
        raise HTTPException(status_code=422, detail=f"preferences must be at most {SCORE_PREFS_CHARS} characters")
    max_jobs = max(1, min(int(request.max_jobs), SCORE_MAX_JOBS))
    if SCORE_LOCK.locked():
        raise HTTPException(status_code=409, detail="Scoring is already running")

    async with SCORE_LOCK:
        jobs = store_write("jobs_to_score", max_jobs, bool(request.rescore)) or []
        scored, blocked, error = [], False, None

        for start in range(0, len(jobs), SCORE_BATCH):
            batch = jobs[start:start + SCORE_BATCH]
            task_id = str(uuid.uuid4())
            store_write("save_task", {"task_id": task_id, "kind": "score", "worker": SCORE_WORKER,
                                      "message": f"İlan puanlama: {len(batch)} ilan", "status": "running",
                                      "created_at": utc_now()})
            messages = [{"role": "system", "content": SCORE_PROMPT},
                        {"role": "user", "content": score_message(cv, request.preferences, batch)}]
            try:
                result = await chat_call(SCORE_WORKER, task_id, messages, SCORE_COMPLETION_TOKENS)
            except BudgetExceeded as exc:
                blocked = True
                store_write("save_task", {"task_id": task_id, "status": "blocked", "error": str(exc), "completed_at": utc_now()})
                add_activity(task_id=task_id, agent=SCORE_WORKER, event="chat", status="blocked",
                             worker=SCORE_WORKER, error=str(exc))
                break
            except Exception as exc:
                error = str(getattr(exc, "detail", None) or exc or type(exc).__name__)[:500]
                fail_stored_task(task_id, f"scoring failed: {error}")
                break

            found = parse_scores(result["reply"], len(batch))
            if not found:
                # No usable answer: stop rather than resend the same listings in a loop.
                error = f"no scores in the answer of {result['model']}"
                fail_stored_task(task_id, f"scoring failed: {error}")
                break
            for n, value in found.items():
                job = batch[n - 1]
                capped, why = cap_score(job, value["score"])
                if why:
                    value = {**value, "score": capped, "reason": f"{value['reason']} [üst sınır {capped}: {why}]"[:300]}
                store_write("score_job", job["job_id"], value["score"], value["reason"], value["track"])
                scored.append({"job_id": job["job_id"], "title": job["title"], "company": job["company"], **value})
            store_write("save_task", {"task_id": task_id, "status": "completed", "completed_at": utc_now(),
                                      "result": {"scored": len(found), "of": len(batch), "model": result["model"],
                                                 "total_tokens": result["usage"].get("total_tokens")}})
            add_activity(task_id=task_id, agent=SCORE_WORKER, event="jobs", status="scored",
                         scored=len(found), of=len(batch), model=result["model"],
                         total_tokens=result["usage"].get("total_tokens"))

    return {"scored": scored, "remaining": store_write("count_unscored") or 0, "blocked": blocked, "error": error}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = store_write("get_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {**job, "package": store_write("get_package", job_id)}


# --------------------------------------------------
# Application packages (J-5, plan-j karar G, H, I)
# --------------------------------------------------

# The user picks a listing; the tailored CV (JARVIS, or Gandalf for PM/BA),
# the cover letter (Willy) and the check (Gordon) are written one after the
# other with the chat model path (no tools, no Ares). Nothing is sent
# anywhere: the user saves it to Obsidian (approved) and applies by hand.
CV_NOTE = "Kariyer/CV.md"
APPLICATIONS_DIR = "Kariyer/Başvurular"
PACKAGE_DESCRIPTION_CHARS = 20000
PACKAGE_PROMPT_DESCRIPTION_CHARS = 6000
PACKAGE_CV_TOKENS = 3000
PACKAGE_LETTER_TOKENS = 900
PACKAGE_REVIEW_TOKENS = 900
# Gordon's unsupported claims go back to the writers this many times before the
# package is left for the user to fix (needs_fix); ~7-10k tokens a round.
PACKAGE_REVISIONS = 2
# gh/gpt-4.1 writing the CV JSON was cut at 120 s twice (2026-11-17), so the
# package steps wait longer; chat keeps 120 s.
PACKAGE_TIMEOUT = 240
PACKAGE_LANGUAGES = {"auto": "the language of the listing (Turkish or English)", "tr": "Turkish", "en": "English"}
USER_JOB_STATUSES = ("new", "dismissed", "applied", "interview", "rejected", "offer")
PM_BA_WORDS = ("project manager", "proje yönet", "business analyst", "iş analist", "analyst", "analist", "scrum", "product owner")
# The master CV's PROJECTS are the candidate's own (personal / course) work;
# a letter once put "Business Insight API" under KurumSoft (2026-11-17).
PROJECT_RULE = (
    "Projects listed in the master CV's PROJECTS section are the candidate's own personal or course projects, "
    "not work done for an employer: never place them at a company or in a job, and never move an employer's work "
    "(or its technologies) into a project. Each job's technologies stay with that job.\n"
)
# L-2a: a stated length of experience must add up from the CV's own dates (L-2a3: the CV too).
YEARS_RULE = (
    "Years of experience: state a number only if the CV's job dates add up to it (paid jobs only, never round up); "
    "otherwise state none.\n"
)
TAILOR_PROMPT = (
    "You tailor the candidate's CV for one job listing.\n"
    "Truth rule: use only facts found in the MASTER CV. Never add an employer, title, date, degree, certificate, "
    "skill, tool, technology, number or achievement the master CV does not state, even if the listing asks for it. "
    "Tailoring is the point: first find the listing's main requirements (role, stack, domain, duties), then:\n"
    "- write the headline and summary around the parts of the CV that meet them;\n"
    "- order the sections, the projects, each job's bullets and the skills so the matching ones come first; "
    "drop or shorten bullets that do not help this listing (keep every job with at least one bullet); "
    "a bullet you keep keeps its numbers (10 minutes -> under 1 minute);\n"
    "- where the CV and the listing name the same thing differently, use the listing's word (REST API / web "
    "services), but only for what the CV really states.\n"
    + PROJECT_RULE +
    "Never make a claim stronger than the master CV: no stronger verbs (contributed -> led/designed), no work mode "
    "(remote, unsupervised), no seniority, no years beyond the dates, no job title the CV does not use. "
    "A requirement the CV does not cover stays uncovered.\n"
    + YEARS_RULE +
    "Write everything in the language given under JOB and set \"language\" to it: translate faithfully the "
    "headline, job titles, month names (Ekim 2023 - Günümüz -> October 2023 - Present), places and degree names; "
    "keep company, school, product and technology names as they are.\n"
    "Keep the name and contact details exactly as in the master CV. It must fit one A4 page: at most 6 bullets "
    "per job, 4 per project, sentences short.\n"
    "\"order\": the sections in the order that suits the listing (the master CV's order is the default).\n"
    "Answer with JSON only, in this shape (the layout is done by code; no Markdown inside the strings):\n"
    + CV_SCHEMA
)
LETTER_PROMPT = (
    "You write a short cover letter (150-250 words) from the candidate for one job listing.\n"
    "Truth rule: only facts from the MASTER CV; do not invent experience, skills, numbers or stories. "
    "Link two or three real points of the CV to what the listing asks for. Never claim more than the CV says "
    "(no stronger verbs, no remote or seniority claims, no general strengths it does not show); do not mention "
    "requirements the candidate lacks.\n"
    + YEARS_RULE + PROJECT_RULE +
    "Say which project or job each point comes from as the CV does (\"in my personal project ...\" / \"at <employer> ...\").\n"
    "Write in the language given under JOB. No placeholders: greet the hiring team in general "
    "(\"Dear Hiring Team\" / \"Sayın İşe Alım Ekibi\") and sign with the candidate's name from the CV.\n"
    "Answer with the letter in Markdown only."
)
REVIEW_PROMPT = (
    "You check an application package against the MASTER CV before the candidate sends it.\n"
    "Fact from the candidate: " + PROJECT_RULE + "Calling these projects personal is therefore supported.\n"
    "unsupported: every statement in the TAILORED CV or the COVER LETTER that the master CV does not support "
    "(an invented or inflated employer, title, date, skill, tool, number, degree), and any personal project "
    "presented as work for an employer or any technology moved to a job that did not use it (also in the summary: "
    "\"enterprise Java\" when the jobs used C#). Each item starts with the exact words of the TAILORED CV or "
    "COVER LETTER in double quotes, then why; an item with no such quote is not unsupported. A true statement that "
    "is only weak or off-topic for this listing is not unsupported either: put the gap under missing.\n"
    "missing: requirements of the listing the master CV does not show (not an error: the candidate lacks them; "
    "never put these under unsupported).\n"
    "notes: wrong or mixed language, spelling errors, missing name or contact lines, and requirements of the "
    "listing that the master CV does show but the tailored CV left out.\n"
    "verdict: \"ok\" when unsupported is empty, else \"fix\".\n"
    'Answer with JSON only: {"verdict": "ok", "unsupported": [], "missing": [], "notes": []}'
)


class PackageRequest(BaseModel):
    description: str = ""
    letter: bool = True
    language: str = "auto"


class JobStatusRequest(BaseModel):
    status: str


def package_writer(job: dict) -> str:
    if job.get("track") == "pm_ba":
        return "gandalf"
    if job.get("track") == "dev":
        return "jarvis"
    title = (job.get("title") or "").lower()
    return "gandalf" if any(word in title for word in PM_BA_WORDS) else "jarvis"


def job_brief(job: dict, language: str) -> str:
    description = " ".join((job.get("description") or "").split())[:PACKAGE_PROMPT_DESCRIPTION_CHARS]
    return (f"JOB:\nTitle: {job['title']}\nCompany: {job.get('company') or '-'}\nLocation: {job.get('location') or '-'}\n"
            f"Write in: {PACKAGE_LANGUAGES[language]}\n\nLISTING:\n{description}")


def master_cv_text(content: str) -> str:
    """The CV note without the header SAVAS AI put above its "---" line."""
    head, sep, rest = content.partition("\n---\n")
    return rest.strip() if sep and len(head) < 800 and head.startswith("# CV") else content.strip()


def strip_fence(text: str) -> str:
    text = (text or "").strip()
    match = re.fullmatch(r"```[a-zA-Z]*\n(.*)\n```", text, re.S)
    return match.group(1).strip() if match else text


# L-2a: an "unsupported" item must quote the package. Quotes of 6+ characters.
QUOTE = re.compile(r'"([^"]{6,})"|“([^”]{6,})”|‘([^’]{6,})’|(?<![A-Za-z])\'([^\']{6,})\'(?![A-Za-z])')


def plain(text: str) -> str:
    return " ".join(re.sub(r"[*_#`>]", " ", text or "").lower().split())


def quotes_package(item: str, package: str) -> bool:
    body = plain(package)
    return any(plain(next(g for g in match.groups() if g)).strip(" .,;:") in body for match in QUOTE.finditer(item))


# L-2a3: the item's own reason says the quote is true, or only that it does not fit the listing.
AFFIRMED = re.compile(r"\b(supported|true|accurate|correct|destekleniyor|doğru)\b", re.I)
NEGATED = re.compile(r"\b(not|no|never|unsupported|without|lacks?|but|however|although|inflat\w*|exaggerat\w*|overstat\w*|"
                     r"değil|yok|desteklemiyor|ama|fakat|abart\w*)\b|n't", re.I)
OFF_TOPIC = re.compile(r"relevan|unrelated|ilgisiz|alakasız|ilanla ilgili değil|"
                       r"the (listing|job|role) (requires|asks|is for|wants|needs)|as the (job|listing) asks|"
                       r"ilan\w* (istiyor|istediği|gerektir)", re.I)


def reason_of(item: str) -> str:
    return QUOTE.sub(" ", item)


def parse_review(reply: str, package: str | None = None) -> dict:
    start, end = (reply or "").find("{"), (reply or "").rfind("}")
    try:
        data = json.loads(reply[start:end + 1]) if 0 <= start < end else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    lists = {key: [" ".join(str(x).split())[:300] for x in data.get(key) or [] if str(x).strip()][:20]
             for key in ("unsupported", "missing", "notes")}
    moved = []
    if package is not None:
        # A listing gap filed as "unsupported" quotes nothing in the package (L-2a: 9 of 13).
        moved = [x for x in lists["unsupported"] if not quotes_package(x, package)]
        lists["unsupported"] = [x for x in lists["unsupported"] if x not in moved]
        lists["missing"] = (lists["missing"] + moved)[:20]
        # Kept quotes whose reason is "true" or "off-topic": no fabrication behind them (L-2a2: React package).
        true_items = [x for x in lists["unsupported"] if AFFIRMED.search(reason_of(x)) and not NEGATED.search(reason_of(x))]
        off_topic = [x for x in lists["unsupported"] if x not in true_items and OFF_TOPIC.search(reason_of(x))]
        lists["unsupported"] = [x for x in lists["unsupported"] if x not in true_items and x not in off_topic]
        lists["notes"] = (lists["notes"] + off_topic)[:20]
        moved += true_items + off_topic
    verdict = data.get("verdict")
    if verdict not in ("ok", "fix"):
        # No readable verdict is never "ok": the user is told to look.
        verdict = "unknown"
    elif lists["unsupported"]:
        verdict = "fix"
    elif moved:
        # "fix" stood on items that were not unsupported after all.
        verdict = "ok"
    return {"verdict": verdict, **lists, **({"moved": len(moved)} if moved else {})}


async def package_step(worker: str, job: dict, step: str, system: str, user: str, tokens: int, steps: list) -> str:
    task_id = str(uuid.uuid4())
    store_write("save_task", {"task_id": task_id, "kind": "package", "worker": worker, "status": "running",
                              "message": f"Başvuru paketi ({step}): {job['title']} · {job.get('company') or '-'}",
                              "created_at": utc_now()})
    try:
        result = await chat_call(worker, task_id, [{"role": "system", "content": system},
                                                   {"role": "user", "content": user}], tokens, PACKAGE_TIMEOUT)
    except BudgetExceeded as exc:
        store_write("save_task", {"task_id": task_id, "status": "blocked", "error": str(exc), "completed_at": utc_now()})
        add_activity(task_id=task_id, agent=worker, event="chat", status="blocked", worker=worker, error=str(exc))
        raise
    except Exception as exc:
        fail_stored_task(task_id, f"package {step} failed: {str(getattr(exc, 'detail', None) or exc)[:300]}")
        raise
    store_write("save_task", {"task_id": task_id, "status": "completed", "completed_at": utc_now(),
                              "result": {"model": result["model"], "total_tokens": result["usage"].get("total_tokens")}})
    steps.append({"step": step, "worker": worker, "task_id": task_id, "model": result["model"],
                  "total_tokens": result["usage"].get("total_tokens"), "finish_reason": result.get("finish_reason")})
    return result["reply"]


def revise_request(cv: str, brief: str, previous: str, unsupported: list) -> str:
    """The writer's first answer back with Gordon's findings, to be written again."""
    found = "\n".join(f"- {x}" for x in unsupported)
    return (f"MASTER CV:\n{cv}\n\n{brief}\n\nYOUR PREVIOUS ANSWER:\n{previous}\n\n"
            f"A REVIEWER FOUND THESE STATEMENTS NOT SUPPORTED BY THE MASTER CV (some may belong to the other "
            f"document; fix those that are yours):\n{found}\n\n"
            "Answer again in the same format: rewrite each of them to say only what the master CV states, or "
            "remove it; keep everything else.")


async def run_package(job: dict, want_letter: bool, language: str):
    job_id, steps = job["job_id"], []
    try:
        note = await read_note(CV_NOTE, f"package-{job_id}")
        cv = master_cv_text(note.get("content") or "")
        if not cv:
            raise ValueError(f"{CV_NOTE} is empty")
        brief = job_brief(job, language)

        cv_data = parse_cv(await package_step(package_writer(job), job, "cv", TAILOR_PROMPT,
                                              f"MASTER CV:\n{cv}\n\n{brief}", PACKAGE_CV_TOKENS, steps))
        tailored = cv_markdown(cv_data)
        letter = None
        if want_letter:
            letter = strip_fence(await package_step("willy", job, "letter", LETTER_PROMPT,
                                                    f"MASTER CV:\n{cv}\n\n{brief}", PACKAGE_LETTER_TOKENS, steps))
        writer, fixed = package_writer(job), []
        for revision in range(PACKAGE_REVISIONS + 1):
            suffix = f"-{revision + 1}" if revision else ""
            review = parse_review(await package_step(
                "gordon", job, "review" + suffix, REVIEW_PROMPT,
                f"MASTER CV:\n{cv}\n\nTAILORED CV:\n{tailored}\n\nCOVER LETTER:\n{letter or '(none)'}\n\n{brief}",
                PACKAGE_REVIEW_TOKENS, steps), f"{tailored}\n{letter or ''}")
            # L-2a: every review is kept with its step, not only the last one.
            steps[-1]["review"] = dict(review)
            # Only a list of unsupported claims can be fixed; an unreadable review goes to the user.
            if not review["unsupported"] or revision == PACKAGE_REVISIONS:
                break
            fixed += review["unsupported"]
            suffix = f"-{revision + 2}"
            cv_data = parse_cv(await package_step(
                writer, job, "cv" + suffix, TAILOR_PROMPT,
                revise_request(cv, brief, json.dumps(cv_data, ensure_ascii=False), review["unsupported"]),
                PACKAGE_CV_TOKENS, steps))
            tailored = cv_markdown(cv_data)
            if letter is not None:
                letter = strip_fence(await package_step(
                    "willy", job, "letter" + suffix, LETTER_PROMPT,
                    revise_request(cv, brief, letter, review["unsupported"]), PACKAGE_LETTER_TOKENS, steps))
        review["fixed"] = fixed[:20]
    except BudgetExceeded as exc:
        store_write("save_package", {"job_id": job_id, "status": "failed", "steps": steps, "error": f"budget: {exc}"})
        return
    except Exception as exc:
        error = str(getattr(exc, "detail", None) or exc or type(exc).__name__)[:500]
        store_write("save_package", {"job_id": job_id, "status": "failed", "steps": steps, "error": error})
        add_activity(task_id=f"package-{job_id}", agent="orchestrator", event="jobs", status="package_failed",
                     job_id=job_id, error=error)
        return

    status = "ready" if review["verdict"] == "ok" else "needs_fix"
    store_write("save_package", {"job_id": job_id, "status": status, "cv": tailored, "cv_data": cv_data, "letter": letter,
                                 "review": review, "steps": steps, "error": None})
    if job["status"] == "new":
        store_write("set_job", job_id, {"status": "package_ready"})
    add_activity(task_id=f"package-{job_id}", agent="orchestrator", event="jobs", status="package_" + status,
                 job_id=job_id, title=job["title"], total_tokens=sum(s.get("total_tokens") or 0 for s in steps))


@app.post("/jobs/{job_id}/package")
async def package_job(job_id: str, request: PackageRequest):
    """Start writing the package; returns at once. GET /jobs/{id} shows it."""
    job = store_write("get_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if request.language not in PACKAGE_LANGUAGES:
        raise HTTPException(status_code=422, detail="language must be auto, tr or en")
    description = (request.description or "").strip()
    if len(description) > PACKAGE_DESCRIPTION_CHARS:
        raise HTTPException(status_code=422, detail=f"description must be at most {PACKAGE_DESCRIPTION_CHARS} characters")
    current = store_write("get_package", job_id)
    if current and current["status"] == "running":
        raise HTTPException(status_code=409, detail="The package is already being written")
    if description:
        store_write("set_job", job_id, {"description": description})
        job["description"] = description
    if not (job.get("description") or "").strip():
        raise HTTPException(status_code=422, detail="İlan metni gerekli: ilanı açıp metnini yapıştır.")

    store_write("save_package", {"job_id": job_id, "status": "running", "language": request.language, "cv": None, "cv_data": None,
                                 "letter": None, "review": None, "steps": [], "error": None})
    add_activity(task_id=f"package-{job_id}", agent="orchestrator", event="jobs", status="package_started",
                 job_id=job_id, title=job["title"])
    work = asyncio.create_task(run_package(job, bool(request.letter), request.language))
    BACKGROUND_JOBS.add(work)
    work.add_done_callback(BACKGROUND_JOBS.discard)
    return {"status": "running", "job_id": job_id}


# ----------------------------------------------------------------------
# Goals (L-3a, docs/plan-l3.md): a recipe in code picks the steps; the
# workers only do the work inside a step. Nothing is sent anywhere.
# ----------------------------------------------------------------------

GOAL_KINDS = ("job_week",)
GOAL_PACKAGES = 3
GOAL_MAX_PACKAGES = 5
GOAL_BUDGET = 120000
# The dearest package so far took 36.8k tokens (L-2a2); one is not started without this much left.
GOAL_PACKAGE_RESERVE = 40000
# Cloud tokens one agent may use for one package; above its daily budget the goal stops.
GOAL_AGENT_RESERVE = 15000
GOAL_MIN_SCORE = 60
GOAL_FAILURES = 2
GOAL_STEP_LABEL = {"pending": "sırada", "needs_text": "ilan metni gerekli", "running": "yazılıyor",
                   "ready": "hazır", "needs_fix": "düzeltilmeli", "failed": "başarısız", "skipped": "atlandı"}


class GoalRequest(BaseModel):
    kind: str = "job_week"
    packages: int = GOAL_PACKAGES


class GoalContinueRequest(BaseModel):
    # job_id -> pasted listing text, for steps waiting for one.
    texts: dict[str, str] = {}


def goal_picks(count: int) -> list[dict]:
    """The best scored new listings without a package, best first."""
    jobs = [job for job in store_write("list_jobs", "new", 500) or []
            if (job.get("score") or 0) >= GOAL_MIN_SCORE and not store_write("get_package", job["job_id"])]
    jobs.sort(key=lambda job: job["score"], reverse=True)
    return [{"job_id": job["job_id"], "title": job["title"], "company": job.get("company") or "", "score": job["score"],
             "url": job.get("url") or "", "status": "pending", "tokens": 0, "error": None} for job in jobs[:count]]


def goal_summary(goal: dict) -> str:
    """Written by code, not by a model (plan-l3 D)."""
    counts = {}
    for step in goal["steps"]:
        counts[step["status"]] = counts.get(step["status"], 0) + 1
    head = (f"İş arama haftası: {len(goal['steps'])} ilan · "
            + " · ".join(f"{GOAL_STEP_LABEL[k]} {v}" for k, v in counts.items())
            + f" · {goal['used']:,} / {goal['budget']:,} token".replace(",", "."))
    if goal.get("error"):
        head += f"\nDurdu: {goal['error']}"
    lines = [f"- {s['title']} · {s['company'] or '-'} (puan {s['score']}): {GOAL_STEP_LABEL[s['status']]}"
             + (f" — {s['error']}" if s.get("error") else "") for s in goal["steps"]]
    return "\n".join([head, *lines])


def goal_running() -> bool:
    return any(goal["status"] == "running" for goal in store_write("list_goals", 20) or [])


async def run_goal(goal_id: str):
    goal = store_write("get_goal", goal_id)
    CLOUD_ONLY.set(True)
    failures, stop = 0, None

    def save():
        store_write("save_goal", {k: goal[k] for k in ("goal_id", "status", "used", "steps", "summary", "error")})

    try:
        for step in goal["steps"]:
            if step["status"] != "pending":
                continue
            job = store_write("get_job", step["job_id"])
            if not job:
                step.update(status="skipped", error="ilan silinmiş")
                continue
            if not (job.get("description") or "").strip():
                step["status"] = "needs_text"
                continue
            current = store_write("get_package", job["job_id"])
            if current and current["status"] == "running":
                step.update(status="skipped", error="paket zaten yazılıyor")
                continue
            if goal["used"] + GOAL_PACKAGE_RESERVE > goal["budget"]:
                stop = f"hedef token sınırı ({goal['used']:,} / {goal['budget']:,})".replace(",", ".")
                break
            agents = (package_writer(job), "willy", "gordon")
            if any(budget_manager.cloud_percent(agent=a, requested_tokens=GOAL_AGENT_RESERVE) > 100 for a in agents):
                stop = "günlük bulut bütçesi yetmiyor (yerel modele düşülmez); yarın devam ettirilebilir"
                break

            step["status"] = "running"
            save()
            store_write("save_package", {"job_id": job["job_id"], "status": "running", "language": "auto", "cv": None,
                                         "cv_data": None, "letter": None, "review": None, "steps": [], "error": None})
            add_activity(task_id=f"package-{job['job_id']}", agent="orchestrator", event="jobs", status="package_started",
                         job_id=job["job_id"], title=job["title"], goal_id=goal_id)
            await run_package(job, True, "auto")
            package = store_write("get_package", job["job_id"]) or {"status": "failed", "steps": [], "error": "paket okunamadı"}
            step["tokens"] = sum(s.get("total_tokens") or 0 for s in package.get("steps") or [])
            goal["used"] += step["tokens"]
            step["status"] = package["status"] if package["status"] in GOAL_STEP_LABEL else "failed"
            step["error"] = (package.get("error") or None) and str(package["error"])[:200]
            if step["status"] == "failed":
                failures += 1
                if CLOUD_ONLY_ERROR in (step["error"] or "") or (step["error"] or "").startswith("budget:"):
                    stop = "günlük bulut bütçesi yetmiyor (yerel modele düşülmez); yarın devam ettirilebilir"
                    break
                if failures >= GOAL_FAILURES:
                    stop = f"{GOAL_FAILURES} paket başarısız"
                    break
            save()
    except Exception as exc:
        goal.update(status="failed", error=str(exc)[:300])
    else:
        waiting = any(step["status"] == "needs_text" for step in goal["steps"])
        goal.update(status="stopped" if stop else "waiting" if waiting else "completed", error=stop)
    goal["summary"] = goal_summary(goal)
    save()
    add_activity(task_id=f"goal-{goal_id}", agent="orchestrator", event="goal", status=goal["status"],
                 goal_id=goal_id, used=goal["used"], error=goal["error"])


def start_goal(goal_id: str):
    work = asyncio.create_task(run_goal(goal_id))
    BACKGROUND_JOBS.add(work)
    work.add_done_callback(BACKGROUND_JOBS.discard)


@app.post("/goals")
async def create_goal(request: GoalRequest):
    """L-3a: start a goal; returns at once. Started by the user only (plan-l3 F)."""
    if request.kind not in GOAL_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of: {', '.join(GOAL_KINDS)}")
    if not 1 <= request.packages <= GOAL_MAX_PACKAGES:
        raise HTTPException(status_code=422, detail=f"packages must be 1-{GOAL_MAX_PACKAGES}")
    if goal_running():
        raise HTTPException(status_code=409, detail="Bir hedef zaten çalışıyor")
    steps = goal_picks(request.packages)
    if not steps:
        raise HTTPException(status_code=422, detail=f"Puanı {GOAL_MIN_SCORE}+ olan, paketi olmayan yeni ilan yok")
    goal_id = str(uuid.uuid4())
    goal = {"goal_id": goal_id, "kind": request.kind, "params": {"packages": request.packages}, "status": "running",
            "budget": GOAL_BUDGET, "used": 0, "steps": steps, "summary": None, "error": None}
    store_write("save_goal", goal)
    add_activity(task_id=f"goal-{goal_id}", agent="orchestrator", event="goal", status="started",
                 goal_id=goal_id, picks=len(steps))
    start_goal(goal_id)
    return {"status": "running", "goal_id": goal_id, "steps": steps}


@app.post("/goals/{goal_id}/continue")
async def continue_goal(goal_id: str, request: GoalContinueRequest):
    """Pasted listing texts for the steps that wait for one; a stopped goal goes on with what is left."""
    goal = store_write("get_goal", goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    if goal["status"] not in ("waiting", "stopped"):
        raise HTTPException(status_code=409, detail=f"Goal is {goal['status']}")
    if goal_running():
        raise HTTPException(status_code=409, detail="Bir hedef zaten çalışıyor")
    waiting = {step["job_id"]: step for step in goal["steps"] if step["status"] == "needs_text"}
    for job_id, text in request.texts.items():
        text = (text or "").strip()
        if job_id not in waiting or not text:
            continue
        if len(text) > PACKAGE_DESCRIPTION_CHARS:
            raise HTTPException(status_code=422, detail=f"description must be at most {PACKAGE_DESCRIPTION_CHARS} characters")
        store_write("set_job", job_id, {"description": text})
        waiting[job_id]["status"] = "pending"
    if not any(step["status"] == "pending" for step in goal["steps"]):
        raise HTTPException(status_code=422, detail="Devam edecek adım yok: ilan metni gerekli")
    store_write("save_goal", {"goal_id": goal_id, "status": "running", "steps": goal["steps"], "error": None})
    add_activity(task_id=f"goal-{goal_id}", agent="orchestrator", event="goal", status="continued", goal_id=goal_id)
    start_goal(goal_id)
    return {"status": "running", "goal_id": goal_id}


@app.get("/goals")
async def list_goals():
    return {"goals": store_write("list_goals", 20) or []}


@app.get("/goals/{goal_id}")
async def get_goal(goal_id: str):
    goal = store_write("get_goal", goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    return goal


def note_name(text: str, limit: int = 50) -> str:
    text = re.sub(r"[/\\\x00-\x1f]+", "-", " ".join((text or "").split())).strip(" .-")
    return text[:limit].strip(" .-") or "ilan"


@app.post("/jobs/{job_id}/package/save")
async def save_package(job_id: str):
    """Propose the package as Obsidian notes; each is written when the user approves it."""
    job = store_write("get_job", job_id)
    package = store_write("get_package", job_id)
    if not job or not package:
        raise HTTPException(status_code=404, detail="Package not found")
    if package["status"] not in ("ready", "needs_fix"):
        raise HTTPException(status_code=409, detail=f"The package is {package['status']}")

    folder = f"{APPLICATIONS_DIR}/{utc_now()[:10]} {note_name(job.get('company'), 40)} - {note_name(job['title'])}"
    review = package.get("review") or {}
    warning = ""
    if review.get("verdict") != "ok":
        items = "\n".join(f"> - {x}" for x in [*review.get("unsupported", []), *review.get("notes", [])][:10])
        warning = f"> [!warning] Gordon: gözden geçir\n{items}\n\n"
    header = f"İlan: [{job['title']} · {job.get('company') or '-'}]({job['url']})\n\n"
    notes = [(f"{folder}/CV.md", warning + header + package["cv"])]
    if package.get("letter"):
        notes.append((f"{folder}/Ön yazı.md", header + package["letter"]))

    proposed = []
    for path, content in notes:
        answer = await propose_note(NoteProposal(path=path, content=content + "\n",
                                                 reason=f"Başvuru paketi: {job['title']} · {job.get('company') or '-'}"))
        proposed.append({"path": path, **answer})
    return {"job_id": job_id, "notes": proposed}


DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PACKAGE_PARTS = {"cv": "CV", "letter": "Ön yazı"}
# The CV photo, taken once from the user's own CV PDF (pdfimages) into data/career/.
CV_PHOTO = Path(os.getenv("CV_PHOTO_PATH", "/data/career/photo.jpg"))


def cv_photo() -> bytes | None:
    try:
        data = CV_PHOTO.read_bytes()
    except OSError:
        return None
    return data if data[:3] == b"\xff\xd8\xff" else None


@app.get("/career/photo")
async def career_photo():
    photo = cv_photo()
    if photo is None:
        raise HTTPException(status_code=404, detail="No photo")
    return Response(photo, media_type="image/jpeg", headers={"cache-control": "private, max-age=3600"})


@app.get("/jobs/{job_id}/package/{part}.docx")
async def package_docx(job_id: str, part: str):
    """The tailored CV or the letter as a Word file, to edit and send by hand."""
    if part not in PACKAGE_PARTS:
        raise HTTPException(status_code=404, detail="Unknown part")
    job = store_write("get_job", job_id)
    package = store_write("get_package", job_id)
    text = (package or {}).get(part)
    if not job or not text:
        raise HTTPException(status_code=404, detail="Package not found")
    name = f"{PACKAGE_PARTS[part]} - {note_name(job.get('company'), 40)} - {note_name(job['title'])}.docx"
    ascii_name = re.sub(r"[^A-Za-z0-9 ._-]+", "_", name)
    data = cv_docx(package["cv_data"], cv_photo()) if part == "cv" and package.get("cv_data") else to_docx(text)
    return Response(data, media_type=DOCX_TYPE, headers={
        "content-disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        "cache-control": "no-store"})


@app.post("/jobs/{job_id}/status")
async def set_job_status(job_id: str, request: JobStatusRequest):
    """The user's tracking: applied, interview, rejected, offer; dismissed hides it."""
    if request.status not in USER_JOB_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(USER_JOB_STATUSES)}")
    job = store_write("get_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    store_write("set_job", job_id, {"status": request.status})
    add_activity(task_id="system", agent="orchestrator", event="jobs", status="status_changed",
                 job_id=job_id, title=job["title"], to=request.status)
    return store_write("get_job", job_id)


@app.get("/metrics")
async def metrics_report(days: int = 7):
    """L-1a: token use, errors and model success over 1, 7 or 30 days (plan-l)."""
    if days not in metrics.DAY_CHOICES:
        raise HTTPException(status_code=422, detail="days must be 1, 7 or 30")
    today, tz = budget_manager.local_today(), budget_manager.budget_tz
    since = metrics.window_start(days, today, tz).isoformat()
    rows = store_write("metric_rows", since, metrics.EVENTS) or {"tasks": [], "events": [], "approvals": []}
    return metrics.report(days, token_manager.read_usage(), rows, today, tz)


@app.get("/usage/today")
def usage_today():
    """Today's token use (budget day, local time); cloud counts for the budget."""
    agents, cloud_total, local_total = {}, 0, 0

    for record in budget_manager.daily_records():
        tokens = int(record.get("total_tokens", 0) or 0)
        agent = agents.setdefault(
            record.get("agent") or "unknown",
            {"calls": 0, "cloud_tokens": 0, "local_tokens": 0},
        )
        agent["calls"] += 1
        if is_cloud(record):
            agent["cloud_tokens"] += tokens
            cloud_total += tokens
        else:
            agent["local_tokens"] += tokens
            local_total += tokens

    for name, agent in agents.items():
        agent["budget"] = budget_manager.budget_for_agent(name)

    budget = budget_manager.system_daily_budget
    return {
        "date": budget_manager.local_today().isoformat(),
        "system": {
            "used": cloud_total,
            "budget": budget,
            "remaining": max(budget - cloud_total, 0),
            "local_tokens": local_total,
        },
        "task_budget": budget_manager.task_budget,
        "request_budget": budget_manager.request_budget,
        "agents": agents,
    }


UI_DIR = os.getenv("UI_DIR", "/app/ui")


def mount_ui() -> bool:
    """Serve the CEO Interface at /ui when its folder is mounted (read only)."""
    if not Path(UI_DIR).is_dir():
        return False

    from fastapi.staticfiles import StaticFiles

    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")
    return True


mount_ui()


@app.get("/activity")
def get_activity(
    limit: int = 100,
):
    if limit < 1:
        raise HTTPException(
            status_code=400,
            detail="Limit must be greater than 0",
        )

    return {
        "count": min(
            limit,
            len(ACTIVITY),
        ),
        "events": ACTIVITY[-limit:],
    }


# --------------------------------------------------
# Approval endpoints
# --------------------------------------------------

@app.get("/approvals")
async def list_approvals(
    status: str | None = None,
):
    # The store also holds approvals from before a restart; the
    # in-memory copy wins for the ones this process knows.
    approvals = {
        approval["approval_id"]: approval
        for approval in store_write("list_approvals", status) or []
    }

    for approval in APPROVALS.values():
        if not status or approval["status"] == status:
            approvals[approval["approval_id"]] = approval

    approvals = sorted(
        approvals.values(),
        key=lambda approval: approval["created_at"],
    )

    return {
        "count": len(approvals),
        "approvals": approvals,
    }


@app.get("/approvals/{approval_id}")
async def get_approval(
    approval_id: str,
):
    approval = APPROVALS.get(
        approval_id
    ) or store_write("get_approval", approval_id)

    if not approval:
        raise HTTPException(
            status_code=404,
            detail="Approval not found",
        )

    return approval


@app.post("/approvals/{approval_id}/approve")
async def approve_endpoint(
    approval_id: str,
):
    approval = APPROVALS.get(
        approval_id
    )

    if not approval:
        raise HTTPException(
            status_code=404,
            detail="Approval not found",
        )

    if approval["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=(
                "Approval is not pending. "
                f"Current status: {approval['status']}"
            ),
        )

    approval["status"] = "approved"
    approval["approved_at"] = utc_now()
    approval["updated_at"] = utc_now()
    persist_approval(approval)

    add_activity(
        task_id=approval["task_id"],
        agent=approval["agent"],
        event="approval",
        status="approved",
        approval_id=approval_id,
        action=approval["action"],
    )

    return await continue_after_approval(
        approval_id
    )


@app.post("/approvals/{approval_id}/reject")
async def reject_endpoint(
    approval_id: str,
):
    approval = APPROVALS.get(
        approval_id
    )

    if not approval:
        raise HTTPException(
            status_code=404,
            detail="Approval not found",
        )

    if approval["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=(
                "Approval is not pending. "
                f"Current status: {approval['status']}"
            ),
        )

    approval["status"] = "rejected"
    approval["rejected_at"] = utc_now()
    approval["updated_at"] = utc_now()
    persist_approval(approval)

    add_activity(
        task_id=approval["task_id"],
        agent=approval["agent"],
        event="approval",
        status="rejected",
        approval_id=approval_id,
        action=approval["action"],
    )

    return await continue_after_approval(
        approval_id
    )


# J-6: the user asks the reviewer to look at a finished task's answer.
MANUAL_REVIEWER = "gordon"
MANUAL_REVIEW_STATUSES = ("completed", "needs_review")
REVIEWS_RUNNING = set()


async def run_manual_review(task: dict, response: str):
    task_id = task["task_id"]
    reviewer = MANUAL_REVIEWER if task.get("worker") != MANUAL_REVIEWER else "gandalf"
    review_id = str(uuid.uuid4())
    add_activity(task_id=task_id, agent="orchestrator", event="review", status="started", reviewer=reviewer, manual=True)
    try:
        review = await run_worker(
            reviewer,
            review_task(task.get("message") or ""),
            task_id,
            "review",
            REVIEW_REPORT_CHARS,
            worker_task_id=review_id,
            inputs=input_block([{"id": "answer", "worker": task.get("worker"), "worker_task_id": task_id,
                                 "report": {"summary": "Kullanıcıya verilen yanıt", "deliverable": response}}]),
            step="review",
            instructions=REVIEW_INSTRUCTIONS,
        )
    except Exception as exc:
        review = {"status": "failed", "worker": reviewer, "error": str(exc)}
    finally:
        REVIEWS_RUNNING.discard(task_id)

    verdict = (review.get("report") or {}).get("verdict") if review.get("status") == "completed" else None
    status = "completed" if verdict == "approve" else "needs_review"
    add_activity(task_id=task_id, agent="orchestrator", event="review", status="done" if verdict == "approve" else "needs_review",
                 reviewer=reviewer, verdict=verdict, manual=True, **({"error": review.get("error")} if review.get("error") else {}))
    result = {**(task.get("result") or {}), "status": status, "review": review_summary(review, review_id, verdict)}
    store_write("save_task", {"task_id": task_id, "status": status, "result": result})
    if task_id in TASKS:
        TASKS[task_id]["status"], TASKS[task_id]["result"] = status, result


class CodeTaskRequest(BaseModel):
    repo: str
    task: str
    files: list[str]
    focus: list[str] = []
    worker: str = "jarvis"


async def run_direct_code(task_id: str, request: CodeTaskRequest):
    """M-3c: a code task without Ares; the same guards (files, tests, review, facts)."""
    TASKS[task_id]["status"] = "running"
    persist_task(task_id)
    try:
        outcome = await execute_worker_tool("delegate_to_worker", {
            "worker": request.worker, "task": request.task,
            "code": {"repo": request.repo, "files": request.files, "focus": request.focus},
        }, task_id)
    except Exception as exc:  # noqa: BLE001 - the task must end
        outcome = {"status": "failed", "error": str(exc)}
    summary = ((outcome.get("report") or {}).get("summary") or outcome.get("error") or "").strip()
    facts = code_fact_lines(task_id)
    response = "\n\n".join(x for x in (summary, facts) if x)
    status = "needs_review" if outcome.get("status") == "error" or not facts or code_open(task_id) else "completed"
    complete_task(task_id, status, error=outcome.get("error"))
    TASKS[task_id]["result"] = {"status": status, "task_id": task_id, "response": response, "code": outcome}
    persist_task(task_id)
    add_activity(task_id=task_id, agent="orchestrator", event="task", status=status)


@app.post("/code-tasks")
async def create_code_task(request: CodeTaskRequest):
    """
    M-3c: start a code task directly (no Ares); returns at once, GET
    /tasks/{id} follows it. Local only: the gateway does not pass it.
    """
    if request.repo not in KNOWN_REPOS:
        raise HTTPException(status_code=422, detail=f"repo must be one of: {', '.join(KNOWN_REPOS)}")
    if not request.task.strip() or not request.files:
        raise HTTPException(status_code=422, detail="task and files are required")
    task_id = create_task(f"[kod · {request.repo}] {request.task.strip()}", request.worker)
    work = asyncio.create_task(run_direct_code(task_id, request))
    BACKGROUND_JOBS.add(work)
    work.add_done_callback(BACKGROUND_JOBS.discard)
    return {"status": "running", "task_id": task_id}


@app.post("/tasks/{task_id}/review")
async def review_task_endpoint(task_id: str):
    """J-6: start a review of a finished top-level task's answer; the task ends completed or needs_review."""
    task = {**(store_write("get_task", task_id) or {}), **(TASKS.get(task_id) or {})}
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    response = ((task.get("result") or {}).get("response") or "").strip()
    if task.get("parent_id") or task.get("kind", "task") != "task" or task.get("status") not in MANUAL_REVIEW_STATUSES:
        raise HTTPException(status_code=409, detail="Yalnızca bitmiş (tamam / incelenmeli) ana görev incelenebilir.")
    if not response:
        raise HTTPException(status_code=409, detail="Görevin incelenecek bir yanıtı yok.")
    if task_id in REVIEWS_RUNNING:
        raise HTTPException(status_code=409, detail="Bu görev zaten inceleniyor.")
    REVIEWS_RUNNING.add(task_id)
    work = asyncio.ensure_future(run_manual_review(task, response))
    BACKGROUND_JOBS.add(work)
    work.add_done_callback(BACKGROUND_JOBS.discard)
    return {"status": "running", "task_id": task_id}


@app.post("/tasks/{task_id}/cancel")
async def cancel_task_endpoint(
    task_id: str,
):
    task = TASKS.get(task_id) or store_write("get_task", task_id)

    if not task:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    job = RUNNING_TASKS.get(task_id)

    if (
        job is None
        and task["status"] == "waiting_approval"
        and task_id in TASKS
    ):
        # Its pending writes will never run; approving them later
        # is refused by the usual "not pending" check.
        for approval in get_pending_approvals_for_task(task_id):
            approval["status"] = "cancelled"
            approval["updated_at"] = utc_now()
            persist_approval(approval)

            add_activity(
                task_id=task_id,
                agent=approval["agent"],
                event="approval",
                status="cancelled",
                approval_id=approval["approval_id"],
                action=approval["action"],
            )

        result = stop_task(task_id, "cancelled", "cancelled by user")
        TASKS[task_id]["result"] = result
        persist_task(task_id)
        record_episode(task_id)

        return {
            "task_id": task_id,
            "status": "cancelled",
        }

    if job is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Task is not running. "
                f"Current status: {task['status']}"
            ),
        )

    CANCEL_REQUESTED.add(task_id)
    job.cancel()

    # Give the run a moment to unwind (budget release, records).
    await asyncio.wait([job], timeout=5)

    return {
        "task_id": task_id,
        "status": TASKS.get(task_id, task)["status"],
    }


@app.get("/memory/episodes")
async def list_episodes(
    limit: int = DEFAULT_TASK_LIST_LIMIT,
    worker: str | None = None,
):
    """Episodes of finished tasks, newest first (Faz F)."""
    limit = max(1, min(int(limit), MAX_TASK_LIST_LIMIT))

    episodes = store_write("list_episodes", limit, worker) or []

    return {
        "count": len(episodes),
        "episodes": episodes,
    }


@app.get("/memory/facts")
async def list_facts(
    status: str | None = None,
    scope: str | None = None,
    include_expired: bool = False,
    limit: int = DEFAULT_TASK_LIST_LIMIT,
):
    """Lasting facts, newest first (Faz F). Expired ones only on request."""
    if status is not None and status not in FACT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown status: {status}",
        )

    limit = max(1, min(int(limit), MAX_TASK_LIST_LIMIT))
    now = None if include_expired else utc_now()

    facts = store_write("list_facts", status, scope, now, limit) or []

    return {
        "count": len(facts),
        "facts": facts,
    }


@app.post("/memory/facts/{fact_id}/resolve")
async def resolve_fact_endpoint(
    fact_id: int,
    request: ResolveFactRequest,
):
    """The user's choice for a flagged fact (Faz F, plan-f karar J)."""
    if request.choice not in ("keep_old", "use_new"):
        raise HTTPException(
            status_code=422,
            detail="choice must be keep_old or use_new",
        )

    if task_store is None:
        raise HTTPException(
            status_code=503,
            detail="Memory store is not available",
        )

    fact = store_write("get_fact", fact_id)

    if not fact:
        raise HTTPException(
            status_code=404,
            detail="Fact not found",
        )

    if fact["status"] != "flagged":
        raise HTTPException(
            status_code=409,
            detail=f"Fact is {fact['status']}, not flagged",
        )

    result = store_write(
        "resolve_fact",
        fact_id,
        request.choice == "use_new",
        utc_now(),
    )

    if result is None:
        raise HTTPException(
            status_code=500,
            detail="The choice could not be saved",
        )

    add_activity(
        task_id="system",
        agent="orchestrator",
        event="memory",
        status="resolved",
        fact_id=fact_id,
        choice=request.choice,
        superseded=result["superseded"],
    )

    return {
        **result,
        "choice": request.choice,
    }


@app.delete("/memory/facts/{fact_id}")
async def delete_fact(
    fact_id: int,
):
    """Soft delete: the fact stays in the table as "deleted"."""
    if task_store is None:
        raise HTTPException(
            status_code=503,
            detail="Memory store is not available",
        )

    fact = store_write("get_fact", fact_id)

    if not fact:
        raise HTTPException(
            status_code=404,
            detail="Fact not found",
        )

    store_write("set_fact_status", fact_id, "deleted")

    add_activity(
        task_id="system",
        agent="orchestrator",
        event="memory",
        status="deleted",
        fact_id=fact_id,
    )

    return {
        "fact_id": fact_id,
        "status": "deleted",
    }


# --------------------------------------------------
# Auto-approval endpoints
# --------------------------------------------------

@app.post("/auto-approval")
def create_auto_approval(
    request: AutoApprovalRequest,
):
    now = datetime.now(timezone.utc)

    expires_at = (
        now
        + timedelta(
            minutes=request.duration_minutes
        )
    )

    policy_id = str(uuid.uuid4())

    policy = {
        "auto_approval_id": policy_id,
        "agent": request.agent,
        "actions": request.actions,
        "paths": request.paths,
        "duration_minutes": (
            request.duration_minutes
        ),
        "status": "active",
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }

    AUTO_APPROVALS[policy_id] = policy

    add_activity(
        task_id="system",
        agent="orchestrator",
        event="auto_approval",
        status="created",
        auto_approval_id=policy_id,
        policy=policy,
    )

    return policy


@app.get("/auto-approval")
def list_auto_approvals():
    cleanup_expired_auto_approvals()

    policies = list(
        AUTO_APPROVALS.values()
    )

    return {
        "count": len(policies),
        "auto_approvals": policies,
    }


@app.delete("/auto-approval/{policy_id}")
def disable_auto_approval(
    policy_id: str,
):
    policy = AUTO_APPROVALS.get(
        policy_id
    )

    if not policy:
        raise HTTPException(
            status_code=404,
            detail=(
                "Auto-approval policy not found"
            ),
        )

    if policy["status"] == "active":
        policy["status"] = "disabled"
        policy["disabled_at"] = utc_now()

        add_activity(
            task_id="system",
            agent="orchestrator",
            event="auto_approval",
            status="disabled",
            auto_approval_id=policy_id,
        )

    return policy


# --------------------------------------------------
# Main task endpoint
# --------------------------------------------------

@app.post("/task")
async def create_task_endpoint(
    request: TaskRequest,
):
    if not OMNIROUTE_KEY:
        raise HTTPException(
            status_code=500,
            detail=(
                "OMNIROUTE_KEY is not configured"
            ),
        )

    idempotency_key = (
        getattr(request, "idempotency_key", None) or ""
    ).strip() or None

    if idempotency_key:
        if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_CHARS:
            raise HTTPException(
                status_code=422,
                detail=(
                    "idempotency_key is longer than "
                    f"{MAX_IDEMPOTENCY_KEY_CHARS} characters"
                ),
            )

        # No await between this lookup and create_task: two requests
        # with the same key cannot both open a task.
        existing = find_idempotent_task(idempotency_key)

        if existing:
            if existing["message"] != request.message:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "idempotency_key was already used for "
                        "a different message"
                    ),
                )

            if existing.get("result"):
                return {
                    **existing["result"],
                    "idempotent_replay": True,
                }

            return {
                "status": existing["status"],
                "task_id": existing["task_id"],
                "idempotent_replay": True,
            }

    task_id = create_task(
        request.message,
        DEFAULT_WORKER,
        idempotency_key,
    )

    worker_context = get_worker_context(
    task_id
    )

    messages = [
        {
            "role": "system",
            "content": (
                f"{SYSTEM_PROMPT}\n"
                f"Bugünün tarihi: {datetime.now().astimezone().date().isoformat()}.\n\n"
                "========================================\n"
                "PERSISTENT WORKER CONTEXT\n"
                "========================================\n\n"
                f"{worker_context}"
            ),
        },
        {
            "role": "user",
            "content": request.message,
        },
    ]

    TASK_CONTEXTS[task_id] = {
        "messages": messages,
        "pending_tools": {},
        "next_iteration": 1,
        "reads": set(),
    }

    update_worker_daily_memory(
        task_id,
        "Work Log",
        (
            f"Task started at {utc_now()}.\n"
            f"Worker: {DEFAULT_WORKER}"
        ),
    )   

    add_activity(
        task_id=task_id,
        agent="orchestrator",
        event="task",
        status="started",
        message=request.message,
    )

    if getattr(request, "background", False):
        job = asyncio.ensure_future(run_in_background(task_id))
        BACKGROUND_JOBS.add(job)
        job.add_done_callback(BACKGROUND_JOBS.discard)

        return {
            "status": "running",
            "task_id": task_id,
            "background": True,
        }

    return await run_tracked(
        task_id
    )
