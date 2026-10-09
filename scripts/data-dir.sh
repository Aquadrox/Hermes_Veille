#!/usr/bin/env bash
# data-dir.sh — chemin absolu, sur l'hôte, du dossier de données monté sur /opt/data.
#
# Source UNIQUE pour les scripts de l'hôte (déploiement, sauvegarde, chien de garde, MCP).
# Même résolution que docker compose (`${VEILLE_DATA_DIR:-./data}`) :
#   1. variable d'environnement VEILLE_DATA_DIR ;
#   2. ligne VEILLE_DATA_DIR= du .env du dépôt ;
#   3. ./data du dépôt (défaut historique).
# Un chemin relatif est résolu depuis le dépôt (comme le fait compose).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dir="${VEILLE_DATA_DIR:-}"
if [ -z "$dir" ] && [ -f "$REPO/.env" ]; then
    dir="$(sed -n 's/^\(export[[:space:]]\{1,\}\)\{0,1\}VEILLE_DATA_DIR=//p' "$REPO/.env" | tail -n 1)"
    dir="${dir%%[[:space:]]#*}"            # commentaire en fin de ligne
    dir="${dir#[\"\']}"; dir="${dir%[\"\']}"  # guillemets
fi
dir="${dir:-./data}"
case "$dir" in
    /*) ;;
    *) dir="$REPO/${dir#./}" ;;
esac
printf '%s\n' "${dir%/}"
