#!/usr/bin/env bash
# backup.sh — archive horodatée du dossier de données (sans .env ni logs).
#
#   ./scripts/backup.sh                       # dans <parent du dossier de données>/backups
#   BACKUP_DIR=/ailleurs KEEP=20 ./scripts/backup.sh
#
# Les archives sont rangées À CÔTÉ des données (hors du dépôt dès que VEILLE_DATA_DIR
# l'est) et réservées à leur propriétaire : elles contiennent l'état de Hermes.
# Les KEEP plus récentes sont conservées. Dernière ligne affichée : chemin de l'archive.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$("$REPO/scripts/data-dir.sh")"
[ -d "$DATA" ] || { echo "✗ dossier de données introuvable : $DATA" >&2; exit 1; }
BACKUP_DIR="${BACKUP_DIR:-$(dirname "$DATA")/backups}"
KEEP="${KEEP:-10}"

umask 077
mkdir -p "$BACKUP_DIR"
ARCHIVE="$BACKUP_DIR/veille-data-$(date +%Y%m%d-%H%M%S).tar.gz"
tar --exclude='./.env' --exclude='./logs' -czf "$ARCHIVE" -C "$DATA" .
find "$BACKUP_DIR" -maxdepth 1 -name 'veille-data-*.tar.gz' -printf '%T@ %p\n' \
    | sort -rn | tail -n +"$((KEEP + 1))" | cut -d' ' -f2- | xargs -r rm -f --
echo "$ARCHIVE"
