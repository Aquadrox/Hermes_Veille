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
log "attente du démarrage du gateway (max 120 s)"
for i in $(seq 1 24); do
    if docker exec "$CONTAINER" test -f /opt/data/veille/.seeded 2>/dev/null; then
        ok "amorçage détecté (assets + job)"
        break
    fi
    sleep 5
    [ "$i" = 24 ] && fail "amorçage non détecté après 120 s — voir : docker compose logs"
done

# --- 6. Vérifications ---
echo
log "vérification du job cron"
if docker exec "$CONTAINER" hermes cron list 2>/dev/null | grep -q "Veille technologique"; then
    docker exec "$CONTAINER" hermes cron list 2>/dev/null | grep -A6 "Veille technologique" | head -10
    ok "job présent"
else
    fail "job absent — vérifie les logs d'amorçage : docker compose logs | grep seed-veille"
fi

echo
log "assets sur le volume"
docker exec "$CONTAINER" ls -la /opt/data/veille/ 2>/dev/null || true

echo
ok "déploiement terminé"
cat <<'EOF'

Prochaines étapes :
  make run-now     # déclencher un rapport immédiatement (test de bout en bout)
  make logs        # suivre les logs
  make verify      # revérifier l'état

⚠️  Si le job tourne déjà sur l'hôte, désactive-le pour éviter un double envoi :
      hermes cron pause <id>
EOF
