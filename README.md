# StartupOS

The founder operating system that opens with work already done. Self-hosted company brain + one agent daemon + approval gate. See `../StartupOS_Research_Brief.md` (what and why) and `../StartupOS_Architecture_Brief.md` (how). `CONTRACTS.md` is the shared contract every directory honors.

## Layout
```
brain/     Markdown brain repo, sync to Postgres, nightly context pack compiler
ingest/    cron workers: Linear, Slack, Brex (deterministic, no LLM)
signals/   rule engine over ingested state → signals; Tier-0 digest text
daemon/    Claude Agent SDK daemon: llm gateway (tiers, budgets, ledger), scheduler, skills, approvals, executors, Slack gateway
api/       FastAPI serving module snapshots, finance, approvals, onboarding, ask
web/       Next.js onboarding + cockpit shell
common/    shared settings, db, models, ids
db/        schema.sql
tests/     unit · functional (Postgres) · regression (golden) · fixtures
```

## Run locally
```
cp .env.example .env            # fill keys
docker compose up -d db && make db
make ingest && make signals && make pack && make digest
make dev-api                    # http://localhost:8000/docs
make dev-daemon
```
Without keys, `make ingest` loads `tests/fixtures/` so everything downstream still works.

## Gates
`make check` = ruff + unit + functional + regression. Nothing merges red.

## Rules (from the Architecture Brief)
No model call in ingest, polling or rendering. Every model call goes through `daemon/llm.py` and lands in `runs`. Every action is an `approvals` row first; executors run only `status='approved'`. Credentials via env / Key Vault only. StartupOS never moves money.
