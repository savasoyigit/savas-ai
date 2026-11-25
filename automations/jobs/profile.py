"""
The rule filter (plan-j karar D). Rules come from the Obsidian note
Kariyer/Profil.md (read only from the host), else from the template next
to this file. A listing gets status "new" or "filtered" with a reason.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

TEMPLATE = Path(__file__).with_name("Profil.md")
VAULT = Path(os.getenv("SAVAS_VAULT", Path.home() / "Obsidian" / "SavasBrain"))
NOTE = "Kariyer/Profil.md"
BLOCK = re.compile(r"```ayarlar\n(.*?)```", re.S)
LISTS = ("roller", "dislanan", "kara_liste", "remote_bolgeler")
# J-4: a new listing scored at least this much is announced (Profil.md: bildirim_esigi).
DEFAULT_THRESHOLD = 70
EXPERIENCE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:years?|yrs|yıl|yil)\b[^.\n]{0,40}?(?:experience|deneyim|tecrübe)"
    r"|(?:experience|deneyim|tecrübe)[^.\n]{0,40}?(\d{1,2})\s*\+?\s*(?:years?|yrs|yıl|yil)\b"
    r"|en az\s*(\d{1,2})\s*(?:yıl|yil)",
    re.I,
)


def norm(text: str) -> str:
    # i/ı/İ/I fold to one letter on both sides: "IT", "İş" and "iş" all match.
    text = (text or "").replace("İ", "i").replace("I", "i").casefold().replace("ı", "i")
    return " ".join(text.split())


def parse_profile(text: str) -> dict:
    match = BLOCK.search(text or "")
    if not match:
        raise ValueError("Profil notunda ```ayarlar bloğu yok")
    rules = {name: [] for name in LISTS}
    rules["en_fazla_deneyim_yili"] = None
    rules["bildirim_esigi"] = DEFAULT_THRESHOLD
    rules["tercihler"] = ""
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep:
            continue
        if key in LISTS:
            rules[key] = [norm(v) for v in value.split(",") if v.strip()]
        elif key in ("en_fazla_deneyim_yili", "bildirim_esigi") and value.strip():
            rules[key] = int(value.strip())
        elif key == "tercihler":
            rules[key] = value.strip()
    if not rules["roller"]:
        raise ValueError("Profil notunda roller boş")
    return rules


def load_profile() -> tuple[dict, str]:
    """(rules, where they came from). A broken note falls back to the template."""
    note = VAULT / NOTE
    if note.is_file():
        try:
            return parse_profile(note.read_text(encoding="utf-8")), NOTE
        except ValueError:
            pass
    return parse_profile(TEMPLATE.read_text(encoding="utf-8")), "şablon"


def preferences(rules: dict) -> str:
    """The candidate's wishes for the scorer, from the same rules."""
    lines = [f"Target roles: {', '.join(rules['roller'][:25])}",
             "Location: Turkey (on-site/hybrid) or full-time remote open to: " + ", ".join(rules["remote_bolgeler"]),
             "Languages: Turkish or English"]
    if rules.get("tercihler"):
        lines.append(f"Other: {rules['tercihler']}")
    return "\n".join(lines)


def contains(text: str, words: list[str]) -> str | None:
    """The first word found as a whole word (or phrase) in text."""
    for word in words:
        if re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", text):
            return word
    return None


def experience_years(description: str) -> int | None:
    years = [int(next(g for g in m.groups() if g)) for m in EXPERIENCE.finditer(description or "")]
    return max(years) if years else None


def judge(job: dict, rules: dict) -> tuple[str, str | None]:
    """("new", None) or ("filtered", reason)."""
    title = norm(job.get("title"))
    if not contains(title, rules["roller"]):
        return "filtered", "rol eşleşmedi"
    if word := contains(title, rules["dislanan"]):
        return "filtered", f"dışlanan: {word}"
    if rules["kara_liste"] and (word := contains(norm(job.get("company")), rules["kara_liste"])):
        return "filtered", f"kara liste: {word}"
    if job.get("source") == "remotive":
        if job.get("job_type") != "full_time":
            return "filtered", f"tam zamanlı değil ({job.get('job_type')})"
        if not contains(norm(job.get("location")), rules["remote_bolgeler"]):
            return "filtered", f"konum: {job.get('location') or '?'}"
    limit = rules.get("en_fazla_deneyim_yili")
    years = experience_years(job.get("description", ""))
    if limit is not None and years is not None and years > limit:
        return "filtered", f"deneyim {years}+ yıl"
    return "new", None
