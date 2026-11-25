"""
The tailored CV as data (J-5): the model fills the fields, the code lays
them out in the user's own CV design (Kariyer/*.pdf: photo top right,
teal headings with a rule, company names in the accent colour, dates in
grey italics). The same data gives the Markdown (panel, Obsidian note,
Gordon's check), the Word file (docx_export.cv_docx) and the PDF view
(ui/app.js printCv).
"""

from __future__ import annotations

import json

SECTIONS = ("summary", "projects", "experience", "skills", "education", "references")
HEADINGS = {
    "tr": {"summary": "ÖZET", "projects": "PROJELER", "experience": "DENEYİM", "skills": "YETENEKLER",
           "education": "EĞİTİM VE SERTİFİKALAR", "references": "REFERANSLAR", "languages": "Diller"},
    "en": {"summary": "SUMMARY", "projects": "PROJECTS", "experience": "EXPERIENCE", "skills": "SKILLS",
           "education": "EDUCATION & CERTIFICATIONS", "references": "REFERENCES", "languages": "Languages"},
}

SCHEMA = """{
 "language": "tr" or "en",
 "name": "the name exactly as in the master CV",
 "headline": "the line under the name (role | key technologies)",
 "contact": [["place", "e-mail"], ["linkedin", "github"]],
 "order": ["summary", "projects", "experience", "skills", "education", "references"],
 "summary": "3-5 sentences",
 "projects": [{"name": "", "description": "one sentence", "bullets": [""]}],
 "experience": [{"title": "", "company": "", "dates": "", "location": "", "bullets": [""]}],
 "skills": [{"label": "", "text": ""}],
 "languages": "",
 "education": [{"title": "", "org": "", "dates": ""}],
 "references": ""
}"""

TEXT = 600
BULLETS = 8


class CvError(ValueError):
    pass


def clean(value, limit: int = TEXT) -> str:
    text = " ".join(str(value or "").split())
    return text.replace("**", "")[:limit]


def items(value, limit: int = 12) -> list:
    return [x for x in (value if isinstance(value, list) else [])][:limit]


def parse_cv(reply: str) -> dict:
    """The model's JSON -> a checked CV; CvError when it is not usable."""
    text = (reply or "").strip()
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1]) if 0 <= start < end else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise CvError("CV JSON okunamadı")

    cv = {
        "language": "en" if data.get("language") == "en" else "tr",
        "name": clean(data.get("name"), 80),
        "headline": clean(data.get("headline"), 160),
        "contact": [[clean(x, 80) for x in items(line, 4) if clean(x)] for line in items(data.get("contact"), 3)
                    if isinstance(line, list)],
        "summary": clean(data.get("summary"), 1200),
        "projects": [{"name": clean(p.get("name"), 80), "description": clean(p.get("description")),
                      "bullets": [clean(b) for b in items(p.get("bullets"), BULLETS) if clean(b)]}
                     for p in items(data.get("projects"), 5) if isinstance(p, dict) and clean(p.get("name"))],
        "experience": [{"title": clean(e.get("title"), 100), "company": clean(e.get("company"), 120),
                        "dates": clean(e.get("dates"), 60), "location": clean(e.get("location"), 80),
                        "bullets": [clean(b) for b in items(e.get("bullets"), BULLETS) if clean(b)]}
                       for e in items(data.get("experience"), 8) if isinstance(e, dict) and clean(e.get("title"))],
        "skills": [{"label": clean(s.get("label"), 60), "text": clean(s.get("text"))}
                   for s in items(data.get("skills"), 8) if isinstance(s, dict) and clean(s.get("text"))],
        "languages": clean(data.get("languages"), 200),
        "education": [{"title": clean(e.get("title"), 120), "org": clean(e.get("org"), 120), "dates": clean(e.get("dates"), 60)}
                      for e in items(data.get("education"), 8) if isinstance(e, dict) and clean(e.get("title"))],
        "references": clean(data.get("references"), 200),
    }
    order = [s for s in items(data.get("order"), 10) if s in SECTIONS]
    cv["order"] = list(dict.fromkeys(order + [s for s in SECTIONS if s not in order]))
    if not cv["name"] or not cv["experience"]:
        raise CvError("CV JSON'da ad ya da deneyim yok")
    # The contact lines are often given flat: ["a", "b", "c", "d"].
    if not cv["contact"] and isinstance(data.get("contact"), list):
        flat = [clean(x, 80) for x in data["contact"] if isinstance(x, str) and clean(x)][:6]
        cv["contact"] = [flat[i:i + 2] for i in range(0, len(flat), 2)]
    return cv


def headings(cv: dict) -> dict:
    return HEADINGS[cv.get("language", "tr")]


def has(cv: dict, section: str) -> bool:
    return bool(cv.get(section)) or (section == "skills" and bool(cv.get("languages")))


def cv_markdown(cv: dict) -> str:
    """The CV as text: what the panel shows, the note keeps and Gordon checks."""
    names = headings(cv)
    lines = [f"# {cv['name']}", f"**{cv['headline']}**" if cv.get("headline") else ""]
    lines += [" • ".join(line) for line in cv.get("contact", [])]
    for section in cv["order"]:
        if not has(cv, section):
            continue
        lines += ["", f"## {names[section]}"]
        if section in ("summary", "references"):
            lines.append(cv[section])
        elif section == "projects":
            for p in cv["projects"]:
                lines.append(f"**{p['name']}**" + (f" — {p['description']}" if p["description"] else ""))
                lines += [f"- {b}" for b in p["bullets"]]
        elif section == "experience":
            for e in cv["experience"]:
                lines.append(f"**{e['title']}**" + (f" — **{e['company']}**" if e["company"] else ""))
                when = " | ".join(x for x in (e["dates"], e["location"]) if x)
                if when:
                    lines.append(f"*{when}*")
                lines += [f"- {b}" for b in e["bullets"]]
        elif section == "skills":
            lines += [f"{s['label']}: {s['text']}" if s["label"] else s["text"] for s in cv["skills"]]
            if cv.get("languages"):
                lines.append(f"**{names['languages']}:** {cv['languages']}")
        elif section == "education":
            lines += [f"**{e['title']}**" + (f" — **{e['org']}**" if e["org"] else "") + (f" *({e['dates']})*" if e["dates"] else "")
                      for e in cv["education"]]
    # Markdown wants a blank line or two spaces to keep single lines apart.
    return "\n".join(line + ("  " if line and not line.startswith(("#", "- ")) else "") for line in lines).strip() + "\n"
