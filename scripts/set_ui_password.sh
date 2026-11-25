#!/usr/bin/env bash
# K-3b (plan-k Ek A3): CEO gateway kullanıcı adı / şifresi. YALNIZ kullanıcı çalıştırır.
# Şifre ekrana yazılmaz, argüman olarak geçmez; dosyaya yalnız scrypt özeti yazılır.
#   scripts/set_ui_password.sh
# Sonra: docker compose up -d ceo-gateway   (yeni değerleri okusun diye)
set -euo pipefail

ROOT="${SAVAS_AI_ROOT:-$HOME/AI}"
OUT="${SAVAS_UI_AUTH_FILE:-$ROOT/secrets/ui-auth.env}"

if [[ -e "$OUT" ]]; then
    read -r -p "$OUT zaten var; değiştirilsin mi (açık oturumlar kapanır)? [e/H] " answer
    [[ "$answer" == "e" || "$answer" == "E" ]] || { echo "vazgeçildi"; exit 1; }
fi

read -r -p "Kullanıcı adı: " user
if [[ ! "$user" =~ ^[A-Za-z0-9._-]{3,32}$ ]]; then
    echo "kullanıcı adı 3-32 karakter: harf, rakam, . _ -" >&2
    exit 2
fi
read -r -s -p "Şifre (en az 12 karakter): " pass1; echo
read -r -s -p "Şifre (tekrar): " pass2; echo
if [[ "$pass1" != "$pass2" ]]; then echo "şifreler aynı değil" >&2; exit 2; fi
if (( ${#pass1} < 12 )); then echo "şifre en az 12 karakter olmalı" >&2; exit 2; fi

umask 077
mkdir -p "$(dirname "$OUT")"
tmp="$(mktemp "$(dirname "$OUT")/.ui-auth.XXXXXX")"
printf '%s' "$pass1" | python3 -I -c '
import secrets, sys
sys.path.insert(0, sys.argv[1])
import auth
password = sys.stdin.read()
print(f"UI_USER={sys.argv[2]}")
print(f"UI_PASSWORD_HASH={auth.hash_password(password)}")
print(f"UI_SESSION_KEY={secrets.token_urlsafe(48)}")
' "$ROOT/gateway/app" "$user" > "$tmp"
unset pass1 pass2
chmod 600 "$tmp"
mv "$tmp" "$OUT"
echo "Yazıldı: $OUT (izin 600). Uygulamak için: cd $ROOT && docker compose up -d ceo-gateway"
