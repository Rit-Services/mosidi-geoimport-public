# Bequeme Befehle für Entwicklung und Abnahme (GNU make; unter Windows z. B. in Git Bash).
PY ?= python
TEST_DATABASE_URL ?= postgresql://geoimport:test@127.0.0.1:55434/geoimport_test_demo
export TEST_DATABASE_URL

.PHONY: help up down test unit integration e2e acceptance lint licenses demo schema schedule-test

help:
	@echo "up | down | test | unit | integration | e2e | acceptance | lint | licenses | demo | schema | schedule-test"

up:            ## Datenbank + Scheduler starten, Schema anlegen
	docker compose up -d --wait db
	docker compose run --rm pipeline init-meta
	docker compose up -d scheduler

down:
	docker compose down

test-db:
	docker compose -f compose.test.yaml up -d --wait

unit:
	$(PY) -m pytest tests/unit

integration: test-db
	$(PY) -m pytest tests/integration

e2e: test-db
	$(PY) -m pytest tests/e2e

test: test-db  ## alle Tests mit Abdeckung
	$(PY) -m pytest --cov=geoimport --cov=webmap --cov-report=term-missing

acceptance:    ## Live-Abnahme gegen die echten Quellen (vg_wfs, INKAR); Protokoll nach docs/abnahme/
	$(PY) scripts/acceptance.py

lint:
	$(PY) -m ruff check geoimport webmap scripts tests
	$(PY) -m ruff format --check .
	$(PY) -m mypy geoimport webmap

licenses:
	$(PY) scripts/gen_third_party.py

schema:        ## JSON-Schemata nach docs/ exportieren
	$(PY) -m geoimport.cli validate-config --schema docs/config.schema.json
	cp geoimport/report/schema.json docs/report.schema.json

demo:          ## Demo-Karte starten und einen Simulationslauf ausführen
	docker compose --profile demo up -d map
	docker compose run --rm -e PIPELINE_ENV=demo pipeline demo --changes

schedule-test: ## Zeitsteuerung prüfen: Testzeitplan jede Minute, ein Lauf mit trigger=schedule
	docker compose run --rm --entrypoint sh pipeline -c \
	  'echo "* * * * * pipeline run --trigger schedule --source geobasis_bb_gemeinden" > /tmp/crontab && timeout 75 supercronic -passthrough-logs /tmp/crontab; true'
