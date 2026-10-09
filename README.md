# veille-hermes

Veilles technologiques **conteneurisées** et pilotables depuis une **interface web privée**.

Chaque veille a son sujet, son brief de mission, sa fréquence et ses destinataires. À chaque
échéance, un agent recherche les actualités de la période écoulée, rédige un rapport structuré
(badges de maturité/confiance, sources numérotées, graphiques, encadrés d'analyse), le convertit
en **PDF stylé** et l'**envoie par e-mail** (destinataires en copie cachée).

La veille d'origine, **Industrie 4.0** (chaque lundi à 6h30, en heures creuses DeepSeek), est créée
ou migrée automatiquement.

## Pourquoi un conteneur ?

Une veille n'est pas un simple script : c'est un **job cron interne à
[Hermes Agent](https://github.com/NousResearch/hermes-agent)**, exécuté par le scheduler de son
gateway. L'image **dérive de l'image officielle** `nousresearch/hermes-agent` (version épinglée),
ce qui apporte la supervision s6, un volume de données unique (`/opt/data`) et des mises à jour
par simple reconstruction.

## Démarrage rapide

```bash
# 1. configurer les secrets
cp .env.example .env && chmod 600 .env && $EDITOR .env
#    DEEPSEEK_API_KEY, BREVO_API_KEY, BREVO_SENDER_*, ALERT_EMAIL, VEILLE_RECIPIENTS
#    VEILLE_DATA_DIR : dossier des données, HORS du dépôt (défaut proposé : /srv/veille-hermes/data)

# 2. premier déploiement : crée le dossier de données, build, démarrage, vérifications
./scripts/deploy.sh --init

# 3. activer l'interface web : générer le mot de passe, coller les 2 lignes dans .env
make ui-password && docker compose up -d

# 4. déclencher un rapport immédiatement (test de bout en bout)
make veilles && make run-now VEILLE=industrie-4-0
```

À chaque démarrage, le conteneur **s'amorce et se réconcilie tout seul** : migration de
l'ancienne disposition, création de la veille par défaut sur une installation neuve (en pause
si `VEILLE_RECIPIENTS` est vide), puis réalignement de chaque job sur sa veille.

## Interface web

L'interface (FastAPI + HTMX) n'est publiée **que sur `127.0.0.1:8088` de l'hôte** : elle n'est
jamais exposée au réseau. On y accède par un tunnel SSH depuis son poste :

```bash
ssh -N -L 8088:127.0.0.1:8088 utilisateur@serveur      # puis http://localhost:8088
```

ou durablement dans `~/.ssh/config` :

```
Host veille
    HostName serveur.exemple
    User utilisateur
    LocalForward 8088 127.0.0.1:8088
```

Fonctions :

- **tableau de bord** : état de chaque veille (active, en pause, en échec), prochain et dernier
  run, dernier rapport et nombre de rapports (lien vers l'historique). Pause, reprise et
  lancement immédiat en un clic ;
- **créer / modifier une veille** : nom, sujet, fréquence (quotidienne, jours ouvrés,
  hebdomadaire, mensuelle ou expression cron, avec aperçu des prochaines exécutions),
  destinataires, brief de mission (à partir d'un modèle, avec aperçu), mise en forme du PDF,
  modèle LLM ;
- **détail** : historique complet des rapports (PDF et Markdown, en tête de page), journal du
  lancement en cours (rafraîchi en direct), coût des exécutions,
  historique d'exécution Hermes, e-mail de test, **archivage** (rien n'est effacé) ;
- **veille composants** : **liste unique de tous les composants**, une ligne chacun (types d'avis en
  badges, fins de vie en rouge et en tête, échéance, lien vers la source), avec **recherche en temps
  réel** (mots-clés sur MPN, fabricant, réf., intitulés et résumés des avis, statut Digi-Key) et un
  **filtre par colonne** ; un clic sur un composant déplie son détail (avis complets, sources, état
  Digi-Key). Même présentation compacte dans le PDF (tableau puis « Détail par composant », MPN
  cliquable) et l'e-mail ; boutons « Lancer l'analyse »
  (complète) et « Vérifier les statuts Digi-Key » (immédiat, sans LLM ni e-mail) — voir plus bas ;
- **sources** : clés d'API des distributeurs (Digi-Key), saisie masquée et test de connexion ;
- **santé** : état des veilles, crédits Brevo et journal d'audit.

Sécurité, en plus du tunnel : un mot de passe administrateur (hash argon2 dans `.env`), une
session signée valable 12 h, un jeton CSRF sur chaque action, une limite de tentatives de
connexion, la vérification de l'en-tête `Host` (contre le *DNS rebinding*), une CSP stricte
(aucun script en ligne, HTMX embarqué) et des noms de fichiers en liste blanche. Sans
`VEILLE_UI_PASSWORD_HASH`, l'interface reste **désactivée**.

## Veille composants (PCN et fins de vie)

Une **veille composants** surveille une liste de références électroniques et détecte les avis
publiés par les fabricants ou repris par les distributeurs : **fin de vie** (EOL, PDN, obsolète),
**dernière commande** (LTB), **NRND** et **modifications** (PCN). Les fins de vie sont mises en
évidence partout : bandeau rouge dans l'interface, encadré rouge dans le PDF, objet d'e-mail
préfixé « [EOL ×N] ».

```
dimanche 23h — job de la veille composants
  1. veillectl components scan <slug>      statut Digi-Key de TOUTES les références (déterministe)
                                           + lot du jour pour la recherche web (40 par défaut)
  2. agent : recherche web (pages PCN des fabricants, Mouser, Digi-Key, Farnell…) -> D-pcn.json
  3. veillectl components finalize …       validation, dédoublonnage, enregistrement, rapport PDF
lundi 6h30 — job de la veille thématique rattachée
  6 bis. veillectl components chapter …    chapitre « Composants — PCN & fins de vie » dans son e-mail
```

- **Liste** : `MPN ; Fabricant ; Réf. interne`, une par ligne, collage depuis un tableur accepté
  (interface, `veillectl components add --file bom.csv`, ou Telegram : « Ajoute le LM317T de Texas
  Instruments à la veille composants »).
- **Import d'une nomenclature CSV ou Excel** (`.csv`, `.xlsx`, `.xlsm`) : dans le formulaire de la
  veille, « Importer un fichier ». Les colonnes MPN, Fabricant et Réf. interne sont reconnues par
  règles (libellés FR / EN usuels, forme des valeurs ; aucun service externe), puis **proposées** :
  feuille, ligne d'en-tête, colonnes et fabricant par défaut se corrigent avant d'appliquer, avec un
  aperçu et les lignes ignorées. « Ajouter à la liste » ou « Remplacer la liste », puis enregistrer
  la veille comme d'habitude.
  - Telegram : envoie le fichier à Hermes. Il le dépose dans `<données>/imports/`, te montre la
    correspondance (`preview_component_import`) et importe après ton accord (`import_components`).
  - CLI : `veillectl components import <slug> bom.xlsx --dry-run`, puis sans `--dry-run`
    (`--replace`, `--mpn COL`, `--manufacturer COL`, `--default-manufacturer X`…).
- **Incrémental** : chaque référence garde la date de sa dernière vérification (Digi-Key et web) ;
  la recherche web ne remonte qu'à cette date (moins 30 jours de marge) et un avis déjà connu
  n'est jamais signalé à nouveau. Avis et dates sont conservés dans `components.json` et
  `notices.json`, avec leurs liens sources.
- **Volume** : le statut Digi-Key couvre toutes les références à chaque exécution (≈ 0,6 s par
  référence, 900 au plus) ; la recherche web, coûteuse en tokens, tourne par **lots**
  (`batch_size`). Avec 300 références et des lots de 40, chacune est recherchée sur le web toutes les
  8 semaines environ, et **en priorité** dès que son statut Digi-Key change.
- **À la demande** (interface ou Telegram) : « Lancer l'analyse » / `run_veille_now` exécute tout de
  suite l'analyse complète ; « Vérifier les statuts Digi-Key » / `check_component_statuses` (ou
  `veillectl components scan <slug> --status-only`) relit en arrière-plan le statut de toutes les
  références, sans LLM, sans coût en tokens et sans e-mail — utile juste après un import de
  nomenclature. Le lot de la prochaine exécution planifiée n'est pas modifié.
- **Livraison** : rattachée à une veille thématique (« Chapitre composants » dans son formulaire),
  la veille composants n'envoie rien elle-même : ses nouveaux avis partent en chapitre de l'e-mail
  de cette veille. Sinon, elle envoie son propre e-mail (HTML lisible sur mobile + PDF).
- **Digi-Key** (facultatif mais recommandé) : créer une application « Production » sur
  [developer.digikey.com](https://developer.digikey.com) avec l'API *Product Information v4*, puis
  saisir son Client ID et son Client Secret dans l'interface, page **Sources** (bouton « Tester la
  connexion »). Sans ces clés, seule la recherche web est utilisée (le rapport le signale).
  - Les clés saisies ne sont **jamais réaffichées** (aperçu masqué `••••1234`). Elles sont stockées
    dans `veilles/.credentials.json` (droits 0600), **exclues des sauvegardes** (à ressaisir après
    une restauration), et chaque modification est tracée dans le journal d'audit, sans les valeurs.
  - Variante « infrastructure » : `DIGIKEY_CLIENT_ID` / `DIGIKEY_CLIENT_SECRET` dans `.env`. Elles
    sont alors prioritaires et affichées en lecture seule dans l'interface.
  - Volontairement, aucune clé ne passe par Telegram ou le MCP, qui ne fait qu'indiquer si une source
    est configurée.

Limites : la recherche web reste probabiliste (un avis peut être manqué : le statut Digi-Key sert
de filet) ; les avis PCN de Digi-Key eux-mêmes ne sont pas lus (leur API exige une connexion
interactive, OAuth « 3-legged ») ; Mouser et Nexar ne sont pas intégrés.

## Piloter depuis Telegram (MCP)

Le serveur MCP `veille-mcp` expose les veilles au **Hermes de l'hôte** (celui qui porte
Telegram) : on interroge, modifie et lance les veilles en conversation, sans passer par
l'interface.

```bash
make mcp-install              # déclare le serveur dans ~/.hermes/config.yaml et le teste
hermes gateway restart        # charge les outils
```

Puis, dans Telegram : « Liste mes veilles », « Résume le dernier rapport de la veille
industrie-4-0 », « Passe la veille cyber au mardi 7h30 », « Ajoute dsi@exemple.fr aux
destinataires », « Mets la veille industrie-4-0 en pause », « Crée une veille sur l'hydrogène
industriel, chaque lundi à 8h, pour moi@exemple.fr », « Quelles fins de vie ont été détectées
ce mois-ci ? »…

- **Transport stdio via `docker exec -i`** : aucun port réseau n'est ouvert.
- **`trust: untrusted`** : les 14 outils de lecture s'exécutent directement ; les 16 outils de
  modification demandent ton **approbation dans Telegram**. `run_veille_now` et
  `send_test_email` envoient de vrais e-mails ; `archive_veille` exige en plus de répéter le slug.
- Chaque action est tracée dans le journal d'audit avec l'origine « telegram ».
- Variante prudente : `./scripts/install-mcp-host.sh --readonly` (lecture seule) ;
  `--print` affiche la configuration YAML sans rien modifier ; `--uninstall` la retire.

Outils : `list_veilles`, `get_veille`, `get_brief`, `list_brief_templates`, `list_reports`,
`read_report`, `get_run_log`, `run_history`, `preview_schedule`, `get_costs`, `health`,
`list_components`, `list_notices`, `preview_component_import` (lecture) ;
`create_veille`, `update_veille`, `set_recipients`, `add_recipients`, `remove_recipients`,
`update_brief`, `edit_brief`, `add_components`, `remove_components`, `import_components`,
`check_component_statuses`,
`pause_veille`, `resume_veille`, `run_veille_now`,
`send_test_email`, `archive_veille` (modification).

## Consommation et coût

DeepSeek renvoie, à chaque appel, le nombre de tokens consommés : entrée, dont lue en cache,
sortie. Son API ne donne en revanche **ni le coût par requête ni d'historique** : seulement le
solde du compte. Hermes enregistre ces tokens par exécution dans sa base (`state.db`, une
session `cron_<job>_<date>` par run).

L'application en calcule le **coût réel** :
- avec les tarifs de [`veille/pricing.json`](veille/pricing.json), modifiable sans reconstruire
  l'image en le copiant dans `data/veilles/pricing.json` ;
- **heures pleines comprises** : du lundi au vendredi, 01h–04h et 06h–10h UTC, DeepSeek facture le
  **double**. L'estimation de Hermes, elle, n'applique que le tarif creux.

Où le voir :
- **Interface** : section « Coût des exécutions » de chaque veille (durée, tokens, heure pleine ou
  creuse, coût) ; page Santé avec le solde DeepSeek et le coût du mois par veille.
- **Telegram** : « Combien a coûté la veille industrie-4-0 ce mois-ci ? » (outil `get_costs`).
- **CLI** : `docker exec veille-hermes veillectl costs [slug] [--days N]`.

L'aperçu d'une fréquence signale quand elle tombe en heures pleines. Exemple : lundi 8h Paris
= 06h UTC, au tarif ×2. Lundi 6h30 tombe en heures creuses : 04h30 UTC l'été, 05h30 l'hiver.

## Ligne de commande (`veillectl`)

Tout ce que fait l'interface est aussi disponible dans le conteneur :

```bash
docker exec veille-hermes veillectl list
docker exec veille-hermes veillectl show industrie-4-0
docker exec veille-hermes veillectl create --name "Veille Cyber OT" --schedule "30 7 * * 1-5" \
    --recipients rssi@exemple.fr,dsi@exemple.fr --template vierge
docker exec veille-hermes veillectl edit veille-cyber-ot --schedule "0 8 * * 1"
docker exec veille-hermes veillectl pause|resume|run|test-email|archive <slug>
docker exec veille-hermes veillectl costs industrie-4-0
docker exec veille-hermes veillectl reconcile
# veille composants
docker exec veille-hermes veillectl create --kind composants --name "Composants carte X" \
    --schedule "0 23 * * 0" --recipients achats@exemple.fr --components-file /opt/data/bom.csv
docker exec veille-hermes veillectl edit industrie-4-0 --include-components composants-carte-x
docker exec veille-hermes veillectl components list|notices composants-carte-x [--type EOL]
```

## Exploitation

| Commande | Effet |
|---|---|
| `make veilles` | liste les veilles et leur état |
| `make run-now VEILLE=<slug>` | lance une veille et attend la fin |
| `make logs` | suit les logs du gateway |
| `make verify` | vérifie conteneur, veilles, jobs et interface |
| `make tunnel` | rappelle la commande de tunnel SSH |
| `make mcp-install` / `make mcp-check` | branche le serveur MCP sur le Hermes de l'hôte / liste ses outils |
| `make ui-password` | génère le hash du mot de passe et le secret de l'interface |
| `make backup` | archive horodatée des données (sans `.env`, clés d'API saisies dans l'interface ni nomenclatures déposées pour import, 10 dernières conservées), rangée à côté du dossier de données |
| `make move-data DEST=…` | sort les données du dépôt git (`VEILLE_DATA_DIR`), sans rien perdre |
| `make test` / `make lint` | tests unitaires avec couverture / ruff + shellcheck (+ hadolint) |
| `make smoke` | teste l'image (dépendances, chaîne PDF, contrat CLI Hermes, veillectl, interface) |
| `make integration` | test de bout en bout du conteneur complet (migration, interface, watchdog) |
| `make version` / `make bump` | version courante / nouvelle version + branche `release/vX.Y.Z` |
| `make shell` | ouvre un shell dans le conteneur |
| `make down` | arrête le conteneur (données conservées) |
| `make update` | `git pull`, puis déploiement sûr : sauvegarde, build, démarrage, données vérifiées |
| `make watchdog-check` | exécute le contrôle de surveillance (sans envoyer d'e-mail) |
| `make watchdog-install` | installe la surveillance sur l'hôte et teste le canal d'alerte |

### Disposition des données (`VEILLE_DATA_DIR` = `/opt/data`)

Tout l'état vit dans **un dossier de l'hôte**, `VEILLE_DATA_DIR` (`.env`), monté sur `/opt/data` :
veilles, rapports, jobs (`cron/jobs.json`), historique d'exécution et consommation (`state.db`),
journal d'audit. L'image ne contient aucune donnée : la remplacer ne perd rien.

- **Hors du dépôt git** (recommandé : `/srv/veille-hermes/data`) : ni un `git pull`, ni un
  re-clone, ni un `git clean -x` ne peuvent l'atteindre. Sans `VEILLE_DATA_DIR`, le défaut
  historique `./data` (dans le dépôt) reste utilisé, et `deploy.sh` le signale.
- **Jamais recréé vide en silence** : si le dossier est absent, `docker compose up` échoue
  (`bind source path does not exist`) au lieu de démarrer sur un état vierge.
- Le chien de garde, le serveur MCP (chemins des PDF) et `make backup` lisent le même
  chemin (`scripts/data-dir.sh`).

```
veilles/<slug>/
    veille.json       métadonnées : nom, sujet, fréquence, PDF, lien vers le job Hermes
    config.md         brief de mission (éditable dans l'interface)
    recipients.txt    destinataires, un par ligne
    job-prompt.md     prompt RENDU depuis le gabarit de l'image (ne pas éditer)
    rapports/         AAAA-MM-JJ-HHhMM-veille.{md,pdf}, -charts.json (composants : -pcn.json) ;
                      horodatés : deux exécutions le même jour ne s'écrasent plus (anciens
                      noms AAAA-MM-JJ-veille toujours lus)
    runs/             journaux des lancements manuels
    components.json   veille composants : références, statut Digi-Key, dates de vérification
    notices.json      veille composants : avis PCN / EOL détectés, sources, livraisons
imports/            nomenclatures déposées pour import (Telegram / CLI), analyses en cours de
                    l'interface (supprimées après 1 h) ; exclu des sauvegardes
veilles/.archive/   veilles archivées
veilles/.audit.log  journal d'audit (interface, CLI, amorçage)
cron/jobs.json      jobs Hermes (état, prochain et dernier run)
state.db            historique des exécutions Hermes (tokens, coûts)
```

Le **job Hermes fait foi** pour l'état (actif / en pause, prochain et dernier run). Le job ne
contient qu'un prompt court qui renvoie vers `job-prompt.md` : une nouvelle image (gabarit
corrigé) ou une modification de la veille prend effet au run suivant, **sans recréer le job**.

## Surveillance (chien de garde)

Le conteneur délivre en `local` : **un échec serait silencieux**, seule l'absence du rapport le
trahirait. Un chien de garde comble ce trou.

Il tourne **sur l'hôte**, pas dans le conteneur : un contrôleur placé dans ce qu'il surveille ne
pourrait signaler ni un conteneur arrêté, ni une clé d'API expirée. Il est **déterministe (aucun
LLM)**, n'utilise que la bibliothèque standard de Python et ne dépend d'aucune installation Hermes
sur l'hôte : il lit le volume et envoie avec le skill `brevo-email` et le `.env` **du dépôt**.

Contrôle quotidien (10h, heure de Paris) :

1. le conteneur tourne ;
2. pour chaque veille **active** (les veilles en pause sont ignorées) : son job existe, son
   dernier run n'est pas en erreur, et un rapport récent et plausible existe. « Récent » dépend
   de **sa** fréquence : plus grand écart entre deux tirs + 24 h (8 jours pour une veille
   hebdomadaire, 2 jours pour une quotidienne).

Les alertes vont à `ALERT_EMAIL` : alerte immédiate à chaque nouveau problème, rappel tous les
3 jours tant qu'il persiste, puis **e-mail de rétablissement**. L'état est conservé dans
`/var/lib/veille-watchdog/state.json`.

```bash
make watchdog-check      # contrôler maintenant (aucun e-mail envoyé)
make watchdog-install    # (ré)installer la surveillance et tester le canal d'alerte
./scripts/install-watchdog.sh --uninstall
```

## Mise à jour

```bash
make update          # git pull --ff-only, puis ./scripts/deploy.sh
```

`deploy.sh` ne touche jamais aux données, et le vérifie :

1. **refuse de démarrer** si le dossier de données est absent (`--init` uniquement pour une
   première installation) ;
2. **sauvegarde** les données, conteneur arrêté pour une archive cohérente (environ 1 s pour
   40 Mo), à côté du dossier de données ;
3. démarre la nouvelle version et attend la fin de **son** amorçage (réconciliation des jobs) ;
4. vérifie que le conteneur monte bien ce dossier, puis **compare l'inventaire** (veilles et
   rapports PDF) avant et après : s'il manque quoi que ce soit, le déploiement échoue et affiche
   la commande de restauration.

Le conteneur est arrêté pendant la sauvegarde : un rapport en cours de génération est
interrompu (comme lors de tout redémarrage). Éviter de déployer pendant une exécution
planifiée.

Restauration manuelle d'une sauvegarde :

```bash
docker compose stop
tar -xzf /srv/veille-hermes/backups/veille-data-AAAAMMJJ-HHMMSS.tar.gz -C /srv/veille-hermes/data
docker compose start
```

### Sortir les données du dépôt (installation existante)

Une seule fois, sur le serveur :

```bash
make move-data DEST=/srv/veille-hermes/data
```

Le conteneur est arrêté, `./data` est déplacé (renommage ; sur un autre disque : copie, puis
l'original est **renommé**, jamais supprimé), `VEILLE_DATA_DIR` est écrit dans `.env`, puis
`deploy.sh` redéploie et vérifie l'inventaire. Le chien de garde est réinstallé s'il l'était ;
relancer `make mcp-install` si le serveur MCP est branché sur le Hermes de l'hôte.

L'image amont est **épinglée** dans le `Dockerfile`. Dependabot propose ses montées de version
en PR ; la CI les valide (tests, lint, smoke test et test d'intégration sur l'image construite,
dont le contrat avec la CLI `hermes cron`) **avant** toute publication sur `ghcr.io`.

## Versions et CI

Version courante : fichier [`VERSION`](VERSION). Chaque livraison incrémente la version et
passe par une branche `release/vX.Y.Z` mergée dans `main` ; le merge crée l'étiquette et la
release GitHub. Détails, suite de tests et commandes locales : [CONTRIBUTING.md](CONTRIBUTING.md).

### Depuis la v0.2 (veille unique dans `data/veille/`)

Rien à faire : au premier démarrage de la nouvelle image, `data/veille/` est déplacé vers
`data/veilles/industrie-4-0/`, avec l'historique des rapports (anti-répétition) et le **même
job** (édité, pas recréé). Un lien `data/veille → veilles/industrie-4-0` garde l'ancien chemin
lisible. `destinataire.txt` devient `recipients.txt`. Ensuite, sur l'hôte :

1. ajoute `ALERT_EMAIL` (et, pour l'interface, le résultat de `make ui-password`) dans `.env` ;
2. réinstalle la surveillance : `make watchdog-install`.

`make update` sauvegarde les données avant toute mise à jour (voir plus haut).

## Arborescence

```
veille-hermes/
├── Dockerfile                    # image dérivée : deps, skills, gabarits, veille_core, interface
├── docker-compose.yml            # service, volume, secrets, port 127.0.0.1:8088, limites
├── Makefile                      # raccourcis d'exploitation
├── requirements-app.txt          # dépendances de veille_core / veille_ui (venv séparé)
├── veille/                       # gabarit du prompt + modèles de brief
├── veille_core/                  # cœur multi-veilles + CLI veillectl
├── veille_ui/                    # interface web (FastAPI + Jinja + HTMX)
├── veille_mcp/                   # serveur MCP (stdio) pour piloter les veilles depuis Hermes
├── skills/                       # brevo-email, styled-pdf-report
├── docker/                       # hook d'amorçage, service s6 de l'interface, veillectl
├── deploy/systemd/               # units de la surveillance (sur l'hôte)
├── scripts/                      # déploiement, tests (smoke, intégration), watchdog, MCP, versions
├── tests/                        # pytest (faux binaire hermes pour le cœur et l'interface)
├── VERSION                       # numéro de version (source unique)
├── docs/PLAN-CONTAINERISATION.md # analyse + plan de conteneurisation (archive)
└── .github/                      # workflows ci / image / release, Dependabot, modèle de PR
```

## Secrets

Aucun secret n'est versionné. Voir [`.env.example`](.env.example) : `DEEPSEEK_API_KEY`,
`BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, `BREVO_SENDER_NAME`, `ALERT_EMAIL`,
`VEILLE_UI_PASSWORD_HASH`, `VEILLE_UI_SECRET`, et facultativement `DIGIKEY_CLIENT_ID`,
`DIGIKEY_CLIENT_SECRET` (plus simple : page **Sources** de l'interface). Le `.env` est ignoré par git **et** par le build
Docker.

## Architecture

Le conteneur n'a besoin que d'accès **sortants** (recherche web, API DeepSeek, API Brevo, API
Digi-Key). Le
seul port publié est celui de l'interface, lié à `127.0.0.1` ; le serveur MCP passe par
`docker exec -i` (stdio), sans port. La livraison se fait **uniquement
par e-mail** (`deliver: local`) ; le gateway Telegram de l'hôte reste indépendant. L'interface,
`veillectl` et `veille-mcp` ne parlent à Hermes que par sa CLI publique (`hermes cron …`) et la lecture de
`cron/jobs.json`, isolées dans `veille_core/hermes.py`.

## Licence

À définir (les skills et le label de l'image déclarent MIT ; aucun fichier `LICENSE` pour l'instant).
