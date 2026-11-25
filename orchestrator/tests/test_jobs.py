"""J-3: job listings in the store and the /jobs endpoints (plan-j karar C)."""

import asyncio
import json

import pytest

from task_store import StoreError, TaskStore


def job(n, **more):
    return {"source": "linkedin", "external_id": str(n), "url": f"https://www.linkedin.com/jobs/view/{n}/",
            "title": f"Backend Developer {n}", "company": "Acme", "status": "new", **more}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


@pytest.fixture
def app(db, orch):
    yield orch
    orch.task_store.close()


def imported(app, jobs):
    return asyncio.run(app.import_jobs(app.JobImport(jobs=jobs)))


def test_store_skips_known_listings(tmp_path):
    store = TaskStore(tmp_path / "s.db")
    first = store.import_jobs([{**job(1), "job_id": "a"}, {**job(2), "job_id": "b", "status": "filtered", "filter_reason": "x"}])
    assert first == {"added": 2, "duplicates": 0, "job_ids": ["a", "b"]}
    again = store.import_jobs([{**job(1), "job_id": "c"},  # same source + id
                               {**job(9), "job_id": "d", "source": "kariyer", "url": job(2)["url"]},  # same url
                               {**job(3), "job_id": "e", "remote": 1}])
    assert again == {"added": 1, "duplicates": 2, "job_ids": ["e"]}
    assert store.get_job("e")["remote"] is True and store.get_job("a")["remote"] is False
    assert [j["job_id"] for j in store.list_jobs()] == ["e", "b", "a"]
    assert [j["job_id"] for j in store.list_jobs("filtered")] == ["b"]
    assert store.count_jobs() == {"new": 2, "filtered": 1}
    with pytest.raises(StoreError):
        store.import_jobs([{**job(4), "job_id": "f", "status": "applied"}])
    store.close()


def test_import_endpoint_counts_new_listings(app):
    answer = imported(app, [job(1), job(2, status="filtered", filter_reason="rol eşleşmedi"), job(3)])
    assert answer["added"] == 3 and answer["new"] == 2
    assert imported(app, [job(1)]) == {"added": 0, "duplicates": 1, "job_ids": [], "new": 0}
    [event] = [e for e in app.ACTIVITY if e["event"] == "jobs"]
    assert event["added"] == 3 and event["new"] == 2


def test_list_leaves_out_the_description_and_get_has_it(app):
    imported(app, [job(1, description="uzun metin")])
    listed = asyncio.run(app.list_jobs())
    assert listed["count"] == 1 and listed["counts"] == {"new": 1} and "description" not in listed["jobs"][0]
    one = asyncio.run(app.get_job(listed["jobs"][0]["job_id"]))
    assert one["description"] == "uzun metin"
    with pytest.raises(app.HTTPException) as caught:
        asyncio.run(app.get_job("yok"))
    assert caught.value.status_code == 404
    with pytest.raises(app.HTTPException):
        asyncio.run(app.list_jobs(status="bilinmeyen"))


@pytest.mark.parametrize("bad", [
    "x", job(1, source="indeed"), job(1, status="applied"), job(1, url="javascript:alert(1)"),
    job(1, url="http://www.linkedin.com/jobs/view/1/"), job(1, title=""), job(1, title="x" * 301),
    job(1, external_id=None) | {"external_id": 5}, job(1, description="x" * 20001),
])
def test_bad_listings_are_refused(app, bad):
    with pytest.raises(app.HTTPException) as caught:
        imported(app, [bad])
    assert caught.value.status_code == 422
    assert asyncio.run(app.list_jobs())["count"] == 0


def test_an_import_is_limited(app):
    with pytest.raises(app.HTTPException):
        imported(app, [job(n) for n in range(201)])


def test_the_profile_note_may_be_proposed(app):
    answer = asyncio.run(app.propose_note(app.NoteProposal(path="Kariyer/Profil.md", content="# p\n", reason="r")))
    assert answer["status"] == "waiting_approval"


# ----------------------------------------------------------------------
# J-4 scoring
# ----------------------------------------------------------------------

import json  # noqa: E402


def answer(content, model="gh/gpt-4.1"):
    return 200, {"model": model, "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020}}


def scores(*items):
    return json.dumps([{"n": n, "score": s, "track": t, "reason": "gerekçe"} for n, s, t in items])


@pytest.fixture
def scoring(app, monkeypatch):
    monkeypatch.setattr(app, "OMNIROUTE_KEY", "test-key")
    sent = []

    def handler(url, payload):
        sent.append(payload)
        n = payload["messages"][1]["content"].count("\n   ")
        return answer("İşte puanlar:\n" + scores(*[(i, 50 + i, "dev") for i in range(1, n + 1)]))

    app.fake_client.handler = staticmethod(handler)
    return sent


def score(app, **more):
    return asyncio.run(app.score_jobs(app.ScoreRequest(**{"cv": "Python backend, 4 yıl", **more})))


def test_parse_scores_keeps_only_good_items(app):
    m = app
    reply = 'x [{"n":1,"score":72.4,"track":"dev","reason":" iyi  eşleşme "},{"n":2,"score":140,"track":"other"},' \
            '{"n":9,"score":50},{"n":3,"score":true},{"n":"4","score":10},"x"] y'
    assert m.parse_scores(reply, 3) == {1: {"score": 72, "track": "dev", "reason": "iyi eşleşme"},
                                        2: {"score": 100, "track": None, "reason": ""}}
    assert m.parse_scores("JSON yok", 3) == {} and m.parse_scores("[bozuk", 3) == {}


def test_new_listings_are_scored_in_batches_of_ten(app, scoring):
    imported(app, [job(n) for n in range(1, 13)] + [job(99, status="filtered", filter_reason="x")])
    result = score(app)
    assert len(result["scored"]) == 12 and result["remaining"] == 0 and not result["blocked"]
    assert len(scoring) == 2 and scoring[0]["max_tokens"] == app.SCORE_COMPLETION_TOKENS
    system, user = scoring[0]["messages"]
    assert "never assume" in system["content"] and user["content"].startswith("CANDIDATE CV:\nPython backend")
    assert "1. Backend Developer 1 | Acme | linkedin" in user["content"] and "(no description)" in user["content"]
    listed = {j["title"]: j for j in asyncio.run(app.list_jobs())["jobs"]}
    assert listed["Backend Developer 1"]["score"] == 51 and listed["Backend Developer 1"]["track"] == "dev"
    assert listed["Backend Developer 99"]["score"] is None  # filtered ones are not scored
    assert score(app)["scored"] == [] and len(scoring) == 2  # nothing left: no call
    tasks = [app.task_store.get_task(e["task_id"]) for e in app.ACTIVITY if e["event"] == "jobs" and e["status"] == "scored"]
    assert [t["kind"] for t in tasks] == ["score", "score"] and tasks[0]["result"]["scored"] == 10


def test_max_jobs_and_description_clip(app, scoring):
    imported(app, [job(1, description="kelime " * 1000), job(2), job(3)])
    result = score(app, max_jobs=1)
    assert len(result["scored"]) == 1 and result["remaining"] == 2
    assert len(scoring[0]["messages"][1]["content"]) < 2000


def test_budget_stops_scoring(app, scoring, monkeypatch):
    imported(app, [job(1)])

    async def refuse(**kwargs):
        raise app.BudgetExceeded("Daily budget exceeded for oracle")

    monkeypatch.setattr(app.budget_manager, "reserve", refuse)
    result = score(app)
    assert result == {"scored": [], "remaining": 1, "blocked": True, "error": None}
    assert any(e["event"] == "chat" and e["status"] == "blocked" and e["worker"] == "oracle" for e in app.ACTIVITY)


def test_an_answer_without_scores_stops_and_fails_the_task(app, monkeypatch):
    monkeypatch.setattr(app, "OMNIROUTE_KEY", "test-key")
    calls = []
    app.fake_client.handler = staticmethod(lambda url, payload: calls.append(1) or answer("Üzgünüm, yapamam."))
    imported(app, [job(n) for n in range(1, 25)])
    result = score(app)
    assert result["scored"] == [] and "no scores" in result["error"] and len(calls) == 1
    assert any(e["event"] == "task" and e["status"] == "failed" for e in app.ACTIVITY)  # -> gateway alert


@pytest.mark.parametrize("more", [{"cv": ""}, {"cv": "x" * 12001}, {"preferences": "x" * 3001}])
def test_bad_score_requests(app, more):
    with pytest.raises(app.HTTPException) as caught:
        score(app, **more)
    assert caught.value.status_code == 422


def test_one_scoring_at_a_time(app, scoring):
    async def go():
        async with app.SCORE_LOCK:
            await app.score_jobs(app.ScoreRequest(cv="x"))
    with pytest.raises(app.HTTPException) as caught:
        asyncio.run(go())
    assert caught.value.status_code == 409


def test_one_posting_in_several_alerts_is_kept_once(tmp_path):
    store = TaskStore(tmp_path / "s.db")
    result = store.import_jobs([
        {**job(1, title="Software Engineer - AI Tooling", company="Jobgether"), "job_id": "a"},
        {**job(2, title="software engineer - ai tooling ", company=" Jobgether"), "job_id": "b"},  # same posting, other country
        {**job(3, title="Software Engineer - AI Tooling", company="Başka"), "job_id": "c"},
        {**job(4, title="Software Engineer", company=""), "job_id": "d"},
        {**job(5, title="Software Engineer", company=""), "job_id": "e"},  # no company: not merged
    ])
    assert result["job_ids"] == ["a", "c", "d", "e"]
    store.close()


def test_rescore_scores_the_scored_ones_again(app, scoring):
    imported(app, [job(1), job(2)])
    assert len(score(app)["scored"]) == 2 and score(app)["scored"] == []
    assert len(score(app, rescore=True)["scored"]) == 2 and len(scoring) == 2


def test_the_prompt_caps_on_site_roles_abroad(app):
    assert "outside Turkey that is on-site or hybrid scores at most 40" in app.SCORE_PROMPT
    assert "at most 65" in app.SCORE_PROMPT


@pytest.mark.parametrize("location,description,score,expected", [
    ("Sofia, Bulgaria (Hybrid)", "", 68, 40), ("Lublin (Hybrid)", "", 65, 40), ("Mielec, Podkarpackie, Poland", "", 73, 65),
    ("Sofia, Sofia City, Bulgaria", "", 75, 65), ("Sofia, Sofia City, Bulgaria", "Remote-friendly team...", 75, 75),
    ("Poland (Remote)", "", 80, 80), ("Czechia", "", 30, 30), ("Istanbul, Türkiye (On-site)", "", 82, 82),
    ("Pendik (Hybrid)", "", 65, 65), ("Europe, USA, Canada", "", 70, 70), ("Prague, Czechia (On-site)", "x", 50, 40),
])
def test_location_caps(app, location, description, score, expected):
    assert app.cap_score({"location": location, "description": description}, score)[0] == expected


def test_a_capped_score_says_so(app, monkeypatch):
    monkeypatch.setattr(app, "OMNIROUTE_KEY", "test-key")
    app.fake_client.handler = staticmethod(lambda url, payload: answer(scores((1, 90, "dev"))))
    imported(app, [job(1, location="Sofia, Bulgaria (Hybrid)")])
    [scored] = score(app)["scored"]
    assert scored["score"] == 40 and scored["reason"].endswith("[üst sınır 40: yurt dışı hibrit/yerinde]")


# ----------------------------------------------------------------------
# J-5 application package
# ----------------------------------------------------------------------

MASTER = "# CV\n\nKaynak: `cv.pdf` (pdftotext, 2026-11-16). ...\n\n---\n\nAyşe Nur Doğan\nJava backend, 4 yıl\n"
CV_JSON = {
    "language": "tr", "name": "AYŞE NUR DOĞAN", "headline": "Java backend developer",
    "contact": [["Kadıköy, İstanbul", "a@b.c"], ["linkedin.com/in/x", "github.com/x"]],
    "summary": "Java backend, 4 yıl.", "projects": [{"name": "Business Insight API", "description": "BI", "bullets": ["JWT"]}],
    "experience": [{"title": "Yazılım Geliştirme Uzmanı", "company": "KurumSoft", "dates": "Ekim 2023 – Günümüz",
                    "location": "İstanbul", "bullets": ["SQL optimize etti"]}],
    "skills": [{"label": "Backend", "text": "Java, Spring"}], "languages": "Türkçe, İngilizce (C1)",
    "education": [{"title": "Lisans", "org": "İMÜ", "dates": "2017 – 2022"}], "references": "Talep üzerine sunulur.",
}
TAILORED = json.dumps(CV_JSON, ensure_ascii=False)
LETTER = "Sayın İşe Alım Ekibi, ..."


@pytest.fixture
def packaging(app, monkeypatch):
    monkeypatch.setattr(app, "OMNIROUTE_KEY", "test-key")
    calls, review = [], {"reply": '{"verdict": "ok", "unsupported": [], "notes": ["Yazım iyi"]}'}

    def handler(url, payload):
        system, user = payload["messages"][0]["content"], payload["messages"][1]["content"]
        calls.append((system[:30], user))
        if system.startswith("You tailor"):
            return answer("```json\n" + TAILORED + "\n```")
        if system.startswith("You write a short cover"):
            return answer(LETTER)
        replies = review.get("replies")
        return answer(replies.pop(0) if replies else review["reply"])

    app.fake_client.handler = staticmethod(handler)

    async def read_note(path, task_id):
        assert path == "Kariyer/CV.md"
        return {"path": path, "content": MASTER}

    monkeypatch.setattr(app, "read_note", read_note)
    written = []

    async def create_note(path, content, task_id):
        written.append((path, content))
        return {"status": "created"}

    monkeypatch.setattr(app, "create_note", create_note)
    return {"calls": calls, "review": review, "written": written}


def package(app, job_id, **body):
    async def go():
        started = await app.package_job(job_id, app.PackageRequest(**body))
        await asyncio.gather(*list(app.BACKGROUND_JOBS))
        return started
    return asyncio.run(go())


def one(app, **more):
    imported(app, [job(1, **more)])
    return asyncio.run(app.list_jobs())["jobs"][0]["job_id"]


def test_a_package_is_written_checked_and_kept(app, packaging):
    job_id = one(app)
    assert package(app, job_id, description="We need a Java backend developer.")["status"] == "running"
    shown = asyncio.run(app.get_job(job_id))
    assert shown["description"] == "We need a Java backend developer." and shown["status"] == "package_ready"
    pkg = shown["package"]
    assert pkg["status"] == "ready" and pkg["letter"] == LETTER
    assert pkg["cv_data"]["name"] == "AYŞE NUR DOĞAN" and pkg["cv_data"]["order"][0] == "summary"
    assert pkg["cv"].startswith("# AYŞE NUR DOĞAN") and "## DENEYİM" in pkg["cv"] and "- SQL optimize etti" in pkg["cv"]
    assert pkg["review"] == {"verdict": "ok", "unsupported": [], "missing": [], "notes": ["Yazım iyi"], "fixed": []}
    assert [s["step"] for s in pkg["steps"]] == ["cv", "letter", "review"]
    assert [s["worker"] for s in pkg["steps"]] == ["jarvis", "willy", "gordon"]
    tailor_system, tailor_user = packaging["calls"][0]
    assert tailor_user.startswith("MASTER CV:\nAyşe Nur Doğan") and "Kaynak:" not in tailor_user  # note header dropped
    assert "Write in: the language of the listing" in tailor_user and "We need a Java" in tailor_user
    assert "MASTER CV:" in packaging["calls"][2][1] and "TAILORED CV:\n# AYŞE NUR DOĞAN" in packaging["calls"][2][1]


def test_pm_listings_go_to_gandalf_and_the_letter_is_optional(app, packaging):
    job_id = one(app, title="IT Project Manager")
    package(app, job_id, description="PM role", letter=False, language="en")
    pkg = asyncio.run(app.get_job(job_id))["package"]
    assert [s["worker"] for s in pkg["steps"]] == ["gandalf", "gordon"] and pkg["letter"] is None
    assert "Write in: English" in packaging["calls"][0][1] and "COVER LETTER:\n(none)" in packaging["calls"][1][1]


def test_unsupported_claims_mark_the_package_for_fixing(app, packaging):
    packaging["review"]["reply"] = '{"verdict": "ok", "unsupported": ["\\"SQL optimize etti\\" abartılı"], "notes": []}'
    job_id = one(app)
    package(app, job_id, description="x")
    pkg = asyncio.run(app.get_job(job_id))["package"]
    assert pkg["status"] == "needs_fix" and pkg["review"]["verdict"] == "fix"


def test_an_unsupported_item_must_quote_the_package(app, packaging):
    # L-2a: listing gaps filed as "unsupported" quote nothing in the package; they become "missing".
    packaging["review"]["reply"] = json.dumps({"verdict": "fix", "unsupported": [
        "No React experience in the master CV, but the job requires it.",
        "Minimum 4 years — CV shows 'about one year of work'.",
    ], "missing": ["Node.js"], "notes": []})
    job_id = one(app)
    package(app, job_id, description="x")
    pkg = asyncio.run(app.get_job(job_id))["package"]
    assert pkg["status"] == "ready" and pkg["review"]["verdict"] == "ok" and pkg["review"]["unsupported"] == []
    assert pkg["review"]["missing"][0] == "Node.js" and len(pkg["review"]["missing"]) == 3 and pkg["review"]["moved"] == 2
    assert len(pkg["steps"]) == 3 and pkg["steps"][-1]["review"]["moved"] == 2


def test_quotes_are_matched_loosely_but_must_exist(app):
    app_module = app
    package_text = "# CV\n- **SQL** optimize etti, raporları hızlandırdı.\nSayın İşe Alım Ekibi"
    assert app_module.quotes_package('COVER LETTER: “sql optimize etti,” — not in CV', package_text)
    assert app_module.quotes_package("Claim of 'Sayın İşe Alım Ekibi' wording", package_text)
    assert not app_module.quotes_package("candidate's main tools are 'over three years of Java'", package_text)
    assert not app_module.quotes_package('"SQL"', package_text)  # too short to count as a quote


def test_gordons_findings_go_back_to_the_writers_until_he_is_satisfied(app, packaging):
    packaging["review"]["replies"] = ['{"verdict": "fix", "unsupported": ["\\"SQL optimize etti\\" Java kurumsal deneyim"], '
                                      '"missing": ["Kafka"], "notes": []}']
    job_id = one(app)
    package(app, job_id, description="x")
    pkg = asyncio.run(app.get_job(job_id))["package"]
    assert pkg["status"] == "ready" and pkg["review"]["fixed"] == ['"SQL optimize etti" Java kurumsal deneyim']
    assert [s["step"] for s in pkg["steps"]] == ["cv", "letter", "review", "cv-2", "letter-2", "review-2"]
    assert pkg["steps"][2]["review"]["verdict"] == "fix" and pkg["steps"][5]["review"]["verdict"] == "ok"
    revise = packaging["calls"][3][1]
    assert "YOUR PREVIOUS ANSWER:" in revise and "Java kurumsal deneyim" in revise and "Kafka" not in revise


def test_a_package_gordon_still_rejects_is_not_ready(app, packaging):
    packaging["review"]["reply"] = '{"verdict": "fix", "unsupported": ["\\"Sayın İşe Alım Ekibi\\" büyük ölçekli sistem"], "notes": []}'
    job_id = one(app)
    package(app, job_id, description="x")
    pkg = asyncio.run(app.get_job(job_id))["package"]
    assert pkg["status"] == "needs_fix" and pkg["review"]["unsupported"] == ['"Sayın İşe Alım Ekibi" büyük ölçekli sistem']
    assert len(pkg["steps"]) == 3 * (app.PACKAGE_REVISIONS + 1) and pkg["steps"][-1]["step"] == "review-3"


def test_listing_gaps_are_information_not_errors(app, packaging):
    packaging["review"]["reply"] = '{"verdict": "ok", "unsupported": [], "missing": ["4 yıl", "AWS"], "notes": []}'
    job_id = one(app)
    package(app, job_id, description="x")
    pkg = asyncio.run(app.get_job(job_id))["package"]
    assert pkg["status"] == "ready" and pkg["review"]["missing"] == ["4 yıl", "AWS"] and len(pkg["steps"]) == 3


def test_an_unreadable_review_is_never_ok(app, packaging):
    packaging["review"]["reply"] = "Her şey yolunda görünüyor."
    job_id = one(app)
    package(app, job_id, description="x")
    pkg = asyncio.run(app.get_job(job_id))["package"]
    assert pkg["status"] == "needs_fix" and pkg["review"]["verdict"] == "unknown"


def test_a_listing_without_text_asks_for_it(app, packaging):
    job_id = one(app)
    with pytest.raises(app.HTTPException) as caught:
        package(app, job_id)
    assert caught.value.status_code == 422 and "İlan metni" in caught.value.detail
    assert packaging["calls"] == []
    for bad in ({"description": "x", "language": "de"}, {"description": "x" * 20001}):
        with pytest.raises(app.HTTPException):
            package(app, job_id, **bad)


def test_budget_or_model_failure_fails_the_package(app, packaging, monkeypatch):
    job_id = one(app, description="x")

    async def refuse(**kwargs):
        raise app.BudgetExceeded("Daily budget exceeded for jarvis")

    monkeypatch.setattr(app.budget_manager, "reserve", refuse)
    package(app, job_id)
    pkg = asyncio.run(app.get_job(job_id))["package"]
    assert pkg["status"] == "failed" and pkg["error"].startswith("budget:")
    assert asyncio.run(app.get_job(job_id))["status"] == "new"


def test_save_proposes_notes_for_approval(app, packaging):
    job_id = one(app, title="Backend / Java Developer", company="Migros One")
    with pytest.raises(app.HTTPException):
        asyncio.run(app.save_package(job_id))  # no package yet
    package(app, job_id, description="x")
    saved = asyncio.run(app.save_package(job_id))
    paths = [n["path"] for n in saved["notes"]]
    day = app.utc_now()[:10]
    assert paths == [f"Kariyer/Başvurular/{day} Migros One - Backend - Java Developer/CV.md",
                     f"Kariyer/Başvurular/{day} Migros One - Backend - Java Developer/Ön yazı.md"]
    assert all(n["status"] == "waiting_approval" for n in saved["notes"]) and packaging["written"] == []
    approval = app.APPROVALS[saved["notes"][0]["approval_id"]]
    assert approval["arguments"]["content"].startswith("İlan: [Backend / Java Developer · Migros One](https://")


def test_a_package_to_fix_carries_gordons_warning(app, packaging):
    packaging["review"]["reply"] = '{"verdict": "fix", "unsupported": ["\\"SQL optimize etti\\" Kubernetes"], "notes": ["Türkçe yazım"]}'
    job_id = one(app)
    package(app, job_id, description="x", letter=False)
    saved = asyncio.run(app.save_package(job_id))
    content = app.APPROVALS[saved["notes"][0]["approval_id"]]["arguments"]["content"]
    assert content.startswith('> [!warning] Gordon: gözden geçir\n> - "SQL optimize etti" Kubernetes\n> - Türkçe yazım')
    assert len(saved["notes"]) == 1


def test_one_package_at_a_time_and_a_restart_fails_it(app, packaging, tmp_path):
    job_id = one(app, description="x")
    app.task_store.save_package({"job_id": job_id, "status": "running"})
    with pytest.raises(app.HTTPException) as caught:
        package(app, job_id)
    assert caught.value.status_code == 409
    assert app.task_store.interrupt_packages() == 1
    assert app.task_store.get_package(job_id)["status"] == "failed"


def test_the_user_tracks_the_listing(app):
    job_id = one(app)
    assert asyncio.run(app.set_job_status(job_id, app.JobStatusRequest(status="applied")))["status"] == "applied"
    for bad in ("package_ready", "filtered", "x"):
        with pytest.raises(app.HTTPException) as caught:
            asyncio.run(app.set_job_status(job_id, app.JobStatusRequest(status=bad)))
        assert caught.value.status_code == 422
    with pytest.raises(app.HTTPException):
        asyncio.run(app.set_job_status("yok", app.JobStatusRequest(status="applied")))


# J-5 download: the package as Word files

def docx_parts(data: bytes):
    import io, zipfile
    import xml.etree.ElementTree as ET
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml",
            "word/_rels/document.xml.rels"} <= set(archive.namelist())
    for name in archive.namelist():
        if name.endswith((".xml", ".rels")):
            ET.fromstring(archive.read(name))  # every part is well-formed XML
    return archive.read("word/document.xml").decode()


def test_markdown_becomes_a_word_document():
    from docx_export import to_docx
    xml = docx_parts(to_docx(
        "# AYŞE NUR DOĞAN  \nJava · Spring  \n[linkedin.com/in/x](https://linkedin.com/in/x)\n\n---\n\n"
        "## DENEYİM\n**Yazılım Uzmanı** — KurumSoft & Co <A.Ş.>\n- SQL sorgularını *iyileştirdi*\n1. ikinci\n\nSon paragraf\nikinci satır"))
    assert 'w:val="Title"' in xml and "AYŞE NUR DOĞAN" in xml and 'w:val="Heading1"' in xml
    assert "linkedin.com/in/x</w:t>" in xml and "https://" not in xml and "---" not in xml
    assert "<w:b/></w:rPr><w:t xml:space=\"preserve\">Yazılım Uzmanı</w:t>" in xml
    assert "KurumSoft &amp; Co &lt;A.Ş.&gt;" in xml
    assert xml.count('w:val="ListItem"') == 2 and "iyileştirdi</w:t>" in xml and "*" not in xml
    assert "Son paragraf ikinci satır" in xml


def test_the_package_downloads_as_docx(app, packaging):
    job_id = one(app, title="Backend / Java Developer", company="Migros One")
    with pytest.raises(app.HTTPException) as caught:
        asyncio.run(app.package_docx(job_id, "cv"))
    assert caught.value.status_code == 404
    package(app, job_id, description="x")
    response = asyncio.run(app.package_docx(job_id, "letter"))
    assert response.media_type.endswith("wordprocessingml.document")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="_n yaz_ - Migros One - Backend - Java Developer.docx"')
    assert "filename*=UTF-8''%C3%96n%20yaz%C4%B1%20-%20Migros%20One" in disposition
    assert "Sayın İşe Alım Ekibi" in docx_parts(response.body)
    cv_xml = docx_parts(asyncio.run(app.package_docx(job_id, "cv")).body)
    assert "AYŞE NUR DOĞAN" in cv_xml and 'w:color w:val="3D7A8C"' in cv_xml and "<w:drawing>" not in cv_xml  # no photo here
    with pytest.raises(app.HTTPException):
        asyncio.run(app.package_docx(job_id, "x"))


def test_a_cv_the_model_wrote_badly_fails_the_package(app, packaging):
    original = app.fake_client.handler

    def handler(url, payload):
        if payload["messages"][0]["content"].startswith("You tailor"):
            return answer("Here is your CV: ...")
        return original(url, payload)

    app.fake_client.handler = staticmethod(handler)
    job_id = one(app)
    package(app, job_id, description="x")
    pkg = asyncio.run(app.get_job(job_id))["package"]
    assert pkg["status"] == "failed" and "CV JSON" in pkg["error"]


def test_parse_cv_checks_and_fills_in():
    from cv_layout import CvError, parse_cv
    cv = parse_cv('ok {"name": "A", "contact": ["x", "y", "z"], "order": ["experience", "bogus"], "language": "en",'
                  ' "experience": [{"title": "Dev", "bullets": ["**b**", "", 3]}], "skills": [{"label": "L"}]}')
    assert cv["contact"] == [["x", "y"], ["z"]] and cv["order"][:2] == ["experience", "summary"]
    assert cv["experience"][0]["bullets"] == ["b", "3"] and cv["skills"] == [] and cv["language"] == "en"
    for bad in ("", "[]", '{"name": "A"}', '{"experience": [{"title": "x"}]}'):
        with pytest.raises(CvError):
            parse_cv(bad)


def test_the_cv_docx_follows_the_design_with_the_photo(tmp_path):
    from cv_layout import parse_cv
    from docx_export import cv_docx
    import io, zipfile
    photo = b"\xff\xd8\xff\xe0" + b"0" * 50
    data = cv_docx(parse_cv(json.dumps(CV_JSON, ensure_ascii=False)), photo)
    xml = docx_parts(data)
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert archive.read("word/media/photo.jpg") == photo and 'r:embed="rIdPhoto"' in xml
    assert "rIdPhoto" in archive.read("word/_rels/document.xml.rels").decode()
    order = [xml.index(x) for x in ("AYŞE NUR DOĞAN", "ÖZET", "PROJELER", "DENEYİM", "YETENEKLER", "EĞİTİM VE SERTİFİKALAR", "REFERANSLAR")]
    assert order == sorted(order)
    assert 'w:color w:val="1F4E5F"' in xml and '<w:bottom w:val="single" w:sz="8" w:space="1" w:color="1F4E5F"/>' in xml
    assert "Kadıköy, İstanbul  •  a@b.c" in xml and "Diller:" in xml


def test_the_photo_is_served_when_there_is_one(app, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CV_PHOTO", tmp_path / "photo.jpg")
    with pytest.raises(app.HTTPException):
        asyncio.run(app.career_photo())
    (tmp_path / "photo.jpg").write_bytes(b"not a jpeg")
    with pytest.raises(app.HTTPException):
        asyncio.run(app.career_photo())
    (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0rest")
    assert asyncio.run(app.career_photo()).media_type == "image/jpeg"


def test_true_or_off_topic_quotes_are_not_unsupported(app):
    """L-2a3: a real quote whose reason says it is true, or only off-topic, does not make the package 'fix'."""
    package_text = "Advanced English (C1).\nDeveloped C# integrations for Logo Tiger ERP.\nover 3 years of professional experience"
    reply = json.dumps({"verdict": "fix", "unsupported": [
        '"Advanced English (C1)." — This is true as per the Master CV.',
        '"Developed C# integrations for Logo Tiger ERP" — not relevant for this React job.',
        '"Developed C# integrations for Logo Tiger ERP" (EXPERIENCE): The listing is for React developers.',
    ], "missing": [], "notes": []})
    review = app.parse_review(reply, package_text)
    assert review["verdict"] == "ok" and review["unsupported"] == [] and review["moved"] == 3
    assert len(review["notes"]) == 2
    real = json.dumps({"verdict": "fix", "unsupported": [
        '"over 3 years of professional experience" — Master CV dates add up to about 1 year, not 3; this is not true.',
    ]})
    assert app.parse_review(real, package_text)["verdict"] == "fix"


# ----------------------------------------------------------------------
# L-3a: goals (docs/plan-l3.md)
# ----------------------------------------------------------------------

def scored(app, *items):
    """items: (n, score, description) -> job ids in the same order."""
    imported(app, [job(n, description=text, title=f"Dev {n}", company=f"C{n}") for n, _, text in items])
    ids = {j["external_id"]: j["job_id"] for j in app.task_store.list_jobs()}
    for n, score, _ in items:
        app.task_store.score_job(ids[str(n)], score, "gerekçe", "dev")
    return [ids[str(n)] for n, _, _ in items]


def run_goal(app, call):
    async def go():
        started = await call
        await asyncio.gather(*list(app.BACKGROUND_JOBS))
        return started
    return asyncio.run(go())


def test_a_goal_packages_the_best_listings_and_waits_for_missing_texts(app, packaging):
    best, low, no_text, second = scored(app, (1, 90, "Java backend"), (2, 40, "x"), (3, 85, ""), (4, 70, "Python"))
    started = run_goal(app, app.create_goal(app.GoalRequest(packages=3)))
    assert [s["job_id"] for s in started["steps"]] == [best, no_text, second]  # score order, low score left out
    goal = asyncio.run(app.get_goal(started["goal_id"]))
    assert goal["status"] == "waiting" and [s["status"] for s in goal["steps"]] == ["ready", "needs_text", "ready"]
    assert goal["used"] == 2 * 3 * 1020 and goal["steps"][0]["tokens"] == 3 * 1020
    assert "ilan metni gerekli 1" in goal["summary"] and "6.120 / 120.000 token" in goal["summary"]
    # The text comes; only that step runs.
    run_goal(app, app.continue_goal(goal["goal_id"], app.GoalContinueRequest(texts={no_text: "Spring Boot"})))
    goal = asyncio.run(app.get_goal(goal["goal_id"]))
    assert goal["status"] == "completed" and [s["status"] for s in goal["steps"]] == ["ready"] * 3
    assert asyncio.run(app.get_job(no_text))["description"] == "Spring Boot"
    # A listing with a package is not picked again.
    with pytest.raises(app.HTTPException) as caught:
        asyncio.run(app.create_goal(app.GoalRequest()))
    assert caught.value.status_code == 422


def test_a_goal_never_falls_back_to_the_local_model(app, packaging, monkeypatch):
    scored(app, (1, 90, "Java"))
    real_route = app.route_model

    def local_route(*args, **kwargs):
        selection = real_route(*args, **kwargs)
        return selection.__class__(**{**selection.__dict__, "model": "local-x", "local": ("local-x",), "fallbacks": ()})

    monkeypatch.setattr(app, "route_model", local_route)
    started = run_goal(app, app.create_goal(app.GoalRequest(packages=1)))
    goal = asyncio.run(app.get_goal(started["goal_id"]))
    assert goal["status"] == "stopped" and "yerel modele düşülmez" in goal["error"]
    assert goal["steps"][0]["status"] == "failed" and app.CLOUD_ONLY_ERROR in goal["steps"][0]["error"]
    assert not [c for c in packaging["calls"]]  # no model was called


def test_a_goal_stops_at_its_token_limit_and_on_the_daily_budget(app, packaging, monkeypatch):
    scored(app, (1, 90, "a"), (2, 80, "b"))
    monkeypatch.setattr(app, "GOAL_BUDGET", app.GOAL_PACKAGE_RESERVE + 10)
    started = run_goal(app, app.create_goal(app.GoalRequest(packages=2)))
    goal = asyncio.run(app.get_goal(started["goal_id"]))
    assert goal["status"] == "stopped" and "hedef token sınırı" in goal["error"]
    assert [s["status"] for s in goal["steps"]] == ["ready", "pending"]

    monkeypatch.setattr(app.budget_manager, "cloud_percent", lambda **kw: 101)
    monkeypatch.setattr(app, "GOAL_BUDGET", 10**6)  # a stopped goal keeps its own limit
    app.task_store.save_goal({"goal_id": goal["goal_id"], "budget": 10**6})
    run_goal(app, app.continue_goal(goal["goal_id"], app.GoalContinueRequest()))
    goal = asyncio.run(app.get_goal(goal["goal_id"]))
    assert goal["status"] == "stopped" and "günlük bulut bütçesi" in goal["error"]


def test_goal_requests_are_checked(app, packaging):
    for bad in (app.GoalRequest(kind="x"), app.GoalRequest(packages=0), app.GoalRequest(packages=6)):
        with pytest.raises(app.HTTPException) as caught:
            asyncio.run(app.create_goal(bad))
        assert caught.value.status_code == 422
    with pytest.raises(app.HTTPException) as caught:  # nothing scored high enough
        asyncio.run(app.create_goal(app.GoalRequest()))
    assert caught.value.status_code == 422
    with pytest.raises(app.HTTPException) as caught:
        asyncio.run(app.get_goal("yok"))
    assert caught.value.status_code == 404
    assert asyncio.run(app.list_goals()) == {"goals": []}


def test_a_running_goal_is_failed_by_a_restart(tmp_path):
    store = TaskStore(tmp_path / "s.db")
    store.save_goal({"goal_id": "g", "kind": "job_week", "status": "running", "budget": 1, "steps": []})
    assert store.interrupt_goals() == 1 and store.get_goal("g")["status"] == "failed"
    with pytest.raises(StoreError):
        store.save_goal({"goal_id": "g", "status": "bilinmeyen"})
    store.close()
