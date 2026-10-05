# veille-hermes — raccourcis d'exploitation.
# `make` ou `make help` pour la liste des cibles.

IMAGE      ?= ghcr.io/aquadrox/hermes-veille
IMAGE_TAG  ?= latest
COMPOSE    ?= docker compose
CONTAINER  ?= veille-hermes

.DEFAULT_GOAL := help

.PHONY: help env build up down restart logs ps run-now shell verify smoke release

help: ## Affiche cette aide
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

env: ## Crée .env depuis .env.example s'il n'existe pas
	@test -f .env || { cp .env.example .env; echo "→ .env créé : remplis les secrets avant de démarrer"; }

build: ## Construit l'image dérivée
	$(COMPOSE) build

up: env ## Démarre le conteneur (crée .env au besoin)
	$(COMPOSE) up -d
	@echo "→ logs : make logs"

down: ## Arrête et supprime le conteneur (le volume ./data est conservé)
	$(COMPOSE) down

restart: ## Redémarre le conteneur (données conservées)
	$(COMPOSE) restart

logs: ## Suit les logs du gateway
	$(COMPOSE) logs -f --tail=100

ps: ## État du conteneur
	$(COMPOSE) ps

verify: ## Vérifie le bon fonctionnement (job, skills, amorçage)
	@echo "── conteneur ──"; docker ps --filter name=$(CONTAINER) --format '{{.Names}}  {{.Status}}'
	@echo "── job cron ──"; docker exec $(CONTAINER) hermes cron list 2>/dev/null | head -20
	@echo "── marqueur d'amorçage ──"; docker exec $(CONTAINER) ls -la /opt/data/veille/ 2>/dev/null

run-now: ## Déclenche immédiatement le job de veille (test de bout en bout)
	@JOB=$$(docker exec $(CONTAINER) hermes cron list 2>/dev/null | grep -oE '\b[0-9a-f]{12}\b' | head -1); \
	if [ -z "$$JOB" ]; then echo "✗ aucun job trouvé — le seed a-t-il tourné ? (make verify)"; exit 1; fi; \
	echo "→ déclenchement du job $$JOB"; \
	docker exec $(CONTAINER) hermes cron run "$$JOB"
	@echo "→ suivi : make logs"

shell: ## Ouvre un shell dans le conteneur
	docker exec -it $(CONTAINER) bash

smoke: ## Construit l'image et exécute le test de fumée
	IMAGE=$(IMAGE):$(IMAGE_TAG) ./scripts/smoke-test.sh

release: ## Construit et pousse l'image sur le registre
	docker build -t $(IMAGE):$(IMAGE_TAG) .
	docker push $(IMAGE):$(IMAGE_TAG)
