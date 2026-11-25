"""
Remotive public API (plan-j karar A). Terms: link back to the Remotive
URL and name Remotive as the source; at most a few requests a day
(we make one); jobs are 24 h delayed. https://remotive.com/api-documentation
"""

from __future__ import annotations

import lib
from jobs.text import to_text

URL = "https://remotive.com/api/remote-jobs"


def to_job(item: dict) -> dict:
    return {
        "source": "remotive",
        "external_id": str(item["id"]),
        "url": item["url"],
        "title": item.get("title") or "",
        "company": item.get("company_name") or "",
        "location": item.get("candidate_required_location") or "",
        "remote": True,
        "language": "en",
        "description": to_text(item.get("description") or "", 19000),
        "posted_at": item.get("publication_date"),
        "job_type": item.get("job_type"),
    }


def fetch() -> list[dict]:
    data = lib.request("GET", URL)
    return [to_job(item) for item in data.get("jobs", []) if item.get("id") and str(item.get("url", "")).startswith("https://")]
