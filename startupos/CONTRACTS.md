# StartupOS — Contracts

Shared shapes every track must honor. Source of truth for Pydantic models (`common/models.py`), the API, and the web app. Derived from `db/schema.sql` and the Sep 3 artifact (`MODULE_SNAPSHOTS`, `BREX_SNAPSHOT`, `EXEC`), so the existing UI renders unchanged.

Read `StartupOS_Architecture_Brief.md` §2–§3 before changing anything here. Changes to this file require a PE review note in `startupos_sdlc_state.md`.

## Directory ownership

| Directory | Track | Purpose |
|---|---|---|
| `common/` | PE | `models.py` (Pydantic), `db.py` (connection, `get_conn()`), `settings.py` (env), `ids.py` |
| `brain/` | A | `brain/repo/*.md` seed files, `brain/sync.py` (repo ⇄ `brain_docs`), `brain/pack.py` (context pack compiler) |
| `ingest/` | A | one module per source: `linear.py`, `slack.py`, `brex.py`; `runner.py` (cron entry); `fixtures.py` (load `tests/fixtures/*.json`) |
| `signals/` | A | `rules.py` (rule registry), `engine.py` (evaluate → upsert `signals`), `digest.py` (evening digest text, Tier 0 template) |
| `daemon/` | B | `llm.py` (ALL model calls), `budget.py`, `scheduler.py`, `skills/*.py`, `approvals.py`, `executors/linear.py`, `gateway/slack.py`, `main.py` |
| `api/` | C | FastAPI app: `main.py`, routers `modules.py`, `finance.py`, `approvals.py`, `onboarding.py`, `ask.py` |
| `web/` | C | Next.js app (onboarding + cockpit shell). Uses the API only. |
| `tests/` | all | `unit/`, `functional/`, `regression/`, `fixtures/`, `conftest.py` |
| `db/`, `scripts/`, `Makefile`, `docker-compose.yml` | PE | schema, tooling |

No track edits another track's directory. Shared code goes in `common/` via the PE.

## Environment (`.env`)

```
DATABASE_URL=postgresql://postgres@localhost:5432/startupos
TENANT_ID=unitone
ANTHROPIC_API_KEY=            # daemon only
LINEAR_API_KEY=               # ingest + executor
SLACK_BOT_TOKEN=              # ingest (read) + gateway (post)
SLACK_CHANNELS=C0123,C0456    # channels to ingest
SLACK_DIGEST_CHANNEL=C0789
BREX_API_TOKEN=               # ingest (read-only)
VERCEL_TOKEN=                 # optional
VERCEL_TEAM_ID=team_rTpjbAHqJufb8oQPlY2yfUMQ
STARTUPOS_TIER2_MODEL=claude-sonnet-4-5
STARTUPOS_TIER1_MODEL=claude-haiku-4-5
STARTUPOS_TEST_DSN=postgresql://postgres@localhost:5432/startupos_test
```

Secrets are read only via `common/settings.py`. Never log them, never write them to Postgres, never put them in fixtures.

## Money and precision

Amounts are `Decimal` in Python, `NUMERIC(14,2)` in Postgres, strings in JSON (`"12500.00"`). Signed for transactions (+ in, − out). Currency ISO code alongside.

## Module snapshot (GET `/modules/{name}`)

Mirrors `MODULE_SNAPSHOTS[name]` in the artifact.

```json
{
  "name": "build",
  "title": "Build",
  "crumb": "Engineering · Linear + Vercel",
  "source": "Linear (UnitoneSentinel) · Vercel (UNITONE)",
  "snapshot_at": "2026-09-04T02:00:00Z",
  "live": false,
  "memory": "Team: … · Repos: … · Cadence: …",
  "tiles": [ {"label": "Open issues", "val": "31", "sub": "…", "cls": ""|"warn"|"bad"} ],
  "signals": [ {"id": "…", "kind": "reply|click|open|bounce", "title": "…", "meta": "…", "action": "Assign", "href": null, "approval_id": "…"} ],
  "table": {"title": "Projects", "accent": "Linear · auto-synced", "cols": ["…"], "rows": [["…"]]},
  "table2": {"…": "optional"},
  "approvals": [ Approval ],
  "next": ["GitHub MCP — …"]
}
```

`tiles` has exactly 4 entries. `rows` are arrays of strings; a cell may contain a limited HTML pill `<span class="status-pill status-{draft|sent|scheduled|paid|unpaid|overdue}">…</span>` or a link `<a href target="_blank" rel="noopener">↗</a>` — nothing else (the API sanitizes).

## Finance snapshot (GET `/finance`)

Mirrors `BREX_SNAPSHOT`: `{snapshot_at, live, accounts[], bills[], vendors[], cards[], expenses[], transactions[]}` with the field names used in the artifact (`balance_breakdown.available_balance` as `"517.26 USD"` string, `amount` strings, `display_name`, `timestamp`). Vendors carry `id, name, status, email, rail, country` only — never bank details.

## Approval

```json
{
  "id": "assign-uni-158",
  "tenant_id": "unitone",
  "module": "build",
  "type": "Linear · assign",
  "target": "UNI-158 → Alexey · due 2026-09-10",
  "preview": "Assign … Rationale …",
  "exec": {"server": "Linear", "tool": "save_issue", "input": {"id": "UNI-158", "assignee": "Alexey", "dueDate": "2026-09-10"}},
  "status": "pending|approved|declined|executed|failed",
  "created_by_run": "run_…", "signal_id": "build.unassigned_high:UNI-158",
  "decided_by": null, "decided_at": null, "decline_reason": null,
  "result": null,
  "created_at": "…"
}
```

Lifecycle: daemon inserts `pending` → human `POST /approvals/{id}/decide {decision: approve|decline, reason?, edited_preview?}` → `approved` → executor runs `exec` → `executed` with `result {text, url}` or `failed` with `result {error, code}`. **Executors refuse any row not in `approved`.** `exec: null` means record-only (decision is logged to `brain_docs decisions.md`, nothing runs).

Executor allow-list (v1): `Linear.save_issue`, `Slack.post_message`. Everything else is record-only until reviewed. Brex is never an executor.

## Signal

`{id, module, rule_id, severity, kind, title, meta, entity, entity_id, suggested_skill, href, first_seen_at, last_seen_at, resolved_at}`. `id = f"{rule_id}:{entity_id}"` so re-evaluation is idempotent (upsert bumps `last_seen_at`; rules that no longer match set `resolved_at`).

Rule registry (`signals/rules.py`) — v1 set, all Tier 0:

| rule_id | module | severity | kind | condition |
|---|---|---|---|---|
| `build.unassigned_high` | build | high | click | priority ∈ {1,2}, no assignee, not done |
| `build.stale_in_progress` | build | medium | open | status In Progress, updated_at > 7d |
| `build.duplicate_titles` | security | medium | reply | two open issues with identical normalized title |
| `build.deploy_failed` | web | medium | bounce | deployment state ERROR within the last 7 days of `now` (never anchored to the feed) |
| `finance.bill_due_7d` | finance | high | reply | bill not CLEARED/SETTLED, due_at ≤ now+7d |
| `finance.cash_low` | finance | high | reply | primary available < 3 × avg monthly outflow (or < configured floor) |
| `finance.unmatched_inflow` | finance | low | open | incoming transaction with no matching invoice/payment record |
| `customers.ask_untouched` | customers | high | reply | issue with `customer:*`/`account:*` label, no update for ≥ 3 calendar days |
| `sales.reply_detected` | sales | high | reply | sequences.replied increased since last observation |
| `sales.stale_draft_campaign` | marketing | medium | open | campaign in draft > 14d (from brain gtm.md front-matter) |
| `marketing.analytics_off` | web | low | click | connection posthog/vercel analytics missing |
| `social.queue_backlog` | social | medium | click | linkedin queue pending > 25 |

## Context pack (`brain/pack.py`)

Input: latest `brain_docs` per slice + open `signals` + last 24h `events` summary + module tile values. Output: Markdown ≤ 10,000 tokens (estimate = chars/4), sections in this order: Identity · ICP · Voice · Pricing · Team · Customers · Finance state · Build state · GTM state · Open signals (top 20 by severity) · Yesterday (counts). Deterministic assembly in v1 (Tier 0); Tier 1 summarization is an optional flag `--summarize` that goes through `daemon/llm.py`. Stored in `context_packs` with `cache_key = sha256(content)`.

## LLM gateway (`daemon/llm.py`)

The only place `anthropic` is imported. `call(tier: int, skill: str, system: str, messages: list, *, trigger: str, max_tokens: int) -> LLMResult{text, tokens_in, tokens_cached, tokens_out, cost_usd, model}`. It: picks the model by tier; marks `system` as cacheable; checks `budgets` before Tier 2 (state `conserve` → only `severity=high` skills; `exhausted` → raise `BudgetExhausted`); writes a `runs` row always. Tier 0 code never imports it.

## Skills (`daemon/skills/`)

Each skill: `name`, `module`, `tier`, `trigger` (`schedule|signal|ask`), `run(ctx) -> list[Approval] | str`. v1 set: `cockpit.morning_pulse` (T2), `cockpit.evening_digest` (T2 short; falls back to `signals/digest.py` T0 text when budget conserve), `build.assign_owner` (T1 → Approval with exec), `build.nudge_stale` (T2 → Approval, Slack exec), `finance.ap_queue` (T0 → record-only approvals with Brex deep links), `customers.prioritize_ask` (T1 → Approval, Linear exec), `ask.answer` (T2, retrieval over brain_docs/messages/documents via Postgres FTS).

## Onboarding (Track C)

`POST /onboarding/tenant {name, website}` → creates tenant, seeds `brain/repo` templates, returns tenant. `POST /onboarding/connections {source, secret_ref, config}` (secret_ref only — the key itself is set in env / Key Vault by the operator in v1). `POST /onboarding/compile` → runs ingest backfill from fixtures or live, signals, pack; returns five memory cards `{slice, draft, sources[]}`. `POST /onboarding/confirm {slice, content}` → writes `brain_docs` (source=human). `GET /cockpit` → pulse text (latest run outcome or T0 fallback), tiles, pending approvals count.

## Run ledger

Every `daemon/llm.py` call writes `runs`. Tier-0 work may also write `runs` rows (tier 0, zero cost) from the daemon or the API — e.g. `onboarding.compile`, executors — so the ledger explains every action, not only model calls. `GET /runs/summary?month=` returns tokens and cost by tier and skill — shown in the Cockpit as the founder's own spend.

## Tenant cadence (added 2026-09-04, PE review)

`tenants.pulse_hour` (local hour, default 7) and `tenants.pulse_channel` (`web|slack|both`) are canonical. `POST /onboarding/cadence` persists them; the daemon scheduler reads them per tenant. `approvals.exec->>'server'` is CHECK-constrained to `Linear|Slack` at the DB level.

## Test isolation

`tests/conftest.py` drops and recreates `startupos_test` once per pytest session. Do not run two `make check` invocations concurrently against the same DSN; set `STARTUPOS_TEST_DSN` per worker if you must.

## Authentication and tenant isolation (Sprint 2, 2026-09-04)

**Roles:** one company-wide role for now. Every `users` row is `owner`; the `role` column stays for later separation but nothing branches on it. Authorization = "is an active user of this tenant".

**Identity sources (all resolve to a `users` row and therefore a tenant):**
1. Browser session — Google OIDC (`GET /auth/google` → Google → `GET /auth/google/callback`), then an httpOnly, SameSite=Lax, Secure (when not localhost) cookie `sos_session` carrying an HMAC-signed opaque session id. State lives in `sessions` (revocable, TTL `STARTUPOS_SESSION_TTL_HOURS`).
2. API token — `Authorization: Bearer sos_<id>_<secret>`; only `sha256` stored in `api_tokens`; created via `POST /auth/tokens` (shown once), revoked via `DELETE /auth/tokens/{id}`.
3. Bootstrap — `POST /auth/bootstrap {token, email}` with `STARTUPOS_BOOTSTRAP_TOKEN` creates/activates the first owner of `TENANT_ID` and returns a session. For the single-operator install before Google is configured. Disabled when the env var is empty.
4. Slack — the gateway maps `slack_user_id` → user via `auth_lookup_slack`; unmapped Slack users cannot approve (the bot replies with a link to `/auth/slack/link`).

**Tenant binding:** the tenant is ALWAYS derived from the authenticated user. `?tenant=` / `X-Tenant-Id` are removed. `api.deps.get_db` opens the connection with `get_conn(tenant_id=user.tenant_id)`, which sets `app.tenant_id`; Row Level Security (`db/rls.sql`) filters every table. Services run as DB role `startupos_app` (NOBYPASSRLS) via `STARTUPOS_APP_DSN`. Cross-tenant ids therefore 404, never 403 (no existence leak).

**Pre-auth lookups** (find the user before the tenant is known) go through `SECURITY DEFINER` functions `auth_lookup_google/token/session/slack` and return only ids — never a cross-tenant row scan from application code.

**Sign-up:** `POST /onboarding/tenant` is the only unauthenticated write and it requires either the bootstrap token or a valid Google id_token; it creates the tenant + owner and returns a session. Google sign-in for an email that matches no user 403s with "ask your owner to invite you" (invites are Sprint 3).

**Acting identity on records:** `approvals.decided_by = users.id`; `runs.acted_by = users.id` for `ask`/`decide` triggers, NULL for scheduled runs.

**Env:** `STARTUPOS_SESSION_SECRET` (required in production; the API refuses to start without it unless `STARTUPOS_DEV=1`), `GOOGLE_CLIENT_ID/SECRET`, `STARTUPOS_PUBLIC_URL`, `STARTUPOS_WEB_URL`, `STARTUPOS_BOOTSTRAP_TOKEN`, `STARTUPOS_APP_DSN`.

**Public routes:** `GET /health`, `GET /auth/google`, `GET /auth/google/callback`, `POST /auth/bootstrap`, `POST /onboarding/tenant`. Everything else requires auth.

## Chief of Staff (hub agent) — spec

Hub-and-spoke: modules are spokes (Sales, Marketing, Customers, Finance, Build, Web, Social, Security). The Chief of Staff runs in the hub — one skill, `cockpit.chief_of_staff`, with the whole brain and every spoke's state in context — and its job is to see across spokes and **raise issues before they land**.

**Inputs (assembled by Tier 0, no model):** context pack; ALL open signals across modules; `events` for the last 7 days grouped by entity; lookahead facts computed by `signals/lookahead.py` (bills due ≤14d vs cash, runway in months, issues with due dates ≤7d, POC milestones from `customers.md` front matter, campaign/sequence ages, deploy cadence last 30d vs prior 30d, stale approvals >48h, budget state); the last 5 Chief of Staff briefs (so it can say what changed); decisions.md tail.

**Output (one Tier-2 call, JSON via `daemon/llm.py`):**
```json
{
  "brief": "≤120 words: the state of the company today, cross-spoke",
  "risks": [ {"horizon_days": 7, "module": "finance", "title": "…", "why": "evidence with ids", "severity": "high|medium|low", "proposal_id": "…|null"} ],
  "asks": [ {"title": "…", "owner": "Kamal|Alexey|…", "by": "2026-09-08", "why": "…"} ],
  "proposals": [ Approval-shaped {module, type, target, preview, exec|null, signal_id|null} ],
  "changes_since_last": ["…"]
}
```
Risks and asks are written to `signals` as `cos.risk:<slug>` (kind `open`, module = the spoke, `suggested_skill = cockpit.chief_of_staff`) so every spoke page shows what the hub sees. Proposals go to `approvals` through `daemon/approvals.py` like any skill — exec only within the allow-list. The brief is stored as the run outcome and is what `/cockpit` shows as the pulse when present.

**Schedule:** daily 06:30 tenant-local (before the pulse, which then reads it), and on demand via `/ask?mode=cos` or Slack `@StartupOS what should I worry about`. Budget: `high_priority` skill (still runs in `conserve`); skips in `exhausted` with a Tier-0 fallback that lists lookahead facts only.

**Prompt lives at `daemon/prompts/chief_of_staff.md`** and is the contract for tone: evidence-first, ids on every claim, no filler, never invents numbers, says "no data" when a spoke is not connected, distinguishes "will happen" (dated facts) from "might happen" (patterns).

**Memory:** the CoS never edits `brain/` directly. It proposes decisions (record-only approvals); approving one appends to `decisions.md`. It reads its own prior briefs from `runs` to avoid re-raising resolved items.

## Sprint 3a — multi-tenant runtime (2026-09-12)

Goal (Architecture Brief §5 Day 0, §7 Week 4): **a second tenant created through the API with its own Linear key gets a real pulse without an operator touching `.env` or running `make`.** Three tracks; contracts below are fixed before code so tracks can run in parallel.

### Track S — per-tenant secrets (`common/secrets.py`, `db/schema.sql`, `db/rls.sql`)
- `tenant_secrets(tenant_id, name, ciphertext BYTEA, created_at, updated_at, PRIMARY KEY (tenant_id, name))` — RLS `tenant_isolation` like every tenant table; `FORCE ROW LEVEL SECURITY`.
- Envelope encryption: `STARTUPOS_MASTER_KEY` (32 bytes, base64/urlsafe) → per-row random 32-byte data key; payload = AES-GCM(data key) ; data key wrapped with AES-GCM(master). Implement with `cryptography` (already pulled in by `PyJWT[crypto]`; add explicit pin `cryptography` to `requirements.txt`). No key → `secrets.put/get` raise `SecretsUnavailable` and the API returns 503 on connection writes.
- `common/secrets.py`: `put(conn, tenant_id, name, value)`, `get(conn, tenant_id, name) -> str | None`, `delete(conn, tenant_id, name)`, `rotate_master(conn, old, new)` (batch re-wrap, superuser conn).
- `settings.secret(ref, *, conn=None, tenant_id=None)`: `env:NAME` unchanged; **`kv:NAME`** → `secrets.get(conn, tenant_id, NAME)`; missing conn/tenant → `ValueError`. Nothing else in the codebase reads `os.environ` for source keys.
- Connections: `POST /onboarding/connections {source, credential?, secret_ref?, config}` — when `credential` is given the API stores it as `kv:<source>_api_key` for the caller's tenant and sets `secret_ref` to that ref; the plaintext is never logged or returned. `GET /onboarding/connections` returns `secret_ref` and `has_credential: bool` only.
- Tests: encrypt/decrypt round trip; wrong master key fails closed; RLS: tenant B cannot read tenant A's row via `startupos_app`; API never echoes the credential.

### Track T — tenant iteration (`daemon/scheduler.py`, `ingest/runner.py`, `daemon/gateway/slack.py`, `daemon/budget.py`)
- `common/tenants.py`: `active_tenants(conn) -> list[dict]` (`status='active'`), `tenant_conn(tenant_id)` = `get_conn(settings.app_dsn or database_url, tenant_id=...)`.
- Scheduler: `build_scheduler()` registers **one job per (job_id, tenant)** with ids `f"{job_id}:{tenant_id}"`; `refresh_tenants` job every 5 min adds/removes tenant jobs as tenants appear (no daemon restart on sign-up). Pulse/CoS hours read from that tenant's cadence. The fallback loop iterates tenants the same way.
- Ingest `--loop`: every pass iterates `active_tenants()`; for each tenant, each source with a `connections` row whose `status <> 'disabled'`; credentials via `settings.secret(connection.secret_ref, conn=…, tenant_id=…)`; `sync(conn, tenant_id, api_key=…)` — sources take the key as an argument, never read env inside. Tenants with no connection for a source are skipped silently. Per-tenant errors are caught and written to `connections.last_error`; one tenant's failure never stops the pass.
- Gateway: Slack events resolve tenant via `auth_lookup_slack(slack_user_id)` (exists) — no `settings.tenant_id` fallback in production; unknown users get `link_hint()`.
- Budget: already keyed by tenant — verify `monthly_allowance` comes from `tenants` (or plan) not settings; add a test with two tenants at different states.
- `settings.tenant_id` survives only as the dev/CLI default (`STARTUPOS_DEV=1` paths, `make` targets, fixtures).

**Track T — as built (2026-09-12).** `common/tenants.py`: `active_tenants(conn)` reads the SECURITY DEFINER `tenants_active()` (db/rls.sql) so the RLS-bound app role can enumerate tenants without a cross-tenant scan; `tenants.status` (`active | suspended | deleted`, default active) was added for it. Scheduler: `build_scheduler()` (no args) = every active tenant + the process-wide `refresh_tenants` job; `build_scheduler("unitone")` pins one tenant with no refresh (dev). `daemon.main --tenant` is now optional. Ingest: `python -m ingest.runner` (and `--loop`) iterate tenants; `--tenant [ID]` / `--fixtures` / `STARTUPOS_DEV=1` select the single-tenant dev pass (`make ingest`). For Track O's backfill jobs call `ingest.runner.run_tenant(conn, tenant_id, [source])` (or `run_connection(conn, tenant_id, connections_row)`) on a tenant-bound connection; a credential equal to `ingest.runner.FIXTURE_CREDENTIAL` (`"fixture"`) reads tests/fixtures. `sync(conn, tenant_id, api_key=…, use_fixtures=…, fixtures_dir=…, channels=… | team_id=…)` in every source; `has_credentials()` is gone. Gateway: `resolve_event_tenant(slack_user_id)` → `auth_lookup_slack`, else `link_hint()`; `dev_tenant` only under `STARTUPOS_DEV=1`. Two multi-tenant defects fixed on the way: `get_conn` now commits the tenant binding before yielding (a `rollback()` used to undo `set_config` and leave the connection unbound), and `signals` / `approvals` primary keys are now `(tenant_id, id)` with a composite FK (`marketing.analytics_off:posthog`, `assign-acm-158` repeat across tenants; the second tenant's tick used to fail on RLS). `db/schema.sql` migrates an existing database in place.

### Track O — onboarding pipeline (`db/schema.sql`, `api/routers/onboarding.py`, `daemon/jobs.py`)
- `tenant_jobs(id, tenant_id, kind, payload JSONB, status queued|running|done|failed, attempts, error, created_at, started_at, finished_at)`; RLS like every tenant table; index on `(status, created_at)`. Kinds: `backfill:<source>`, `signals`, `context_pack`, `chief_of_staff`, `morning_pulse`.
- `POST /onboarding/compile` → enqueues, in order, `backfill:<source>` for each connected source, then `signals`, `context_pack`, `chief_of_staff`, `morning_pulse`; returns `{cards, counts, jobs: [...]}` immediately (cards from whatever is in the DB now). Idempotent: if a queued/running job of the same kind exists for the tenant, it is not duplicated.
- `daemon/jobs.py: service_jobs()` — runs every 15 s (scheduler job, not per tenant): `SELECT … FOR UPDATE SKIP LOCKED` one queued job at a time, in `created_at` order, **respecting order within a tenant** (a job runs only when no earlier job for that tenant is queued/running). Each job runs under a tenant-bound connection; failures record `error`, `attempts`, and retry up to 3 with backoff; the chain continues past a failed backfill (the pulse still runs on whatever landed).
- `GET /onboarding/status` adds `jobs: {queued, running, done, failed, last_error}` and `first_pulse_ready: bool` (a `runs` row for `cockpit.morning_pulse` exists for the tenant).
- Web wizard polls `/onboarding/status` and shows the chain progress instead of "compiling…".
- Acceptance test (functional, fixtures, no env keys): create tenant B via `POST /onboarding/tenant`, `POST /onboarding/connections {source: linear, credential: "fixture"}` (with `--fixtures` semantics: a credential equal to the literal `fixture` makes ingest read `tests/fixtures`), `POST /onboarding/compile`, drive `service_jobs()` until idle, assert tenant B has issues, signals, a context pack and a `cockpit.morning_pulse` run, and that tenant A's counts are unchanged.

**PE review (2026-09-12) — credentials are per tenant everywhere.** `env:` secret_refs are the install tenant's (`TENANT_ID`) own keys: `POST /onboarding/connections` accepts one only from that tenant and only at the source's canonical variable (`common.secrets.OPERATOR_ENV_REFS`; 403 otherwise — before this, any signed-up tenant could point its Linear/Slack/Brex connection at the operator's key and ingest the operator's data, or probe env-variable existence through `has_credential`). `settings.secret("env:…", tenant_id=X)` raises `SecretRefForbidden` for any other tenant, so a pre-existing row cannot be honoured by ingest either. Executors (`daemon/executors`) now act with the approval tenant's own credential — `common.secrets.credential_for_source(conn, tenant_id, source)` resolves the tenant's `connections.secret_ref` on its bound connection; no credential → the approval fails (`executor_error`), never a fallback to the environment (the install tenant with no connection row still falls back to its operator env ref). Every service runs `common.secrets.check_master_key()` at start: a malformed `STARTUPOS_MASTER_KEY` refuses to start; an unset one logs the consequence once.

**Track O — as built (2026-09-12).** `tenant_jobs` as specified plus `run_after TIMESTAMPTZ` (retry backoff: 15 s, then 60 s; a job is not claimable before it) and a `CHECK` on `status`; RLS `tenant_isolation` + `FORCE`. Queue helpers live in `common/jobs.py` (`enqueue_chain`, `status_summary`, `public`) so the API never imports `daemon/`; the executor is `daemon/jobs.py`. Claiming is the SECURITY DEFINER `tenant_jobs_claim(p_tenants TEXT[] DEFAULT NULL)` in `db/rls.sql` (the daemon keeps running as `startupos_app`): one row per call, `FOR UPDATE SKIP LOCKED`, oldest `(created_at, id)` first, only when no earlier job of that tenant is queued/running; a row left `running` for > 30 min (dead daemon) is claimable again. `service_jobs()` is registered once per process (`daemon.scheduler.SERVICE_JOBS_ID`, every 15 s; the pinned dev scheduler and the fallback loop pass the pinned tenant ids) and drains until idle or 50 jobs per pass; each job runs on `tenant_conn(tenant_id)`, a failed attempt is rolled back, then the row is finished on the same connection. Backfills enqueue only for sources ingest has a module for (`linear|slack|brex|vercel`, `common.jobs.BACKFILL_SOURCES`); a connection with no stored credential fails its 3 attempts (recorded, never raised) and the chain continues. `POST /onboarding/compile` returns `{cards, counts, jobs, outcome, tier}` (`jobs[*].enqueued` says whether the row is new); `GET /onboarding/status.jobs` is `{queued, running, done, failed, last_error, chain}` where `chain` is the latest row per kind in chain order; `first_pulse_ready` mirrors `steps.pulse`. Two read-only additions: `GET /onboarding/cards` (cards + counts without queueing — the wizard re-reads them when the backfill lands) and `GET /onboarding/jobs` (raw rows, operators). The wizard polls `/onboarding/status` every 3 s while the chain is active and offers "paste the key" (→ `credential`) next to the operator reference on the Sources step. With no `ANTHROPIC_API_KEY` the chain lands a Tier-0 `cockpit.morning_pulse` run (status `degraded`, preceded by the gate's `refused: no model credentials` row) — the acceptance test runs without a model.

