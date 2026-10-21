#!/usr/bin/env bash
# Copia diaria para sacar del servidor (rclone, otra máquina, Drive).
# Uso: DATABASE_URL=postgresql://... RCLONE_REMOTE=drive:kinkara ./scripts/backup.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${BACKUP_DIR:-$ROOT/data/backups}"
mkdir -p "$OUT"

if [[ "${DATABASE_URL:-}" == postgresql:* || "${DATABASE_URL:-}" == postgres:* ]]; then
  FILE="$OUT/kinkara-$STAMP.dump"
  pg_dump --format=custom --file "$FILE" "$DATABASE_URL"
else
  SRC="${SQLITE_PATH:-$ROOT/data/kinkara.db}"
  FILE="$OUT/kinkara-$STAMP.db"
  sqlite3 "$SRC" ".backup '$FILE'"
fi

gzip -f "$FILE"
FILE="$FILE.gz"

if [[ -n "${RCLONE_REMOTE:-}" ]]; then
  rclone copy "$FILE" "$RCLONE_REMOTE" --immutable
fi

# Conserva 30 copias locales.
ls -1t "$OUT"/kinkara-* 2>/dev/null | tail -n +31 | xargs -r rm -f
echo "Copia lista: $FILE"
