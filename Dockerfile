# syntax=docker/dockerfile:1
#
# veille-hermes — image dérivée de Hermes Agent embarquant la veille technologique.
#
# On dérive de l'image officielle (jamais de fork) pour n'ajouter que :
#   1. les dépendances système de la chaîne PDF   (pandoc, WeasyPrint, poppler)
#   2. les skills custom                           (auto-synchronisés au boot)
#   3. les gabarits des veilles + un hook de seed   (réconciliation à chaque boot)
#   4. veille_core / veillectl, l'interface web     (venv séparé, service s6)
#   5. veille-mcp : serveur MCP pour piloter les veilles depuis Hermes (Telegram)
#   6. veille-guard : plugin Hermes, garde-fou des veilles contre l'injection de prompt
#   7. deepseek-search-fetch : plugin Hermes, recherche web des veilles par DeepSeek Search & Fetch
#
# Tout l'état vit dans le volume /opt/data ; l'image reste stateless et
# remplaçable par un simple `docker compose pull && up -d`.
#
# Image amont ÉPINGLÉE (FROM littéral : Dependabot propose les montées de version
# en PR, validées par les tests et le smoke test de la CI — jamais de dérive silencieuse).
FROM nousresearch/hermes-agent:v0.22.2

LABEL org.opencontainers.image.title="veille-hermes" \
      org.opencontainers.image.description="Veille technologique hebdomadaire : Hermes Agent + chaîne PDF (pandoc/WeasyPrint) + envoi e-mail Brevo" \
      org.opencontainers.image.source="https://github.com/Aquadrox/Hermes_Veille" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="nousresearch/hermes-agent:v0.22.2"

# ── 1. Dépendances système de la chaîne PDF ────────────────────────────────────
# pandoc : Markdown enrichi -> HTML ;  weasyprint : HTML -> PDF ;
# poppler-utils : pdftoppm/pdftotext (contrôle qualité et vérification) ;
# polices : rendu correct des rapports ; python3-venv : venv de l'application (section 4).
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        pandoc \
        weasyprint \
        poppler-utils \
        fonts-dejavu-core \
        fonts-liberation \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*
# NE PAS terminer par `USER hermes` : /init (s6-overlay) doit démarrer en root
# pour pouvoir chown le volume /opt/data. Les services supervisés, eux, passent
# en utilisateur `hermes` via s6-setuidgid. (L'image amont finit aussi en root.)

# ── 2. Skills custom ───────────────────────────────────────────────────────────
# Le hook de boot de l'image officielle synchronise /opt/hermes/skills vers
# $HERMES_HOME/skills : baker les skills ici suffit à les déployer.
COPY skills/ /opt/hermes/skills/

# ── 3. Gabarits des veilles + hook de seed ─────────────────────────────────────
# Copiés hors du volume : brief(s) de départ et gabarit du prompt des jobs. Le
# hook rend à chaque boot le prompt de chaque veille depuis ce gabarit : une
# nouvelle image propage donc ses corrections de prompt sans recréer les jobs.
COPY veille/job-prompt.tmpl.md veille/job-prompt-composants.tmpl.md veille/pricing.json /opt/hermes/veille-defaults/
COPY veille/templates/ /opt/hermes/veille-defaults/templates/
COPY --chmod=0755 docker/cont-init.d/50-seed-veille /etc/cont-init.d/50-seed-veille

# ── 4. veille_core (veillectl) + interface web ─────────────────────────────────
# Venv dédié : on ne touche pas aux dépendances de Hermes. L'interface parle à
# Hermes uniquement par sa CLI publique (`hermes cron …`).
# Python de Debian imposé (/usr/bin/python3 ; `python3` du PATH est celui de Hermes) et venv
# standard : l'image amont ne fournit plus `uv` dans le PATH depuis la v0.22.
COPY requirements-app.txt /opt/veille-app/
WORKDIR /opt/veille-app
RUN /usr/bin/python3 -m venv /opt/veille-app/.venv \
    && /opt/veille-app/.venv/bin/pip install --quiet --no-cache-dir --disable-pip-version-check -r requirements-app.txt
WORKDIR /opt/hermes
COPY VERSION /opt/veille-app/VERSION
COPY veille_core/ /opt/veille-app/veille_core/
COPY veille_ui/ /opt/veille-app/veille_ui/
COPY veille_mcp/ /opt/veille-app/veille_mcp/
# veillectl (CLI) et veille-mcp (serveur MCP stdio pour Hermes)
COPY --chmod=0755 docker/bin/ /usr/local/bin/
# Service s6-rc (même schéma que le `dashboard` amont), ajouté au bundle `user`.
COPY --chmod=0755 docker/s6-rc.d/ /etc/s6-overlay/s6-rc.d/

# ── 6. Garde-fou contre l'injection de prompt ─────────────────────────────────
# Plugin Hermes « intégré » (propriété root, lecture seule : l'agent ne peut pas le modifier),
# activé par le hook de seed ; ses règles sont dans veille_core/guard.py.
COPY docker/hermes-plugins/veille-guard/ /opt/hermes/plugins/veille-guard/

# ── 7. Recherche web par DeepSeek Search & Fetch ──────────────────────────────
# Plugin communautaire vendorisé à un commit relu (voir son VENDORED.md), même emplacement en lecture
# seule ; web_search / web_extract gardent leur nom (garde-fou inchangé). Activé par le hook de seed
# si VEILLE_WEB_BACKEND vaut deepseek (défaut).
COPY docker/hermes-plugins/deepseek-search-fetch/ /opt/hermes/plugins/deepseek-search-fetch/

# TERMINAL_MAX_FOREGROUND_TIMEOUT : `veillectl components scan` (≈ 17 min au pire, appelé avec
# timeout = guard.SCAN_TIMEOUT = 1200) reste au premier plan ; au-delà, Hermes la passerait en
# arrière-plan, dont le résultat ne se lit que par un outil refusé par le garde-fou
# (veille_core/guard.py). Valeur >= SCAN_TIMEOUT vérifiée par tests/test_guard.py.
ENV TZ=Europe/Paris \
    VEILLE_DEFAULTS=/opt/hermes/veille-defaults \
    TERMINAL_MAX_FOREGROUND_TIMEOUT=1200
EXPOSE 8088
