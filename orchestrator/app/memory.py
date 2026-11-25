"""
Memory 2.0 (roadmap Faz F, docs/plan-f.md).

Pure functions; storage is in task_store.py (same database).

F-2: an episode is a short record of a finished task, built without an
LLM call from what the task already left: its message, its answer, the
reports of its workers and the writes it made (plan-f karar B).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from context_engine import TRIM_MARK, clip_text

EPISODE_DAYS = 180
EPISODE_TITLE_CHARS = 200
EPISODE_ANSWER_CHARS = 600
EPISODE_WORKER_CHARS = 200
EPISODE_ERROR_CHARS = 200
# Approval statuses worth remembering, with the word used in the summary.
WRITE_OUTCOMES = {
    "executed": "Yazıldı",
    "rejected": "Reddedildi",
    "failed": "Yazılamadı",
    "interrupted": "Yarıda kaldı",
    "cancelled": "İptal edildi",
}


def one_line(text) -> str:
    return " ".join(str(text or "").split())


def write_target(arguments: dict | None) -> str:
    arguments = arguments or {}
    if "destination" in arguments:
        return f"{arguments.get('source', '?')} → {arguments['destination']}"
    return str(arguments.get("path", "?"))


def build_episode(
    task: dict,
    children: list[dict] = (),
    approvals: list[dict] = (),
    events: list[dict] = (),
    now: datetime | None = None,
) -> dict:
    """
    task: the top-level task record (task_store / TASKS shape) with its
    final status and result. children: its worker tasks. approvals:
    its approvals. events: its activity events (for auto-approved
    writes, which leave no approval record).
    """
    lines = [f"Sonuç: {task['status']}"]

    if task.get("error"):
        lines[0] += f" ({clip_text(one_line(task['error']), EPISODE_ERROR_CHARS)})"

    result = task.get("result") or {}
    answer = one_line(result.get("response"))
    if answer:
        lines.append(f"Cevap: {clip_text(answer, EPISODE_ANSWER_CHARS)}")

    workers = []
    for child in sorted(children, key=lambda c: c.get("created_at") or ""):
        worker = child.get("worker") or "?"
        workers.append(worker)
        report = child.get("result") or {}
        summary = one_line(report.get("summary")) if isinstance(report, dict) else ""
        if summary:
            lines.append(f"{worker}: {clip_text(summary, EPISODE_WORKER_CHARS)}")
        else:
            lines.append(f"{worker}: {child.get('status', '?')}")

    for approval in sorted(approvals, key=lambda a: a.get("created_at") or ""):
        outcome = WRITE_OUTCOMES.get(approval.get("status"))
        if outcome:
            lines.append(f"{outcome}: {approval.get('action')} {write_target(approval.get('arguments'))}")

    for event in events:
        if event.get("event") == "approval" and event.get("status") == "auto_approved":
            lines.append(f"Otomatik onayla yazıldı: {event.get('action')} {event.get('path', '?')}")

    now = now or datetime.now(timezone.utc)

    return {
        "task_id": task["task_id"],
        "worker": task.get("worker"),
        "workers": sorted(set(workers)),
        "status": task["status"],
        "title": clip_text(one_line(task.get("message")), EPISODE_TITLE_CHARS),
        "summary": "\n".join(lines),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=EPISODE_DAYS)).isoformat(),
    }


# ----------------------------------------------------------------------
# F-3: lasting facts (plan-f karar D, E, F)
# ----------------------------------------------------------------------

MAX_FACT_KEY_CHARS = 100
MAX_FACT_VALUE_CHARS = 500
DEFAULT_IMPORTANCE = 2
# importance -> days a fact is kept; None: no expiry.
IMPORTANCE_DAYS = {1: 30, 2: 180, 3: None}
MAX_EXPIRES_IN_DAYS = 3650
# Expired facts stay (not retrieved) this long, then they are deleted.
FACT_PURGE_DAYS = 30
FACT_SCOPES = ("global", "ares", "gandalf", "jarvis", "oracle", "willy", "gordon")


def fold(text: str) -> str:
    """Turkish lower case: İ → i, I → ı, then the usual lower()."""
    return str(text).replace("İ", "i").replace("I", "ı").lower()


def normalize(text: str) -> str:
    """Key / value form used to compare facts: folded, single spaces, no edge punctuation."""
    return " ".join(fold(text).split()).strip(" .,;:!?\"'`")


def fact_expiry(importance: int, expires_in_days: int | None, now: datetime) -> str | None:
    days = expires_in_days if expires_in_days is not None else IMPORTANCE_DAYS[importance]
    return None if days is None else (now + timedelta(days=days)).isoformat()


def later_expiry(first: str | None, second: str | None) -> str | None:
    """The later of two expiry times; None (no expiry) wins."""
    if first is None or second is None:
        return None
    return max(first, second)


def new_fact(arguments: dict, task_id: str, source: str, now: datetime | None = None) -> dict:
    """
    remember() arguments -> fact record. Raises ValueError with a
    message for the model when an argument is not usable.
    """
    key = " ".join(str(arguments.get("key") or "").split())
    value = " ".join(str(arguments.get("value") or "").split())

    if not key or len(key) > MAX_FACT_KEY_CHARS:
        raise ValueError(f"key 1–{MAX_FACT_KEY_CHARS} karakter olmalı.")
    if not value or len(value) > MAX_FACT_VALUE_CHARS:
        raise ValueError(f"value 1–{MAX_FACT_VALUE_CHARS} karakter olmalı.")

    importance = arguments.get("importance", DEFAULT_IMPORTANCE)
    if isinstance(importance, bool) or importance not in IMPORTANCE_DAYS:
        raise ValueError("importance 1, 2 veya 3 olmalı.")

    scope = arguments.get("scope") or "global"
    if scope not in FACT_SCOPES:
        raise ValueError(f"scope şunlardan biri olmalı: {', '.join(FACT_SCOPES)}.")

    expires_in_days = arguments.get("expires_in_days")
    if expires_in_days is not None and (
        isinstance(expires_in_days, bool)
        or not isinstance(expires_in_days, int)
        or not 1 <= expires_in_days <= MAX_EXPIRES_IN_DAYS
    ):
        raise ValueError(f"expires_in_days 1–{MAX_EXPIRES_IN_DAYS} arası bir tam sayı olmalı.")

    now = now or datetime.now(timezone.utc)

    return {
        "key": key,
        "key_norm": normalize(key),
        "value": value,
        "scope": scope,
        "importance": importance,
        "source": source,
        "task_id": task_id,
        "status": "active",
        "confirmations": 1,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "expires_at": fact_expiry(importance, expires_in_days, now),
    }


def same_fact_value(first: str, second: str) -> bool:
    return normalize(first) == normalize(second)


# ----------------------------------------------------------------------
# F-4: retrieval (plan-f karar G, H)
# ----------------------------------------------------------------------

# Measured on tests/data/retrieval_tr.json (F-4): the first 4 letters of
# each word as an FTS5 prefix found 45/45 relevant records (exact words
# 34/45, plain prefix 37/45, first 5 letters 44/45) and ranked a
# relevant one first for 32/32 queries, at 0.78 unrelated hits per query.
QUERY_STEM_CHARS = 4
MIN_QUERY_WORD_CHARS = 3
MAX_QUERY_WORDS = 12
STOPWORDS = frozenset("""
ve ile bir bu şu o da de ki mi mı mu mü için gibi daha çok ne nasıl neden niye hangi kaç
ben sen biz siz bana beni benim bunu şunu onu olan olarak var yok kadar sonra önce her hiç
ama veya ya en göre lütfen yap yaz et misin mısın nedir neydi ise zaman nerede nereye
nereden mıyım miyim yeni
araç araçları araçsız kullan kullanma kullanmadan kullanarak delege delegate et etmeden etsin
görev görevi görevini hatırla yanıtla yanıt cevap cevapla açıkla kendin kimseye worker workers ares
""".split())

ARES_RETRIEVAL_CHARS = 1200
WORKER_RETRIEVAL_CHARS = 800
MAX_RETRIEVED_FACTS = 5
MAX_RETRIEVED_EPISODES = 3
RETRIEVED_LINE_CHARS = 300
EPISODE_LINE_TITLE_CHARS = 120
MIN_EPISODE_ANSWER_CHARS = 40

# Words split at "_" too, as the unicode61 index does ("delegate_to_workers").
WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)?")


def content_words(text: str) -> list[str]:
    """Distinct content words (folded; suffix after an apostrophe dropped)."""
    words = []
    for word in WORD.findall(fold(text or "")):
        word = word.split("'")[0]
        if len(word) < MIN_QUERY_WORD_CHARS or word in STOPWORDS or word.isdigit():
            continue
        if word not in words:
            words.append(word)
    return words


def query_words(text: str) -> list[str]:
    """Task text -> the content words searched for."""
    return content_words(text)[:MAX_QUERY_WORDS]


# G-4: an episode of the same task (same message again) told Ares the
# work was done; it skipped the delegation and made the result up. Such
# episodes are not retrieved.
NEAR_DUPLICATE_SIMILARITY = 0.8
# Extra episodes searched so that dropped duplicates leave room.
DUPLICATE_SEARCH_SLACK = 3


def near_duplicate(title: str, text: str) -> bool:
    """
    True when an episode title (the task message, maybe clipped) and
    the task text share at least 80 % of their content words.
    """
    title = one_line(title)
    text = one_line(text)
    if title.endswith(TRIM_MARK):
        title = title[: -len(TRIM_MARK)]
        text = text[: len(title)]
    first, second = set(content_words(title)), set(content_words(text))
    if not first or not second:
        return False
    return len(first & second) / len(first | second) >= NEAR_DUPLICATE_SIMILARITY


def fts_query(text: str) -> str | None:
    """FTS5 MATCH expression for the task text, None when nothing is worth searching."""
    # ı → i: the full-text indexes hold I, İ and ı as "i" (task_store).
    stems = dict.fromkeys(f'"{word[:QUERY_STEM_CHARS].replace("ı", "i")}"*' for word in query_words(text))
    return " OR ".join(stems) or None


def fact_line(fact: dict) -> str:
    line = f"- {fact['key']}: {one_line(fact['value'])} ({fact['source']}, {fact['created_at'][:10]})"
    return clip_text(line, RETRIEVED_LINE_CHARS)


def episode_line(episode: dict) -> str:
    """
    Title clipped on its own, the rest of the line for the answer: one
    clip_text over both cut at the title's first sentence end and lost
    the answer (F-6).
    """
    line = (
        f"- Görev ({episode['created_at'][:10]}, {episode['status']}): "
        f"{clip_text(episode['title'], EPISODE_LINE_TITLE_CHARS)}"
    )
    answer = next(
        (row[len("Cevap: "):] for row in episode["summary"].split("\n") if row.startswith("Cevap: ")), ""
    )
    room = RETRIEVED_LINE_CHARS - len(line) - len(" → ")
    if answer and room >= MIN_EPISODE_ANSWER_CHARS:
        line += " → " + clip_text(answer, room)
    return line[:RETRIEVED_LINE_CHARS]


def retrieved_lines(facts: list[dict] = (), episodes: list[dict] = ()) -> list[str]:
    """Best match first: facts, then episodes."""
    return [fact_line(f) for f in facts] + [episode_line(e) for e in episodes]
