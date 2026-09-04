.PHONY: db lint unit functional regression check dev-api dev-daemon ingest signals pack digest

PY ?= python3
TEST_DSN ?= postgresql://postgres@localhost:5432/startupos_test

db:            ## apply schema to DATABASE_URL
	$(PY) scripts/apply_schema.py

lint:
	ruff check . && ruff format --check .

unit:
	$(PY) -m pytest tests/unit -q

functional:    ## needs Postgres at $(TEST_DSN)
	STARTUPOS_TEST_DSN=$(TEST_DSN) $(PY) -m pytest tests/functional -q -m functional

regression:
	$(PY) -m pytest tests/regression -q -m regression

check: lint unit functional regression
	@echo "✓ all gates green"

ingest:        ## one ingest pass (live if keys present, else fixtures)
	$(PY) -m ingest.runner

signals:
	$(PY) -m signals.engine

pack:
	$(PY) -m brain.pack

digest:
	$(PY) -m signals.digest

dev-api:
	uvicorn api.main:app --reload --port 8000

dev-daemon:
	$(PY) -m daemon.main
