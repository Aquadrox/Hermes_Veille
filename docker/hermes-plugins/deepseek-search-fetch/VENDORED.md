# deepseek-search-fetch (vendorisé)

Plugin Hermes **communautaire** (Jason Swindle, licence MIT, non affilié à DeepSeek) : `web_search` et
`web_extract` passent par l'API Search & Fetch de DeepSeek (`api.deepseek.com/v1/tools/search|fetch`).

- Source : https://github.com/JasonSwindle/hermes-deepseek-search-fetch
- Commit relu : `b17ae50d243beacfd6ee9171e42f2af29b3c6d65` (v1.0.8, celui du catalogue Hermes au 2026-10-21)
- Fichiers repris **sans modification** : `__init__.py`, `plugin.yaml`, `LICENSE` (ni les tests, ni les
  scripts de test réels, qui dépensent du crédit).
- SHA-256 : `__init__.py` 7139f7eb50475598a8e240264288951af1414dfcda8b0c8e1cbe9426b68670c9,
  `plugin.yaml` f537e5b49492d74e321e77ccef63ba3e9d1159cbc26a09c3fe1a70c26509747e.

Relu avant intégration : appels réseau vers `api.deepseek.com` seulement (tout autre hôte est refusé, hors
boucle locale pour les tests), ni fichier, ni sous-processus, ni télémétrie ; clé lue par l'aide de Hermes,
jamais journalisée. Il tourne dans le processus de Hermes, avec ses variables d'environnement : une mise à
jour se relit de la même façon puis se recopie ici (jamais `hermes plugins install` dans le conteneur).

Activé par l'amorçage (`docker/cont-init.d/50-seed-veille`), sauf `VEILLE_WEB_BACKEND=hermes`.
