# Plan d'amélioration — corrections, multi-veilles et interface de gestion

> Statut : **phases 0, 1 et 2 implémentées** (voir CHANGELOG, section « Non publié »).
> Écarts par rapport au plan : métadonnées en `veille.json` (et non YAML) pour que le chien de
> garde de l'hôte les lise avec la seule bibliothèque standard ; gabarit du prompt en
> `string.Template` (`veille/job-prompt.tmpl.md`) ; interface déclarée en service **s6-rc**
> (même schéma que le `dashboard` amont) ; image amont épinglée par un `FROM` littéral
> (`v2026.9.24`) pour que Dependabot propose les montées de version. La phase 3 reste à faire.

## Contexte

Le dépôt conteneurise **une** veille (Industrie 4.0) : un job cron interne à Hermes Agent,
créé une seule fois au premier boot par `docker/cont-init.d/50-seed-veille`. Le job lit
`veille/config.md`, écrit un PDF et l'envoie par Brevo. Un chien de garde (`scripts/watchdog.py`)
tourne sur l'hôte. Tout repose sur un seul job, un seul destinataire et des chemins figés.

Objectif : (1) corriger les fragilités actuelles, (2) généraliser à **N veilles**,
(3) ajouter une **interface web** joignable seulement via un tunnel SSH (`127.0.0.1`), pour
créer, modifier, mettre en pause et lancer des veilles, et pour régler leur fréquence et leurs
destinataires.

Décisions prises : **FastAPI + Jinja + HTMX**, **mot de passe admin unique** (hash dans `.env`,
session + CSRF), **plusieurs destinataires envoyés en Cci** (un seul envoi Brevo).

Faits établis sur Hermes (amont) :
- `hermes cron` sait faire `create/edit/pause/resume/run/remove/runs/status`.
- Les jobs sont dans `/opt/data/cron/jobs.json`, avec verrou `flock` et écriture atomique. Le
  gateway relit ce fichier à chaque tick, donc un processus externe peut le modifier sans risque.
- Formats de planification acceptés : cron 5 champs, `every monday 8am`, `every 2h`, ISO.
- Champs utiles d'un job : `id, name, schedule, prompt, skills, deliver, workdir, enabled,
  state, last_run_at, next_run_at, last_status, last_error, failure_deliver`.
- `hermes dashboard` existe, mais il gère la config, les clés et les sessions. Il ne connaît pas
  la notion de veille.

---

## Phase 0 — Corrections de l'existant (avant toute nouvelle fonction)

| # | Problème constaté | Correction |
|---|---|---|
| 0.1 | **Le prompt du job est figé à la création.** `cron create` copie le *texte* de `job-prompt.md` dans le job. Éditer le fichier dans le volume ou mettre à jour l'image ne change rien, puisque le marqueur `.seeded` bloque tout. | Le job reçoit un prompt court et stable (« lis et exécute `/opt/data/veilles/<slug>/job-prompt.md` »). Le vrai prompt est un fichier rendu depuis un template. Au boot, une réconciliation (`hermes cron edit`) réaligne le job si besoin. |
| 0.2 | **Bug du watchdog.** `install-watchdog.sh` réécrit le chemin du script, mais pas `--volume`. `DEFAULT_VOLUME=/root/veille-hermes/data` reste en dur : si le dépôt est ailleurs, le watchdog surveille un volume vide. | Ajouter `--volume $REPO/data` à `ExecStart` (ou lire `VEILLE_VOLUME` dans un fichier d'environnement de l'unit). |
| 0.3 | Le watchdog dépend du **Hermes de l'hôte** (`~/.hermes/skills/.../brevo_send.py` et `~/.hermes/.env`), alors que ce Hermes est en pause depuis la bascule. | Utiliser le `skills/email/brevo-email/scripts/brevo_send.py` **du dépôt**. Charger la clé depuis le `.env` du dépôt (ajouter une option `--env-file` à `brevo_send.py`). |
| 0.4 | `check_last_run` analyse le texte de `hermes cron list` (« Last run: »), fragile et limité à un seul job. | Lire `data/cron/jobs.json` directement depuis l'hôte (lecture seule) : `last_status`, `last_run_at` et `next_run_at` pour chaque job. |
| 0.5 | Les alertes partent au destinataire **du rapport**. | Ajouter `ALERT_EMAIL` dans `.env` (adresse d'administration). |
| 0.6 | `brevo_send.py --to-file` ne lit que la **première ligne**. | Lire toutes les lignes non vides et commentées autorisées. Ajouter `--bcc` et `--bcc-file`. En Cci, mettre l'expéditeur en `To` (Brevo exige au moins un `to`). |
| 0.7 | L'image amont est en `latest` dans `Dockerfile`, `docker-compose.yml` et la CI. La CI reconstruit chaque lundi sans tester. Contraire à la décision D1 du plan d'origine. | Épingler `HERMES_BASE` sur un tag ou un digest (`.env` et argument de la CI). La CI lance `scripts/smoke-test.sh` **avant** le push, plus `shellcheck` et `ruff`. Ajouter Dependabot ou Renovate pour proposer les montées de version. |
| 0.8 | Une adresse personnelle est versionnée dans `veille/destinataire.txt`. | La remplacer par `exemple@domaine.tld` ; la vraie valeur vit dans le volume. |
| 0.9 | Le modèle `deepseek-v4-pro` est en dur dans le seed. | Variables `VEILLE_MODEL` et `VEILLE_PROVIDER` dans `.env`. |
| 0.10 | `make run-now` prend le premier identifiant hexadécimal trouvé : faux dès qu'il y a deux jobs. | `make run-now VEILLE=<slug>`, via la CLI de la phase 1. |
| 0.11 | Petits défauts : `build_report.py` ne supprime jamais son `mkdtemp`, `brevo_send.py` ouvre des fichiers sans les fermer, licence MIT dans le `LABEL` mais « à définir » dans le README. | Nettoyage (`TemporaryDirectory`, `with open`), choisir une licence. |
| 0.12 | Aucun test unitaire. | Ajouter `tests/` avec pytest : `build_report` (SVG, slots), `brevo_send` (payload, Cci, `--to-file` multi-lignes, HTTP simulé), `watchdog` (décisions d'état, fraîcheur). |
| 0.13 | Pas de sauvegarde de `./data` (rapports, jobs, briefs). | `make backup` : archive tar horodatée de `data/` sans `.env`. Rétention documentée. |

---

## Phase 1 — Modèle multi-veilles (cœur partagé par la CLI et l'interface)

### Disposition sur le volume
```
/opt/data/veilles/<slug>/
  veille.yaml        # métadonnées (voir ci-dessous) — source de vérité « métier »
  config.md          # brief éditable (prompt de mission)
  job-prompt.md      # RENDU depuis le template (ne pas éditer à la main)
  recipients.txt     # un e-mail par ligne
  rapports/          # AAAA-MM-JJ-veille.{md,pdf}, -charts.json
/opt/data/veilles/.archive/<slug>-<date>/   # veilles supprimées (suppression douce)
```

### `veille.yaml`
```yaml
slug: industrie-4-0
name: "Veille Industrie 4.0"
job_id: e982d99ccf9a          # lien vers le job Hermes
schedule: "every monday 8am"  # recopié : le job Hermes fait foi, réconcilié au boot
period_label: "semaine"       # « semaine écoulée », « mois écoulé », « 24 dernières heures »
pdf: {title: "...", header: "...", footer: "Kindara — document interne", accent: "#0f766e"}
model: null                   # null = modèle par défaut Hermes
created_at: ...
```
**Le job Hermes fait foi** pour l'état (en pause ou non, prochain run, dernier statut).
`veille.yaml` ne recopie que ce qu'il faut pour recréer le job. On n'a donc jamais deux
sources de vérité qui divergent sur « en pause ? ».

### Template de prompt générique
`veille/job-prompt.md.j2` remplace `veille/job-prompt.md`. Les chemins `/opt/data/veilles/{{slug}}/…`,
la période (`{{period_label}}`), les titres et en-têtes du PDF et l'envoi
(`--bcc-file recipients.txt`) deviennent des variables. Les thèmes de recherche codés en dur
à l'étape 3 (Industrie 4.0, Chine…) sortent du prompt : c'est le **brief** qui les porte.
Le brief Industrie 4.0 actuel devient `veille/templates/industrie-4-0.md`, et
`veille/templates/vierge.md` sert de squelette (structure, badges, conventions PDF), proposé
quand on crée une veille.

### Paquet Python `veille_core/` (nouveau, dans le dépôt, copié dans l'image)
- `store.py` : lecture et écriture de `veille.yaml`, des destinataires et du brief.
  Validation des e-mails et des slugs. Écriture atomique.
- `hermes.py` : **adaptateur unique** vers Hermes. Écritures par la CLI `hermes cron`
  (`create/edit/pause/resume/run/remove`, appelée avec une liste d'arguments, jamais via un
  shell). Lectures directes de `cron/jobs.json`. Isoler ce module permet de suivre l'amont
  sans toucher au reste.
- `schedule.py` : préréglages (quotidien, hebdomadaire avec jour et heure, mensuel) vers une
  expression cron, validation, calcul du seuil de fraîcheur (période + 1 jour de marge) pour
  le watchdog.
- `render.py` : rend `job-prompt.md` depuis le template.
- `service.py` : opérations métier (`create_veille`, `update_veille`, `pause`, `resume`,
  `run_now`, `archive`, `reconcile_all`). La CLI et l'interface les appellent ; elles
  n'appellent jamais `hermes.py` directement.

### CLI `veillectl` (dans le conteneur et via `make`)
`veillectl list | show <slug> | create … | edit <slug> … | pause | resume | run | archive | reconcile`.
Ajouter les cibles `make veille-list`, `make run-now VEILLE=slug`, etc.

### Migration et seed
- `50-seed-veille` devient : création des répertoires, puis `veillectl migrate` (une seule
  fois), puis `veillectl reconcile` **à chaque boot**. La réconciliation recrée un job manquant,
  re-rend `job-prompt.md` et réaligne le prompt court.
- `migrate` : si `/opt/data/veille/` existe (ancienne disposition), déplacer son contenu vers
  `/opt/data/veilles/industrie-4-0/` en **conservant `rapports/`** (règle anti-répétition),
  créer `veille.yaml`, puis `hermes cron edit <id>` (prompt, workdir). Pas de recréation, donc
  l'historique des runs est conservé.

### Watchdog multi-veilles
`watchdog.py` parcourt `data/veilles/*/veille.yaml` et croise avec `jobs.json`. Pour chaque veille
**active** (les veilles en pause sont ignorées) : statut du dernier run, fraîcheur du rapport selon
**sa** fréquence, taille plausible. L'état anti-spam est tenu par veille dans `state.json`. Un seul
e-mail récapitulatif est envoyé à `ALERT_EMAIL`.

---

## Phase 2 — Interface web (FastAPI + Jinja + HTMX)

### Déploiement
- Service **s6 supplémentaire** dans le même conteneur : `docker/services.d/veille-ui/run`, sous
  l'utilisateur `hermes`. Il utilise le même `hermes` CLI que `make run-now`, donc
  `run` se comporte à l'identique. Il est supervisé et redémarré par s6 ; une panne de
  l'interface n'arrête pas le gateway. Désactivable avec `VEILLE_UI_ENABLED=false`.
- Environnement Python séparé `/opt/veille-ui/.venv` (avec `uv`, déjà dans l'image) pour ne pas
  toucher le venv de Hermes. Dépendances : `fastapi`, `uvicorn`, `jinja2`, `python-multipart`,
  `itsdangerous`, `argon2-cffi`, `pyyaml`. HTMX est **copié dans le dépôt** (`ui/static/`),
  sans CDN.
- `docker-compose.yml` : `ports: ["127.0.0.1:8088:8088"]`, **jamais** `0.0.0.0`. La CI vérifie
  ce point avec un grep.
- Accès : `ssh -N -L 8088:127.0.0.1:8088 user@serveur`, puis http://localhost:8088. À documenter
  dans le README (avec un exemple `~/.ssh/config` contenant `LocalForward`).

### Sécurité (défense en profondeur)
- `VEILLE_UI_PASSWORD_HASH` (argon2) dans `.env`, généré par `make ui-password`.
  `VEILLE_UI_SECRET` sert à signer les cookies.
- Cookie de session `HttpOnly`, `SameSite=Strict`, expiration 12 h. **Jeton CSRF** sur chaque
  POST (formulaires et en-tête HTMX).
- Limitation des tentatives de connexion (en mémoire). En-têtes CSP stricts (`script-src 'self'`).
- Aucun chemin de fichier fourni par l'utilisateur : tout passe par le slug validé
  (`^[a-z0-9-]{2,40}$`) et les noms de rapports par liste blanche, ce qui interdit toute
  traversée de chemin.
- Journal d'audit `/opt/data/veilles/.audit.log` : qui a fait quoi, et quand.

### Écrans
1. **Tableau de bord** : liste des veilles (nom, fréquence lisible, état actif ou en pause,
   prochain run, dernier run et statut, dernier PDF). Actions en ligne : pause/reprise,
   « Lancer maintenant ».
2. **Créer / modifier une veille** : nom, sujet, fréquence (préréglages + mode cron avancé avec
   aperçu des 3 prochaines exécutions), destinataires (une adresse par ligne, validées), titres
   du PDF, modèle (facultatif). Le brief est édité dans un textarea pré-rempli depuis un modèle
   (vierge ou Industrie 4.0), avec aperçu Markdown.
3. **Détail d'une veille** : historique des rapports (téléchargement et aperçu du PDF), historique
   des runs (`hermes cron runs`), dernière erreur. Bouton « Envoyer un e-mail de test aux
   destinataires ».
4. **Archiver** : suppression douce. Le job est supprimé et le répertoire déplacé dans
   `.archive/`. Confirmation obligatoire.
5. **Santé** : résultat du watchdog (lecture de `state.json` si monté, sinon les mêmes contrôles
   faits à la volée), quota Brevo (`GET /v3/account`).

### Code
```
ui/
  app.py           # FastAPI : routes, dépendance d'authentification, middleware CSRF
  auth.py
  templates/       # base.html, dashboard.html, veille_form.html, veille_detail.html, partials/
  static/          # htmx.min.js, app.css
```
Les routes n'appellent que `veille_core.service`.

---

## Phase 3 — Confort (après la mise en service)
- Versions du brief : copie horodatée de `config.md` à chaque enregistrement, avec diff et
  restauration dans l'interface.
- Coût par run (jetons), si `hermes cron runs` l'expose.
- Décaler automatiquement deux veilles programmées à la même heure (limite de 4 Go de RAM).
- Dupliquer une veille.
- Contrôle qualité du rapport avant l'envoi (nombre de sources, aucune URL brute, présence de la
  synthèse) via un script déterministe appelé par le prompt.

---

## Fichiers clés

Modifiés : `docker/cont-init.d/50-seed-veille`, `Dockerfile`, `docker-compose.yml`, `Makefile`,
`.env.example`, `scripts/watchdog.py`, `scripts/install-watchdog.sh`,
`deploy/systemd/veille-watchdog.service`, `skills/email/brevo-email/scripts/brevo_send.py`
(+ `SKILL.md`), `skills/productivity/styled-pdf-report/scripts/build_report.py`,
`scripts/smoke-test.sh`, `scripts/deploy.sh`, `.github/workflows/build.yml`, `README.md`,
`CHANGELOG.md`.

Nouveaux : `veille_core/`, `ui/`, `veille/job-prompt.md.j2`, `veille/templates/`,
`docker/services.d/veille-ui/run`, `tests/`.

## Ordre de livraison (un commit ou une PR par étape)
1. Phase 0 (0.2 → 0.6 d'abord, car ce sont des bugs réels), puis tests et CI.
2. `veille_core` + `veillectl` + migration + nouveau seed, validés sur une copie de `./data`.
3. Watchdog multi-veilles.
4. Interface en lecture seule (tableau de bord, détail, rapports), puis authentification.
5. Interface en écriture (créer, modifier, pause, lancer, archiver).
6. Phase 3 selon les besoins.

## Vérification
- `pytest tests/` (core, brevo avec HTTP simulé, watchdog, routes de l'interface avec
  `TestClient` : authentification obligatoire, CSRF refusé, slug invalide refusé).
- `make smoke` étendu : `veillectl list` répond, l'interface répond `401/303` sans session, le
  rendu du template et `hermes cron list` dans l'image fonctionnent (ce qui couvre le contrat
  avec la CLI amont).
- **Migration** : copier le `data/` de production dans un répertoire de test, lancer le conteneur
  avec `COMPOSE_PROJECT_NAME=veille-test`, une **clé Brevo de test** et un job en pause.
  Vérifier : même `job_id`, rapports présents dans `veilles/industrie-4-0/rapports/`, deux
  redémarrages sans doublon de job.
- **De bout en bout** : créer une veille « test » en fréquence quotidienne depuis l'interface (via
  le tunnel SSH), avec deux destinataires. « Lancer maintenant », puis vérifier le PDF, l'e-mail
  reçu en Cci par les deux (`--verify` delivered) et l'historique des runs. Mettre en pause et
  vérifier que `next_run_at` disparaît et que le watchdog ignore la veille.
- **Exposition** : `ss -ltnp | grep 8088` ne montre que `127.0.0.1`. `curl` depuis une autre
  machine échoue.
- `make watchdog-check` avec une veille dont le rapport est trop vieux : une alerte, puis aucun
  rappel avant 3 jours.
