# SAVAS AI otomasyonları (Faz J, `docs/plan-j.md`)

Host'ta systemd **user** timer'larıyla çalışır; uygulamanın içinde zamanlayıcı yok. Ortak parçalar `lib.py`'de.
Durum ve çıktılar: `data/automations/`. Bildirim: gateway `POST /internal/notify` (anahtar `secrets/automation.env`).

| Otomasyon | Ne zaman | LLM | Ne yapar |
|---|---|---|---|
| `daily_summary.py` | her gün 21:30 | yok | Günün görev/token özeti → iPhone; günlük anlık görüntü |
| `daily_summary.py --snapshot` | her gün 23:55 | yok | Gün sonu anlık görüntüsü (bildirim yok) |
| `collect_jobs.py --dump-mail` | 08:30, 18:30 | Oracle (J-4) | iş uyarıları (Hotmail → Gmail) + Remotive (günde 1) → süzgeç (`Kariyer/Profil.md`) → `POST /jobs/import` → Oracle puanı (`POST /jobs/score`, CV = `Kariyer/CV.md`, 10'arlı, en çok 30/çalışma) → `bildirim_esigi` (60) üstü iPhone |
| `weekly_report.py` | pazar 21:40 | yok | Son 7 gün raporu → `data/automations/reports/YYYY-Www.md` + iPhone; aynı metin Obsidian notu olarak **önerilir** (`SAVAS-AI/Raporlar/`), onaylayınca yazılır |

Not önerisi: `lib.propose_note(path, content, reason)` → orchestrator `POST /notes/propose` (yalnız yerel; yalnız `SAVAS-AI/Raporlar/`). Not, ajanların `create_note`'u gibi onay katmanından geçer (otomatik onay kuralları da geçerli); LLM yok.

İlan toplama (J-3): `jobs/` — `parsers.py` (LinkedIn, Kariyer.net uyarı e-postaları; biçim gerçek örneklerle doğrulanacak, örnekler `data/automations/mail-samples/`), `profile.py` (süzgeç; kurallar Obsidian `Kariyer/Profil.md`, yoksa `jobs/Profil.md` şablonu), `remotive.py` (kaynak: Remotive, bağlantı Remotive'e), `mail.py` (Gmail IMAP, salt okunur, BODY.PEEK). Deneme: `python3 automations/collect_jobs.py --dry-run` (içe aktarmaz, bildirmez). CV notu: `--propose-cv` (en yeni `~/AI/Kariyer/*.pdf`, pdftotext → onaya). Yalnız puanlama: `--score-only`. Tahmin: 10 ilanlık grup ≈ 4–6 bin Oracle token (ölçüm: 2 ilan = 2.448).

**E-posta bağlantısı (bir kez, kullanıcı).** Microsoft kişisel hesapla uygulama kaydını kapattı (2026-11-16 denendi), bu yüzden uyarılar Hotmail'den Gmail'e yönlendirilir ve Gmail IMAP ile salt okunur okunur:
1. Ayrı bir Gmail hesabı aç (ya da var olanı kullan). Google hesabında **2 Adımlı Doğrulama**yı aç.
2. https://myaccount.google.com/apppasswords → ad: `SAVAS AI` → 16 harflik **uygulama şifresi**ni al.
3. Outlook.com → Ayarlar → Posta → **Kurallar** → Yeni kural: "Gönderen" `linkedin.com` ve `kariyer.net` (ayrı koşullar) → eylem **"Yeniden yönlendir" (Redirect to)** → Gmail adresin. (Redirect asıl göndereni korur; "Forward" da çalışır.)
4. `bash ~/AI/scripts/set_gmail.sh` → adres + uygulama şifresi (ekranda görünmez) → "Gmail bağlantısı tamam".
5. `python3 ~/AI/automations/collect_jobs.py --dry-run` → "e-posta: … mesaj" satırı.

Hata bildirimleri (başarısız görev, bütçe sınırı, model uyarısı) timer değil, gateway izleyicisidir (15 s).

**Kurulum (kullanıcı, bir kez):**

```
bash ~/AI/scripts/set_automation_key.sh
cd ~/AI && docker compose up -d ceo-gateway
loginctl enable-linger "$USER"
bash ~/AI/scripts/install_automations.sh
```

Elle çalıştırma: `python3 ~/AI/automations/daily_summary.py`. Kayıtlar: `journalctl --user -u savas-daily-summary`.
Yeni otomasyon = `automations/<ad>.py` (`lib.main`) + `systemd/savas-<ad>.{service,timer}` + `install_automations.sh` listesine ekleme + bu tablo.
