# veille-hermes — raccourcis d'exploitation.
# `make` ou `make help` pour la liste des cibles.

IMAGE      ?= ghcr.io/aquadrox/hermes-veille
IMAGE_TAG  ?= latest
COMPOSE    ?= docker compose
CONTAINER  ?= veille-hermes
VEILLE     ?=
BACKUP_DIR ?=
PYTHON     ?= python3

.DEFAULT_GOAL := help

.PHONY: help env build up down restart logs ps veilles run-now shell verify smoke integration test lint \
        bump version mcp-install mcp-check release update backup move-data ui-password tunnel watchdog-install watchdog-check

help: ## Affiche cette aide
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

env: ## Crée .env depuis .env.example s'il n'existe pas
	@test -f .env || { cp .env.example .env; chmod 600 .env; echo "→ .env créé : remplis les secrets avant de démarrer"; }

build: ## Construit l'image dérivée
	$(COMPOSE) build

up: env ## Démarre le conteneur (crée .env au besoin)
	$(COMPOSE) up -d
	@echo "→ logs : make logs"

down: ## Arrête et supprime le conteneur (les données sont conservées)
	$(COMPOSE) down

restart: ## Redémarre le conteneur (données conservées)
	$(COMPOSE) restart

logs: ## Suit les logs du gateway
	$(COMPOSE) logs -f --tail=100

ps: ## État du conteneur
	$(COMPOSE) ps

veilles: ## Liste les veilles et leur état
	@docker exec $(CONTAINER) veillectl list

verify: ## Vérifie le bon fonctionnement (conteneur, veilles, jobs, interface)
	@echo "── conteneur ──"; docker ps --filter name=$(CONTAINER) --format '{{.Names}}  {{.Status}}  {{.Ports}}'
	@echo "── veilles ──"; docker exec $(CONTAINER) veillectl list 2>/dev/null
	@echo "── jobs cron ──"; docker exec $(CONTAINER) hermes cron list 2>/dev/null | head -30
	@echo "── interface ──"; curl -s -o /dev/null -w "http://127.0.0.1:8088/login → %{http_code}\n" http://127.0.0.1:8088/login || echo "interface injoignable"

run-now: ## Lance une veille et attend la fin : make run-now VEILLE=<slug>
	@test -n "$(VEILLE)" || { echo "✗ précise la veille : make run-now VEILLE=<slug>  (liste : make veilles)"; exit 1; }
	docker exec $(CONTAINER) veillectl run "$(VEILLE)" --wait

shell: ## Ouvre un shell dans le conteneur
	docker exec -it $(CONTAINER) bash

smoke: ## Construit l'image et exécute le test de fumée
	IMAGE=$(IMAGE) IMAGE_TAG=$(IMAGE_TAG) ./scripts/smoke-test.sh

integration: ## Test de bout en bout du conteneur complet (migration, interface, watchdog)
	IMAGE=$(IMAGE) IMAGE_TAG=$(IMAGE_TAG) ./scripts/integration-test.sh

test: ## Tests unitaires avec couverture (pip install -r requirements-dev.txt)
	$(PYTHON) -m pytest -q --cov=veille_core --cov=veille_ui --cov=veille_mcp --cov-report=term-missing --cov-fail-under=75

lint: ## ruff + shellcheck (+ hadolint s'il est installé)
	ruff check .
	shellcheck scripts/*.sh docker/cont-init.d/50-seed-veille docker/s6-rc.d/veille-ui/run docker/s6-rc.d/veille-ui/finish docker/bin/*
	@if command -v hadolint >/dev/null; then hadolint Dockerfile; else echo "(hadolint absent : vérifié par la CI)"; fi

version: ## Affiche la version courante
	@cat VERSION

bump: ## Nouvelle version de correctif + branche release/vX.Y.Z (PART=minor|major possible)
	./scripts/bump-version.sh $(or $(PART),patch) --branch

update: ## git pull, puis déploiement sûr (sauvegarde, build, démarrage, données vérifiées)
	git pull --ff-only
	./scripts/deploy.sh

backup: ## Archive horodatée des données (sans .env), à côté du dossier de données
	@archive="$$(BACKUP_DIR="$(BACKUP_DIR)" ./scripts/backup.sh)" && echo "→ sauvegarde créée (10 dernières conservées) : $$archive"

move-data: ## Sort les données du dépôt : make move-data DEST=/srv/veille-hermes/data
	@test -n "$(DEST)" || { echo "✗ précise la destination : make move-data DEST=/srv/veille-hermes/data"; exit 1; }
	./scripts/move-data.sh "$(DEST)"

ui-password: ## Génère le hash du mot de passe et le secret de l'interface (à coller dans .env)
	@docker run --rm -it --entrypoint /opt/veille-app/.venv/bin/python -w /opt/veille-app $(IMAGE):$(IMAGE_TAG) -m veille_ui.passwd

mcp-install: ## Branche le serveur MCP sur le Hermes de l'hôte (Telegram), trust: untrusted
	./scripts/install-mcp-host.sh

mcp-check: ## Liste les outils exposés par le serveur MCP du conteneur
	@docker exec -u hermes $(CONTAINER) veille-mcp --check

tunnel: ## Rappelle la commande de tunnel SSH vers l'interface
	@echo "Depuis ton poste :  ssh -N -L 8088:127.0.0.1:8088 <utilisateur>@<serveur>"
	@echo "puis ouvrir        :  http://localhost:8088"

watchdog-check: ## Exécute le contrôle de surveillance maintenant (aucun e-mail envoyé)
	python3 scripts/watchdog.py --volume "$$(./scripts/data-dir.sh)" --env-file "$(CURDIR)/.env" --dry-run

watchdog-install: ## Installe la surveillance (timer systemd quotidien) et teste le canal
	./scripts/install-watchdog.sh --test

release: ## Construit et pousse l'image sur le registre
	docker build -t $(IMAGE):$(IMAGE_TAG) .
	docker push $(IMAGE):$(IMAGE_TAG)
