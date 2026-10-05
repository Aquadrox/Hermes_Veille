#!/usr/bin/env bash
# deploy.sh — construit, démarre et vérifie le conteneur de veille.
#
# Usage :
#   ./scripts/deploy.sh              # build + démarrage + vérifications
#   ./scripts/deploy.sh --pull       # tire d'abord l'image amont (Hermes)
#   ./scripts/deploy.sh --no-build   # redémarre sans reconstruire
#
# Prérequis : docker + docker compose, et un fichier .env rempli.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

CONTAINER="veille-hermes"
PULL=0
BUILD=1
for arg in "$@"; do
    case "$arg" in
        --pull)     PULL=1 ;;
        --no-build) BUILD=0 ;;
        -h|--help)  sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Argument inconnu : $arg" >&2; exit 2 ;;
    esac
done

log()  { printf '\033[36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; }

# --- 1. Pré-vol ---
command -v docker >/dev/null || { fail "docker introuvable"; exit 1; }
docker compose version >/dev/null 2>&1 || { fail "docker compose introuvable"; exit 1; }

if [ ! -f .env ]; then
    fail "fichier .env absent — copie .env.example et remplis les secrets"
    exit 1
fi
if grep -qE '^(DEEPSEEK_API_KEY|BREVO_API_KEY)=$' .env; then
    fail "des secrets sont vides dans .env (DEEPSEEK_API_KEY / BREVO_API_KEY)"
    exit 1
fi
ok "pré-vol OK (.env présent et renseigné)"

# --- 2. Image amont (optionnel) ---
if [ "$PULL" = 1 ]; then
    log "tirage de l'image amont Hermes"
    docker compose pull --ignore-buildable 2>/dev/null || true
fi

# --- 3. Build ---
if [ "$BUILD" = 1 ]; then
    log "construction de l'image dérivée"
    docker compose build
fi

# --- 4. Démarrage ---
log "démarrage du conteneur"
docker compose up -d

# --- 5. Attente de l'amorçage ---
log "attente de l'amorçage (max 120 s)"
for i in $(seq 1 24); do
    if docker exec "$CONTAINER" test -f /opt/data/veilles/.bootstrapped 2>/dev/null; then
        ok "amorçage détecté (veilles réconciliées)"
        break
    fi
    sleep 5
    [ "$i" = 24 ] && fail "amorçage non détecté après 120 s — voir : docker compose logs | grep seed-veille"
done

# --- 6. Vérifications ---
echo
log "veilles et jobs"
if docker exec "$CONTAINER" veillectl list; then
    ok "veilles lisibles"
else
    fail "veillectl en échec — voir : docker compose logs | grep seed-veille"
fi

echo
log "interface web (127.0.0.1:8088)"
if grep -qE '^VEILLE_UI_PASSWORD_HASH=.+' .env; then
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8088/login || true)
    if [ "$code" = 200 ]; then ok "interface joignable (tunnel : make tunnel)"; else fail "interface injoignable (HTTP $code)"; fi
else
    echo "  interface désactivée : génère un mot de passe avec  make ui-password"
fi

echo
ok "déploiement terminé"
cat <<'EOF'

Prochaines étapes :
  make veilles                    # lister les veilles
  make run-now VEILLE=<slug>      # déclencher un rapport immédiatement (test de bout en bout)
  make tunnel                     # accès à l'interface web par tunnel SSH
  make logs                       # suivre les logs
EOF
