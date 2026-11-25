"""
Context engine (roadmap Faz D, docs/plan-d.md).

Pure functions; no I/O and no LLM call. Budgets are in characters: there
is no tokenizer in the image, and character limits are deterministic.

D-3: compact worker report. A worker answers with one JSON object; what
Ares receives is that object, validated and cut to the worker's share of
the delegation budget. Output that is not JSON falls back to trimmed
plain text.

D-4: read tool results. Search results become a short list of unique
paths; a long note is returned in parts the model continues with an
offset.

D-5: budgeted context. The identity file and the daily task file are
placed by priority; what carries no information is left out, and what
does not fit is cut oldest first.
"""

from __future__ import annotations

import json
import re

# One delegation call returns at most this much report text to Ares,
# split between its workers.
DELEGATION_RESULT_CHARS = 3000
MIN_REPORT_CHARS = 600

MAX_SUMMARY_WORDS = 150
MAX_FINDINGS = 5
MAX_ARTIFACTS = 5
MAX_OPEN_QUESTIONS = 3
MAX_ITEM_CHARS = 200

# Completion limit of a cloud worker call (decision C).
MIN_WORKER_COMPLETION_TOKENS = 400

TRIM_MARK = " [kırpıldı]"
EMPTY_RESPONSE = "Worker boş cevap döndürdü."

STATUSES = ("ok", "partial", "failed")
UNSTRUCTURED = "unstructured"
STATUS_ALIASES = {
    "completed": "ok",
    "complete": "ok",
    "done": "ok",
    "success": "ok",
    "tamam": "ok",
    "tamamlandı": "ok",
}
LIST_FIELDS = {
    "findings": MAX_FINDINGS,
    "artifacts": MAX_ARTIFACTS,
    "open_questions": MAX_OPEN_QUESTIONS,
}
CONTENT_FIELDS = ("summary", "findings", "deliverable")
# Review calls (Faz G-3) add a verdict; anything else is no verdict.
VERDICT_ALIASES = {
    "approve": "approve",
    "approved": "approve",
    "revise": "revise",
    "revision": "revise",
    "changes_requested": "revise",
}

FENCE = re.compile(r"```[A-Za-z]*\s*\n?(.*?)```", re.DOTALL)
SENTENCE_ENDS = (". ", "! ", "? ", ".\n", "!\n", "?\n")


def report_share(worker_count: int) -> int:
    """Characters one worker's report may take in a delegation of this size."""
    return max(
        MIN_REPORT_CHARS,
        DELEGATION_RESULT_CHARS // max(int(worker_count or 1), 1),
    )


def summary_words(share: int) -> int:
    return min(MAX_SUMMARY_WORDS, share // 16)


def worker_completion_tokens(share: int, reserve: int) -> int:
    """
    max_tokens of a cloud worker call: enough for a report of `share`
    characters (measured: about 3.5 characters per token) plus slack,
    never above the normal completion reserve.
    """
    return min(
        max(share // 3 + 100, MIN_WORKER_COMPLETION_TOKENS),
        reserve,
    )


def report_instructions(share: int) -> str:
    """Text added to the worker prompt; states the schema and this call's limits."""
    return (
        "Cevabını YALNIZCA aşağıdaki şemada tek bir JSON nesnesi olarak ver; "
        "JSON dışında metin veya kod bloğu işareti yazma.\n"
        "{\n"
        '  "status": "ok" | "partial" | "failed",\n'
        f'  "summary": "sonucun özeti, en fazla {summary_words(share)} kelime",\n'
        f'  "findings": ["en fazla {MAX_FINDINGS} kısa madde"],\n'
        '  "artifacts": ["ilgili not yolları; yoksa boş liste"],\n'
        f'  "open_questions": ["en fazla {MAX_OPEN_QUESTIONS} açık soru; yoksa boş liste"],\n'
        '  "deliverable": "yalnız görevin ürünü kod veya metinse, ürünün kendisi; '
        'değilse bu alanı yazma"\n'
        "}\n"
        f"Raporun toplamı en fazla {share} karakter olsun; sığmayan kısım kırpılır. "
        "Varsayımlarını summary veya findings içinde belirt."
    )


def serialized_length(report: dict) -> int:
    return len(json.dumps(report, ensure_ascii=False))


def clip_text(text: str, limit: int, boundaries=SENTENCE_ENDS) -> str:
    """
    Cut text to at most `limit` characters including TRIM_MARK. Prefers
    the last boundary in the second half of the kept part, then the last
    space.
    """
    if len(text) <= limit:
        return text

    room = limit - len(TRIM_MARK)
    if room <= 0:
        return ""

    kept = text[:room]
    cut = max(kept.rfind(boundary) for boundary in boundaries)
    if cut >= room // 2:
        # keep the punctuation, drop the whitespace after it
        kept = kept[:cut + 1]
    elif kept.rfind(" ") >= room // 2:
        kept = kept[:kept.rfind(" ")]

    return kept.rstrip() + TRIM_MARK


def clip_words(text: str, max_words: int) -> str:
    words = list(re.finditer(r"\S+", text))
    if len(words) <= max_words:
        return text
    return text[:words[max_words - 1].end()] + TRIM_MARK


def as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False)


def as_items(value, max_items: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]

    items = []
    for item in value:
        text = as_text(item)
        if not text:
            continue
        if len(text) > MAX_ITEM_CHARS:
            text = text[:MAX_ITEM_CHARS - 1].rstrip() + "…"
        items.append(text)

    return items[:max_items]


def find_json_object(text: str) -> dict | None:
    """Whole output, then fenced blocks, then the outermost braces."""
    candidates = [text.strip()]
    candidates += [block.strip() for block in FENCE.findall(text)]

    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            # strict=False: models often leave raw newlines in strings
            value = json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

    return None


def normalize_report(data: dict) -> dict | None:
    """Known keys only, right types, field limits. None if it carries no content."""
    status = as_text(data.get("status")).lower()
    status = STATUS_ALIASES.get(status, status)

    report = {
        "status": status if status in STATUSES else "partial",
        "summary": clip_words(as_text(data.get("summary")), MAX_SUMMARY_WORDS),
    }

    for field, max_items in LIST_FIELDS.items():
        items = as_items(data.get(field), max_items)
        if items:
            report[field] = items

    deliverable = as_text(data.get("deliverable"))
    if deliverable:
        report["deliverable"] = deliverable

    verdict = VERDICT_ALIASES.get(as_text(data.get("verdict")).lower())
    if verdict:
        report["verdict"] = verdict

    if not any(report.get(field) for field in CONTENT_FIELDS):
        return None

    return report


def shrink_text_field(
    report: dict, field: str, share: int, boundaries=SENTENCE_ENDS, floor: int = 0,
) -> None:
    """
    Cut one text field until the report fits, but not below `floor`
    characters. An emptied deliverable is removed.
    """
    while serialized_length(report) > share and len(report.get(field) or "") > floor:
        text = report[field]
        # JSON escaping can make one character cost more than one; loop.
        limit = len(text) - (serialized_length(report) - share)
        report[field] = clip_text(text, max(min(limit, len(text) - 1), floor), boundaries)

    if field == "deliverable" and not report.get(field):
        report.pop(field, None)


def fit_report(report: dict, share: int) -> dict:
    """
    Cut the report to `share` serialized characters. Order: findings from
    the end, open questions from the end, deliverable, summary. Artifacts
    go only when the summary would otherwise fall under a quarter of the
    share.
    """
    report = {
        key: list(value) if isinstance(value, list) else value
        for key, value in report.items()
    }

    for field in ("findings", "open_questions"):
        while serialized_length(report) > share and report.get(field):
            report[field].pop()
        if not report.get(field):
            report.pop(field, None)

    shrink_text_field(report, "deliverable", share)
    shrink_text_field(report, "summary", share, floor=share // 4)

    while serialized_length(report) > share and report.get("artifacts"):
        report["artifacts"].pop()
    if not report.get("artifacts"):
        report.pop("artifacts", None)

    shrink_text_field(report, "summary", share)

    return report


def fallback_report(text: str, share: int) -> dict:
    text = (text or "").strip()
    if not text:
        return {"status": "failed", "summary": EMPTY_RESPONSE}

    report = {"status": UNSTRUCTURED, "summary": text}
    shrink_text_field(report, "summary", share, boundaries=("\n\n",) + SENTENCE_ENDS)
    return report


def parse_worker_report(text: str, share: int) -> tuple[dict, bool]:
    """
    Worker output -> (report for Ares, parsed). parsed is False when the
    output did not follow the schema and was trimmed as plain text.
    """
    data = find_json_object(text or "")
    report = normalize_report(data) if data is not None else None

    if report is None:
        return fallback_report(text, share), False

    return fit_report(report, share), True


# ----------------------------------------------------------------------
# D-4: read tool results (search_notes, read_note)
# ----------------------------------------------------------------------

MAX_SEARCH_RESULTS = 20
MAX_NOTE_CHARS = 6000


def compact_search(result: dict) -> dict:
    """search_notes result -> unique note paths, at most MAX_SEARCH_RESULTS."""
    paths = []
    for item in result.get("results") or []:
        path = item.get("path") if isinstance(item, dict) else item
        if path and path not in paths:
            paths.append(path)

    compact = {
        "query": result.get("query"),
        "count": len(paths),
        "results": paths[:MAX_SEARCH_RESULTS],
    }

    extra = len(paths) - MAX_SEARCH_RESULTS
    if extra > 0:
        compact["note"] = f"{extra} sonuç daha var; sorguyu daralt."

    return compact


def note_offset(value) -> int:
    """The model's offset argument as a usable position; anything odd is the start."""
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def compact_note(result: dict, offset: int = 0) -> dict:
    """
    read_note result -> the part of the note starting at `offset`, at
    most MAX_NOTE_CHARS, cut after a line end. The parts of a note join
    back to the note exactly. A short note read from the start is
    returned unchanged.
    """
    content = result.get("content")
    if not isinstance(content, str):
        return result

    total = len(content)
    if offset == 0 and total <= MAX_NOTE_CHARS:
        return result

    part = content[offset:offset + MAX_NOTE_CHARS]
    if offset + len(part) < total:
        cut = part.rfind("\n")
        # a line longer than half the window is cut mid-line instead
        if cut >= MAX_NOTE_CHARS // 2:
            part = part[:cut + 1]

    end = offset + len(part)
    compact = {**result, "content": part, "total_chars": total}
    if offset:
        compact["offset"] = offset

    if end < total:
        compact["truncated"] = True
        compact["next_offset"] = end
        compact["note"] = (
            f"Not kırpıldı: {total} karakterin {offset}–{end} arası. "
            f"Devamı için read_note(path, offset={end})."
        )
    elif not part:
        compact["note"] = "offset notun sonunda; okunacak içerik kalmadı."

    return compact


def repeated_search(query: str) -> dict:
    return {
        "query": query,
        "repeat": True,
        "note": "Bu sorgu bu görevde zaten çalıştırıldı; sonuç önceki tool sonucunda.",
    }


def repeated_note(path: str, offset: int = 0) -> dict:
    return {
        "path": path,
        "offset": offset,
        "repeat": True,
        "note": "Notun bu kısmı bu görevde zaten okundu; içerik önceki tool sonucunda.",
    }


# ----------------------------------------------------------------------
# D-5: budgeted context (identity file + daily task file)
# ----------------------------------------------------------------------

ARES_CONTEXT_CHARS = 6000
WORKER_CONTEXT_CHARS = 4000

IDENTITY_HEADING = "# WORKER IDENTITY\n\n"
DAILY_HEADING = "\n\n# CURRENT DAILY WORK MEMORY\n\n"
RETRIEVED_HEADING = "\n\n# RETRIEVED MEMORY\n\n"
RETRIEVED_NOTE = "Önceki kayıtlardan; eski olabilir, kaynağı ve tarihi yanında."
# Ares has the remember tool; the block must not read as "already saved" (F-7).
ARES_RETRIEVED_NOTE = (
    RETRIEVED_NOTE + " Yalnız okumak içindir; yeni bilgi kaydı için remember çağır."
    # G-4: an old episode must not stand in for the work asked now.
    " Görev özetleri geçmiş kayıttır: istenen işi yeniden yap ya da delege et,"
    " eski sonucu yeni sonuç gibi sunma."
)

# Sections of the identity file that grow over time. Everything else in
# the file is rules and is never cut.
MEMORY_SECTIONS = ("Important Decisions", "Learned Principles", "Things To Remember")

# Daily sections that carry nothing the model needs: the worker's name is
# in the identity, the task is the user message.
DAILY_SKELETON = ("Worker", "Task")
INITIAL_STATUS = "In Progress"
WORK_LOG_LINES = 5
MIN_STATE_CHARS = 300

SECTION_START = re.compile(r"(?m)^## ")


def split_sections(text: str) -> list[tuple[str | None, str]]:
    """
    Markdown -> [(heading, chunk)] cut at "## " lines; the part before
    the first heading has heading None. The chunks join back to the text.
    """
    starts = [match.start() for match in SECTION_START.finditer(text)]
    bounds = [0, *starts, len(text)]

    sections = []
    for begin, end in zip(bounds, bounds[1:]):
        chunk = text[begin:end]
        if not chunk:
            continue
        heading = chunk.split("\n", 1)[0][3:].strip() if begin in starts else None
        sections.append((heading, chunk))

    return sections


def section_body(chunk: str) -> str:
    return chunk.split("\n", 1)[1] if "\n" in chunk else ""


def render_section(heading: str, lines: list[str], dropped: int, what: str) -> str:
    body = "\n".join(lines).strip("\n")
    if dropped:
        body = f"({dropped} eski {what} çıkarıldı)\n{body}".rstrip("\n")
    return f"## {heading}\n\n{body}\n\n"


def fit_identity(identity: str, room: int) -> str:
    """
    Identity file for the context: rules as they are, empty memory
    sections left out. If it does not fit in `room`, the oldest memory
    items (top lines) go, from the largest memory section first. Rules
    are kept even when they alone are over `room`.
    """
    sections = []
    for heading, chunk in split_sections(identity):
        if heading in MEMORY_SECTIONS:
            if not section_body(chunk).strip():
                continue
            sections.append({"heading": heading, "chunk": chunk, "dropped": 0,
                             "lines": section_body(chunk).split("\n")})
        else:
            sections.append({"chunk": chunk})

    def text() -> str:
        return "".join(section["chunk"] for section in sections).rstrip()

    memory = [section for section in sections if "lines" in section]

    while len(text()) > room:
        filled = [section for section in memory if any(line.strip() for line in section["lines"])]
        if not filled:
            break

        section = max(filled, key=lambda item: len(item["chunk"]))
        lines = section["lines"]
        while not lines[0].strip():
            lines.pop(0)
        lines.pop(0)

        section["dropped"] += 1
        section["chunk"] = render_section(section["heading"], lines, section["dropped"], "madde")

    return text()


def fit_daily(daily: str, room: int, include_task: bool = False) -> str:
    """
    Daily task file for the context: only the sections that say
    something. If it does not fit in `room`, the work log is cut to its
    last lines, then the current state to its last part.
    """
    sections = {}
    for heading, chunk in split_sections(daily):
        body = section_body(chunk).strip()
        if heading is None or not body:
            continue
        if heading in DAILY_SKELETON and not (heading == "Task" and include_task):
            continue
        if heading == "Status" and body == INITIAL_STATUS:
            continue
        sections[heading] = body

    def text() -> str:
        return "\n\n".join(f"## {heading}\n\n{body}" for heading, body in sections.items())

    if len(text()) > room and "Work Log" in sections:
        lines = [line for line in sections["Work Log"].split("\n") if line.strip()]
        dropped = len(lines) - WORK_LOG_LINES
        if dropped > 0:
            sections["Work Log"] = f"({dropped} eski satır çıkarıldı)\n" + "\n".join(lines[-WORK_LOG_LINES:])

    over = len(text()) - room
    if over > 0 and "Current State" in sections:
        state = sections["Current State"]
        keep = max(len(state) - over - len(TRIM_MARK), MIN_STATE_CHARS)
        if keep < len(state):
            tail = state[-keep:]
            # start at a line start when one is near
            line_start = tail.find("\n")
            if 0 <= line_start < keep // 2:
                tail = tail[line_start + 1:]
            sections["Current State"] = f"{TRIM_MARK.strip()}\n{tail}"

    return text()


def fit_lines(lines: list[str], room: int) -> str:
    """Whole lines in order while they fit in `room`; empty when none does."""
    kept, used = [], 0
    for line in lines:
        cost = len(line) + (1 if kept else 0)
        if used + cost > room:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept)


def build_context(
    identity: str, daily: str | None, budget: int, task_chars: int = 0, include_task: bool = False,
    retrieved: list[str] = (), retrieved_chars: int = 0, retrieved_note: str = RETRIEVED_NOTE,
) -> str:
    """
    Context for one LLM call within `budget` characters, by priority:
    rules > persistent memory > task > retrieved memory > daily memory.
    `task_chars` is the size of the task text sent as the user message;
    it is never cut and comes out of the room left for what follows.

    retrieved: memory lines, best first (Faz F); at most
    `retrieved_chars` of them, whole lines only, after `retrieved_note`.

    Rules and the task are never cut, so the result can still be over
    budget; context_over_budget() tells.
    """
    context = IDENTITY_HEADING + fit_identity(identity, budget - len(IDENTITY_HEADING))

    if retrieved:
        room = min(retrieved_chars, budget - len(context) - task_chars - len(RETRIEVED_HEADING))
        block = fit_lines([retrieved_note, *retrieved], room)
        if "\n" in block:
            context += RETRIEVED_HEADING + block

    if daily:
        room = budget - len(context) - task_chars - len(DAILY_HEADING)
        daily_text = fit_daily(daily, room, include_task)
        if daily_text:
            context += DAILY_HEADING + daily_text

    return context


def context_over_budget(context: str, budget: int, task_chars: int = 0) -> int:
    """Characters over budget (0 when it fits)."""
    return max(len(context) + task_chars - budget, 0)
