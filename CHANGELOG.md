# Journal des modifications

Toutes les modifications notables de ce projet sont documentées dans ce fichier.

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et le projet adhère au
[versionnage sémantique](https://semver.org/lang/fr/).

## [Non publié]

### Modifié

- **Bascule effectuée** : le job de veille de l'hôte est mis en pause ; le conteneur est désormais
  la seule source de production. L'historique des rapports (3 semaines) a été migré dans le volume
  (`/opt/data/veille/rapports/`) afin de préserver la contrainte anti-répétition.

### Ajouté

- Cible `make update` — `git pull` puis reconstruction et redéploiement : le cycle de mise à jour.

### Prévu

- Alerte en cas d'échec : le conteneur délivre en `local` (pas de chat), donc un échec est
  silencieux — prévoir une notification (cron séparé ou supervision externe).

## [0.2.0] — 2026-10-05

Conteneurisation : la veille devient une image Docker dérivée, déployable et mettable à jour par
`pull` + recréation.

### Ajouté

- `Dockerfile` — image dérivée de `nousresearch/hermes-agent` : dépendances système
  (`pandoc`, `weasyprint`, `poppler-utils`, polices), bakage des skills et des assets par défaut.
- `docker-compose.yml` — service `veille`, volume `./data:/opt/data`, secrets par `env_file`,
  `TZ=Europe/Paris`, limites mémoire/CPU, rotation des logs.
- `docker/cont-init.d/50-seed-veille` — amorçage du **premier démarrage** : création des
  répertoires, seed des assets (sans écrasement), réglage du modèle et du fuseau, création
  **idempotente** du job cron.
- `veille/job-prompt.md` — prompt du job adapté aux chemins du conteneur (`/opt/data/...`).
- `scripts/deploy.sh` — déploiement en une commande : pré-vol, build, démarrage, attente de
  l'amorçage, vérifications.
- `scripts/smoke-test.sh` — contrôle dans l'image des dépendances, des skills et de la chaîne
  PDF complète (Markdown → PDF).
- `Makefile` — raccourcis d'exploitation (`build`, `up`, `logs`, `run-now`, `verify`, `smoke`…).
- `.github/workflows/build.yml` — construction et publication de l'image sur `ghcr.io`
  (à chaque push sur `main`, à chaque étiquette `v*`, et chaque lundi).

### Modifié

- `README.md` — reflète l'état implémenté et la procédure de déploiement.

### Corrigé

- Image dérivée : conserver `root` comme utilisateur final (et non `hermes`), sinon `/init`
  (s6-overlay) ne peut pas prendre possession du volume `/opt/data` et le conteneur boucle au
  démarrage (`chown /opt/data failed`, puis `Permission denied`).
- Hook de seed : créer les répertoires **en tant que `hermes`** — sinon ils appartiennent à root
  et les copies d'assets échouent silencieusement en permission refusée.

## [0.1.0] — 2026-10-05

Première version : import dans un dépôt versionné du système de veille technologique
jusqu'ici hébergé « à plat » sur le VPS (Hermes installé en git, gateway sous systemd).

### Ajouté

- `veille/config.md` — brief de mission éditable : rôle, cadre temporel, anti-répétition,
  règles de fiabilité, structure du rapport, conventions de mise en forme, couverture, ton.
- `veille/destinataire.txt` — destinataire e-mail par défaut.
- Skill `skills/email/brevo-email/` — envoi d'e-mail via l'API transactionnelle Brevo
  (pièces jointes, vérification de livraison via l'API d'événements).
- Skill `skills/productivity/styled-pdf-report/` — génération de rapports PDF stylés à partir de
  Markdown enrichi (pandoc + WeasyPrint + graphiques SVG, sans dépendance Python).

### Historique (avant versionnement)

Fonctionnalités déjà opérationnelles sur le VPS et importées telles quelles :

- **2026-09-18** — Mise en place du job cron hebdomadaire « Veille technologique »
  (lundi 8h, heure de Paris) et de son archivage hebdomadaire des rapports.
- **2026-09-18** — Chaîne PDF (`pandoc` + `weasyprint`) et envoi e-mail (API Brevo,
  expéditeur validé) rendues opérationnelles de bout en bout.
- **2026-09-18** — Refonte du format du rapport : badges de maturité / confiance / type de
  source, fiches item standardisées (Fait · Pourquoi ça compte · Maturité · Confiance · Source),
  encadrés « À retenir » / « Point de vigilance », tableaux comparatifs, frise de la semaine,
  glossaire, sources numérotées en hyperliens, bandeau de chiffres clés et graphiques.
- **2026-09-21** — Mesure comparative des architectures de génération : modèle « pro » seul
  contre architecture hybride « flash → revue pro ».

### Notes

- Les secrets (`BREVO_API_KEY`, `DEEPSEEK_API_KEY`, …) ne sont **jamais** versionnés —
  voir `.env.example`.
- L'image Docker n'existe pas encore à ce stade ; la démarche est décrite dans
  `docs/PLAN-CONTAINERISATION.md`.
