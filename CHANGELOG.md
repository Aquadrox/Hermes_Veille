# Journal des modifications

Toutes les modifications notables de ce projet sont documentées dans ce fichier.

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et le projet adhère au
[versionnage sémantique](https://semver.org/lang/fr/).

## [Non publié]

## [0.2.2] — 2026-10-05

Suite de tests GitHub et gestion professionnelle des versions.

### Ajouté

- Workflow **`ci`** (push `main` / `release/**`, PR) : cohérence de la version, ruff,
  shellcheck, hadolint, `docker compose config`, contrôle « ports sur 127.0.0.1 », pytest sur
  Python 3.11 / 3.12 / 3.13 avec couverture minimale de 75 % (résumé et rapports JUnit/XML en
  artefacts), audit des dépendances (`pip-audit`).
- Workflow **`release`** : à chaque merge sur `main`, étiquette `vX.Y.Z` et release GitHub
  avec la section du CHANGELOG.
- `scripts/integration-test.sh` (`make integration`) : conteneur complet (s6, gateway, seed,
  interface) sur un volume v0.2 — migration (même job, historique, lien de compatibilité),
  interface (session, CSRF, Host, pause/reprise effectives côté Hermes, version affichée),
  redémarrage sans doublon, chien de garde.
- `scripts/check-version.sh`, `scripts/bump-version.sh` (`make bump`, `make version`),
  `requirements-dev.txt`, `CONTRIBUTING.md` (versions, branches `release/vX.Y.Z`, tests),
  modèle de PR, configuration hadolint justifiée.

### Modifié

- Workflow `build` renommé **`image`** : build → smoke test → test d'intégration → push ; les
  images sont étiquetées avec leur version (`X.Y.Z`) et portent le label OCI `version`.
- `make test` mesure la couverture ; `make lint` inclut hadolint s'il est installé.

### Corrigé

- CI : le nom d'image est mis en minuscules (le propriétaire du dépôt, « Aquadrox », produisait
  une étiquette Docker invalide).

### Sécurité

- Dépendances de l'application mises à jour : **Starlette 0.48 → 1.7** (5 avis de sécurité dont
  CVE-2026-48710, signalés par `pip-audit`), FastAPI 0.142, Uvicorn 0.54, python-multipart 0.0.32.

## [0.2.1] — 2026-10-05

Multi-veilles et interface web de gestion. Migration automatique depuis la v0.2.

### Ajouté

- **Plusieurs veilles** : chacune dans `data/veilles/<slug>/` (`veille.json`, brief `config.md`,
  `recipients.txt`, rapports), avec sa fréquence, ses destinataires et sa mise en forme PDF.
- `veille_core/` — cœur partagé : stockage, fréquences (préréglages ↔ cron, aperçu, seuil de
  fraîcheur), rendu du prompt, **adaptateur unique** vers `hermes cron`, opérations métier
  (création, modification, pause, reprise, lancement, archivage, réconciliation) et journal d'audit.
- `veillectl` — CLI de gestion des veilles dans le conteneur.
- Fichier `VERSION` (source unique du numéro de version) : affiché par `veillectl --version`
  et en pied de page de l'interface, copié dans l'image.
- **Interface web** `veille_ui/` (FastAPI + Jinja + HTMX) : tableau de bord, création et édition
  (fréquence avec aperçu, destinataires, brief avec aperçu, PDF), lancement immédiat avec journal
  en direct, rapports, historique, e-mail de test, archivage, page santé (crédits Brevo, audit).
  Publiée **sur 127.0.0.1 uniquement** (accès par tunnel SSH), mot de passe argon2, session
  signée, CSRF, anti-force brute, contrôle de l'en-tête `Host`, CSP stricte. Service s6 dédié :
  une panne de l'interface n'affecte pas le gateway.
- Gabarit générique du prompt (`veille/job-prompt.tmpl.md`) et modèles de brief
  (`veille/templates/industrie-4-0.md`, `veille/templates/vierge.md`).
- `brevo_send.py` : plusieurs destinataires par fichier, `--bcc` / `--bcc-file` (les
  destinataires ne se voient pas), `--env-file`.
- Tests unitaires (pytest, faux binaire `hermes`) : cœur, migration, watchdog, skills, interface.
- CI : lint (ruff, shellcheck), tests, contrôle « port publié sur 127.0.0.1 uniquement », puis
  build → **smoke test** → push (plus de publication d'une image non testée). Dependabot.
- Smoke test étendu : contrat de la CLI `hermes cron`, cycle de vie `veillectl` contre le vrai
  Hermes, démarrage de l'interface.
- Cibles `make veilles`, `make run-now VEILLE=<slug>`, `make ui-password`, `make tunnel`,
  `make backup`, `make test`, `make lint`.

### Modifié

- **Image amont épinglée** (`nousresearch/hermes-agent:v2026.9.24`) au lieu de `latest`.
- Hook `50-seed-veille` : s'exécute à **chaque** démarrage (migration, amorçage, réconciliation).
  Le job ne contient plus qu'un prompt court renvoyant vers `job-prompt.md`, re-rendu au boot :
  une nouvelle image propage ses corrections de prompt sans recréer les jobs. Auparavant, le
  texte du prompt était figé dans le job à sa création.
- Modèle et fournisseur configurables (`VEILLE_MODEL`, `VEILLE_PROVIDER`).
- **Chien de garde multi-veilles** : seuil de fraîcheur propre à chaque veille, veilles en pause
  ignorées, lecture directe de `cron/jobs.json`, alertes à `ALERT_EMAIL` (et non plus au
  destinataire du rapport), envoi avec le skill et le `.env` **du dépôt** : plus aucune
  dépendance à une installation Hermes sur l'hôte.
- `build_report.py` : répertoire temporaire supprimé après usage, fichiers correctement fermés.

### Corrigé

- Surveillance : `install-watchdog.sh` ne réécrivait que le chemin du script, pas celui du
  volume. Installé ailleurs que dans `/root/veille-hermes`, le chien de garde surveillait un
  volume inexistant.
- `brevo_send.py --to-file` ignorait toutes les adresses après la première.
- `make run-now` prenait le premier identifiant de job trouvé (faux dès deux jobs).

### Supprimé

- `veille/destinataire.txt` (adresse personnelle versionnée) : les destinataires vivent dans le
  volume, et la veille par défaut lit `VEILLE_RECIPIENTS`.

### Migration depuis la v0.2

Automatique au premier démarrage : `data/veille/` → `data/veilles/industrie-4-0/`, historique et
job conservés (édité, pas recréé), lien de compatibilité `data/veille`. Annulée proprement si
l'édition du job échoue. Côté hôte : ajouter `ALERT_EMAIL` au `.env`, puis `make watchdog-install`.

### Bascule (avant 0.2.1)

#### Modifié

- **Bascule effectuée** : le job de veille de l'hôte est mis en pause ; le conteneur est désormais
  la seule source de production. L'historique des rapports (3 semaines) a été migré dans le volume
  (`/opt/data/veille/rapports/`) afin de préserver la contrainte anti-répétition.

#### Ajouté

- Cible `make update` — `git pull` puis reconstruction et redéploiement : le cycle de mise à jour.
- **Surveillance (chien de garde)** — `scripts/watchdog.py`, `scripts/install-watchdog.sh` et un
  timer systemd quotidien (10h) **sur l'hôte** : détecte les pannes silencieuses (conteneur
  arrêté, rapport trop ancien ou tronqué, dernier run en échec) et prévient par e-mail, avec
  e-mail de rétablissement et rappel tous les 3 jours. Déterministe (aucun LLM) et volontairement
  placé **hors** du conteneur : un contrôleur interne ne pourrait signaler ni un conteneur arrêté
  ni une clé d'API expirée.
- Cibles `make watchdog-install` et `make watchdog-check`.

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
