"""J-3: job collection (plan-j karar A-D). Network stubbed; mails are synthetic until real samples exist."""

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import collect_jobs  # noqa: E402
import lib  # noqa: E402
from jobs import cv, mail, parsers, profile, remotive, text  # noqa: E402

RULES = profile.parse_profile(profile.TEMPLATE.read_text(encoding="utf-8"))

LINKEDIN_MAIL = {
    "from": {"emailAddress": {"address": "jobalerts-noreply@linkedin.com"}},
    "receivedDateTime": "2026-11-16T06:00:00Z",
    "body": {"content": """<html><head><style>.x{}</style></head><body><table>
      <tr><td><a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc&amp;refId=x"><img src="logo.png"></a></td>
          <td><a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc">Backend Developer (Python)</a>
              <p>Acme Teknoloji · İstanbul, Türkiye (Hibrit)</p></td></tr>
      <tr><td><a href="https://tr.linkedin.com/comm/jobs/view/4099999999">IT Business Analyst</a>
              <p>Beta Bank</p><p>Ankara (Remote)</p></td></tr>
      <tr><td><a href="https://www.linkedin.com/comm/jobs/view/4012345678/">View job</a></td></tr>
      <tr><td><a href="https://www.linkedin.com/comm/jobs/search?keywords=x">See all jobs</a></td></tr>
    </table></body></html>"""},
}
KARIYER_MAIL = {
    "from": {"emailAddress": {"address": "bilgi@mail.kariyer.net"}},
    "receivedDateTime": "2026-11-16T07:00:00Z",
    "body": {"content": """<div><a href="https://www.kariyer.net/is-ilani/gamma-yazilim-proje-yoneticisi-3456789?utm=mail">Proje Yöneticisi</a>
      <div>Gamma Yazılım | İstanbul(Avr.)</div>
      <a href="https://www.kariyer.net/is-ilani/gamma-yazilim-proje-yoneticisi-3456789">İlanı Gör</a></div>"""},
}


def test_html_to_text_and_tokens():
    assert text.to_text("<p>Merhaba &amp; <b>dünya</b></p><script>x()</script><ul><li>bir</li><li>iki</li></ul>") == "Merhaba & dünya\nbir\niki"
    assert ("link", "https://a.b/?x=1&y=2", "Git") in text.tokens('<a href="https://a.b/?x=1&amp;y=2"> Git </a>')


def test_linkedin_alert():
    jobs = parsers.parse(LINKEDIN_MAIL)
    assert [(j["external_id"], j["title"], j["company"], j["location"], j["remote"]) for j in jobs] == [
        ("4012345678", "Backend Developer (Python)", "Acme Teknoloji", "İstanbul, Türkiye (Hibrit)", False),
        ("4099999999", "IT Business Analyst", "Beta Bank", "Ankara (Remote)", True),
    ]
    assert jobs[0]["url"] == "https://www.linkedin.com/jobs/view/4012345678/"  # tracking dropped
    assert jobs[0]["source"] == "linkedin" and jobs[0]["posted_at"] == "2026-11-16T06:00:00Z"


def test_kariyer_alert():
    [job] = parsers.parse(KARIYER_MAIL)
    assert job["external_id"] == "3456789" and job["title"] == "Proje Yöneticisi"
    assert job["company"] == "Gamma Yazılım" and job["location"] == "İstanbul(Avr.)"
    assert job["url"] == "https://www.kariyer.net/is-ilani/gamma-yazilim-proje-yoneticisi-3456789"


def test_other_senders_and_lookalike_links_are_ignored():
    assert parsers.parse({**LINKEDIN_MAIL, "from": {"emailAddress": {"address": "x@linkedin.com.evil.io"}}}) == []
    fake = {**LINKEDIN_MAIL, "body": {"content": '<a href="https://linkedin.com.evil.io/jobs/view/4012345678">Backend</a>'}}
    assert parsers.parse(fake) == []
    assert parsers.parse({**KARIYER_MAIL, "body": {"content": '<a href="http://www.kariyer.net/is-ilani/x-3456789">Backend</a>'}}) == []


def test_profile_template_parses():
    assert "backend" in RULES["roller"] and "iş analisti" in RULES["roller"] and RULES["en_fazla_deneyim_yili"] == 7
    assert RULES["kara_liste"] == []
    with pytest.raises(ValueError):
        profile.parse_profile("blok yok")


@pytest.mark.parametrize("title,expected", [
    ("Backend Developer (Python)", "new"), ("IT Business Analyst", "new"), ("İŞ ANALİSTİ", "new"),
    ("Proje Yöneticisi", "new"), ("Senior Full-Stack Engineer", "new"), ("Kıdemli Yazılım Geliştirici", "new"),
    ("Frontend Developer", "filtered"), ("Backend Intern", "filtered"), ("Stajyer Yazılım Geliştirici", "filtered"),
    ("Director of Engineering", "filtered"), ("Muhasebe Uzmanı", "filtered"), ("Backendless Admin", "filtered"),
])
def test_titles(title, expected):
    assert profile.judge({"title": title, "source": "linkedin"}, RULES)[0] == expected


def test_blacklist_remotive_rules_and_experience():
    rules = {**RULES, "kara_liste": [profile.norm("Kötü A.Ş.")]}
    assert profile.judge({"title": "Backend Developer", "company": "Kötü A.Ş.", "source": "kariyer"}, rules) == ("filtered", "kara liste: kötü a.ş.")
    remote = {"title": "Backend Developer", "source": "remotive", "job_type": "full_time", "location": "USA"}
    assert profile.judge(remote, RULES) == ("filtered", "konum: USA")
    assert profile.judge({**remote, "location": "Europe, USA"}, RULES) == ("new", None)
    assert profile.judge({**remote, "location": "Worldwide", "job_type": "contract"}, RULES)[0] == "filtered"
    assert profile.experience_years("We need 10+ years of experience in Java.") == 10
    assert profile.experience_years("En az 8 yıl deneyimli") == 8
    assert profile.experience_years("Founded 25 years ago. 3+ years experience.") == 3
    assert profile.judge({**remote, "location": "Worldwide", "description": "8+ years experience"}, RULES) == ("filtered", "deneyim 8+ yıl")


def test_the_obsidian_note_wins_and_a_broken_one_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "VAULT", tmp_path)
    assert profile.load_profile()[1] == "şablon"
    (tmp_path / "Kariyer").mkdir()
    (tmp_path / "Kariyer" / "Profil.md").write_text("```ayarlar\nroller: muhasebe\n```\n")
    rules, where = profile.load_profile()
    assert where == "Kariyer/Profil.md" and rules["roller"] == ["muhasebe"]
    (tmp_path / "Kariyer" / "Profil.md").write_text("bozuk")
    assert profile.load_profile()[1] == "şablon"


def test_remotive_mapping():
    job = remotive.to_job({"id": 7, "url": "https://remotive.com/remote-jobs/x-7", "title": "Backend", "company_name": "C",
                           "candidate_required_location": "Europe", "job_type": "full_time",
                           "description": "<p>Python</p><p>8+ years experience</p>", "publication_date": "2026-11-13T17:29:57"})
    assert job["external_id"] == "7" and job["description"] == "Python\n8+ years experience" and job["remote"]


REDIRECTED = (b"From: LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>\r\nTo: x@gmail.com\r\n"
              b"Subject: Backend Developer: Acme\r\nDate: Mon, 16 Nov 2026 09:00:00 +0300\r\n"
              b"Content-Type: text/html; charset=utf-8\r\n\r\n"
              b'<a href="https://www.linkedin.com/comm/jobs/view/4012345678/?x=1">Backend Developer</a><p>Acme \xc2\xb7 Istanbul</p>')
FORWARDED = ("From: Savas <user@hotmail.com>\r\nSubject: FW: yeni ilanlar\r\nDate: Mon, 16 Nov 2026 10:00:00 +0300\r\n"
             "Content-Type: text/plain; charset=utf-8\r\n\r\n"
             "From: Kariyer.net <bilgi@mail.kariyer.net>\nProje Yöneticisi\nhttps://www.kariyer.net/is-ilani/gamma-proje-yoneticisi-3456789\n").encode()


def test_a_redirected_alert_keeps_its_sender():
    message = mail.to_message(REDIRECTED)
    assert message["from"]["emailAddress"]["address"] == "jobalerts-noreply@linkedin.com"
    assert message["receivedDateTime"] == "2026-11-16T06:00:00Z"
    [job] = parsers.parse(message)
    assert job["external_id"] == "4012345678" and job["company"] == "Acme"


def test_a_forwarded_alert_is_known_by_the_quoted_sender():
    message = mail.to_message(FORWARDED)
    assert message["from"]["emailAddress"]["address"] == "bilgi@mail.kariyer.net"
    [job] = parsers.parse(message)
    assert job["external_id"] == "3456789" and job["source"] == "kariyer"


class FakeImap:
    """A read-only inbox of (uid, raw) pairs."""
    mails = []
    logins = []

    def __init__(self, host, port, timeout=None):
        assert (host, port) == ("imap.gmail.com", 993)

    def login(self, user, password):
        if password != "abcdefghijklmnop":
            raise mail.imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        FakeImap.logins.append(user)

    def select(self, box, readonly=False):
        assert box == "INBOX" and readonly
        return "OK", [str(len(self.mails)).encode()]

    def response(self, name):
        return name, [b"77"]

    def uid(self, command, *args):
        if command == "SEARCH":
            if args[1] == "UID":
                low = int(args[2].split(":")[0])
                found = [u for u, _ in self.mails if u >= low] or [self.mails[-1][0]]
            else:
                found = [u for u, _ in self.mails]
            return "OK", [" ".join(map(str, found)).encode()]
        assert command == "FETCH" and args[1] == "(BODY.PEEK[])"  # never marks read
        raw = dict(self.mails)[int(args[0])]
        return "OK", [(b"1 (BODY[] {n}", raw), b")"]

    def logout(self):
        pass


@pytest.fixture
def gmail(tmp_path, monkeypatch):
    path = tmp_path / "gmail.env"
    path.write_text("GMAIL_ADDRESS=x@gmail.com\nGMAIL_APP_PASSWORD=abcdefghijklmnop\n")
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeImap)
    FakeImap.mails, FakeImap.logins = [(5, REDIRECTED), (6, FORWARDED)], []
    return path


def test_gmail_reads_new_mails_once(gmail):
    state = {}
    first = mail.fetch_new(state, gmail)
    assert len(first) == 2 and state == {"mail_uid": 6, "mail_uidvalidity": "77"}
    assert mail.fetch_new(state, gmail) == []  # "7:*" returns uid 6 again: skipped
    FakeImap.mails.append((9, REDIRECTED))
    assert len(mail.fetch_new(state, gmail)) == 1 and state["mail_uid"] == 9


def test_a_new_uidvalidity_starts_over(gmail):
    state = {"mail_uid": 900, "mail_uidvalidity": "1"}
    assert len(mail.fetch_new(state, gmail)) == 2 and state["mail_uidvalidity"] == "77"


def test_gmail_settings(tmp_path, gmail):
    with pytest.raises(mail.MailNotConfigured):
        mail.read_settings(tmp_path / "yok.env")
    gmail.write_text("GMAIL_ADDRESS=x@gmail.com\nGMAIL_APP_PASSWORD=wrongwrongwrongw\n")
    with pytest.raises(lib.AutomationError) as caught:
        mail.fetch_new({}, gmail)
    assert "wrongwrong" not in str(caught.value) and "uygulama şifresi" in str(caught.value)


def test_gmail_script_refuses_bad_input_and_hides_the_password(tmp_path):
    import subprocess
    script = str(HERE.parent / "scripts" / "set_gmail.sh")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SAVAS_MAIL_FILE": str(tmp_path / "g.env")}
    bad = subprocess.run(["bash", script], input="x@hotmail.com\n", capture_output=True, text=True, env=env)
    assert bad.returncode == 1 and not (tmp_path / "g.env").exists()
    short = subprocess.run(["bash", script], input="x@gmail.com\nabc\n", capture_output=True, text=True, env=env)
    assert short.returncode == 1 and not (tmp_path / "g.env").exists()


@pytest.fixture
def collector(tmp_path, monkeypatch):
    monkeypatch.setattr(lib, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(profile, "VAULT", tmp_path / "vault")
    calls = {"import": [], "notify": [], "since": [], "score": []}

    def fetch_new(state):
        calls["since"].append(state.get("mail_uid"))
        fresh = [] if state.get("mail_uid") else [LINKEDIN_MAIL, KARIYER_MAIL]
        state["mail_uid"] = 2
        return fresh

    monkeypatch.setattr(mail, "fetch_new", fetch_new)
    monkeypatch.setattr(remotive, "fetch", lambda: [remotive.to_job({"id": 1, "url": "https://remotive.com/x-1", "title": "Sales Lead",
                                                                      "job_type": "full_time", "candidate_required_location": "Europe"})])

    def request(method, url, body=None, headers=None, timeout=None):
        if url.endswith("/jobs/score"):
            calls["score"].append(body)
            return {"scored": [{"job_id": "a", "title": "Backend Developer", "company": "Acme", "score": 82, "track": "dev", "reason": "uygun"},
                               {"job_id": "b", "title": "IT Business Analyst", "company": "Beta", "score": 64, "track": "pm_ba", "reason": "olası"},
                               {"job_id": "c", "title": "Proje Yöneticisi", "company": "Gamma", "score": 30, "track": "pm_ba", "reason": "zayıf"}],
                    "remaining": 0, "blocked": False, "error": None}
        calls["import"].append(body["jobs"])
        return {"added": len(body["jobs"]), "duplicates": 0, "new": sum(j["status"] == "new" for j in body["jobs"])}

    monkeypatch.setattr(lib, "request", request)
    monkeypatch.setattr(lib, "notify", lambda *a: calls["notify"].append(a))
    return calls


def test_a_run_imports_filtered_listings_and_pushes_the_new_ones(collector):
    collect_jobs.run(dry_run=False, dump=True)
    [jobs] = collector["import"]
    assert [(j["source"], j["status"]) for j in jobs] == [("linkedin", "new"), ("linkedin", "new"), ("kariyer", "new"), ("remotive", "filtered")]
    assert "job_type" not in jobs[3] and jobs[3]["filter_reason"] == "rol eşleşmedi"
    assert collector["notify"] == [("Yeni ilanlar", "3 yeni ilan (puansız: Kariyer/CV.md yok)", "/ui/#jobs", "new-jobs")]
    assert collector["score"] == []  # no CV note, no scoring
    assert len(list((lib.STATE_DIR / "mail-samples").glob("*.json"))) == 2
    assert lib.State("collect-jobs").load()["mail_uid"] == 2

    collect_jobs.run(dry_run=False, dump=False)  # next run: only newer mail, Remotive not again today
    assert collector["since"] == [None, 2] and collector["import"][1:] == []


def test_a_dry_run_imports_and_pushes_nothing(collector):
    collect_jobs.run(dry_run=True, dump=False)
    assert collector["import"] == [] and collector["notify"] == [] and lib.State("collect-jobs").load() == {}


def test_without_mail_remotive_still_runs(collector, monkeypatch):
    def missing(state):
        raise mail.MailNotConfigured("gmail.env yok")
    monkeypatch.setattr(mail, "fetch_new", missing)
    collect_jobs.run(dry_run=False, dump=False)
    assert [j["source"] for j in collector["import"][0]] == ["remotive"]


def test_a_failing_source_still_imports_the_other_and_exits_with_an_error(collector, monkeypatch):
    def down():
        raise lib.AutomationError("GET remotive -> HTTP 503")
    monkeypatch.setattr(remotive, "fetch", down)
    with pytest.raises(lib.AutomationError, match="503"):
        collect_jobs.run(dry_run=False, dump=False)
    assert len(collector["import"][0]) == 3


def test_with_a_cv_new_listings_are_scored_and_only_good_ones_announced(collector):
    (profile.VAULT / "Kariyer").mkdir(parents=True)
    (profile.VAULT / "Kariyer" / "CV.md").write_text("# CV\nPython backend, 4 yıl")
    collect_jobs.run(dry_run=False, dump=False)
    [sent] = collector["score"]
    assert sent["cv"].startswith("# CV") and "Target roles: backend" in sent["preferences"] and sent["max_jobs"] == 30
    assert collector["notify"] == [("1 uygun ilan (≥70)", "82 · Backend Developer · Acme", "/ui/#jobs", "new-jobs")]


def test_scoring_errors_fail_the_run_after_the_import(collector, monkeypatch):
    (profile.VAULT / "Kariyer").mkdir(parents=True)
    (profile.VAULT / "Kariyer" / "CV.md").write_text("cv")
    real = lib.request

    def request(method, url, body=None, headers=None, timeout=None):
        if url.endswith("/jobs/score"):
            return {"scored": [], "remaining": 5, "blocked": False, "error": "no scores in the answer of m"}
        return real(method, url, body, headers, timeout)

    monkeypatch.setattr(lib, "request", request)
    with pytest.raises(lib.AutomationError, match="no scores"):
        collect_jobs.run(dry_run=False, dump=False)
    assert len(collector["import"]) == 1


def test_threshold_and_preferences_come_from_the_profile():
    rules = profile.parse_profile("```ayarlar\nroller: backend\nremote_bolgeler: europe\nbildirim_esigi: 75\ntercihler: İstanbul Avrupa yakası\n```")
    assert rules["bildirim_esigi"] == 75
    assert profile.preferences(rules) == ("Target roles: backend\nLocation: Turkey (on-site/hybrid) or full-time remote open to: europe\n"
                                          "Languages: Turkish or English\nOther: İstanbul Avrupa yakası")
    assert RULES["bildirim_esigi"] == 70 and profile.DEFAULT_THRESHOLD == 70
    assert profile.parse_profile("```ayarlar\nroller: x\n```")["bildirim_esigi"] == 70


def test_cv_is_proposed_from_the_newest_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "PDF_DIR", tmp_path)
    with pytest.raises(lib.AutomationError, match="PDF yok"):
        cv.newest_pdf()
    (tmp_path / "eski.pdf").write_bytes(b"%PDF")
    (tmp_path / "yeni.pdf").write_bytes(b"%PDF")
    import os
    os.utime(tmp_path / "eski.pdf", (1, 1))
    assert cv.newest_pdf().name == "yeni.pdf"
    monkeypatch.setattr(cv, "pdf_text", lambda pdf: "Savaş\nBackend Developer\n" * 20)
    proposed = []
    monkeypatch.setattr(lib, "propose_note", lambda *a: proposed.append(a) or {"status": "waiting_approval"})
    collect_jobs.propose_cv()
    path, content, reason = proposed[0]
    assert path == "Kariyer/CV.md" and content.startswith("# CV\n\nKaynak: `yeni.pdf`") and "Backend Developer" in content
    monkeypatch.setattr(cv, "pdf_text", lambda pdf: "kısa")
    with pytest.raises(lib.AutomationError, match="metin çıkmadı"):
        collect_jobs.propose_cv()


def test_pdftotext_reads_a_real_pdf(tmp_path):
    # A minimal one-page PDF with the text "Merhaba CV".
    body = b"BT /F1 12 Tf 20 100 Td (Merhaba CV) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>stream\n" % len(body) + body + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref
    (tmp_path / "cv.pdf").write_bytes(out)
    assert cv.pdf_text(tmp_path / "cv.pdf") == "Merhaba CV"


def test_rescore_sends_rescore_and_never_pushes(collector):
    (profile.VAULT / "Kariyer").mkdir(parents=True)
    (profile.VAULT / "Kariyer" / "CV.md").write_text("cv")
    collect_jobs.rescore()
    assert collector["score"][0]["rescore"] is True and collector["notify"] == [] and collector["import"] == []
