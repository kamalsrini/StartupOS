.PHONY: db lint unit functional regression check dev-api dev-daemon ingest ingest-all signals pack digest

PY ?= PYTHONPATH=. python3
TEST_DSN ?= $(or $(STARTUPOS_TEST_DSN),postgresql://postgres@localhost:5432/startupos_test)

db:            ## apply schema to DATABASE_URL
	$(PY) scripts/apply_schema.py

lint:
	ruff check . && ruff format --check .

unit:
	$(PY) -m pytest tests/unit -q

functional:    ## needs Postgres at $(TEST_DSN)
	STARTUPOS_TEST_DSN=$(TEST_DSN) $(PY) -m pytest tests/functional -q -m functional

regression:    ## goldens (no DB) + the Sprint 3b end-to-end walk, which needs Postgres at $(TEST_DSN)
	STARTUPOS_TEST_DSN=$(TEST_DSN) $(PY) -m pytest tests/regression -q -m regression

check: lint unit functional regression
	@echo "✓ all gates green"

ingest:        ## one dev/CLI pass for TENANT_ID (live if env keys present, else skipped; --fixtures for sample data)
	$(PY) -m ingest.runner --tenant

ingest-all:    ## one pass over every active tenant's connections (what the ingest service loops on)
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
