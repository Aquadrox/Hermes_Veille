# Plan de conteneurisation — Veille technologique hebdomadaire

> Statut : **proposition à valider** avant implémentation.
> Date : 2026-10-05.

---

## 1. Analyse de l'existant

### 1.1 Inventaire de ce qui tourne aujourd'hui

| Composant | Emplacement actuel | Nature |
|---|---|---|
| Job cron « Veille technologique hebdomadaire » | `~/.hermes/cron/jobs.json` (id `e982d99ccf9a`) | **définition de job interne à Hermes** |
| Brief de mission (prompt éditable) | `/root/veille/config.md` | fichier éditable |
| Destinataire e-mail | `/root/veille/destinataire.txt` | fichier éditable |
| Rapports produits (PDF + .md + charts) | `/root/veille/rapports/` | artefacts |
| Skill `brevo-email` | `~/.hermes/skills/email/brevo-email/` | skill (script + doc) |
| Skill `styled-pdf-report` | `~/.hermes/skills/productivity/styled-pdf-report/` | skill (script + CSS + doc) |
| Secrets | `~/.hermes/.env` (`BREVO_API_KEY`, `DEEPSEEK_API_KEY`, …) | secrets |
| Configuration Hermes | `~/.hermes/config.yaml` (`model`, `timezone: Europe/Paris`) | config |
| Runtime agent | Hermes v0.21.2 installé **en git** dans `/usr/local/lib/hermes-agent`, gateway lancé par systemd | application |
| Dépendances système | `pandoc`, `weasyprint`, `poppler-utils` | paquets apt |

### 1.2 Le constat central

**La veille n'est pas un programme autonome : c'est un job cron *à l'intérieur* de Hermes.**
La production du rapport est assurée par un **agent LLM** (recherche web, rédaction, vérification) exécuté par le *scheduler* du gateway Hermes. Les seules briques « classiques » — construction du PDF (pandoc + weasyprint) et envoi du mail (API Brevo) — sont des scripts appelés **par** l'agent via des skills.

→ Conteneuriser la veille revient donc à **conteneuriser une instance Hermes** embarquant les assets de la veille. Il n'existe pas de « simple script » à mettre en boîte ; un conteneur sans l'agent devrait réimplémenter la boucle agent (hors sujet).

### 1.3 La bonne nouvelle : l'image officielle fait 80 % du travail

`nousresearch/hermes-agent:latest` **est déjà présente localement (4,78 Go)** et fournit :

- base `debian:13.4`, Python 3.13, `uv`, Node 26, Playwright/Chromium ;
- **s6-overlay en PID 1** : gateway supervisé, redémarrage auto, arrêt propre ;
- **un seul volume de données : `/opt/data`** (= le `~/.hermes` du conteneur) qui contient `config.yaml`, `.env`, `skills/`, `cron/`, `sessions/`, `logs/` ;
- un arbre d'installation **immuable** `/opt/hermes` (modifications du cœur = PR + nouvelle image, pas d'édition à chaud) ;
- un hook de boot (`stage2-hook.sh`) qui **synchronise `/opt/hermes/skills` → `/opt/data/skills`** ⇒ nos skills custom bakés dans l'image s'auto-déploient au premier démarrage ;
- un pattern **documenté** d'image dérivée : `FROM nousresearch/hermes-agent:latest` + `apt-get install` (en `USER root`) + `USER hermes` ;
- mise à jour = `docker pull` + recréation du conteneur, **données préservées**, migrations de config automatiques.

→ On ne part pas de zéro : on **dérive**.

---

## 2. Architecture cible

```
              docker run --name veille-hermes  … gateway run
                              │
┌─────────────────────────────▼──────────────────────────────────────┐
│  Conteneur « veille-hermes »  (image dérivée, s6-overlay PID 1)     │
│                                                                     │
│  /opt/hermes            ← install Hermes (immuable, héritée amont)  │
│    ├── skills/email/brevo-email/**            ← bakés (dérivée)     │
│    └── skills/productivity/styled-pdf-report/** ← bakés (dérivée)   │
│                                                                     │
│  /opt/data              ← VOLUME PERSISTANT (bind mount)            │
│    ├── config.yaml       model: deepseek-v4-pro / timezone: Paris   │
│    ├── .env              secrets (injectés, jamais bakés)           │
│    ├── cron/jobs.json    job « Veille technologique » (seedé)       │
│    ├── skills/           copie synchronisée des skills bundlés      │
│    ├── logs/             logs gateway (routés aussi vers docker logs)│
│    └── veille/
│        ├── config.md          brief (seedé puis éditable)
│        ├── destinataire.txt   destinataire (éditable)
│        └── rapports/          PDF/MD produits
└─────────────────────────────────────────────────────────────────────┘
```

Le scheduler cron vit **dans** le processus gateway : le conteneur **doit** tourner en `gateway run`.

---

## 3. Décisions de conception (à valider)

| # | Décision | Recommandation |
|---|---|---|
| **D1** | Base de l'image | Image **dérivée** de `nousresearch/hermes-agent` épinglée par digest/tag (pas de `latest` flottant en prod). |
| **D2** | Isolation des données | **Volume dédié** (`./data`) — ne **pas** partager le `~/.hermes` de l'hôte (règle Hermes : jamais deux gateways sur le même répertoire). |
| **D3** | Skills custom | Bakés dans `/opt/hermes/skills/{email,productivity}/…` → auto-synchronisés dans le volume au boot. |
| **D4** | Brief & destinataire | Bakés comme **défauts** puis *seedés* dans le volume au 1er boot (modifiables ensuite **sans rebuild**). |
| **D5** | Job cron | **Seedé idempotemment** au boot (hook `cont-init.d`) : créé seulement s'il n'existe pas. |
| **D6** | Secrets | Injectés via `env_file` / `environment` (jamais dans l'image ; `.env` en `.dockerignore`). |
| **D7** | Fuseau horaire | `TZ=Europe/Paris` **et** `timezone: Europe/Paris` dans le `config.yaml` seedé (le scheduler lit la config Hermes, pas `TZ` seul). |
| **D8** | Modèle | `model.default: deepseek-v4-pro`, `provider: deepseek` dans le `config.yaml` seedé. |
| **D9** | **Livraison** | ⚠️ Le job délivre aujourd'hui vers `origin` (ce chat Telegram). Dans un conteneur neuf il n'y a pas d'`origin` → passer à **`deliver: local`** (e-mail uniquement) **ou** un couple `telegram:<chat_id>` explicite. |
| **D10** | **Token Telegram** | ⚠️ Un token de bot ne peut être utilisé que par **un seul gateway**. Soit le conteneur n'utilise **pas** Telegram (e-mail seul), soit on **migre** entièrement le gateway dans le conteneur et le Hermes de l'hôte s'arrête. **Décision requise.** |
| **D11** | Sortie des rapports | Volume persistant + option de copie/commit vers un dépôt d'archives, ou envoi e-mail seul. |
| **D12** | Mises à jour | `docker compose pull && up -d` sur l'image amont → rebuild de la dérivée → recréation ; données intactes. |

---

## 4. Structure du dépôt cible

```
veille-hermes/
├── Dockerfile                     # image dérivée : deps + skills + assets
├── docker-compose.yml             # service veille (volume, env, TZ, limites)
├── Makefile                       # build / up / logs / run-now / smoke / release
├── .env.example                   # secrets attendus (sans valeurs)
├── .gitignore                     # .env, data/, *.pdf produits…
├── .dockerignore                  # .env, data/, .git…
├── README.md                      # démarrage, exploitation, dépannage
├── CHANGELOG.md                   # journal des versions (Keep a Changelog)
├── docs/
│   ├── PLAN-CONTAINERISATION.md   # ce document
│   └── RUNBOOK.md                 # exploitation : forcer un run, lire les logs, restaurer
├── veille/
│   ├── config.md                  # le brief (défaut, seedé)
│   └── destinataire.txt           # destinataire par défaut
├── skills/
│   ├── email/brevo-email/
│   └── productivity/styled-pdf-report/
├── docker/
│   └── cont-init.d/
│       └── 50-seed-veille         # seed assets + création idempotente du job cron
├── scripts/
│   └── smoke-test.sh              # build + run + déclenchement + vérif
└── .github/workflows/
    └── build.yml                  # build & push de l'image (ghcr.io)
```

---

## 5. Plan d'implémentation par phases

### P0 — Geler l'existant
- Exporter le job cron courant (`jobs.json`) comme référence.
- Copier les 2 skills et le brief dans le dépôt.
- **Vérif** : `diff` entre l'exporté et la copie = vide.

### P1 — Squelette du dépôt
- Arborescence ci-dessus, `README`, `CHANGELOG`, `.gitignore`, `.dockerignore`, `.env.example`.
- **Vérif** : `git status` propre, aucun secret versionné.

### P2 — Dockerfile dérivé
- `FROM nousresearch/hermes-agent:<tag>` → `USER root` → `apt-get install -y --no-install-recommends pandoc weasyprint poppler-utils fonts-dejavu` → `USER hermes`.
- `COPY skills/ /opt/hermes/skills/` (dépôt des skills custom).
- `COPY veille/ /opt/hermes/veille-defaults/` (défauts à seeder).
- **Vérif** : `docker build` OK ; `docker run --rm <img> sh -lc 'pandoc --version; weasyprint --version'` renvoie les versions.

### P3 — Hook de seed (`docker/cont-init.d/50-seed-veille`)
- Crée `$HERMES_HOME/veille/{rapports}` s'ils manquent.
- Copie `config.md` / `destinataire.txt` **uniquement s'ils n'existent pas** (préserve les éditions).
- Crée le job cron **s'il n'existe pas** (`hermes cron list` + création idempotente), avec les bons champs (schedule `every monday 8am`, `workdir`, `skills`, `deliver`).
- **Vérif** : redémarrer le conteneur 2× → aucun doublon de job, éditions préservées.

### P4 — Compose + Makefile
- Volume `./data:/opt/data`, `env_file: .env`, `TZ=Europe/Paris`, `restart: unless-stopped`, limites mémoire/CPU, `container_name: veille-hermes`.
- **Vérif** : `docker compose up -d` puis `docker compose logs -f` montre le gateway supervisé.

### P5 — Smoke test de bout en bout
- `docker exec veille-hermes hermes cron run <id>` (déclenchement manuel).
- Vérifier : PDF produit dans `/opt/data/veille/rapports/`, e-mail `delivered` (API events Brevo).
- **Vérif** : statut `ok` + `delivered` ; sortie attendue consignée dans le README.

### P6 — CI/CD
- GitHub Actions : build multi-étapes, cache BuildKit, push sur `ghcr.io/<org>/veille-hermes:{sha,latest}`.
- Option : rebuild automatique quand l'image amont bouge (schedule hebdo).
- **Vérif** : un push sur `main` produit une image tirée et démarrée en local.

### P7 — Bascule (cutover)
- **Désactiver** le job de l'hôte (sinon double exécution + double e-mail).
- Démarrer le conteneur, vérifier un run manuel, puis laisser le tir du lundi.
- **Vérif** : un seul e-mail reçu le lundi, un seul PDF produit.

### P8 — Documentation & exploitation
- `README` (démarrage, mise à jour, sauvegarde), `docs/RUNBOOK.md`, `CHANGELOG` à jour.
- **Vérif** : une personne extérieure peut déployer et forcer un run en suivant le README seul.

---

## 6. Risques et points de vigilance

| Risque | Impact | Mitigation |
|---|---|---|
| **Deux gateways, un seul token Telegram** | Le bot répond en double / instable | Trancher D10 : e-mail seul, ou migration complète du gateway |
| **Double exécution du job** (hôte + conteneur) | 2 e-mails le lundi | Désactiver le job hôte avant la bascule (P7) |
| **Secrets versionnés** | Fuite de clés | `.env` en `.gitignore` **et** `.dockerignore` ; secrets par variables d'env |
| **Taille d'image** (~5 Go) | Pull lent, stockage | Épingler une version, nettoyer les couches (`rm -rf /var/lib/apt/lists/*`), registre dédié |
| **RAM/CPU** | OOM, lenteur | Limites `deploy.resources` (2–4 Go conseillés) |
| **Fuseau horaire** | Rapport à 10h au lieu de 8h | `TZ` **et** `timezone:` en config (leçon apprise) |
| **`deliver: origin` sans origine** | Aucune livraison | Passer à `local` ou cible Telegram explicite |
| **Dérive de l'image amont** | Casse à la mise à jour | Épingler le tag/digest + rebuilds planifiés + smoke test |
| **Droits UID/GID du volume** | « Permission denied » | `HERMES_UID`/`HERMES_GID` (ou `PUID`/`PGID`) alignés sur l'hôte |
| **Fonts manquantes** | PDF dégradé | Installer `fonts-dejavu`/`fonts-liberation` dans l'image |

---

## 7. Ce qu'il me faut de ta part pour démarrer

1. **Nom du dépôt et hébergeur** (GitHub ? GitLab ? registre d'images cible ?).
2. **Décisions D9/D10** : le conteneur doit-il garder l'envoi Telegram, ou rester e-mail seul ?
3. **Stratégie d'image** : registre privé (ghcr.io) ou build local sur le VPS ?
4. Confirmation que l'on **conserve** le Hermes de l'hôte le temps de la bascule (cohabitation), puis décision de sort.
