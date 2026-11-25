"""
Job alert e-mails -> listings (plan-j karar A, I).

Each parser finds the listing links of its site, takes the link text as
the title and the next plain lines as company and location. Alerts carry
no full description: the user pastes it for a package (karar I).
The formats are guessed until real alerts are seen: run
collect_jobs.py --dump-mail and refine against the saved samples.
"""

from __future__ import annotations

import re

from jobs.text import clip, tokens

LINKEDIN = re.compile(r"^https://(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(?P<id>\d{6,})")
KARIYER = re.compile(r"^https://(?:www\.)?kariyer\.net/is-ilani/(?P<slug>[a-z0-9-]*?-?(?P<id>\d{5,}))(?:[/?#]|$)")
NOT_TITLES = {"view job", "apply", "easy apply", "see all jobs", "ilanı gör", "başvur", "hemen başvur", "detaylar",
              "ilana git", "incele", "view", "jobs", "unsubscribe"}


def sender_site(address: str) -> str | None:
    domain = (address or "").rsplit("@", 1)[-1].lower()
    if domain == "linkedin.com" or domain.endswith(".linkedin.com"):
        return "linkedin"
    if domain == "kariyer.net" or domain.endswith(".kariyer.net"):
        return "kariyer"
    return None


def listings(markup: str, pattern: re.Pattern, canonical) -> list[dict]:
    items = tokens(markup)
    found: dict[str, dict] = {}
    for i, token in enumerate(items):
        if token[0] != "link":
            continue
        match = pattern.match(token[1])
        if not match:
            continue
        job_id = match.group("id")
        job = found.setdefault(job_id, {"external_id": job_id, "url": canonical(match), "title": "", "after": []})
        text = token[2]
        if not job["title"] and text and text.lower() not in NOT_TITLES and len(text) > 2:
            job["title"] = text
            # The plain lines right after the title: company, location.
            for later in items[i + 1:i + 8]:
                if later[0] == "link" and pattern.match(later[1]) and later[2]:
                    break
                if later[0] == "text":
                    job["after"].append(later[1])
                if len(job["after"]) >= 3:
                    break
    out = []
    for job in found.values():
        if not job["title"]:
            continue
        company, location = company_location(job.pop("after"))
        out.append({**job, "title": clip(job["title"], 300), "company": clip(company, 200),
                    "location": clip(location, 200)})
    return out


def company_location(lines: list[str]) -> tuple[str, str]:
    lines = [line for line in lines if line.strip(" ·|-")]
    if not lines:
        return "", ""
    for separator in (" · ", " | ", " - "):
        if separator in lines[0]:
            company, location = lines[0].split(separator, 1)
            return company.strip(), location.strip()
    return lines[0], lines[1] if len(lines) > 1 else ""


def parse(message: dict) -> list[dict]:
    """A Graph message ({from, subject, body{content}}) -> listings with source."""
    address = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "")
    site = sender_site(address)
    markup = (message.get("body") or {}).get("content", "")
    if site == "linkedin":
        jobs = listings(markup, LINKEDIN, lambda m: f"https://www.linkedin.com/jobs/view/{m['id']}/")
    elif site == "kariyer":
        jobs = listings(markup, KARIYER, lambda m: f"https://www.kariyer.net/is-ilani/{m['slug']}")
    else:
        return []
    remote_words = ("remote", "uzaktan")
    return [{**job, "source": site, "posted_at": message.get("receivedDateTime"),
             "remote": any(w in (job["location"] + " " + job["title"]).lower() for w in remote_words)}
            for job in jobs]
