# Journal des modifications

Toutes les modifications notables de ce projet sont documentées dans ce fichier.

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et le projet adhère au
[versionnage sémantique](https://semver.org/lang/fr/).

## [Non publié]

## [0.3.0] — 2026-10-09

Veille composants : détection des avis PCN et des fins de vie (EOL) d'une nomenclature.

### Ajouté

- **Veille composants** (`kind: composants`) : une liste de références « MPN ; Fabricant ; Réf.
  interne » (collage tableur accepté, plus de 300 références prévues), surveillée chaque semaine
  (dimanche 23h par défaut, en heures creuses DeepSeek). Elle réutilise tout le cycle de vie d'une
  veille : job Hermes, pause, lancement, archivage, coûts, rapports PDF, chien de garde.
  - **Statut Digi-Key** (API Product Information v4, OAuth 2-legged, `DIGIKEY_CLIENT_ID` /
    `DIGIKEY_CLIENT_SECRET`) vérifié pour TOUS les composants à chaque exécution : un passage en
    Obsolete, Last Time Buy ou Not For New Designs devient un avis. Débit limité, budget de 900
    appels et 15 minutes par passage ; facultatif.
  - **Recherche web** par l'agent, en **lot tournant** (`batch_size`, 40 par défaut) : jamais
    vérifiés d'abord, puis les références dont le statut Digi-Key a changé, puis les plus anciennes.
  - **Incrémental** : chaque référence garde ses dates de vérification ; un avis déjà connu (même
    référence et même numéro d'avis ou même lien) n'est jamais signalé deux fois, ses nouvelles
    sources sont fusionnées.
  - Le LLM ne fait que chercher et écrire un JSON ; validation, dédoublonnage, persistance
    (`components.json`, `notices.json`), rapport PDF et e-mail sont déterministes
    (`veillectl components scan|finalize`).
- **Chapitre « Composants — PCN & fins de vie »** dans l'e-mail d'une veille thématique
  (`include_components`) : nouveaux avis depuis le précédent envoi, fins de vie en tête dans un
  encadré rouge, rappel de toutes les fins de vie actives, objet préfixé « [EOL ×N] ». Sans
  rattachement, la veille composants envoie son propre e-mail (HTML + PDF).
- Interface : bouton « + Veille composants », formulaire avec la liste des références, page de
  détail avec bandeau **Fins de vie et dernières commandes**, avis filtrables par type (sans
  JavaScript), liens sources, état de chaque référence ; badge et compteur EOL sur le tableau de
  bord.
- MCP : `list_components`, `list_notices` (lecture), `add_components`, `remove_components`
  (modification) ; `create_veille(kind=…, components_list=…, include_components=…)`,
  `update_veille(include_components=…, batch_size=…)`. 27 outils (13 en lecture).
- CLI : `veillectl components list|notices|add|remove|scan|finalize|chapter`,
  `create --kind composants --components-file`, `edit --include-components / --batch-size`.
- **Page « Sources »** de l'interface : saisie des clés d'API Digi-Key avec un test de connexion.
  Les valeurs ne sont jamais réaffichées (aperçu masqué). Stockage dans `veilles/.credentials.json`
  (0600, écriture atomique, exclu de `make backup`), audit sans les valeurs. Les clés du `.env`
  restent prioritaires (lecture seule dans l'interface). Aucune saisie possible par
  Telegram / MCP : `health` indique seulement si une source est configurée.
- Skill `brevo-email` : `--html-file` (corps HTML sans limite de taille de ligne de commande).
- Tests : `tests/test_components.py`, `tests/test_digikey.py` et `tests/test_credentials.py` ; smoke test de la veille
  composants sur l'image (PDF réel, chapitre).

### Modifié

- `veille_core/envfile.py` : lecture unique des secrets (environnement, puis `.env` du volume),
  partagée par DeepSeek, Brevo et Digi-Key (trois copies auparavant).

### Sécurité

- Les avis trouvés sur le web sont des données non fiables : schéma strict, liens `http(s)`
  uniquement (ni `javascript:`, ni identifiants dans l'URL), tailles bornées, échappement
  systématique dans le Markdown du PDF (pas de HTML brut transmis à pandoc / WeasyPrint), dans
  l'e-mail HTML et dans l'interface. Les secrets Digi-Key ne figurent jamais dans les journaux.
- Sauvegardes : les clés saisies dans l'interface en sont exclues, comme le `.env`.

## [0.2.7] — 2026-10-09

Données persistantes à travers les mises à jour, garanties et vérifiées.

### Ajouté

- **`VEILLE_DATA_DIR`** : dossier des données sur l'hôte, monté sur `/opt/data`. Il est
  recommandé hors du dépôt git (`/srv/veille-hermes/data`), pour qu'aucune mise à jour (re-clone,
  `git clean -x`) ne puisse l'atteindre. Le défaut `./data` est conservé, donc rien ne change
  pour une installation existante.
- **`make move-data DEST=…`** (`scripts/move-data.sh`) sort les données du dépôt sans rien
  perdre : renommage, ou copie puis original renommé (jamais supprimé), réglage dans `.env`,
  redéploiement vérifié, réinstallation du chien de garde.
- `scripts/data-dir.sh` : résolution unique du dossier de données côté hôte, partagée par le
  déploiement, la sauvegarde, le chien de garde et le serveur MCP.
- `scripts/backup.sh` (`make backup`) : archives rangées à côté des données, réservées au
  propriétaire (`umask 077`).
- Interface : nombre de rapports sur le tableau de bord, avec un lien vers l'historique.
- Tests : `tests/test_persistence.py` (32 tests : scripts de l'hôte exécutés avec un faux
  `docker`), réconciliation sur un `jobs.json` corrompu, historique des rapports dans
  l'interface. Le test d'intégration simule une **mise à jour** (conteneur supprimé puis recréé :
  veilles, jobs, rapports et audit identiques) et vérifie le refus de démarrer sans dossier de
  données.

### Modifié

- **`deploy.sh` / `make update`**, déploiement sûr :
  - refus de démarrer si le dossier de données est absent (`--init` pour une première
    installation) ;
  - **sauvegarde automatique** conteneur arrêté ;
  - attente de l'amorçage de *ce* démarrage (et non plus du marqueur du premier) ;
  - vérification que le conteneur monte bien le dossier attendu (`docker inspect`) ;
  - **inventaire avant et après** : le déploiement échoue s'il manque une veille ou un rapport ;
  - code de sortie non nul si l'amorçage échoue, si la réconciliation signale une erreur, si
    `veillectl` ou l'interface ne répondent pas (auparavant : message, mais succès).
- `docker-compose.yml` : volume en syntaxe longue avec `create_host_path: false`. Un dossier
  absent fait échouer le démarrage au lieu de repartir d'un état vide en silence.
- Page d'une veille : l'historique des rapports passe **en tête**, avant le coût des exécutions,
  avec le nombre de rapports et une présentation en cartes sur mobile.

### Corrigé

- Réconciliation au démarrage et modification d'une veille : un `cron/jobs.json` **illisible**
  (droits, contenu corrompu) n'est plus pris pour une absence de jobs. Auparavant, chaque job
  était recréé : nouveaux identifiants, historique d'exécution et coûts orphelins, envois en
  double. Désormais, rien n'est modifié et l'erreur est signalée. Même correctif pour
  l'archivage (le job n'était pas supprimé et continuait d'envoyer des e-mails) et la migration
  de l'ancienne disposition.

## [0.2.6] — 2026-10-05

Consommation de tokens et coût réel de chaque rapport.

### Ajouté

- **Coût des exécutions**, calculé à partir des tokens que Hermes enregistre par exécution
  (`state.db`, une session `cron_<job>_<date>` par run, détail par modèle, tâches auxiliaires
  comprises). La lecture se fait en lecture seule, dans `veille_core/hermes.py` (`run_usage`).
  - Tarifs DeepSeek dans `veille/pricing.json` (surchargeable dans le volume), **heures pleines
    comprises** : lun–ven 01h–04h et 06h–10h UTC, tarif ×2. L'estimation de Hermes n'applique que
    le tarif creux et sous-estimait donc de moitié le rapport du lundi 8h.
  - Interface : section « Coût des exécutions » par veille (durée, tokens, heure pleine ou creuse,
    coût, totaux du mois et des 30 jours) ; page Santé : **solde DeepSeek** (`GET /user/balance`)
    et coût du mois par veille.
  - Telegram : outil MCP **`get_costs`** (lecture seule, 23 outils au total). `health` ajoute le
    solde et le coût du mois, et `preview_schedule` signale les heures pleines.
  - CLI : `veillectl costs [slug] [--days N]`.
- Aperçu de fréquence (interface et MCP) : avertissement « heure pleine DeepSeek (tarif ×2) »,
  avec les créneaux creux conseillés.
- Smoke test : lecture d'une session créée par Hermes lui-même, pour vérifier le contrat avec son
  schéma de base.

### Modifié

- Veille par défaut d'une installation neuve : **lundi 6h30** (heures creuses), au lieu de 8h.
- Calculs de planification dans un fuseau explicite (`Europe/Paris`, variable `VEILLE_TZ`), au lieu
  du fuseau du processus.

## [0.2.5] — 2026-10-05

Outils de développement à jour (reprend la PR Dependabot groupée #15).

### Modifié

- `requirements-dev.txt` : `pytest-cov` 7.0.0 → 7.1.0, `ruff` 0.15.10 → 0.16.10,
  `pip-audit` 2.10.0 → 2.10.1 ; même version de `pip-audit` dans le job d'audit de la CI.
  Aucun effet sur l'image ni sur le serveur.

## [0.2.4] — 2026-10-05

Mise à jour des actions GitHub et Dependabot groupé.

### Modifié

- Actions GitHub en dernière version majeure, toutes sur Node 24 (fin des avertissements de
  dépréciation de Node 20) : `actions/checkout` 4 → 7, `actions/setup-python` 5 → 7,
  `actions/upload-artifact` 4 → 7, `docker/setup-buildx-action` 3 → 4,
  `docker/metadata-action` 5 → 6, `docker/build-push-action` 6 → 7, `docker/login-action`
  3 → 4, `hadolint/hadolint-action` 3.1 → 3.5. Reprend les PR Dependabot #2, #3, #4, #7 et #9.
  hadolint 2.15 (embarqué par l'action) ajoute la règle DL3066 : exception justifiée pour le
  `USER root` voulu (même raison que DL3002).
- Dependabot : **une PR groupée par écosystème et par mois** (au lieu d'une PR par paquet).
  Ces PR servent de signal : leur contenu est repris dans la prochaine branche
  `release/vX.Y.Z`, car chaque merge sur `main` doit incrémenter la version.

## [0.2.3] — 2026-10-05

Piloter les veilles depuis Hermes (Telegram) : serveur MCP.

### Ajouté

- **Serveur MCP `veille-mcp`** (`veille_mcp/`, SDK `mcp` 2.3, transport stdio) : 22 outils.
  - Lecture (annotés `readOnlyHint`) : `list_veilles`, `get_veille`, `get_brief`,
    `list_brief_templates`, `list_reports`, `read_report` (Markdown d'un rapport, pour le
    résumer), `get_run_log`, `run_history`, `preview_schedule`, `health`.
  - Modification : `create_veille`, `update_veille`, `set_recipients`, `add_recipients`,
    `remove_recipients`, `update_brief`, `edit_brief` (remplacement ciblé), `pause_veille`,
    `resume_veille`, `run_veille_now`, `send_test_email`, `archive_veille` (`destructiveHint`,
    exige de répéter le slug).
  - Fréquences en langage clair (`frequency`, `time`, `day_of_week` « lundi »…, `day_of_month`
    ou `cron`) ; erreurs métier renvoyées proprement à l'agent ; actions tracées dans le journal
    d'audit avec leur origine (`VEILLE_MCP_ACTOR`, ex. « telegram ») ; chemins hôte des PDF
    (`VEILLE_HOST_DATA`) pour les envoyer en pièce jointe ; mode `--readonly`.
- `scripts/install-mcp-host.sh` (`make mcp-install`) : déclare le serveur dans le Hermes de
  l'hôte (`docker exec -i`, **aucun port ouvert**, `trust: untrusted` : toute modification
  demande une approbation dans Telegram), puis teste la connexion avec `hermes mcp test`.
- Tests : 14 tests à travers le vrai protocole MCP (client en mémoire et sous-processus stdio) ;
  smoke test avec le **client MCP de Hermes lui-même** (`hermes mcp test` : 22 outils) ;
  test d'intégration via `docker exec -i` (lecture, pause/reprise effectives, confirmation
  d'archivage, audit « telegram »).

## [0.2.2] — 2026-10-05

Suite de tests GitHub et gestion professionnelle des versions.

### Ajouté

- Workflow **`ci`** (push `main` / `release/**`, PR) : cohérence de la version, ruff,
  shellcheck, hadolint, `docker compose config`, contrôle « ports sur 127.0.0.1 », pytest sur
  Python 3.11 / 3.12 / 3.13 avec couverture minimale de 75 % (résumé et rapports JUnit/XML en
  artefacts), audit des dépendances (`pip-audit`).
- Workflow **`release`** : à chaque merge sur `main`, étiquette `vX.Y.Z` et release GitHub
  avec la section du CHANGELOG, posées sur le commit où la version est arrivée sur `main` ;
  rattrape les versions encore sans étiquette (dont la 0.2.1).
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
