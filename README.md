# veille-hermes

Veille technologique hebdomadaire **conteneurisée** (Industrie 4.0, robotique, automatisation,
IA industrielle).

Chaque **lundi à 8h (heure de Paris)**, un agent : recherche les actualités de la semaine écoulée,
rédige un rapport structuré (badges de maturité/confiance, sources numérotées, graphiques,
encadrés d'analyse), le convertit en **PDF stylé** et l'**envoie par e-mail**.

## Pourquoi un conteneur ?

La veille n'est pas un simple script : c'est un **job cron interne à
[Hermes Agent](https://github.com/NousResearch/hermes-agent)**, exécuté par le scheduler de son
gateway. L'image **dérive de l'image officielle** `nousresearch/hermes-agent`, ce qui apporte la
supervision s6, un volume de données unique (`/opt/data`) et des mises à jour par simple
`pull` + recréation.

## Démarrage rapide

```bash
# 1. configurer les secrets
cp .env.example .env && $EDITOR .env        # DEEPSEEK_API_KEY, BREVO_API_KEY, BREVO_SENDER_*

# 2. déployer (build + démarrage + vérifications)
./scripts/deploy.sh

# 3. déclencher un rapport immédiatement (test de bout en bout)
make run-now
```

Au premier démarrage, le conteneur **s'amorce tout seul** : création des répertoires, copie des
assets (brief, destinataire) sur le volume, réglage du modèle et du fuseau, création du job cron.
Les éditions ultérieures du brief et du destinataire, faites **dans le volume**, sont préservées.

## Exploitation

| Commande | Effet |
|---|---|
| `make logs` | suit les logs du gateway |
| `make verify` | vérifie conteneur, job cron et assets |
| `make run-now` | déclenche un rapport immédiatement |
| `make smoke` | teste l'image (dépendances, skills, chaîne PDF) |
| `make shell` | ouvre un shell dans le conteneur |
| `make down` | arrête le conteneur (volume conservé) |
| `make update` | met à jour les sources (`git pull`) puis reconstruit et redéploie |
| `make watchdog-check` | exécute le contrôle de surveillance (sans envoyer d'e-mail) |
| `make watchdog-install` | installe la surveillance sur l'hôte et teste le canal d'alerte |

Fichiers modifiables sans reconstruire l'image (sur le volume, `./data`) :

- `data/veille/config.md` — le brief de mission (le prompt éditable du rapport)
- `data/veille/destinataire.txt` — l'adresse d'envoi
- `data/veille/rapports/` — les rapports produits

## Surveillance (chien de garde)

Le conteneur délivre en `local` : **un échec serait silencieux** — seule l'absence du rapport le
trahirait. Un chien de garde comble ce trou.

Il tourne **sur l'hôte**, pas dans le conteneur — un contrôleur placé dans ce qu'il surveille ne
pourrait signaler ni un conteneur arrêté, ni une clé d'API expirée. Il est en outre **déterministe
(aucun LLM)** : il alerte encore quand la production, elle, est en panne.

Contrôles quotidiens (10h, heure de Paris — soit 2 h après le tir du lundi) :

1. le conteneur tourne ;
2. un rapport récent et plausible existe sur le volume (date lue dans le **nom** du fichier, donc
   immunisée contre les copies, plus un seuil de taille) ;
3. le dernier run du job s'est terminé en succès.

Comportement : alerte immédiate à la première détection, rappel tous les 3 jours tant que le
problème persiste, puis **e-mail de rétablissement** au retour à la normale. L'état est conservé
dans `/var/lib/veille-watchdog/state.json`, ce qui évite le spam quotidien.

```bash
make watchdog-check      # contrôler maintenant (aucun e-mail envoyé)
make watchdog-install    # (ré)installer la surveillance et tester le canal d'alerte
./scripts/install-watchdog.sh --uninstall
```

## Mise à jour

```bash
docker compose pull && docker compose up -d     # récupère l'image amont + recrée
```

Le dépôt est reconstruit et publié sur `ghcr.io` par la CI à chaque push sur `main`, à chaque
étiquette `v*`, et **automatiquement chaque lundi** (pour suivre les correctifs de l'image amont).

## Arborescence

```
veille-hermes/
├── Dockerfile                    # image dérivée : deps système + skills + assets
├── docker-compose.yml            # service, volume, secrets, fuseau, limites
├── Makefile                      # raccourcis d'exploitation
├── CHANGELOG.md                  # journal des versions
├── docs/PLAN-CONTAINERISATION.md # analyse + plan par phases (archive)
├── veille/                       # brief, destinataire, prompt du job (défauts)
├── skills/                       # brevo-email, styled-pdf-report
├── docker/cont-init.d/           # hook d'amorçage du premier démarrage
├── deploy/systemd/               # units de la surveillance (sur l'hôte)
├── scripts/                      # deploy.sh, smoke-test.sh, watchdog.py, install-watchdog.sh
└── .github/workflows/build.yml   # CI : build & push ghcr.io
```

## Secrets

Aucun secret n'est versionné. Voir [`.env.example`](.env.example) : `DEEPSEEK_API_KEY`,
`BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, `BREVO_SENDER_NAME`. Le `.env` est ignoré par git **et**
par le build Docker.

## Architecture

Le conteneur n'expose aucun port : il n'a besoin que d'accès **sortants** (recherche web, API
DeepSeek, API Brevo). La livraison se fait **uniquement par e-mail** (`deliver: local`) — le
gateway Telegram de l'hôte reste indépendant.

## Licence

À définir.
