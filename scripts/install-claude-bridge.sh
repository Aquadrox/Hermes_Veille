#!/usr/bin/env bash
# install-claude-bridge.sh — installe sur l'HÔTE le pont Claude Code des veilles.
#
# Option « recherche déléguée à Claude » d'une veille (désactivée par défaut) : l'agent Hermes rédige
# toujours le rapport, mais la recherche web est faite par `claude -p` sur l'hôte, qui renvoie un JSON
# sourcé (conservé dans veilles/<slug>/recherches/). Le conteneur ne voit ni le binaire ni les
# identifiants de Claude : il passe par une socket Unix, /run/veille-claude-bridge/bridge.sock, dans un dossier
# au service (systemd) monté en lecture seule dans le conteneur (/opt/claude-bridge, docker-compose.yml) ;
# aucun port n'est ouvert. Jamais dans le dossier de données : le conteneur peut le modifier.
#
# Prérequis : Claude Code installé et connecté (`claude auth status`) pour l'utilisateur CLAUDE_USER.
#
# Usage :
#   sudo ./scripts/install-claude-bridge.sh              # installe, démarre et vérifie le pont
#   sudo ./scripts/install-claude-bridge.sh --test       # … puis une courte recherche réelle (quota Claude)
#   sudo CLAUDE_USER=exploitant ./scripts/install-claude-bridge.sh   # compte où Claude Code est connecté
#   sudo MAX_PER_DAY=40 TIMEOUT=1800 MODEL=sonnet ./scripts/install-claude-bridge.sh
#   ./scripts/install-claude-bridge.sh --check           # état du pont (sans rien modifier)
#   sudo ./scripts/install-claude-bridge.sh --uninstall
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
UNIT_DIR="/etc/systemd/system"
SERVICE="veille-claude-bridge.service"
CONTAINER="${CONTAINER:-veille-hermes}"
CLAUDE_USER="${CLAUDE_USER:-root}"
MAX_PER_DAY="${MAX_PER_DAY:-20}"
TIMEOUT="${TIMEOUT:-1200}"
MODEL="${MODEL:-}"
MODE=install
DO_TEST=0

for arg in "$@"; do
    case "$arg" in
        --uninstall) MODE=uninstall ;;
        --check)     MODE=check ;;
        --test)      DO_TEST=1 ;;
        -h|--help)   sed -n '2,21p' "$0"; exit 0 ;;
        *) echo "option inconnue : $arg" >&2; exit 2 ;;
    esac
done

ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

DATA="$("$REPO/scripts/data-dir.sh")"
SOCK_DIR="/run/veille-claude-bridge"  # RuntimeDirectory de l'unité ; monté dans le conteneur (docker-compose.yml)
SOCK="$SOCK_DIR/bridge.sock"

health() {
    curl -sf --max-time 5 --unix-socket "$SOCK" http://bridge/health
}

check() {
    if systemctl is-active --quiet "$SERVICE"; then ok "service $SERVICE actif"; else warn "service $SERVICE inactif"; fi
    if out="$(health)"; then
        ok "pont joignable sur $SOCK : $out"
    else
        fail "pont injoignable sur $SOCK (journalctl -u $SERVICE)"
    fi
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
        if docker exec -u hermes "$CONTAINER" veillectl research status; then
            ok "pont joignable depuis le conteneur $CONTAINER"
        else
            warn "conteneur $CONTAINER : pont injoignable (image ou docker-compose.yml antérieurs à la v0.9.0 ? docker compose up -d)"
        fi
    else
        warn "conteneur $CONTAINER arrêté : vérification depuis le conteneur sautée"
    fi
}

case "$MODE" in
    uninstall)
        systemctl disable --now "$SERVICE" 2>/dev/null || true
        rm -f "$UNIT_DIR/$SERVICE" "$SOCK"
        systemctl daemon-reload
        ok "pont Claude désinstallé (résultats conservés dans /var/lib/veille-claude-bridge)"
        exit 0 ;;
    check)
        check
        exit 0 ;;
esac

[ "$(id -u)" = 0 ] || fail "à lancer en root (sudo) : unité systemd du pont"
command -v systemctl >/dev/null || fail "systemd requis"
command -v python3 >/dev/null || fail "python3 requis sur l'hôte"
command -v curl >/dev/null || fail "curl requis (vérification du pont)"
[ -f "$REPO/scripts/claude-bridge.py" ] || fail "claude-bridge.py introuvable dans $REPO"
[ -d "$DATA" ] || fail "dossier de données introuvable : $DATA (./scripts/deploy.sh --init)"
[[ "$MAX_PER_DAY" =~ ^[0-9]+$ && "$TIMEOUT" =~ ^[0-9]+$ ]] || fail "MAX_PER_DAY et TIMEOUT : nombres entiers"
[[ "$MODEL" =~ ^[A-Za-z0-9._:-]*$ ]] || fail "MODEL : caractères non pris en charge"

# Compte où Claude Code est connecté : son HOME porte les identifiants (~/.claude), jamais copiés ailleurs.
CLAUDE_HOME="$(getent passwd "$CLAUDE_USER" | cut -d: -f6)"
[ -n "$CLAUDE_HOME" ] || fail "utilisateur inconnu : $CLAUDE_USER"
CLAUDE_BIN="${CLAUDE_BIN:-}"
if [ -z "$CLAUDE_BIN" ]; then
    for c in "$CLAUDE_HOME/.local/bin/claude" "$(sudo -u "$CLAUDE_USER" -H bash -lc 'command -v claude' 2>/dev/null || true)"; do
        [ -n "$c" ] && [ -x "$c" ] && { CLAUDE_BIN="$(readlink -f "$c")"; break; }
    done
fi
[ -n "$CLAUDE_BIN" ] && [ -x "$CLAUDE_BIN" ] || fail "binaire claude introuvable pour $CLAUDE_USER (CLAUDE_BIN=/chemin/claude)"
ok "Claude Code : $CLAUDE_BIN ($(sudo -u "$CLAUDE_USER" -H "$CLAUDE_BIN" --version 2>/dev/null || echo 'version ?'))"
if sudo -u "$CLAUDE_USER" -H "$CLAUDE_BIN" auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
    ok "Claude Code connecté pour $CLAUDE_USER"
else
    fail "Claude Code n'est pas connecté pour $CLAUDE_USER : lance « claude » une fois sous ce compte (/login)"
fi

# Groupe de la socket : celui du dossier de données, c.-à-d. de l'utilisateur hermes du conteneur (lu, jamais modifié).
HERMES_GID="$(stat -c '%g' "$DATA")"
for p in "$REPO" "$CLAUDE_BIN" "$CLAUDE_HOME"; do
    [[ "$p" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "chemin non pris en charge par l'unité systemd : $p"
done
[[ "$CLAUDE_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || fail "nom d'utilisateur non pris en charge : $CLAUDE_USER"

# Aucun dossier créé ni modifié ici : systemd crée celui de la socket (RuntimeDirectory), au compte du service.
sed -e "s|^ExecStart=.*|ExecStart=/usr/bin/python3 $REPO/scripts/claude-bridge.py --socket $SOCK --group $HERMES_GID --claude $CLAUDE_BIN|" \
    -e "s|^User=.*|User=$CLAUDE_USER|" \
    -e "s|^Environment=HOME=.*|Environment=HOME=$CLAUDE_HOME|" \
    -e "s|^Environment=CLAUDE_BRIDGE_MAX_PER_DAY=.*|Environment=CLAUDE_BRIDGE_MAX_PER_DAY=$MAX_PER_DAY|" \
    -e "s|^Environment=CLAUDE_BRIDGE_TIMEOUT=.*|Environment=CLAUDE_BRIDGE_TIMEOUT=$TIMEOUT|" \
    -e "s|^Environment=CLAUDE_BRIDGE_MODEL=.*|Environment=CLAUDE_BRIDGE_MODEL=$MODEL|" \
    "$REPO/deploy/systemd/$SERVICE" > "$UNIT_DIR/$SERVICE"
if [ "$CLAUDE_USER" != root ]; then
    # Un compte non root ne peut pas changer le groupe de la socket : il prend celui de hermes comme groupe principal.
    sed -i -e "s| --group $HERMES_GID||" "$UNIT_DIR/$SERVICE"
    sed -i -e "/^User=/a Group=$HERMES_GID" "$UNIT_DIR/$SERVICE"
fi
chmod +x "$REPO/scripts/claude-bridge.py"

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"
for _ in $(seq 1 20); do
    [ -S "$SOCK" ] && health >/dev/null && break
    sleep 0.5
done
ok "pont installé : $SERVICE ($CLAUDE_USER, $MAX_PER_DAY recherches/jour, $TIMEOUT s max)"
check

if [ "$DO_TEST" = 1 ]; then
    echo
    echo "▸ recherche de test (une requête web, quota Claude)"
    id="test-$(date +%Y%m%d-%H%M%S)"
    curl -sf --unix-socket "$SOCK" -H 'Content-Type: application/json' http://bridge/jobs -d "{\"id\":\"$id\",
        \"prompt\":\"Cherche sur le web (WebSearch) la dernière version stable de Python. Réponds selon le schéma.\",
        \"schema\":{\"type\":\"object\",\"properties\":{\"version\":{\"type\":\"string\"},\"url\":{\"type\":\"string\"}},
        \"required\":[\"version\",\"url\"]}}" >/dev/null || fail "recherche de test refusée"
    for _ in $(seq 1 60); do
        out="$(curl -sf --unix-socket "$SOCK" "http://bridge/jobs/$id")"
        case "$out" in *'"state": "done"'*|*'"state": "error"'*) break ;; esac
        sleep 5
    done
    python3 -c 'import json,sys; j=json.loads(sys.argv[1]); r=j.get("result") or {}
print(j["state"], j.get("error") or "", json.dumps(r.get("structured_output"), ensure_ascii=False),
      "coût équivalent API : %s $" % r.get("total_cost_usd"))' "$out"
fi

echo
echo "Ensuite : activer l'option par veille — interface (Modifier → « Recherche déléguée à Claude »),"
echo "MCP (update_veille claude_research=true) ou : docker exec -u hermes $CONTAINER veillectl edit <slug> --claude-research on"
