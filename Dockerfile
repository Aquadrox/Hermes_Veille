# syntax=docker/dockerfile:1
#
# veille-hermes — image dérivée de Hermes Agent embarquant la veille technologique.
#
# On dérive de l'image officielle (jamais de fork) pour n'ajouter que trois choses :
#   1. les dépendances système de la chaîne PDF   (pandoc, WeasyPrint, poppler)
#   2. les skills custom                           (auto-synchronisés au boot)
#   3. les assets par défaut + un hook de seed      (premier démarrage)
#
# Tout l'état vit dans le volume /opt/data ; l'image reste stateless et
# remplaçable par un simple `docker compose pull && up -d`.
#
ARG HERMES_BASE=nousresearch/hermes-agent:latest
FROM ${HERMES_BASE}

LABEL org.opencontainers.image.title="veille-hermes" \
      org.opencontainers.image.description="Veille technologique hebdomadaire : Hermes Agent + chaîne PDF (pandoc/WeasyPrint) + envoi e-mail Brevo" \
      org.opencontainers.image.source="https://github.com/Aquadrox/Hermes_Veille" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="${HERMES_BASE}"

# ── 1. Dépendances système de la chaîne PDF ────────────────────────────────────
# pandoc : Markdown enrichi -> HTML ;  weasyprint : HTML -> PDF ;
# poppler-utils : pdftoppm/pdftotext (contrôle qualité et vérification) ;
# polices : rendu correct des rapports.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        pandoc \
        weasyprint \
        poppler-utils \
        fonts-dejavu-core \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
# NE PAS terminer par `USER hermes` : /init (s6-overlay) doit démarrer en root
# pour pouvoir chown le volume /opt/data. Les services supervisés, eux, passent
# en utilisateur `hermes` via s6-setuidgid. (L'image amont finit aussi en root.)

# ── 2. Skills custom ───────────────────────────────────────────────────────────
# Le hook de boot de l'image officielle synchronise /opt/hermes/skills vers
# $HERMES_HOME/skills : baker les skills ici suffit à les déployer.
COPY skills/ /opt/hermes/skills/

# ── 3. Assets par défaut + hook de seed ────────────────────────────────────────
# Copiés hors du volume : ils servent de valeurs par défaut, seedées au premier
# démarrage puis modifiables dans le volume sans reconstruire l'image.
COPY veille/config.md veille/destinataire.txt veille/job-prompt.md /opt/hermes/veille-defaults/
COPY --chmod=0755 docker/cont-init.d/50-seed-veille /etc/cont-init.d/50-seed-veille

ENV TZ=Europe/Paris
