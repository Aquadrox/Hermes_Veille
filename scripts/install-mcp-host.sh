#!/usr/bin/env bash
# install-mcp-host.sh — branche le serveur MCP des veilles sur le Hermes de l'HÔTE
# (celui qui porte Telegram), pour piloter les veilles en conversation.
#
# Transport stdio via `docker exec -i` : aucun port ouvert. Le serveur est déclaré
# `trust: untrusted` : seuls les outils de lecture s'exécutent sans approbation, toute
# modification demande ton accord dans Telegram.
#
# Usage :
#   ./scripts/install-mcp-host.sh              # configure, teste, rappelle le redémarrage
#   ./scripts/install-mcp-host.sh --readonly   # outils de lecture uniquement
#   ./scripts/install-mcp-host.sh --print      # affiche la configuration YAML sans rien modifier
#   ./scripts/install-mcp-host.sh --uninstall
#   NAME=veilles CONTAINER=veille-hermes HERMES=hermes ./scripts/install-mcp-host.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="${NAME:-veilles}"
CONTAINER="${CONTAINER:-veille-hermes}"
HERMES="${HERMES:-hermes}"
ACTOR="${ACTOR:-telegram}"
READONLY=0
MODE=install

for arg in "$@"; do
    case "$arg" in
        --readonly)  READONLY=1 ;;
        --print)     MODE=print ;;
        --uninstall) MODE=uninstall ;;
        -h|--help)   sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "option inconnue : $arg" >&2; exit 2 ;;
    esac
done

ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Arguments de `docker exec` : utilisateur hermes (fichiers du volume), origine des actions
# pour le journal d'audit, et chemin hôte des données (pièces jointes PDF envoyables).
ARGS=(exec -i -u hermes -e "VEILLE_MCP_ACTOR=$ACTOR" -e "VEILLE_HOST_DATA=$REPO/data" "$CONTAINER" veille-mcp)
[ "$READONLY" = 1 ] && ARGS+=(--readonly)
ARGS_JSON="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${ARGS[@]}")"

if [ "$MODE" = print ]; then
    cat <<YAML
# À ajouter dans ~/.hermes/config.yaml (Hermes de l'hôte), puis : $HERMES gateway restart
mcp_servers:
  $NAME:
    command: docker
    args: $ARGS_JSON
    trust: untrusted        # toute modification demande une approbation (Telegram)
    timeout: 120
    connect_timeout: 60
YAML
    exit 0
fi

command -v "$HERMES" >/dev/null || fail "CLI « $HERMES » introuvable (HERMES=/chemin/vers/hermes)"

if [ "$MODE" = uninstall ]; then
    "$HERMES" mcp remove "$NAME"
    ok "serveur MCP « $NAME » retiré — redémarre le gateway : $HERMES gateway restart"
    exit 0
fi

command -v docker >/dev/null || fail "docker introuvable"
[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = true ] \
    || fail "conteneur « $CONTAINER » arrêté — make up"
docker exec -u hermes "$CONTAINER" veille-mcp --check >/dev/null \
    || fail "veille-mcp indisponible dans le conteneur (image antérieure à 0.2.3 ? make update)"
ok "veille-mcp disponible dans « $CONTAINER »"

"$HERMES" config set "mcp_servers.$NAME.command" docker >/dev/null
"$HERMES" config set "mcp_servers.$NAME.args" "$ARGS_JSON" >/dev/null
"$HERMES" config set "mcp_servers.$NAME.trust" untrusted >/dev/null
"$HERMES" config set "mcp_servers.$NAME.timeout" 120 >/dev/null
"$HERMES" config set "mcp_servers.$NAME.connect_timeout" 60 >/dev/null
ok "configuration écrite (mcp_servers.$NAME, trust: untrusted$([ "$READONLY" = 1 ] && echo ', lecture seule'))"

"$HERMES" mcp test "$NAME" || fail "test de connexion en échec"

cat <<EOF2

Dernière étape : redémarrer le gateway pour charger les outils :
    $HERMES gateway restart

Puis, dans Telegram, par exemple :
    « Liste mes veilles »
    « Mets la veille industrie-4-0 en pause »
    « Ajoute direction@exemple.fr aux destinataires de la veille industrie-4-0 »
    « Résume le dernier rapport de la veille industrie-4-0 »
EOF2
