# Contribuer — versions, branches et tests

## Versionnage

- Le numéro de version vit dans **un seul fichier** : [`VERSION`](VERSION) (`X.Y.Z`).
- **Chaque livraison incrémente la version.** Par défaut, on incrémente `Z` (`make bump`).
  `make bump PART=minor` (incrément de `Y`) ou `make bump PART=major` (incrément de `X`)
  restent possibles pour une rupture explicite.
- Chaque version a sa section `## [X.Y.Z] — AAAA-MM-JJ` dans le [CHANGELOG](CHANGELOG.md)
  ([Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) : Ajouté / Modifié / Corrigé /
  Supprimé / Sécurité).

## Branches

| Branche | Rôle |
|---|---|
| `main` | version publiée ; chaque merge crée l'étiquette `vX.Y.Z` et la release GitHub |
| `release/vX.Y.Z` | une branche par version, ouverte par `make bump`, mergée dans `main` par PR |

Déroulé d'une livraison :

```bash
git switch main && git pull
make bump                     # VERSION 0.2.2 -> 0.2.3, section CHANGELOG, branche release/v0.2.3
# … développement, commits (messages : feat:, fix:, chore:, docs:, test:, ci:) …
make lint && make test        # vérifications locales rapides
git push -u origin release/v0.2.3
# PR release/v0.2.3 -> main ; merge quand la CI est verte
```

Plusieurs versions peuvent être préparées à la suite : chaque branche part de la précédente
(`release/v0.2.3` part de `release/v0.2.2`) et elles se mergent **dans l'ordre**.

## Suite de tests (GitHub Actions)

| Workflow | Déclencheurs | Contrôles |
|---|---|---|
| [`ci`](.github/workflows/ci.yml) | push `main` / `release/**`, PR | version (VERSION > base, CHANGELOG, nom de branche) · ruff · shellcheck · hadolint · `docker compose config` · ports sur 127.0.0.1 · pytest sur Python 3.11 / 3.12 / 3.13, couverture ≥ 75 % · pip-audit |
| [`image`](.github/workflows/image.yml) | push `main` / `release/**` / `v*`, PR, hebdomadaire | build de l'image → smoke test → **test d'intégration** (conteneur complet) → push sur ghcr.io (`main` et étiquettes uniquement) |
| [`release`](.github/workflows/release.yml) | merge sur `main` | étiquette `vX.Y.Z` + release GitHub avec les notes du CHANGELOG |

Une image n'est **jamais** publiée sans avoir passé le smoke test et le test d'intégration.

En local :

```bash
pip install -r requirements-dev.txt
make lint            # ruff, shellcheck (+ hadolint s'il est installé)
make test            # pytest + couverture
make smoke           # image : dépendances, chaîne PDF, contrat CLI Hermes, veillectl, interface
make integration     # conteneur complet : migration, interface, redémarrage, watchdog
```
