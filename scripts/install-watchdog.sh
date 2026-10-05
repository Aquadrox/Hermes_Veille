#!/usr/bin/env bash
# install-watchdog.sh — installe la surveillance de la veille sur l'HÔTE.
#
# Le chien de garde tourne sur l'hôte, HORS du conteneur : un contrôleur placé
# dans ce qu'il surveille ne pourrait signaler ni un conteneur arrêté, ni une
# clé d'API expirée. Il est en outre déterministe (aucun LLM), donc il alerte
# encore quand la production, elle, est en panne.
#
# Usage :
#   sudo ./scripts/install-watchdog.sh              # installe et active le timer
#   REPO=/autre/chemin ./scripts/install-watchdog.sh
#   ./scripts/install-watchdog.sh --test            # envoie aussi un e-mail de test
#   ./scripts/install-watchdog.sh --uninstall
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
UNIT_DIR="/etc/systemd/system"
SERVICE="veille-watchdog.service"
TIMER="veille-watchdog.timer"
DO_TEST=0

for arg in "$@"; do
    case "$arg" in
        --uninstall) systemctl disable --now "$TIMER" 2>/dev/null || true
                     rm -f "$UNIT_DIR/$SERVICE" "$UNIT_DIR/$TIMER"
                     systemctl daemon-reload
                     echo "✓ surveillance désinstallée"
                     exit 0 ;;
        --test)      DO_TEST=1 ;;
        *)           echo "option inconnue : $arg" >&2; exit 2 ;;
    esac
done

[ -f "$REPO/scripts/watchdog.py" ] || { echo "✗ watchdog.py introuvable dans $REPO" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "✗ systemd requis" >&2; exit 1; }

install -d -m 0755 "$UNIT_DIR"

# Le service est généré pour pointer sur CE dépôt (le fichier versionné utilise
# le chemin par défaut /root/veille-hermes).
sed "s|/root/veille-hermes/scripts/watchdog.py|$REPO/scripts/watchdog.py|" \
    "$REPO/deploy/systemd/$SERVICE" > "$UNIT_DIR/$SERVICE"
install -m 0644 "$REPO/deploy/systemd/$TIMER" "$UNIT_DIR/$TIMER"
chmod +x "$REPO/scripts/watchdog.py"

# État persistant (anti-spam : n'alerte qu'au changement d'état).
install -d -m 0755 /var/lib/veille-watchdog

systemctl daemon-reload
systemctl enable --now "$TIMER" >/dev/null

echo "✓ surveillance installée et active"
systemctl list-timers "$TIMER" --no-pager | head -3

if [ "$DO_TEST" = 1 ]; then
    echo
    echo "▸ envoi d'un e-mail de test du canal d'alerte"
    python3 "$REPO/scripts/watchdog.py" --test-notify
fi
