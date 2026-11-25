#!/usr/bin/env bash
# J-2 (plan-j F): host otomasyonlarının gateway'e bildirim gönderme anahtarı.
# YALNIZ kullanıcı çalıştırır. Rastgele anahtar üretilir, ekrana yazılmaz; dosya 600.
#   bash ~/AI/scripts/set_automation_key.sh
# Sonra: cd ~/AI && docker compose up -d ceo-gateway
set -euo pipefail

ROOT="${SAVAS_AI_ROOT:-$HOME/AI}"
OUT="${SAVAS_AUTOMATION_KEY_FILE:-$ROOT/secrets/automation.env}"

if [[ -e "$OUT" ]]; then
    read -r -p "$OUT zaten var; yeni anahtar üretilsin mi? [e/H] " answer
    [[ "$answer" == "e" || "$answer" == "E" ]] || { echo "vazgeçildi"; exit 1; }
fi

umask 077
mkdir -p "$(dirname "$OUT")"
tmp="$(mktemp "$(dirname "$OUT")/.automation.XXXXXX")"
# 48 bayt rastgele, base64url ($ ve : yok: compose güvenli).
python3 -c 'import secrets; print("SAVAS_NOTIFY_KEY=" + secrets.token_urlsafe(48))' > "$tmp"
if ! grep -qE '^SAVAS_NOTIFY_KEY=[A-Za-z0-9_-]{60,}$' "$tmp"; then
    rm -f "$tmp"
    echo "anahtar üretilemedi" >&2
    exit 1
fi
chmod 600 "$tmp"
mv "$tmp" "$OUT"
echo "Yazıldı: $OUT (izin 600). Uygulamak için: cd $ROOT && docker compose up -d ceo-gateway"
