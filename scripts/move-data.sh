#!/usr/bin/env bash
# move-data.sh — sort le dossier de données du dépôt git, sans rien perdre.
#
#   ./scripts/move-data.sh /srv/veille-hermes/data       (make move-data DEST=…)
#
# 1. arrête le conteneur ;
# 2. déplace les données : renommage atomique sur le même système de fichiers, sinon
#    copie (cp -a, vers <dest>.partial puis renommée) et l'original est RENOMMÉ
#    <source>.moved-<date> — jamais supprimé. En cas d'échec, l'ancienne configuration
#    est relancée ;
# 3. écrit VEILLE_DATA_DIR dans .env ;
# 4. réinstalle le chien de garde s'il est installé (il lit les données sur l'hôte) ;
# 5. redéploie (sauvegarde, inventaire et vérifications de deploy.sh).
#
# Source : le dossier actuellement configuré ; s'il n'existe pas encore ou s'il est déjà
# la destination (VEILLE_DATA_DIR réglé à la main avant de déplacer), ./data du dépôt.
#
# À l'intérieur du conteneur rien ne change (/opt/data) : aucune donnée n'est réécrite.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

DEST="${1:-}"
DEST="${DEST%/}"
# Chemin absolu et sans caractère spécial : il est écrit dans .env et dans l'unité systemd.
[[ "$DEST" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "usage : $0 /chemin/absolu/des/donnees (lettres, chiffres, . _ - /)"
[ -f .env ] || fail ".env absent"
SRC="$("$REPO/scripts/data-dir.sh")"
if { [ "$SRC" = "$DEST" ] || [ ! -d "$SRC" ]; } && [ -n "$(ls -A "$REPO/data" 2>/dev/null)" ]; then
    SRC="$REPO/data"
fi
[ "$SRC" != "$DEST" ] || { ok "les données sont déjà dans $DEST"; exit 0; }
[ -d "$SRC" ] || fail "dossier de données introuvable : $SRC"
case "$DEST/" in "$SRC"/*) fail "la destination ne peut pas être dans la source ($SRC)" ;; esac
[ ! -e "$DEST.partial" ] || fail "copie interrompue précédente : vérifier puis supprimer $DEST.partial"
if [ -e "$DEST" ]; then
    { [ -d "$DEST" ] && [ -z "$(ls -A "$DEST")" ]; } || fail "la destination existe et n'est pas vide : $DEST"
    rmdir "$DEST"
fi

echo "▸ arrêt du conteneur"
docker compose down
# Échec avant la bascule de .env : les données sont intactes à l'ancien emplacement.
trap 'rm -rf "$DEST.partial"; echo "✗ échec : ancienne configuration relancée" >&2; docker compose up -d' ERR

mkdir -p "$(dirname "$DEST")"
if [ "$(stat -c %d "$SRC")" = "$(stat -c %d "$(dirname "$DEST")")" ]; then
    mv -T "$SRC" "$DEST"
    ok "données déplacées : $SRC → $DEST"
else
    cp -a -T "$SRC" "$DEST.partial"   # copie interrompue : jamais prise pour une destination valide
    mv -T "$DEST.partial" "$DEST"
    mv -T "$SRC" "$SRC.moved-$(date +%Y%m%d-%H%M%S)"
    ok "données copiées vers $DEST (original conservé : $SRC.moved-*, à supprimer à la main)"
fi

if grep -q '^VEILLE_DATA_DIR=' .env; then
    sed -i "s|^VEILLE_DATA_DIR=.*|VEILLE_DATA_DIR=$DEST|" .env
else
    printf '\n# Dossier des données (hors du dépôt git)\nVEILLE_DATA_DIR=%s\n' "$DEST" >> .env
fi
trap - ERR
ok "VEILLE_DATA_DIR=$DEST écrit dans .env"
export VEILLE_DATA_DIR="$DEST"   # prime sur une éventuelle valeur héritée du shell

if [ -f /etc/systemd/system/veille-watchdog.service ]; then
    "$REPO/scripts/install-watchdog.sh"
fi
"$REPO/scripts/deploy.sh" --no-build
echo
echo "Si le serveur MCP est branché sur le Hermes de l'hôte (chemins des PDF) : make mcp-install"
