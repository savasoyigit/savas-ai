#!/usr/bin/env bash
# K-3f (plan-k Ek B3): Web Push VAPID anahtarı. YALNIZ kullanıcı çalıştırır.
# Anahtar gateway image'i içinde (ağsız) üretilir, ekrana yazılmaz; dosya 600.
#   scripts/set_ui_push_keys.sh
# Sonra: docker compose up -d ceo-gateway
set -euo pipefail

ROOT="${SAVAS_AI_ROOT:-$HOME/AI}"
OUT="${SAVAS_UI_PUSH_FILE:-$ROOT/secrets/ui-push.env}"
IMAGE="${SAVAS_GATEWAY_IMAGE:-ai-ceo-gateway}"

if [[ -e "$OUT" ]]; then
    read -r -p "$OUT zaten var; yeni anahtar üretilsin mi (telefonlar yeniden abone olmalı)? [e/H] " answer
    [[ "$answer" == "e" || "$answer" == "E" ]] || { echo "vazgeçildi"; exit 1; }
fi

read -r -p "İletişim e-postası (push servisleri sorun olursa buna yazar): " email
if [[ ! "$email" =~ ^[^[:space:]@\$]+@[^[:space:]@\$]+\.[^[:space:]@\$]+$ ]]; then
    echo "geçerli bir e-posta girin" >&2
    exit 2
fi

umask 077
mkdir -p "$(dirname "$OUT")"
tmp="$(mktemp "$(dirname "$OUT")/.ui-push.XXXXXX")"
docker run --rm --network none "$IMAGE" python -c 'import push; print("VAPID_PRIVATE_KEY=" + push.new_vapid_key())' > "$tmp"
printf 'VAPID_SUBJECT=mailto:%s\n' "$email" >> "$tmp"
if ! grep -qE '^VAPID_PRIVATE_KEY=[A-Za-z0-9_-]{43}$' "$tmp"; then
    rm -f "$tmp"
    echo "anahtar üretilemedi (gateway image'i güncel mi? docker compose build ceo-gateway)" >&2
    exit 1
fi
chmod 600 "$tmp"
mv "$tmp" "$OUT"
echo "Yazıldı: $OUT (izin 600). Uygulamak için: cd $ROOT && docker compose up -d ceo-gateway"
