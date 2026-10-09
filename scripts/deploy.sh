#!/usr/bin/env bash
# deploy.sh — construit, démarre et vérifie le conteneur de veille.
#
# Usage :
#   ./scripts/deploy.sh              # build + sauvegarde + démarrage + vérifications
#   ./scripts/deploy.sh --init       # première installation : crée le dossier de données
#   ./scripts/deploy.sh --pull       # tire d'abord l'image amont (Hermes)
#   ./scripts/deploy.sh --no-build   # redémarre sans reconstruire
#
# Les données (veilles, rapports, jobs, historique) vivent dans VEILLE_DATA_DIR, hors de
# l'image : avant de démarrer la nouvelle version, elles sont sauvegardées (conteneur
# arrêté : archive cohérente) et inventoriées ; le déploiement échoue si une veille ou un
# rapport manque ensuite.
#
# Prérequis : docker + docker compose, et un fichier .env rempli.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

CONTAINER="veille-hermes"
PULL=0
BUILD=1
INIT=0
for arg in "$@"; do
    case "$arg" in
        --init)     INIT=1 ;;
        --pull)     PULL=1 ;;
        --no-build) BUILD=0 ;;
        -h|--help)  sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "Argument inconnu : $arg" >&2; exit 2 ;;
    esac
done

log()  { printf '\033[36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }

# Veilles (archivées comprises) et rapports PDF : l'application n'efface jamais rien,
# donc aucun des deux nombres ne doit baisser au fil d'un déploiement.
count() { { find "$DATA/veilles" "$@" -type f 2>/dev/null || true; } | wc -l; }
inventory() { echo "$(count -name veille.json) $(count -path '*/rapports/*' -name '*.pdf')"; }

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

DATA="$("$REPO_DIR/scripts/data-dir.sh")"
if [ ! -d "$DATA" ]; then
    if [ "$INIT" = 1 ]; then
        # --init ne doit jamais masquer des données existantes restées dans le dépôt.
        if [ "$DATA" != "$REPO_DIR/data" ] && [ -n "$(ls -A "$REPO_DIR/data" 2>/dev/null)" ]; then
            fail "des données existent dans $REPO_DIR/data : make move-data DEST=$DATA (pas --init)"
            exit 1
        fi
        mkdir -p "$DATA" && chmod 0750 "$DATA"  # propriétaire réglé par le hook amont
        ok "dossier de données créé : $DATA"
    else
        # Ne jamais repartir d'un dossier vide sans le vouloir (re-clone, mauvais dossier…).
        fail "dossier de données introuvable : $DATA"
        if [ "$DATA" != "$REPO_DIR/data" ] && [ -n "$(ls -A "$REPO_DIR/data" 2>/dev/null)" ]; then
            echo "  des données existent dans $REPO_DIR/data : make move-data DEST=$DATA" >&2
        fi
        echo "  première installation : ./scripts/deploy.sh --init" >&2
        exit 1
    fi
fi
case "$DATA/" in
    "$REPO_DIR"/*) warn "données DANS le dépôt ($DATA) : un re-clone ou un « git clean -x » les effacerait — make move-data DEST=/srv/veille-hermes/data" ;;
esac
ok "pré-vol OK (.env renseigné, données : $DATA)"
RC=0

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

# --- 4. Sauvegarde (conteneur arrêté : archive cohérente) ---
read -r v0 r0 <<< "$(inventory)"
ARCHIVE=""
if [ -n "$(ls -A "$DATA")" ]; then
    log "sauvegarde des données ($v0 veille(s), $r0 rapport(s) PDF)"
    docker compose stop
    if ! ARCHIVE="$("$REPO_DIR/scripts/backup.sh" | tail -n 1)"; then
        fail "sauvegarde impossible : déploiement annulé, ancienne version relancée"
        docker compose start
        exit 1
    fi
    ok "sauvegarde : $ARCHIVE"
fi

# --- 5. Démarrage ---
log "démarrage du conteneur"
START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker compose up -d

# --- 6. Attente de l'amorçage de CE démarrage (le marqueur .bootstrapped date du premier) ---
log "attente de l'amorçage (max 120 s)"
for i in $(seq 1 24); do
    boot_log="$(docker logs --since "$START" "$CONTAINER" 2>&1 || true)"
    if grep -q 'seed-veille\] amorçage terminé' <<< "$boot_log"; then
        if grep -q 'reconcile: .*ERREUR' <<< "$boot_log"; then
            fail "réconciliation en erreur : $(grep 'reconcile: .*ERREUR' <<< "$boot_log" | head -3)"
            RC=1
        else
            ok "amorçage terminé (veilles réconciliées)"
        fi
        break
    fi
    sleep 5
    if [ "$i" = 24 ]; then
        fail "amorçage non détecté après 120 s — voir : docker compose logs | grep seed-veille"
        RC=1
    fi
done

# Le conteneur monte-t-il bien CE dossier ? (sinon l'inventaire comparerait le mauvais)
mounted="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/opt/data"}}{{.Source}}{{end}}{{end}}' "$CONTAINER")"
if [ "$(realpath -m "$mounted")" != "$(realpath -m "$DATA")" ]; then
    fail "le conteneur monte « $mounted » au lieu de « $DATA » : vérifier VEILLE_DATA_DIR (.env, environnement)"
    exit 1
fi

# --- 7. Rien ne doit avoir disparu ---
read -r v1 r1 <<< "$(inventory)"
if [ "$v1" -lt "$v0" ] || [ "$r1" -lt "$r0" ]; then
    fail "données manquantes après déploiement : $v0 → $v1 veilles, $r0 → $r1 rapports PDF"
    [ -n "$ARCHIVE" ] && echo "  restauration : docker compose stop && tar -xzf '$ARCHIVE' -C '$DATA' && docker compose start" >&2
    exit 1
fi
ok "données conservées : $v1 veille(s), $r1 rapport(s) PDF"

# --- 8. Vérifications ---
echo
log "veilles et jobs"
if docker exec "$CONTAINER" veillectl list; then
    ok "veilles lisibles"
else
    fail "veillectl en échec — voir : docker compose logs | grep seed-veille"
    RC=1
fi

echo
log "interface web (127.0.0.1:8088)"
if grep -qE '^VEILLE_UI_PASSWORD_HASH=.+' .env; then
    for _ in 1 2 3 4 5; do  # le service démarre juste après l'amorçage
        code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8088/login || true)
        [ "$code" = 200 ] && break
        sleep 2
    done
    if [ "$code" = 200 ]; then ok "interface joignable (tunnel : make tunnel)"; else fail "interface injoignable (HTTP $code)"; RC=1; fi
else
    echo "  interface désactivée : génère un mot de passe avec  make ui-password"
fi

echo
if [ "$RC" != 0 ]; then
    fail "déploiement terminé AVEC DES ERREURS (données vérifiées intactes) — voir ci-dessus"
    exit "$RC"
fi
ok "déploiement terminé"
cat <<'EOF'

Prochaines étapes :
  make veilles                    # lister les veilles
  make run-now VEILLE=<slug>      # déclencher un rapport immédiatement (test de bout en bout)
  make tunnel                     # accès à l'interface web par tunnel SSH
  make logs                       # suivre les logs
EOF
