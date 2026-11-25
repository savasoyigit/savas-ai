#!/usr/bin/env bash
# J-3 (plan-j karar B, revize): iş uyarılarının yönlendirildiği Gmail'e salt okunur IMAP erişimi. YALNIZ kullanıcı çalıştırır.
# Uygulama şifresi ekrana yazılmaz (read -s); secrets/gmail.env 600. Sonunda bir kez giriş denenir.
#   bash ~/AI/scripts/set_gmail.sh
set -euo pipefail
ROOT="${SAVAS_AI_ROOT:-$HOME/AI}"
OUT="${SAVAS_MAIL_FILE:-$ROOT/secrets/gmail.env}"

read -r -p "Gmail adresi: " address
[[ "$address" =~ ^[^@[:space:]]+@(gmail|googlemail)\.com$ ]] || { echo "bir @gmail.com adresi gir" >&2; exit 1; }
read -r -s -p "Google uygulama şifresi (16 harf, boşluklu da olur; ekranda görünmez): " password
echo
password="${password// /}"
[[ "$password" =~ ^[a-zA-Z]{16}$ ]] || { echo "uygulama şifresi 16 harf olmalı" >&2; exit 1; }

umask 077
mkdir -p "$(dirname "$OUT")"
tmp="$(mktemp "$(dirname "$OUT")/.gmail.XXXXXX")"
printf 'GMAIL_ADDRESS=%s\nGMAIL_APP_PASSWORD=%s\n' "$address" "$password" > "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$OUT"
unset password
echo "Yazıldı: $OUT (izin 600). Giriş deneniyor…"
SAVAS_MAIL_FILE="$OUT" python3 "$ROOT/automations/jobs/mail.py" check
