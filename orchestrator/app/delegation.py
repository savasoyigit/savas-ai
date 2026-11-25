"""
Dependency-aware delegation (roadmap Faz G, docs/plan-g.md).

Pure functions; main.py runs the workers.

G-2: delegate_to_workers items may carry an `id` and `depends_on`
(ids of other items). Items run in layers: every item waits for the
ones it depends on and gets their reports as an "# INPUTS" block built
here, from the stored reports, never through Ares (plan-g karar B).
Ares gets the full report of the last items only; items whose result
went on to another item come back as a short reference.
"""

from __future__ import annotations

from context_engine import clip_text

MAX_CHAIN_DEPTH = 3
MAX_ID_CHARS = 32
# Room for all inputs of one item; split between its dependencies.
INPUT_CHARS = 3000
INPUT_HEADING = "# INPUTS (önceki worker raporları; bu görevden önce çalıştılar)"
REFERENCE_SUMMARY_CHARS = 300
SKIPPED_ERROR = "Not run: a task it depends on did not complete ({ids})."


# G-3 review chain (plan-g karar E, F): the reviewer's report takes this
# much of the delegation budget, the worker's output the rest.
REVIEW_REPORT_CHARS = 1000
VERDICTS = ("approve", "revise")
REVIEW_INSTRUCTIONS = (
    "Bu bir inceleme görevi. Girdideki çıktıyı görev açısından incele. "
    'JSON\'a "verdict" alanını ekle: düzeltme gerekiyorsa "revise" yaz ve '
    'düzeltilecekleri findings içinde somut olarak belirt; gerekmiyorsa "approve".'
)
# J-6 (user, 2026-11-17; replaces plan-g karar F): after a fix round the
# reviewer looks again, at most this many fix rounds. Output the reviewer did
# not approve (or could not review) is not done: the task ends needs_review.
MAX_REVIEW_ROUNDS = 2
REVIEWED_AFTER_FIX = (
    "report is the revised output, which the reviewer approved; the review is about that last version."
)
NEEDS_REVIEW_NOTE = (
    "The reviewer did not approve this output (or could not review it): it is NOT done. "
    "Tell the user plainly that it needs their review and give the reviewer's findings; "
    "do not present it as finished."
)


class GraphError(ValueError):
    """The delegation graph cannot run; the message is for the model."""


def item_id(item: dict) -> str | None:
    value = item.get("id")
    return None if value in (None, "") else str(value)


def plan_layers(delegations: list[dict]) -> list[list[int]]:
    """
    Item indexes in layers: an item comes after every item it depends
    on. Without any depends_on everything is one layer (the old
    parallel run). Raises GraphError when the graph cannot run.
    """
    ids: dict[str, int] = {}
    for index, item in enumerate(delegations):
        name = item_id(item)
        if name is None:
            continue
        if len(name) > MAX_ID_CHARS:
            raise GraphError(f"id may have at most {MAX_ID_CHARS} characters: {name[:40]}")
        if name in ids:
            raise GraphError(f"Duplicate id: {name}")
        ids[name] = index

    needs: list[set[int]] = []
    for index, item in enumerate(delegations):
        depends_on = item.get("depends_on") or []
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            raise GraphError("depends_on must be a list of ids.")
        wanted = set()
        for name in depends_on:
            if name not in ids:
                raise GraphError(f"depends_on refers to an unknown id: {name}")
            if ids[name] == index:
                raise GraphError(f"A task cannot depend on itself: {name}")
            wanted.add(ids[name])
        needs.append(wanted)

    layers: list[list[int]] = []
    placed: set[int] = set()
    while len(placed) < len(delegations):
        layer = [i for i in range(len(delegations)) if i not in placed and needs[i] <= placed]
        if not layer:
            raise GraphError("depends_on has a cycle.")
        layers.append(layer)
        placed.update(layer)

    if len(layers) > MAX_CHAIN_DEPTH:
        raise GraphError(f"A dependency chain may have at most {MAX_CHAIN_DEPTH} steps.")

    return layers


def dependents(delegations: list[dict]) -> set[int]:
    """Indexes of items another item depends on (their result is an input)."""
    ids = {item_id(item): index for index, item in enumerate(delegations) if item_id(item)}
    return {ids[name] for item in delegations for name in item.get("depends_on") or [] if name in ids}


def report_body(report: dict) -> str:
    """Deliverable first, then summary, then findings (plan-g: priority)."""
    parts = []
    if report.get("deliverable"):
        parts.append(f"Deliverable:\n{report['deliverable']}")
    if report.get("summary"):
        parts.append(f"Summary: {report['summary']}")
    findings = [f"- {finding}" for finding in report.get("findings") or []]
    if findings:
        parts.append("Findings:\n" + "\n".join(findings))
    # Faz H: what the [n] citations point to.
    if report.get("sources"):
        parts.append("Sources:\n" + "\n".join(report["sources"]))
    return "\n".join(parts)


def input_block(sources: list[dict]) -> str:
    """
    sources: [{"id", "worker", "worker_task_id", "report"}] of the
    items this one depends on. Each gets an equal share of INPUT_CHARS.
    """
    if not sources:
        return ""

    share = INPUT_CHARS // len(sources)
    sections = []
    for source in sources:
        head = f"## {source['id']} · {source['worker']} (task {source['worker_task_id']})\n"
        body = clip_text(report_body(source["report"]), max(share - len(head), 0))
        sections.append(head + body)

    return INPUT_HEADING + "\n\n" + "\n\n".join(sections)


def reference(item_name: str | None, result: dict, worker_task_id: str) -> dict:
    """What Ares gets for an item whose result went on to another item."""
    report = result.get("report") or {}
    ref = {
        "worker": result.get("worker"),
        "status": result.get("status"),
        "task_id": worker_task_id,
        "summary": clip_text(" ".join(str(report.get("summary") or "").split()), REFERENCE_SUMMARY_CHARS),
        "note": "Its full report was passed to the tasks that depend on it.",
    }
    if item_name:
        ref = {"id": item_name, **ref}
    return ref


def review_task(task: str) -> str:
    return f"Şu görev için yazılan çıktıyı incele (girdide):\n{task}"


def revision_task(task: str) -> str:
    return (
        f"{task}\n\nDüzeltme turu: önceki çıktın (draft) ve inceleyenin bulguları "
        "(review) girdide. Bulguları gider ve çıktının tamamını yeniden ver."
    )


def review_summary(result: dict, worker_task_id: str, verdict: str | None) -> dict:
    """What Ares gets about the review."""
    report = result.get("report") or {}
    summary = {
        "worker": result.get("worker"),
        "status": result.get("status"),
        "task_id": worker_task_id,
        "verdict": verdict,
    }
    if result.get("status") != "completed":
        summary["error"] = result.get("error")
        return summary
    summary["summary"] = report.get("summary")
    if report.get("findings"):
        summary["findings"] = report["findings"]
    return summary
