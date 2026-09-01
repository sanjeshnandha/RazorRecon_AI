.DEFAULT_GOAL := help
SHELL := /bin/bash
export DATABASE_URL ?= postgresql://finctl:finctl@localhost:5433/finctl

PYTHON      ?= python3.12
SEED        ?= 42
SETTLEMENTS ?= 100
LABEL       ?= demo
TICK        ?= 10
PORT        ?= 8000

help:                     ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:                  ## install python deps
	$(PYTHON) -m pip install -r requirements.txt

db-up:                    ## start postgres (docker compose)
	docker compose up -d
	@echo "waiting for postgres…"
	@until docker compose exec -T db pg_isready -U finctl -d finctl >/dev/null 2>&1; \
	  do sleep 1; done; echo "ready."

db-down:                  ## stop postgres
	docker compose down

schema:                   ## (re)create the schema -- DESTRUCTIVE
	psql "$$DATABASE_URL" -q -f db/schema.sql
	psql "$$DATABASE_URL" -q -f db/indexes.sql
	@echo "schema loaded."

generate:                 ## generate a seeded dataset
	$(PYTHON) -m generator.generate --seed $(SEED) --settlements $(SETTLEMENTS) --label $(LABEL)

generate-clean:           ## generate WITHOUT anomalies (the phase-4 gate)
	$(PYTHON) -m generator.generate --seed $(SEED) --settlements $(SETTLEMENTS) \
	  --label clean --clean

reconcile:                ## run the engine over the newest dataset
	$(PYTHON) -m scripts.reconcile

tick:                     ## append one settlement cycle, then re-reconcile
	$(PYTHON) -m generator.append --settlements $(TICK) $(if $(DATASET),--dataset $(DATASET),)

test:                     ## run the golden tests (needs a database)
	$(PYTHON) -m pytest tests/ -q

web:                      ## copy the SPA into api/static
	./web/build.sh

serve: web                ## run the API + UI on http://localhost:$(PORT)
	uvicorn api.main:app --host 0.0.0.0 --port $(PORT) --reload

demo: db-up schema generate reconcile ## full pipeline from an empty database
	@echo
	@echo "  now run:  make serve   →  http://localhost:$(PORT)"

.PHONY: help install db-up db-down schema generate generate-clean reconcile test web serve demo
