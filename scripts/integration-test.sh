#!/usr/bin/env bash
# integration-test.sh — test de bout en bout du conteneur COMPLET (s6, gateway, seed,
# interface) sur un volume à l'ancienne disposition (v0.2), puis le chien de garde de l'hôte.
#
#   IMAGE=ghcr.io/aquadrox/hermes-veille IMAGE_TAG=ci ./scripts/integration-test.sh
#
# Vérifie : migration (même job, historique conservé, lien de compatibilité),
# réconciliation idempotente au redémarrage, interface (session, CSRF, Host, pause /
# reprise effectives côté Hermes), mise à jour simulée (conteneur RECRÉÉ : veilles,
# rapports, jobs et historique conservés), garde-fou du dossier de données absent,
# watchdog (aucune alerte sur un volume sain).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-ghcr.io/aquadrox/hermes-veille}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
REF="${IMAGE}:${IMAGE_TAG}"
PORT="${PORT:-18088}"
NAME="veille-it-$$"
WORK="$(mktemp -d)"
DATA="$WORK/data"
PASSWORD="integration-password-123"
BASE="http://127.0.0.1:$PORT"

log()  { printf '\033[36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; docker logs --tail 60 "$NAME" >&2 2>/dev/null || true; exit 1; }
as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }
hx() { docker exec "$NAME" "$@"; }
hxu() { docker exec -u hermes "$NAME" "$@"; }  # écritures : fichiers à l'utilisateur hermes
# Jamais « cmd | grep -q » avec pipefail : grep -q ferme le tube tôt et le SIGPIPE
# de l'écrivain fait échouer le pipeline au hasard. On capture, puis on cherche.
contains() { grep -qE -- "$2" <<< "$1"; }

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    as_root rm -rf "$WORK"
}
trap cleanup EXIT

# --- 1. Volume à l'ancienne disposition + job existant ---
log "volume v0.2 : data/veille/ + job « Veille technologique hebdomadaire »"
mkdir -p "$DATA/veille/rapports"
cp "$REPO_DIR/veille/templates/industrie-4-0.md" "$DATA/veille/config.md"
echo "boss@example.org" > "$DATA/veille/destinataire.txt"
echo "ancien prompt" > "$DATA/veille/job-prompt.md"
touch "$DATA/veille/.seeded"
head -c 40000 /dev/urandom > "$DATA/veille/rapports/$(date +%Y-%m-%d)-veille.pdf"
as_root chown -R 10000:10000 "$DATA"
docker run --rm -v "$DATA:/opt/data" -e HERMES_HOME=/opt/data --user 10000:10000 \
    --entrypoint /opt/hermes/.venv/bin/hermes "$REF" \
    cron create "every monday 8am" "ancien prompt" --name "Veille technologique hebdomadaire" \
    --skill brevo-email --skill styled-pdf-report --workdir /opt/data/veille --deliver local >/dev/null
LEGACY_JOB="$(as_root python3 -c "import json,sys; d=json.load(open(sys.argv[1])); j=d['jobs'] if isinstance(d,dict) else d; print(j[0]['id'])" "$DATA/cron/jobs.json")"
ok "job existant : $LEGACY_JOB"

# --- 2. Secrets de test ---
HASH="$(docker run --rm --entrypoint /opt/veille-app/.venv/bin/python -w /opt/veille-app "$REF" \
    -c "from veille_ui.auth import hash_password; print(hash_password('$PASSWORD'))")"
cat > "$WORK/.env" <<ENV
DEEPSEEK_API_KEY=test
BREVO_API_KEY=test
BREVO_SENDER_EMAIL=sender@example.org
ALERT_EMAIL=ops@example.org
VEILLE_UI_PASSWORD_HASH='$HASH'
VEILLE_UI_SECRET='$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')'
ENV

# --- 3. Démarrage complet ---
start() {
    docker run -d --name "$NAME" --env-file "$WORK/.env" -e TZ=Europe/Paris \
        -v "$DATA:/opt/data" -p "127.0.0.1:$PORT:8088" "$REF" gateway run >/dev/null
    for _ in $(seq 1 60); do
        if hx test -f /opt/data/veilles/.bootstrapped 2>/dev/null \
            && curl -fs "$BASE/healthz" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    fail "conteneur non prêt après 120 s"
}
log "démarrage du conteneur complet"
start
ok "conteneur prêt (seed terminé, interface joignable)"

# --- 4. Migration ---
log "migration"
contains "$(hx veillectl list)" '^industrie-4-0 +actif' || fail "veille migrée absente ou inactive"
META="$DATA/veilles/industrie-4-0/veille.json"
as_root grep -q "\"job_id\": \"$LEGACY_JOB\"" "$META" || fail "job non conservé (attendu $LEGACY_JOB)"
as_root test -L "$DATA/veille" || fail "lien de compatibilité data/veille absent"
contains "$(as_root ls "$DATA/veilles/industrie-4-0/rapports/")" '-veille\.pdf$' || fail "historique non migré"
as_root test -f "$DATA/veilles/industrie-4-0/recipients.txt" || fail "recipients.txt absent"
contains "$(hx hermes cron list)" 'industrie-4-0' || fail "job non renommé"
ok "même job, historique et destinataires conservés, lien data/veille en place"

# --- 5. Interface ---
log "interface web"
JAR="$WORK/cookies"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")" = 303 ] || fail "/ accessible sans session"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example' "$BASE/login")" = 400 ] || fail "Host étranger accepté"
[ "$(curl -s -o /dev/null -w '%{http_code}' -d password=faux "$BASE/login")" = 401 ] || fail "mauvais mot de passe accepté"
[ "$(curl -s -c "$JAR" -o /dev/null -w '%{http_code}' -d "password=$PASSWORD" "$BASE/login")" = 303 ] || fail "connexion refusée"
CSRF="$(curl -s -b "$JAR" "$BASE/" | grep -o 'name="csrf" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"$//')"
[ -n "$CSRF" ] || fail "jeton CSRF introuvable"
[ "$(curl -s -b "$JAR" -o /dev/null -w '%{http_code}' -X POST "$BASE/veilles/industrie-4-0/pause")" = 403 ] || fail "POST sans CSRF accepté"
job_state() {  # état du job selon jobs.json (source de vérité de Hermes)
    as_root python3 -c "import json,sys; d=json.load(open(sys.argv[1])); j=(d['jobs'] if isinstance(d,dict) else d)[0]; print('paused' if (j.get('state')=='paused' or j.get('paused_at') or j.get('enabled') is False) else 'active')" "$DATA/cron/jobs.json"
}
code="$(curl -s -b "$JAR" -o "$WORK/r.html" -w '%{http_code}' -d "csrf=$CSRF" "$BASE/veilles/industrie-4-0/pause")"
[ "$code" = 303 ] || fail "pause : HTTP $code — $(grep -o 'flash err">[^<]*' "$WORK/r.html")"
[ "$(job_state)" = paused ] || fail "pause sans effet côté Hermes"
code="$(curl -s -b "$JAR" -o "$WORK/r.html" -w '%{http_code}' -d "csrf=$CSRF" "$BASE/veilles/industrie-4-0/resume")"
[ "$code" = 303 ] || fail "reprise : HTTP $code — $(grep -o 'flash err">[^<]*' "$WORK/r.html")"
[ "$(job_state)" = active ] || fail "reprise sans effet côté Hermes"
contains "$(curl -s -b "$JAR" "$BASE/")" "veille-hermes v$(tr -d '[:space:]' < "$REPO_DIR/VERSION")" || fail "version absente du pied de page"
ok "session, CSRF, Host, pause/reprise effectives, version affichée"

# --- 6. Redémarrage : réconciliation idempotente ---
log "redémarrage (réconciliation idempotente)"
docker restart "$NAME" >/dev/null
sleep 5
for _ in $(seq 1 60); do curl -fs "$BASE/healthz" >/dev/null 2>&1 && break; sleep 2; done
contains "$(docker logs "$NAME" 2>&1)" 'reconcile: industrie-4-0: ok' || fail "réconciliation non idempotente"
N_JOBS="$(as_root python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(len(d['jobs'] if isinstance(d,dict) else d))" "$DATA/cron/jobs.json")"
[ "$N_JOBS" = 1 ] || fail "$N_JOBS jobs après redémarrage (doublon ?)"
ok "aucun doublon, veille réconciliée « ok »"

# --- 6b. Mise à jour simulée : conteneur RECRÉÉ sur le même dossier (= `up -d` d'une image neuve) ---
log "mise à jour simulée (conteneur supprimé puis recréé)"
contains "$(hxu veillectl create --name 'Veille Persistance' --schedule '0 7 * * 2' \
    --recipients ops@example.org --template vierge)" 'veille créée' || fail "création de veille impossible"
for day in 2026-09-21 2026-09-28 2026-10-05; do
    hxu sh -c "head -c 20000 /dev/urandom > /opt/data/veilles/veille-persistance/rapports/$day-veille.pdf"
done
snapshot() {  # tout ce qui doit survivre : veilles + jobs, rapports, journal d'audit
    as_root sh -c "cd '$DATA' && cat veilles/*/veille.json cron/jobs.json | grep -E '\"(slug|job_id|id|name|schedule)\"' | grep -v updated; ls veilles/*/rapports/ | sort; wc -l < veilles/.audit.log"
}
BEFORE="$(snapshot)"
docker rm -f "$NAME" >/dev/null
start
[ "$(snapshot)" = "$BEFORE" ] || fail "état modifié par la recréation du conteneur : $(diff <(echo "$BEFORE") <(snapshot))"
contains "$(docker logs "$NAME" 2>&1)" 'reconcile: veille-persistance: ok' || fail "veille non réconciliée « ok » après recréation"
contains "$(curl -s -b "$JAR" "$BASE/veilles/veille-persistance")" '2026-09-21-veille\.pdf' || fail "rapports anciens absents de l'interface"
contains "$(curl -s -b "$JAR" "$BASE/")" 'veille-persistance#rapports">3 rapports' || fail "nombre de rapports absent du tableau de bord"
hxu veillectl archive veille-persistance >/dev/null
ok "veilles, jobs, rapports et audit identiques après recréation ; historique visible"

# --- 6c. Dossier de données absent : refus de démarrer (jamais d'état vide silencieux) ---
log "dossier de données absent : docker compose doit refuser de démarrer"
compose_dir="$WORK/compose"
mkdir -p "$compose_dir"
cp "$REPO_DIR/docker-compose.yml" "$compose_dir/"
printf 'VEILLE_DATA_DIR=%s\nIMAGE=%s\nIMAGE_TAG=%s\nVEILLE_UI_PORT=%s\n' \
    "$WORK/absent" "$IMAGE" "$IMAGE_TAG" "$((PORT + 1))" > "$compose_dir/.env"
if out="$(docker compose --project-directory "$compose_dir" -p "veille-it-guard-$$" up -d --no-build 2>&1)"; then
    docker compose --project-directory "$compose_dir" -p "veille-it-guard-$$" down >/dev/null 2>&1 || true
    fail "démarrage accepté sur un dossier de données absent : $out"
fi
contains "$out" "$WORK/absent" || fail "refus pour une autre raison que le dossier absent : $out"
[ ! -e "$WORK/absent" ] || fail "dossier de données créé en silence"
ok "démarrage refusé, aucun dossier vide créé"

# --- 7. MCP depuis l'hôte (comme le Hermes de Telegram : stdio via docker exec -i) ---
log "MCP via docker exec -i (lecture, modification, audit)"
mcp_call() {  # $1 = outil, $2 = arguments JSON ; renvoie la réponse JSON-RPC de l'appel
    { printf '%s\n' \
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"integration","version":"1"}}}' \
        '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
        "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}"
      sleep 6; } | docker exec -i -u hermes -e VEILLE_MCP_ACTOR=telegram -e VEILLE_HOST_DATA="$DATA" "$NAME" veille-mcp 2>/dev/null \
      | grep '"id":2'
}
out="$(mcp_call list_veilles '{}')"
contains "$out" 'industrie-4-0' || fail "MCP list_veilles : $out"
out="$(mcp_call pause_veille '{"slug":"industrie-4-0"}')"
contains "$out" '"isError":false' || fail "MCP pause_veille : $out"
[ "$(job_state)" = paused ] || fail "pause MCP sans effet côté Hermes"
out="$(mcp_call resume_veille '{"slug":"industrie-4-0"}')"
[ "$(job_state)" = active ] || fail "reprise MCP sans effet côté Hermes : $out"
out="$(mcp_call archive_veille '{"slug":"industrie-4-0","confirm_slug":"non"}')"
contains "$out" '"isError":true' || fail "archivage MCP sans confirmation accepté : $out"
contains "$(as_root tail -3 "$DATA/veilles/.audit.log")" '"actor": "telegram", "action": "resume"' || fail "audit MCP absent"
out="$(mcp_call list_reports '{"slug":"industrie-4-0"}')"
contains "$out" "$DATA/veilles/industrie-4-0/rapports/" || fail "chemin hôte du PDF absent : $out"
out="$(mcp_call get_costs '{"slug":"industrie-4-0"}')"
contains "$out" '"isError":false' || fail "MCP get_costs : $out"
contains "$(curl -s -b "$JAR" "$BASE/veilles/industrie-4-0")" 'Aucune exécution mesurée' || fail "section coûts absente"
ok "lecture, pause/reprise effectives, confirmation d'archivage exigée, audit « telegram », chemins hôte, coûts"

# --- 8. Chien de garde de l'hôte ---
log "chien de garde (hôte, dry-run)"
as_root python3 "$REPO_DIR/scripts/watchdog.py" --volume "$DATA" --container "$NAME" \
    --env-file "$WORK/.env" --state-file "$WORK/state.json" --dry-run \
    || fail "le chien de garde signale un problème sur un volume sain"
ok "chien de garde : tout va bien"

echo
ok "test d'intégration réussi"
