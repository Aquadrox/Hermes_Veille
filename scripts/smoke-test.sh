#!/usr/bin/env bash
# smoke-test.sh — vérifie que l'image construite contient bien toute la chaîne.
#
# Usage : ./scripts/smoke-test.sh
#   IMAGE=ghcr.io/aquadrox/hermes-veille IMAGE_TAG=latest ./scripts/smoke-test.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

IMAGE="${IMAGE:-ghcr.io/aquadrox/hermes-veille}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
REF="${IMAGE}:${IMAGE_TAG}"

log()  { printf '\033[36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; }

# --- 1. L'image existe ? Sinon on la construit ---
if ! docker image inspect "$REF" >/dev/null 2>&1; then
    log "image $REF absente — construction"
    docker build -t "$REF" .
fi
ok "image $REF disponible"

# --- 2. Dépendances système + skills ---
log "contrôle des dépendances et des skills dans l'image"
docker run --rm --entrypoint /bin/bash "$REF" -lc '
    set -e
    echo "  pandoc    : $(pandoc --version | head -1)"
    echo "  weasyprint: $(weasyprint --version)"
    echo "  pdftoppm  : $(pdftoppm -v 2>&1 | head -1)"
    echo "  python    : $(python3 --version)"
    for s in /opt/hermes/skills/email/brevo-email/SKILL.md \
             /opt/hermes/skills/productivity/styled-pdf-report/SKILL.md \
             /opt/hermes/skills/productivity/styled-pdf-report/scripts/build_report.py \
             /opt/hermes/skills/productivity/styled-pdf-report/assets/report.css \
             /opt/hermes/veille-defaults/config.md \
             /etc/cont-init.d/50-seed-veille ; do
        [ -f "$s" ] || { echo "MANQUANT: $s"; exit 1; }
    done
    echo "  skills et assets : présents"
' || { fail "contrôle des dépendances/skills échoué"; exit 1; }
ok "dépendances et skills présents"

# --- 3. Test réel de la chaîne PDF (Markdown -> PDF) ---
log "test de la chaîne PDF de bout en bout dans le conteneur"
docker run --rm --entrypoint /bin/bash "$REF" -lc '
    set -e
    T=$(mktemp -d)
    cat > "$T/r.md" <<"MD"
::: {.keyfigures}
::: {.fig}
**42 %**

chiffre de démonstration
:::
:::

## Section de test

::: {.card}
### Acteur — test
[Production]{.badge .m-production} [Confiance élevée]{.badge .c-high}

**Fait.** Test de la chaîne.
:::

<div class="chart-slot"></div>
MD
    echo "{\"charts\":[{\"title\":\"Démo\",\"source\":\"test\",\"bars\":[{\"label\":\"A\",\"value\":10,\"value_label\":\"10\"}]}]}" > "$T/c.json"
    python3 /opt/hermes/skills/productivity/styled-pdf-report/scripts/build_report.py \
        --md "$T/r.md" --out "$T/r.pdf" --data "$T/c.json" \
        --title "Smoke test" --subtitle "Conteneur" --meta "Test | ~1 min | CI" \
        --header "Smoke test" --footer "veille-hermes" >/dev/null
    test -s "$T/r.pdf" || { echo "PDF non produit"; exit 1; }
    echo "  PDF généré : $(stat -c%s "$T/r.pdf") octets"
' || { fail "chaîne PDF en échec"; exit 1; }
ok "chaîne PDF fonctionnelle"

echo
ok "smoke test réussi — l'image est prête"
