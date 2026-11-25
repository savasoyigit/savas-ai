#!/usr/bin/env bash
# Faz I-4 (plan-i karar E): sandbox'taki değişikliği canlı repoya YALNIZ
# kullanıcı uygular. Orchestrator'a ve runner'a mount edilmez / çağrılmaz.
#
#   scripts/apply_patch.sh <görev-id>          # kuru deneme, onay sorar
#   scripts/apply_patch.sh <görev-id> --yes    # onay sormadan (dikkat)
#
# Sıra: yol kontrolü → patch --dry-run → onay → tar yedeği → patch.
# .orig / .rej dosyası bırakmaz (CLAUDE.md: .bak yok, yedek tar ile).
# Rebuild / restart yapmaz; o protokoldeki ayrı durma noktası.
set -euo pipefail

ROOT="${SAVAS_AI_ROOT:-$HOME/AI}"
SANDBOX="${SAVAS_SANDBOX:-$ROOT/sandbox}"
REPOS="${SAVAS_REPOS:-$ROOT/runner/repos.json}"
BACKUPS="${SAVAS_BACKUPS:-$HOME/backups}"

task="${1:-}"
confirm="${2:-}"

if [[ ! "$task" =~ ^[a-z0-9-]{8,64}$ ]]; then
    echo "kullanım: $0 <görev-id> [--yes]" >&2
    exit 2
fi

dir="$SANDBOX/$task"
patchfile="$dir/change.patch"

if [[ ! -s "$patchfile" ]]; then
    echo "yama yok veya boş: $patchfile" >&2
    exit 1
fi

repo_name=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["repo"])' "$dir/repo.json")
repo_path=$(python3 -I -c 'import json,sys; e=json.load(open(sys.argv[1]))["repos"][sys.argv[2]]; print(e if isinstance(e, str) else e["path"])' "$REPOS" "$repo_name")

# Yamanın dokunduğu yollar: mutlak / .. / secret ve araç klasörleri yasak.
paths=$(grep -E '^(\+\+\+|---) ' "$patchfile" | awk '{print $2}' | grep -v '^/dev/null$' | sed -E 's#^[ab]/##' | sort -u)
if echo "$paths" | grep -Eq '(^/|(^|/)\.\.(/|$)|(^|/)(secrets|data|\.git|\.venv|backups|sandbox)(/|$)|\.env)'; then
    echo "yasak yol içeren yama; uygulanmadı:" >&2
    echo "$paths" | grep -E '(^/|(^|/)\.\.(/|$)|(^|/)(secrets|data|\.git|\.venv|backups|sandbox)(/|$)|\.env)' >&2
    exit 1
fi

echo "Repo: $repo_name ($repo_path)"
echo "Dosyalar:"
echo "$paths" | sed 's/^/  /'
echo "Satır: +$(grep -cE '^\+[^+]|^\+$' "$patchfile" || true) / -$(grep -cE '^-[^-]|^-$' "$patchfile" || true)"

if ! patch --dry-run --batch --forward --no-backup-if-mismatch -p1 -d "$repo_path" < "$patchfile" >/dev/null; then
    echo "kuru deneme başarısız (canlı dosya değişmiş olabilir); hiçbir şey değişmedi" >&2
    exit 1
fi
echo "Kuru deneme: temiz."

if [[ "$confirm" != "--yes" ]]; then
    read -r -p "Canlı repoya uygulansın mı? [e/H] " answer
    if [[ "$answer" != "e" && "$answer" != "E" ]]; then
        echo "vazgeçildi; hiçbir şey değişmedi"
        exit 1
    fi
fi

mkdir -p "$BACKUPS"
backup="$BACKUPS/$repo_name-$(date +%F-%H%M%S)-pre-patch-${task:0:8}.tgz"
tar --exclude='secrets' --exclude='data' --exclude='.venv' --exclude='__pycache__' \
    --exclude='sandbox' --exclude='sock' \
    -czf "$backup" -C "$(dirname "$repo_path")" "$(basename "$repo_path")"
echo "Yedek: $backup"

patch --batch --forward --no-backup-if-mismatch -p1 -d "$repo_path" < "$patchfile"
date -Iseconds > "$dir/applied"
echo "Uygulandı. Testleri koş; rebuild / restart ayrıca onay ister (bu script yapmaz)."
