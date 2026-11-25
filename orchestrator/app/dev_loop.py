"""
Code tasks in a sandbox copy (roadmap Faz I-3, docs/plan-i.md karar D).

Pure functions; main.py runs the worker and the runner.

A delegation with `code` gives the worker the files it names (from the
sandbox copy, never the live code) and asks for search / replace
blocks. The orchestrator applies them to the copy, the runner tests the
copy without network, and a failure goes back to the worker; at most
MAX_ROUNDS rounds. Nothing goes live (karar E).
"""

from __future__ import annotations

import re
from pathlib import Path

from context_engine import TRIM_MARK, clip_text

MAX_ROUNDS = 3
MAX_CODE_FILES = 5
MAX_PATH_CHARS = 200
# M-2 (plan-m karar C): big files come as a map plus excerpts.
MAX_FOCUS = 5
MAX_FOCUS_CHARS = 60
MAX_NEEDS = 3
MAX_NEED_LINES = 300
FOCUS_LINES = 40
FOCUS_HITS = 8
MAP_CHARS = 2000
TEST_PROFILES = ("pytest",)
# The worker's report goes to the orchestrator, not to Ares: room for edits.
CODE_REPORT_CHARS = 6000
FILE_INPUT_CHARS = 12000
# M-3c: two files must fit one call under REQUEST_TOKEN_BUDGET (8.000 with a 2.000 reserve).
TOTAL_INPUT_CHARS = 16000
TEST_OUTPUT_CHARS = 3000
# M-3b: the copy's file list for Ares (missing files) and the reviewer.
MAX_LISTED = 200
LIST_SKIP = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".woff", ".woff2", ".pdf")
# Owner of files created in the copy: the runner's user on the host.
SANDBOX_OWNER = (1000, 1000)

FILES_HEADING = "# FILES (sandbox kopyası; canlı kod değil)"
# I-4: what the reviewer gets, and what a revise sends back.
DIFF_HEADING = "# DIFF (sandbox → canlı repo; henüz uygulanmadı)"
DIFF_INPUT_CHARS = 6000
REVIEW_HEADING = "# REVIEW (inceleyenin bulguları; bunları gider)"
TEST_HEADING = "# TEST (önceki turun sonucu)"
REJECTED_HEADING = "# UYGULANMAYAN BLOKLAR (önceki tur)"
EDIT_INSTRUCTIONS = (
    "Bu bir kod görevi. Değişiklikleri deliverable alanına yalnız şu bloklarla yaz; "
    "dosyanın tamamını yazma:\n"
    "FILE: göreli/yol.py\n<<<<<<< SEARCH\n(dosyadaki birebir eski satırlar)\n=======\n"
    "(yeni satırlar)\n>>>>>>> REPLACE\n"
    "SEARCH dosyada birebir ve bir kez geçmeli; yeni dosya için SEARCH boş kalır. "
    "Büyük dosyalardan yalnız harita ve satır aralıkları gelir; SEARCH'ü yalnız gördüğün "
    "satırlardan al. Başka bir aralık ya da başka bir dosya gerekirse deliverable'a "
    "NEED: yol:başlangıç-bitiş satırını yaz (en çok 3); bir sonraki turda gelir. "
    "Dosya yolu uydurma: yalnız verilen ya da NEED ile istediğin dosyaları değiştir. "
    "Değişiklik gerekmiyorsa deliverable yazma."
)
NOTHING_LIVE = "Changes are only in the sandbox copy; nothing went live."

FORBIDDEN_PARTS = {"secrets", "data", ".git", ".venv", "backups", "sandbox", "sock"}
NEED_LINE = re.compile(r"^NEED:[ \t]*(?P<path>[^\s:]+):(?P<start>\d+)-(?P<end>\d+)[ \t]*$", re.MULTILINE)
# Map entries (M-2): definitions first, then page structure; a short label each.
MAP_DEFINITIONS = (
    re.compile(r"^\s*(?:async\s+)?(def|class)\s+(\w+)"),                                # Python
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(\w+)"),             # JavaScript
    re.compile(r"^\s*(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"),
    re.compile(r"^\s*(@(?:app|router)\.\w+\([^)]{0,60}\))"),                             # routes
)
MAP_STRUCTURE = (
    re.compile(r"<(section|script|style|template|form|dialog|main|nav|header|footer)\b"),
    re.compile(r"\sid=[\"']([^\"']{1,40})[\"']"),
)
MARKDOWN_HEADING = re.compile(r"^(#{1,4}\s.{0,60})")
EDIT_BLOCK = re.compile(
    r"^FILE:[ \t]*(?P<path>[^\n]+?)[ \t]*\n"
    r"<<<<<<< SEARCH\n(?P<search>.*?)^=======\n(?P<replace>.*?)^>>>>>>> REPLACE[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


class CodeError(ValueError):
    """The code argument or an edit cannot be used; the message is for the model."""


def clean_code(value) -> dict | None:
    """The `code` argument -> {"repo", "files", "test", "focus"}. Raises CodeError."""
    if value in (None, {}):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("repo"), str):
        raise CodeError("code must be an object with repo.")

    files = value.get("files") or []
    if not isinstance(files, list) or not all(isinstance(f, str) and f.strip() for f in files):
        raise CodeError("code.files must be a list of paths.")
    files = list(dict.fromkeys(f.strip().lstrip("/") for f in files))
    if len(files) > MAX_CODE_FILES:
        raise CodeError(f"code.files may name at most {MAX_CODE_FILES} files.")
    for path in files:
        check_path(path)

    test = value.get("test")
    if test not in (None, "", *TEST_PROFILES):
        raise CodeError(f"code.test must be one of: {', '.join(TEST_PROFILES)}.")

    focus = value.get("focus") or []
    if not isinstance(focus, list) or not all(isinstance(f, str) and f.strip() for f in focus):
        raise CodeError("code.focus must be a list of search terms.")
    focus = list(dict.fromkeys(f.strip()[:MAX_FOCUS_CHARS] for f in focus))[:MAX_FOCUS]

    return {"repo": value["repo"], "files": files, "test": test or None, "focus": focus}


def check_path(path: str) -> str:
    """A relative path inside the copy, away from secrets and tool folders."""
    parts = Path(path).parts
    if (
        not path or len(path) > MAX_PATH_CHARS or Path(path).is_absolute()
        or ".." in parts or any(p in FORBIDDEN_PARTS for p in parts)
        or any(p.startswith(".env") or p.endswith(".env") for p in parts)
    ):
        raise CodeError(f"Path not allowed: {path}")
    return path


def inside(root: Path, path: str) -> Path:
    target = (root / check_path(path)).resolve()
    if root.resolve() not in target.parents:
        raise CodeError(f"Path not allowed: {path}")
    return target


def parse_edits(text: str) -> list[dict]:
    """Complete blocks only; a block cut by the report limit is left out."""
    text = (text or "").replace("\r\n", "\n")
    if text.endswith(TRIM_MARK):
        text = text[: -len(TRIM_MARK)]
    return [
        {"path": m["path"].strip().lstrip("/"), "search": m["search"], "replace": m["replace"]}
        for m in EDIT_BLOCK.finditer(text)
    ]


def apply_edits(root: Path, edits: list[dict], owner=SANDBOX_OWNER, chown=None) -> dict:
    """
    Apply blocks in order. SEARCH must occur exactly once; an empty
    SEARCH creates a file that does not exist yet. A block that does
    not apply is reported and the others go on.
    """
    import os

    chown = chown or os.chown
    applied, rejected = [], []
    for edit in edits:
        try:
            target = inside(root, edit["path"])
            if edit["search"] == "":
                if target.exists():
                    raise CodeError("file exists; an empty SEARCH only creates new files")
                # M-3b: no invented folders; a new file goes next to existing code.
                if not target.parent.is_dir():
                    raise CodeError("new files only in an existing folder")
                created = [p for p in reversed(target.parents) if not p.exists()]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(edit["replace"], encoding="utf-8")
                for path in [*created, target]:
                    if owner:
                        chown(path, *owner)
            else:
                if not target.is_file():
                    raise CodeError("file does not exist")
                text = target.read_text(encoding="utf-8")
                count = text.count(edit["search"])
                if count != 1:
                    raise CodeError("SEARCH not found" if count == 0 else f"SEARCH found {count} times")
                target.write_text(text.replace(edit["search"], edit["replace"], 1), encoding="utf-8")
            if edit["path"] not in applied:
                applied.append(edit["path"])
        except (CodeError, OSError, UnicodeDecodeError) as exc:
            rejected.append({"path": edit["path"], "error": str(exc)})
    return {"applied": applied, "rejected": rejected}


def parse_needs(text: str, paths: list[str] | None = None) -> list[dict]:
    """NEED: path:start-end lines of a report; any allowed path unless paths is given (M-3a)."""
    needs = []
    for m in NEED_LINE.finditer((text or "").replace("\r\n", "\n")):
        path = m["path"].lstrip("/")
        start, end = int(m["start"]), int(m["end"])
        try:
            check_path(path)
        except CodeError:
            continue
        if (paths is not None and path not in paths) or start < 1 or end < start:
            continue
        needs.append({"path": path, "start": start, "end": min(end, start + MAX_NEED_LINES - 1)})
        if len(needs) == MAX_NEEDS:
            break
    return needs


def map_label(line: str, patterns) -> str | None:
    for pattern in patterns:
        m = pattern.search(line)
        if not m:
            continue
        if pattern is MAP_DEFINITIONS[0]:
            return f"{m[1]} {m[2]}"
        if pattern in MAP_DEFINITIONS[1:3]:
            return f"{m[1]}()"
        if pattern is MAP_STRUCTURE[0]:
            return f"<{m[1]}>"
        if pattern is MAP_STRUCTURE[1]:
            return f"#{m[1]}"
        return m[1]
    return None


def file_map(lines: list[str], markdown: bool = False) -> str:
    """
    Numbered outline of a big file, at most MAP_CHARS: definitions
    (functions, classes, routes) first, then sections and ids.
    """
    tiers = [(MARKDOWN_HEADING,)] if markdown else [MAP_DEFINITIONS, MAP_STRUCTURE]
    chosen, size, full = {}, 0, False
    for patterns in tiers:
        for number, line in enumerate(lines, 1):
            if number in chosen:
                continue
            label = map_label(line, patterns)
            if label is None:
                continue
            entry = f"{number} {label}"
            if size + len(entry) + 1 > MAP_CHARS - 2:
                full = True
                break
            chosen[number] = entry
            size += len(entry) + 1
        if full:
            break
    return "\n".join(chosen[n] for n in sorted(chosen)) + ("\n…" if full else "")


def focus_spans(lines: list[str], focus: list[str]) -> list[tuple[int, int]]:
    """0-based [start, end) windows of FOCUS_LINES around the first hits of each term."""
    spans = []
    for term in focus:
        needle = term.casefold()
        hits = [i for i, line in enumerate(lines) if needle in line.casefold()][:FOCUS_HITS]
        spans += [(max(i - FOCUS_LINES, 0), min(i + FOCUS_LINES + 1, len(lines))) for i in hits]
    return spans


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def excerpt(body: str, limit: int, focus: list[str], needs: list[tuple[int, int]],
            markdown: bool = False) -> str:
    """A big file as its map plus the asked ranges and focus windows, within limit."""
    lines = body.split("\n")
    outline = file_map(lines, markdown)
    head = f"(büyük dosya: {len(lines)} satır; yalnız harita ve aralıklar, metin birebir)\n"
    if outline:
        head += f"### harita (satır ad)\n{outline}\n"
    # Asked ranges first, then focus windows; the start of the file if nothing else.
    asked = merge_spans([(s - 1, min(e, len(lines))) for s, e in needs])
    windows = [w for w in merge_spans(focus_spans(lines, focus))
               if not any(w[0] < e and s < w[1] for s, e in asked)]
    spans = asked + windows or [(0, len(lines))]
    parts, room = [], limit - len(head)
    for start, end in spans:
        # Whole lines only: a cut line would not match a SEARCH block.
        label = f"### satır {start + 1}-{end}\n"
        kept, size = [], len(label) + 10
        for line in lines[start:end]:
            if size + len(line) + 1 > room:
                break
            kept.append(line)
            size += len(line) + 1
        if not kept:
            continue
        end = start + len(kept)
        block = f"### satır {start + 1}-{end}\n" + "\n".join(kept)
        parts.append(block)
        room -= len(block) + 2
        if room <= 100:
            break
    return head + "\n\n".join(parts)


def file_inputs(root: Path, paths: list[str], focus: list[str] | None = None,
                needs: list[dict] | None = None) -> str:
    """
    The named files of the copy. A file within its share comes whole; a
    bigger one as a map plus the asked ranges and focus windows (M-2).
    Share: FILE_INPUT_CHARS each, TOTAL_INPUT_CHARS together.
    """
    if not paths:
        return ""
    share = min(FILE_INPUT_CHARS, TOTAL_INPUT_CHARS // len(paths))
    sections = []
    for path in paths:
        try:
            target = inside(root, path)
            body = target.read_text(encoding="utf-8") if target.is_file() else "(dosya yok; yeni dosya olabilir)"
        except (CodeError, OSError, UnicodeDecodeError) as exc:
            body = f"(okunamadı: {exc})"
        head = f"## {path}\n"
        room = max(share - len(head), 0)
        if len(body) > room:
            asked = [(n["start"], n["end"]) for n in needs or [] if n["path"] == path]
            body = excerpt(body, room, focus or [], asked, path.endswith(".md"))
        sections.append(head + body)
    return FILES_HEADING + "\n\n" + "\n\n".join(sections)


def feedback(test: dict | None, rejected: list[dict]) -> str:
    """What the next round gets: rejected blocks and the end of the failed test output."""
    parts = []
    if rejected:
        lines = [f"- {r['path']}: {r['error']}" for r in rejected]
        parts.append(REJECTED_HEADING + "\n" + "\n".join(lines))
    if test and not test.get("passed"):
        output = (test.get("output") or "")[-TEST_OUTPUT_CHARS:]
        state = "zaman aşımı" if test.get("timed_out") else f"çıkış kodu {test.get('exit_code')}"
        parts.append(f"{TEST_HEADING}\nTestler geçmedi ({state}). Çıktının sonu:\n{output}")
    return "\n\n".join(parts)


def repo_files(root: Path) -> list[str]:
    """Text files of the copy (relative paths), at most MAX_LISTED."""
    files = sorted(
        str(p.relative_to(root)) for p in root.rglob("*")
        if p.is_file() and not p.is_symlink() and not p.name.lower().endswith(LIST_SKIP)
        and "__pycache__" not in p.parts
    )
    return files[:MAX_LISTED]


def missing_files(root: Path, paths: list[str]) -> list[str]:
    """M-3b: named files that are not in the copy (Ares must not guess paths)."""
    missing = []
    for path in paths:
        try:
            if not inside(root, path).is_file():
                missing.append(path)
        except CodeError:
            missing.append(path)
    return missing


REVIEW_CODE_RULES = (
    "Ayrıca kontrol et: değişiklik görevi gerçekten yapıyor mu, doğru dosyada mı "
    "ve projenin dilinde/yapısında mı (dosya listesine bak)? İlgisiz, yeni uydurulmuş "
    "ya da projeye uymayan bir dosya varsa ya da testler geçmediyse \"revise\"."
)


def diff_input(diff: dict, test: dict | None, files: list[str] | None = None) -> str:
    """The reviewer's input: the patch, the test state (I-4) and the copy's files (M-3b)."""
    if test is None:
        state = "Testler koşulmadı."
    elif test.get("status") != "ok":
        state = "Testler koşulamadı."
    else:
        state = "Testler geçti." if test.get("passed") else "Testler geçmedi."
    changed = ", ".join(diff.get("files") or [])
    repo = files or []
    body = clip_text(diff.get("diff") or "", DIFF_INPUT_CHARS, boundaries=("\n",))
    listing = ""
    if repo:
        listing = "\n\nProjedeki dosyalar: " + ", ".join(repo) + "\n" + REVIEW_CODE_RULES
    return f"{DIFF_HEADING}\nDosyalar: {changed}\n{state}\n\n{body}{listing}"


def review_findings(review: dict) -> str:
    """A revise verdict as notes for the fix round."""
    report = review.get("report") or {}
    lines = [str(report.get("summary") or "").strip()]
    lines += [f"- {finding}" for finding in report.get("findings") or []]
    return REVIEW_HEADING + "\n" + "\n".join(line for line in lines if line)
