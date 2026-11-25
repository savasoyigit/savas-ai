#!/usr/bin/env bash
# J-2 (plan-j K): SAVAS AI otomasyonlarını systemd user timer olarak kurar. Kullanıcı çalıştırır.
#   bash ~/AI/scripts/install_automations.sh            kur / güncelle + başlat
#   bash ~/AI/scripts/install_automations.sh --remove   durdur + kaldır
# Oturum kapalıyken de çalışmaları için bir kez: loginctl enable-linger "$USER"
set -euo pipefail

ROOT="${SAVAS_AI_ROOT:-$HOME/AI}"
SRC="$ROOT/automations/systemd"
DEST="$HOME/.config/systemd/user"
TIMERS=(savas-daily-summary savas-daily-snapshot savas-weekly-report savas-collect-jobs)

if [[ "${1:-}" == "--remove" ]]; then
    for name in "${TIMERS[@]}"; do
        systemctl --user disable --now "$name.timer" 2>/dev/null || true
        rm -f "$DEST/$name.timer" "$DEST/$name.service"
    done
    systemctl --user daemon-reload
    echo "Kaldırıldı."
    exit 0
fi

mkdir -p "$DEST"
for name in "${TIMERS[@]}"; do
    install -m 644 "$SRC/$name.service" "$SRC/$name.timer" "$DEST/"
done
systemctl --user daemon-reload
for name in "${TIMERS[@]}"; do
    systemctl --user enable --now "$name.timer"
done

if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
    echo "Not: oturum kapalıyken de çalışması için bir kez: loginctl enable-linger $USER"
fi
systemctl --user list-timers 'savas-*' --no-pager
