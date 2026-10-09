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
             /opt/hermes/veille-defaults/job-prompt.tmpl.md \
             /opt/hermes/veille-defaults/job-prompt-composants.tmpl.md \
             /opt/hermes/veille-defaults/templates/composants.md \
             /opt/hermes/veille-defaults/pricing.json \
             /opt/hermes/veille-defaults/templates/industrie-4-0.md \
             /opt/hermes/veille-defaults/templates/vierge.md \
             /etc/cont-init.d/50-seed-veille \
             /etc/s6-overlay/s6-rc.d/veille-ui/run \
             /etc/s6-overlay/s6-rc.d/user/contents.d/veille-ui \
             /usr/local/bin/veillectl \
             /usr/local/bin/veille-mcp ; do
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

# --- 4. Contrat avec la CLI Hermes (options utilisées par veille_core/hermes.py) ---
log "contrat de la CLI \`hermes cron\`"
docker run --rm --entrypoint /bin/bash "$REF" -lc '
    set -e
    H=/opt/hermes/.venv/bin/hermes
    c=$($H cron create --help); e=$($H cron edit --help)
    for f in --name --workdir --deliver --skill --model --provider --paused; do
        echo "$c" | grep -q -- "$f" || { echo "cron create sans $f"; exit 1; }
    done
    for f in --name --schedule --prompt --workdir --model; do
        echo "$e" | grep -q -- "$f" || { echo "cron edit sans $f"; exit 1; }
    done
    for sub in pause resume run remove runs; do $H cron "$sub" --help >/dev/null; done
    echo "  options attendues : présentes"
' || { fail "la CLI hermes cron a changé — adapter veille_core/hermes.py"; exit 1; }
ok "CLI hermes cron compatible"

# --- 5. veillectl de bout en bout contre le VRAI Hermes (sans gateway) ---
log "veillectl : création, pause, réconciliation, archivage"
docker run --rm --entrypoint /bin/bash -e HERMES_HOME=/tmp/h "$REF" -lc '
    set -e
    mkdir -p /tmp/h
    veillectl create --name "Smoke" --schedule "0 8 * * 1" --recipients smoke@example.org --template vierge
    veillectl list | grep -q "^smoke"
    grep -q -- "--bcc-file /tmp/h/veilles/smoke/recipients.txt" /tmp/h/veilles/smoke/job-prompt.md
    veillectl pause smoke && veillectl list | grep -q "en pause"
    veillectl resume smoke
    veillectl reconcile | grep -q "smoke: ok"
    veillectl archive smoke
    echo "  cycle de vie : OK"
' || { fail "veillectl en échec contre la CLI Hermes"; exit 1; }
ok "veillectl fonctionnel"

# --- 5 bis. Veille composants : scan, enregistrement, PDF réel, chapitre (sans Digi-Key ni e-mail) ---
log "veille composants : scan, enregistrement, PDF, chapitre"
docker run --rm --entrypoint /bin/bash -e HERMES_HOME=/tmp/h \
    -e BUILD_REPORT=/opt/hermes/skills/productivity/styled-pdf-report/scripts/build_report.py "$REF" -lc '
    set -e
    mkdir -p /tmp/h && cd /tmp/h
    printf "LM317T;Texas Instruments;R-1\nNE555P;Texas Instruments\n" > bom.csv
    veillectl create --kind composants --name "Smoke composants" --schedule "0 23 * * 0" \
        --recipients smoke@example.org --components-file bom.csv
    veillectl create --name "Smoke veille" --schedule "0 8 * * 1" --recipients smoke@example.org \
        --template vierge --include-components smoke-composants
    grep -q "veillectl components chapter smoke-composants --for smoke-veille" /tmp/h/veilles/smoke-veille/job-prompt.md
    veillectl components scan smoke-composants | grep -q "\"configured\": false"
    D=$(date +%F); R=/tmp/h/veilles/smoke-composants/rapports
    echo "{\"notices\":[{\"mpns\":[\"LM317T\"],\"type\":\"EOL\",\"title\":\"Fin de vie\",\"sources\":[{\"url\":\"https://www.ti.com/eol/1\",\"label\":\"TI\"}]}]}" > "$R/$D-pcn.json"
    veillectl components finalize smoke-composants "$R/$D-pcn.json" | grep -q "OK: veille composants — 1 nouvel"
    size=$(stat -c%s "$R/$D-veille.pdf"); test "$size" -gt 20000
    M=/tmp/h/veilles/smoke-veille/rapports/$D-veille.md; echo "# Rapport" > "$M"
    veillectl components chapter smoke-composants --for smoke-veille --append "$M" | grep -q "PREFIXE_SUJET=\[EOL"
    grep -q "Nouvelles fins de vie" "$M"
    echo "  veille composants : OK (PDF $size octets)"
' || { fail "veille composants en échec"; exit 1; }
ok "veille composants fonctionnelle"

# --- 6. Interface web : démarre et exige une session ---
log "interface web"
docker run --rm --entrypoint /bin/bash -e HERMES_HOME=/tmp/h \
    -e VEILLE_UI_SECRET=smoke -e VEILLE_UI_PASSWORD_HASH=x "$REF" -lc '
    set -e
    mkdir -p /tmp/h && cd /opt/veille-app
    .venv/bin/uvicorn veille_ui.app:app --host 127.0.0.1 --port 8088 >/tmp/ui.log 2>&1 &
    for _ in $(seq 1 30); do .venv/bin/python -c "import urllib.request as u; u.urlopen(\"http://127.0.0.1:8088/healthz\")" 2>/dev/null && break; sleep 1; done
    .venv/bin/python - <<"PY"
import urllib.error, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
op = urllib.request.build_opener(NoRedirect)
try:
    op.open("http://127.0.0.1:8088/")
    raise SystemExit("page protégée accessible sans session")
except urllib.error.HTTPError as e:
    assert e.code == 303 and e.headers["location"] == "/login", e.code
print("  / sans session -> 303 /login : OK")
PY
' || { fail "interface web en échec"; exit 1; }
ok "interface web fonctionnelle"

# --- 7. Serveur MCP, vu par le client MCP de Hermes lui-même ---
log "serveur MCP (client MCP de Hermes)"
docker run --rm --entrypoint /bin/bash -e HERMES_HOME=/tmp/h "$REF" -lc '
    set -e
    H=/opt/hermes/.venv/bin/hermes
    mkdir -p /tmp/h
    veille-mcp --check
    $H config set mcp_servers.veilles.command veille-mcp >/dev/null
    $H config set mcp_servers.veilles.trust untrusted >/dev/null
    out=$(timeout 120 $H mcp test veilles 2>&1)
    echo "$out" | grep -q "Tools discovered: 28" || { echo "$out"; exit 1; }
    echo "  Hermes : connexion OK, 28 outils découverts"
' || { fail "serveur MCP en échec"; exit 1; }
ok "serveur MCP fonctionnel"

# --- 8. Contrat avec la base de sessions de Hermes (consommation de tokens) ---
log "lecture de la consommation dans state.db (session créée par Hermes lui-même)"
docker run --rm --entrypoint /bin/bash -e HERMES_HOME=/tmp/h "$REF" -lc '
    set -e
    mkdir -p /tmp/h && cd /opt/hermes
    .venv/bin/python - <<"PY"
from pathlib import Path
from hermes_state import SessionDB
db = SessionDB(Path("/tmp/h/state.db"))
sid = "cron_abc123def456_20261005_063000"
db.ensure_session(sid, source="cron", model="deepseek-v4-pro")
db.update_token_counts(sid, input_tokens=1000, output_tokens=200, cache_read_tokens=3000,
                       model="deepseek-v4-pro", api_call_count=2)
PY
    cd /opt/veille-app && PYTHONPATH=/opt/veille-app .venv/bin/python - <<"PY"
from veille_core import hermes, pricing
runs = hermes.run_usage("abc123def456")
assert len(runs) == 1, runs
m = {x["model"]: x for x in runs[0]["models"]}["deepseek-v4-pro"]
assert (m["input_tokens"], m["cache_read_tokens"], m["output_tokens"]) == (1000, 3000, 200), m
usd, _ = pricing.run_cost("deepseek-v4-pro", input_tokens=1000, cache_read_tokens=3000, output_tokens=200)
assert usd and usd > 0
print("  session Hermes lue : 1000 / 3000 en cache / 200 tokens, tarif connu")
PY
' || { fail "la base de sessions de Hermes a changé — adapter veille_core/hermes.py (run_usage)"; exit 1; }
ok "lecture de la consommation compatible"

echo
ok "smoke test réussi — l'image est prête"
