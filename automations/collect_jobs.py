#!/usr/bin/env python3
"""
Job collection (J-3, plan-j karar A-D, F). No LLM.

  collect_jobs.py               alerts (Hotmail -> Gmail) + Remotive -> filter -> POST /jobs/import -> push
  collect_jobs.py --dry-run     same, but import nothing and push nothing; print what would be imported
  collect_jobs.py --dump-mail   also save the alert mails (data/automations/mail-samples/) to refine parsers
  collect_jobs.py --propose-profile   propose Kariyer/Profil.md (the filter rules) for approval
  collect_jobs.py --propose-cv        propose Kariyer/CV.md (text of the newest PDF in ~/AI/Kariyer) for approval
  collect_jobs.py --score-only        no collecting: score waiting listings and announce the good ones
  collect_jobs.py --rescore           no collecting: score every new listing again (after a prompt or CV change); no push

After an import Oracle scores the new listings against Kariyer/CV.md (J-4,
POST /jobs/score); only those at or above bildirim_esigi are announced.

Remotive is asked once a day (its terms: a few requests a day at most).
"""

import json
import sys

import lib
from jobs import cv, mail, parsers, profile, remotive

NAME = "collect-jobs"
IMPORT_CHUNK = 200
SAMPLES = 20
SCORE_JOBS = 30
SCORE_TIMEOUT = 600  # up to 3 batches; a local fallback answers slowly


def from_mail(state: dict, dump: bool) -> tuple[list[dict], str]:
    """Listings from alert mails (Gmail, redirected from Hotmail) since the last look; (jobs, note)."""
    try:
        messages = mail.fetch_new(state)
    except mail.MailNotConfigured as exc:
        return [], f"e-posta kapalı ({exc})"

    jobs, alerts = [], 0
    for message in messages:
        found = parsers.parse(message)
        site = parsers.sender_site(((message.get("from") or {}).get("emailAddress") or {}).get("address", ""))
        if site:
            alerts += 1
            if dump:
                save_sample(message, site)
        jobs += found
    return jobs, f"e-posta: {len(messages)} mesaj, {alerts} uyarı, {len(jobs)} ilan"


def save_sample(message: dict, site: str):
    """The alert as Graph gave it, to write parsers against (local only, data/)."""
    folder = lib.STATE_DIR / "mail-samples"
    folder.mkdir(parents=True, exist_ok=True)
    existing = sorted(folder.glob("*.json"))
    for old in existing[: max(0, len(existing) - SAMPLES + 1)]:
        old.unlink()
    stamp = message["receivedDateTime"].replace(":", "")
    (folder / f"{stamp}-{site}.json").write_text(json.dumps(message, ensure_ascii=False, indent=1), encoding="utf-8")


def from_remotive(state: dict) -> tuple[list[dict], str]:
    today = lib.now_local().date().isoformat()
    if state.get("remotive_day") == today:
        return [], "Remotive: bugün alındı"
    jobs = remotive.fetch()
    state["remotive_day"] = today
    return jobs, f"Remotive: {len(jobs)} ilan"


def judged(jobs: list[dict], rules: dict) -> list[dict]:
    out = []
    for job in jobs:
        status, reason = profile.judge(job, rules)
        job = {k: v for k, v in job.items() if k != "job_type"}
        out.append({**job, "status": status, **({"filter_reason": reason} if reason else {})})
    return out


def import_all(jobs: list[dict]) -> dict:
    total = {"added": 0, "duplicates": 0, "new": 0}
    for start in range(0, len(jobs), IMPORT_CHUNK):
        answer = lib.request("POST", lib.ORCHESTRATOR_URL + "/jobs/import", {"jobs": jobs[start:start + IMPORT_CHUNK]})
        for key in total:
            total[key] += answer.get(key, 0)
    return total


def run(dry_run: bool, dump: bool):
    rules, rules_from = profile.load_profile()
    state = lib.State(NAME)
    data = state.load()

    notes, jobs = [f"kurallar: {rules_from}"], []
    for collect in (lambda: from_mail(data, dump), lambda: from_remotive(data)):
        try:
            found, note = collect()
        except lib.AutomationError as exc:
            # One source failing does not stop the other; the run still exits 1 at the end.
            found, note = [], f"HATA {exc}"
        jobs += found
        notes.append(note)

    jobs = judged(jobs, rules)
    fresh = [j for j in jobs if j["status"] == "new"]
    if dry_run:
        for job in jobs:
            print(f"  {job['status']:8} {job['source']:8} {job['title'][:60]} · {job['company'][:30]}"
                  f"{' — ' + job['filter_reason'] if job.get('filter_reason') else ''}")
        lib.log(NAME, f"dry run: {len(fresh)}/{len(jobs)} geçti · " + " · ".join(notes))
        return

    result = import_all(jobs) if jobs else {"added": 0, "duplicates": 0, "new": 0}
    state.save(data)
    lib.log(NAME, f"{result['new']} yeni, {result['added'] - result['new']} elenen, {result['duplicates']} bilinen · "
                  + " · ".join(notes))
    try:
        notes.append(score_and_announce(rules, result["new"]))
    except lib.AutomationError as exc:
        notes.append(f"HATA puanlama: {exc}")
    if any(note.startswith("HATA") for note in notes):
        raise lib.AutomationError("; ".join(n for n in notes if n.startswith("HATA")))


def score_and_announce(rules: dict, new_count: int) -> str:
    """Score what waits (this run's and earlier unscored), then push the good ones."""
    text = cv.load()
    if text is None:
        # No CV note yet: the old behaviour, every new listing announced unscored.
        if new_count:
            lib.notify("Yeni ilanlar", f"{new_count} yeni ilan (puansız: Kariyer/CV.md yok)", "/ui/#jobs", "new-jobs")
        return "puanlama kapalı (Kariyer/CV.md yok)"

    answer = lib.request("POST", lib.ORCHESTRATOR_URL + "/jobs/score",
                         {"cv": text, "preferences": profile.preferences(rules), "max_jobs": SCORE_JOBS},
                         timeout=SCORE_TIMEOUT)
    threshold = rules.get("bildirim_esigi", profile.DEFAULT_THRESHOLD)
    good = sorted((j for j in answer["scored"] if j["score"] >= threshold), key=lambda j: -j["score"])
    if good:
        best = good[0]
        body = f"{best['score']} · {best['title']} · {best['company']}"[:150]
        if len(good) > 1:
            body += f" (+{len(good) - 1})"
        lib.notify(f"{len(good)} uygun ilan (≥{threshold})", body, "/ui/#jobs", "new-jobs")
    note = f"puan: {len(answer['scored'])} ilan, {len(good)} ≥{threshold}, {answer['remaining']} bekliyor"
    if answer.get("blocked"):
        note += " (Oracle bütçesi doldu)"
    if answer.get("error"):
        raise lib.AutomationError(f"{note}; {answer['error']}")
    return note


def rescore():
    rules, _ = profile.load_profile()
    text = cv.load()
    if text is None:
        raise lib.AutomationError("Kariyer/CV.md yok")
    answer = lib.request("POST", lib.ORCHESTRATOR_URL + "/jobs/score",
                         {"cv": text, "preferences": profile.preferences(rules), "max_jobs": SCORE_JOBS, "rescore": True},
                         timeout=SCORE_TIMEOUT)
    threshold = rules.get("bildirim_esigi", profile.DEFAULT_THRESHOLD)
    good = sum(j["score"] >= threshold for j in answer["scored"])
    lib.log(NAME, f"yeniden puan: {len(answer['scored'])} ilan, {good} ≥{threshold}"
                  + (f"; HATA {answer['error']}" if answer.get("error") else ""))


def score_only():
    rules, _ = profile.load_profile()
    lib.log(NAME, score_and_announce(rules, 0))


def propose_cv():
    pdf = cv.newest_pdf()
    text = cv.pdf_text(pdf)
    if len(text) < 200:
        raise lib.AutomationError(f"{pdf.name}: metin çıkmadı (taranmış PDF olabilir)")
    content = cv.note_text(pdf, text, lib.now_local().date().isoformat())
    answer = lib.propose_note(cv.NOTE, content, f"CV metni ({pdf.name})")
    lib.log(NAME, f"CV önerildi ({len(text)} karakter): {answer.get('status')} — telefondan onayla")


def propose_profile():
    text = profile.TEMPLATE.read_text(encoding="utf-8")
    answer = lib.propose_note(profile.NOTE, text, "İlan süzgeci kuralları (J-3)")
    lib.log(NAME, f"Profil önerildi: {answer.get('status')} — telefondan onayla")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--propose-profile" in args:
        lib.main(NAME + "-profile", propose_profile)
    elif "--propose-cv" in args:
        lib.main(NAME + "-cv", propose_cv)
    elif "--rescore" in args:
        lib.main(NAME, rescore)
    elif "--score-only" in args:
        lib.main(NAME, score_only)
    else:
        lib.main(NAME, lambda: run("--dry-run" in args, "--dump-mail" in args))
