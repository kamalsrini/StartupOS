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


## Sprint 3b — Slack delivery and install (2026-09-13)

Goal (Architecture Brief §4 Cockpit, §5 Everyday, §7 Weeks 1–2 and 4): **the founder never has to open the web app.** The pulse, the digest and high-severity signals arrive in the tenant's own Slack; approvals carry Approve/Decline buttons that execute through the existing gate; and a second tenant installs StartupOS into its own workspace over OAuth without an operator touching `.env`. Three tracks; ownership is exclusive — a track never edits another track's files.

Shared vocabulary: **install** = a `slack_installations` row (one Slack workspace ↔ one tenant). **delivery** = an outbound post StartupOS makes on a schedule or on a signal. **interaction** = an inbound button press or slash command over HTTPS.

### Track D — delivery (owns `daemon/delivery.py`, `daemon/slack_blocks.py`, `signals/digest.py`, `daemon/skills/cockpit/*.py`, scheduler wiring in `daemon/scheduler.py`)
- `daemon/slack_blocks.py`: pure renderers, no I/O. `pulse_blocks(text, tiles, approvals_count, web_url)`, `digest_blocks(data)`, `signal_blocks(signal)`, `approval_blocks(approval, *, decided=None)` — each returns `(fallback_text, blocks)`; `fallback_text` is always non-empty (screen readers, notifications). `approval_blocks` renders Approve/Decline buttons with `action_id` `approval_approve` / `approval_decline` and `value` = approval id; when `decided` is given it renders the resolved state with no buttons (Track B updates messages in place with this).
- `daemon/delivery.py`: `deliver(conn, tenant_id, kind, payload, *, channel=None) -> dict`. Resolves the target channel: explicit `channel` → `tenants.pulse_channel` when it names a channel → the install's `default_channel` → skip. `tenants.pulse_channel` stays `web|slack|both` for **where**, and a new `tenants.slack_channel TEXT` names **which** channel (default `#general`); `web` means never post. Token comes from `common.secrets.credential_for_source(conn, tenant_id, "slack")` — no environment reads. Posting goes through `daemon/executors/slack.py` (extended by Track D with an optional `blocks` argument; the executor stays the only Slack write path). Every delivery writes a `deliveries` row.
- `deliveries(id BIGSERIAL, tenant_id, kind, ref TEXT, channel, ts TEXT, status sent|skipped|failed, error, created_at, UNIQUE (tenant_id, kind, ref))` with RLS. `ref` is what makes a delivery unique: `signal:<signal_id>`, `pulse:<YYYY-MM-DD>`, `digest:<YYYY-MM-DD>`, `approval:<approval_id>`. **Dedupe is the UNIQUE constraint, not a query** — a duplicate insert means "already delivered, skip".
- Wiring: `cockpit.morning_pulse` and `cockpit.evening_digest` deliver after they produce text (they still return it for the web); `tick_15m` delivers new signals of severity `high` (and `cos.risk:*` of any severity) one message each, capped at 5 per tick per tenant with an overflow line; `daemon/approvals.py::propose` is NOT touched — Track B posts approvals.
- Failures never break the caller: a delivery exception is caught, recorded `failed` with the error, logged once.
- Tests (Track D owns `tests/unit/test_slack_blocks.py`, `tests/unit/test_delivery.py`, `tests/functional/test_delivery.py`): block shape is valid Block Kit (every block has `type`; no block exceeds Slack's 3000-char text limit; buttons carry `action_id`+`value`); `web` posts nothing; `both` posts and still returns web text; dedupe blocks a second send of the same `ref`; two tenants deliver to their own channels with their own tokens; a failing post records `failed` and the tick continues.

### Track I — install and inbound HTTP (owns `api/routers/slack.py`, `auth/slack_sig.py`, `auth/slack_install.py`, `db/schema.sql` slack tables, `web/app/settings/*`)
- `slack_installations(tenant_id PK, team_id UNIQUE, team_name, bot_user_id, default_channel, installed_by, installed_at, revoked_at)`; the bot token itself is NOT stored here — it goes to `tenant_secrets` as `slack_bot_token` and the install writes/updates the tenant's `connections` row for `slack` with `secret_ref = kv:slack_bot_token`, so Track D and the executors resolve it through `credential_for_source` unchanged.
- `GET /slack/install` (authenticated) → 302 to Slack's `oauth/v2/authorize` with scopes `chat:write,chat:write.public,commands,im:history,app_mentions:read,users:read,channels:read` and `state` = an `itsdangerous` signed token carrying `tenant_id` + nonce, max age 10 min. `GET /slack/oauth/callback` (public) → verifies state, exchanges the code with `oauth.v2.access`, stores the bot token, upserts the installation, redirects to `web_url/settings?slack=connected`. Errors redirect with `?slack=error&reason=…` and never leak the code or token.
- `auth/slack_sig.py::verify(headers, body, signing_secret, *, now=None)`: HMAC-SHA256 over `v0:{timestamp}:{body}`, `hmac.compare_digest`, reject timestamps older than 300 s (replay), reject a missing/malformed header. `STARTUPOS_SLACK_SIGNING_SECRET` and `STARTUPOS_SLACK_CLIENT_ID/SECRET` are new install-level env vars (one Slack app serves all tenants — that is how Slack distribution works); absent → the install routes 503 with a clear message and the daemon logs one warning at start.
- `POST /slack/events` (public, signature-verified): URL-verification challenge echo; `app_uninstalled`/`tokens_revoked` → mark `revoked_at`, delete the tenant's `slack_bot_token` secret, set the `connections` row `status='disabled'`; `app_mention`/`message.im` → resolve the tenant from `team_id` via the installation (and the user via `auth_lookup_slack`), then hand the text to the EXISTING `daemon.gateway.slack.handle_text` — the Socket Mode gateway stays for dev, HTTP is the production path. Events are acknowledged within 3 s: the handler enqueues a `tenant_jobs` row of kind `slack_event` and returns 200 immediately; the daemon's `service_jobs` does the work and posts the reply.
- `POST /slack/interactivity` (public, signature-verified) → parses the `payload` form field and dispatches to `daemon/slack_actions.py::handle_action` (Track B's file; Track I lands a stub that 404s on unknown `action_id`, so the two tracks merge without conflict).
- Tenant resolution for every inbound request is `team_id → slack_installations`, never a header or query parameter; an unknown `team_id` returns 200 with an ephemeral "this workspace is not connected" so Slack does not retry.
- Tests (Track I owns `tests/unit/test_slack_sig.py`, `tests/functional/test_slack_install.py`): signature accept/reject (good, tampered body, stale timestamp, missing header, wrong secret); state token accept/reject/expiry; a full fake OAuth exchange writes the installation + secret + connection row and never logs the token; uninstall revokes all three; unknown team is a clean 200; the challenge echo works.

### Track B — approval buttons (owns `daemon/slack_actions.py`, approval delivery in `daemon/delivery.py::deliver_approval` only, `tests/*_slack_actions.py`) — starts after Tracks D and I merge
- When an approval is proposed and the tenant's channel is Slack-bound, `deliver_approval` posts `approval_blocks` and records `deliveries.ref = approval:<id>` with the message `ts`.
- `handle_action(payload, *, conn_factory)`: verifies the acting Slack user maps to a user of the installation's tenant (`auth_lookup_slack`; mismatch → ephemeral "link your account" with no state change); loads the approval under that tenant's connection; `approve` → `approvals.decide(Decision(approve, acted_by=<user_id>))` then the existing executor path with the tenant's own credential; `decline` → records the decision. Both then update the original message in place (`chat.update` with `approval_blocks(decided=…)`) so the buttons cannot be pressed twice; a second press of an already-decided approval is a no-op that just re-renders.
- Idempotency and race: the decision goes through the existing `SELECT … FOR UPDATE` path in `approvals.decide`; a double click therefore decides once. The action handler must never execute outside the approval gate.
- Tests: approve executes and updates the message; decline records a reason and updates; a user from another tenant cannot decide (nothing changes, ephemeral reply); double click executes once; an execution failure leaves the approval decided but records the error and says so in the message.

### Environment (new, install-level — one Slack app for the whole install)
`STARTUPOS_SLACK_CLIENT_ID`, `STARTUPOS_SLACK_CLIENT_SECRET`, `STARTUPOS_SLACK_SIGNING_SECRET`. Absent → install/events/interactivity 503 or warn; nothing else degrades. Per-tenant bot tokens are never env vars.

**Track D — as built (2026-09-13).** `daemon/slack_blocks.py` is pure (`pulse_blocks`, `digest_blocks`, `signal_blocks`, `approval_blocks`, each → `(fallback_text, blocks)`), plus the primitives the renderers and the tests share: `truncate`/`split_text` (a >3000-character pulse becomes several sections instead of being cut), `finish` (the 50-block cap) and `validate(blocks)` → list of problems, which `deliver` runs before every post so a message Slack would reject is recorded `failed` instead of sent. `daemon/delivery.py::deliver(conn, tenant_id, kind, payload, *, channel=None)` → `{status, ref, channel, ts|error|reason}`; `kind ∈ {pulse, digest, signal, approval}`; it never raises for a delivery problem (only an unknown `kind` does). Channel: explicit → `tenants.slack_channel` → the install's `default_channel` (read defensively — Track I's table may not exist yet) → skip; `pulse_channel = web` never posts, even with an explicit channel. Token: `credential_for_source(conn, tenant_id, "slack")` only; no credential, or an unusable master key, is a clean `skipped`. Deviations from the spec, both deliberate: (1) `deliveries.status` also allows `pending` — the row is claimed *before* the post so the UNIQUE constraint is a real lock between two daemons, and is then finished `sent`/`failed`; a row that never reached `sent` (and a `pending` row older than 15 min, i.e. a crashed daemon) is re-claimable, so a skipped or failed ref is retried rather than blacklisted; (2) the tenant FK is `ON DELETE CASCADE`, so deleting a tenant is not blocked by its Slack history. `deliveries.ts` and the executor's new `ts`/`channel` return keys exist for Track B's `chat.update`; `deliver_approval` is present as a documented `NotImplementedError` extension point. Wiring: `cockpit.morning_pulse` / `cockpit.evening_digest` produce their text, then `delivery.deliver_text` (which swallows everything — a Slack outage never turns a paid-for Tier-2 run into a failure), and still return the text for the web; `scheduler.deliver_signals` runs inside `tick_15m` between the signal skills and the executors, posting open `high` signals and every `cos.risk` with no delivery row, 5 per tick, the overflow line (`+N more waiting in the cockpit`) on the last message. `signals/digest.py::post_to_slack(text, *, conn=None, tenant_id=None)` routes through delivery when given a connection (per-tenant token, channel, ledger, Block Kit) and keeps its operator-env behaviour otherwise. Gates green (299 unit / 64 functional / 7 regression).

**Track I — as built (2026-09-13).** `slack_installations(tenant_id PK, team_id UNIQUE, team_name, bot_user_id, default_channel DEFAULT '#general', installed_by, installed_at, revoked_at)` with `tenant_isolation` + `FORCE` like every tenant table. Tenant resolution for an inbound Slack request is the SECURITY DEFINER `slack_lookup_install(team_id)` (db/rls.sql) — the same pattern as the auth lookups, returning one row and no credential; a revoked row is returned with `revoked_at` set so the caller answers "not connected". `auth/slack_sig.py::verify(headers, body, signing_secret, *, now=None)` returns True or raises `SlackSignatureError` (`SlackSigningNotConfigured` is a subclass, so the routers answer 503 rather than 403 when the secret is missing); the 300 s window rejects future timestamps as well as stale ones. `auth/slack_install.py` owns state (`itsdangerous`, salt `sos_slack_install`, 10 min, nonce per link), `authorize_url`, `exchange_code` (injectable client — no test touches the network), `save_installation` (token → `tenant_secrets.slack_bot_token`, then `connections[slack].secret_ref = kv:slack_bot_token` with `config.team_id`, then the installation row) and `revoke` (all three, always). A workspace already installed for another tenant raises `WorkspaceTaken` — checked through the cross-tenant lookup and again by the UNIQUE index, so a race is refused too — and the callback redirects `?slack=error&reason=workspace_taken` having written nothing. Routes: `GET /slack/install` (authenticated), `GET /slack/oauth/callback`, `POST /slack/events`, `POST /slack/interactivity` (public, signature-verified) plus one read-only addition, `GET /slack/status` (authenticated), which is what `web/app/settings` renders — it never returns a token. The public set is now enumerated as `api.main.PUBLIC_PATHS` and a functional test walks `app.routes` and asserts every other route answers 401 with a Bearer challenge. Events: challenge echo; `app_uninstalled`/`tokens_revoked` revoke; `app_mention`/`message` enqueue a `tenant_jobs` row of the new kind `slack_event` (registered in `daemon/jobs.py::HANDLERS`, constant in `common/jobs.py`) and return 200 inside Slack's 3 s — the daemon then answers through the existing `daemon.gateway.slack.handle_text` and posts with `daemon/executors/slack.py::post_message(token=…)` using that tenant's own bot token. Enqueue is idempotent on Slack's `event_id` (Slack retries); the bot's own messages, subtypes and empty text are acknowledged and dropped. `daemon/slack_actions.py` is a stub owned by Track B: `handle_action(payload, *, conn_factory)` raises `UnknownAction` for every `action_id`, which the router turns into 404. New env: `STARTUPOS_SLACK_CLIENT_ID`, `STARTUPOS_SLACK_CLIENT_SECRET`, `STARTUPOS_SLACK_SIGNING_SECRET` (`common/settings.py` reads them at call time; `auth.slack_install.warn_if_unconfigured` logs one line at API and daemon start).

**Track B — as built (2026-09-13).** `daemon/approvals.py::propose` posts the approval the first time it inserts a row (`announce` → `delivery.deliver_approval`, best effort in both directions: a web-only tenant, a missing credential or a Slack outage is a `skipped`/`failed` deliveries row, never a failed proposal), so the buttons exist wherever approvals are already proposed — the cockpit skills, the Chief of Staff, the finance and customer queues — with no caller change. `delivery.deliver_approval(conn, tenant_id, approval, *, channel=None, decided=None)` accepts a model, a row or a dict and is a thin wrapper on `deliver`, so the ledger, the dedupe and the tenant's own token are Track D's unchanged. `daemon/executors/slack.py::update_message` is the new `chat.update` — same shape as `post_message` (token argument, injectable client, never the environment), deliberately NOT in the executors allow-list: an approval can never *ask* for a chat.update, it is how the gate reports what it did. `daemon/slack_actions.py::handle_action(payload, *, conn_factory)` keeps Track I's seam (`UnknownAction` → 404, raised before any I/O) and runs in this order: `team_id` → `slack_lookup_install` → `auth_lookup_slack`, and a presser who is unmapped OR mapped to another tenant changes nothing and gets an ephemeral link hint; then, on a tenant-bound connection, `approvals.decide` (its `SELECT … FOR UPDATE` is what makes the double click decide once — the loser's `ValueError` is the no-op, not an error) and, for approve only, `executors.run_approved`, which re-reads the row and resolves the tenant's own credential. Both paths end in one `chat.update` with `approval_blocks(decided=…)`, so the buttons disappear and a second press simply re-renders. `chat.update`'s target is the deliveries row (`approval:<id>`, `status='sent'`, `ts` not null) and nothing else: an approval proposed while Slack was disconnected has no message to update and still decides cleanly (`{"updated": false, "reason": …}`). Nothing below the router raises — a stranger, a missing message, a dead `chat.update` and a failing executor are each a dict, and an execution failure leaves the approval decided (`failed`) with `result.error` recorded and quoted in the updated message. Deviations, all small and deliberate: (1) `daemon/gateway/slack.py` gained `lookup_slack_user(conn, slack_user_id)` — the same `auth_lookup_slack` query, now callable on an already-open unbound connection, with `resolve_slack_user` delegating to it, so the HTTP path does not open a second connection or duplicate the SQL; (2) two tests owned by other tracks were updated to the new reality rather than left red — Track D's `test_deliver_approval_is_track_bs_extension_point` (the `NotImplementedError` is gone) and Track I's interactivity 404 test (which asserted the stub by pressing `approval_approve`; it now presses an action id nobody owns); (3) the handler's success dict carries `{ok, approval, status, decided, acted_by, message}` — no `text`/`blocks`, so Slack ignores it rather than replacing the message a second time. Tests: `tests/unit/test_slack_actions.py` (23) and `tests/functional/test_slack_actions.py` (9, RLS-bound `startupos_app` connections, the signed HTTP route, and a real two-thread double click). Gates green (338 unit / 102 functional / 7 regression).

**PE follow-ups (2026-09-13).** Three residual risks from the Principal Engineer review, closed surgically. (1) **The Socket Mode gateway could post as the operator on another tenant's behalf.** `daemon/gateway/slack.py::start()` now returns None (logging one line: Slack is served over HTTP — `POST /slack/events` → `tenant_jobs` → `daemon/jobs.py::run_slack_event`) unless `common.tenants.dev_mode()`, so a production process never opens the listener that holds the process-wide operator token; and the reply path resolves the token for the tenant the event actually resolved to (`reply_token` → `credential_for_source`), falling back to `SLACK_BOT_TOKEN` only for the install tenant (`settings.tenant_id`) and dropping the reply, with a warning, when neither exists. The listener moved out of `start()` to module level (`_listener`, plus the `_new_web_client` seam) so it is testable without a socket. (2) **An unexpected handler exception was a FastAPI 500.** `POST /slack/interactivity` still 404s on `UnknownAction`, but anything else from `daemon/slack_actions.py::handle_action` is now logged once with the action ids and the team and answered 200 + ephemeral (`INTERACTIVITY_FAILED`): a 5xx would make Slack retry the press and show the founder a red error, which is worse than a clear "the cockpit still has this approval". (3) **Pre-existing signals were permanently suppressed when Slack was connected later.** A delivery skipped for a *missing credential* now writes NO `deliveries` row at all (the simpler of the two options — the alternative, teaching `undelivered_signals` to re-claim such rows, would have split the "already handled" rule across two places). `pulse_channel = web` still writes its `skipped` row, because that is a deliberate "never post"; the UNIQUE-constraint dedupe for real sends is untouched. A tenant that accumulates open high/`cos.risk` signals before its install therefore still has them when the credential appears, and the next tick posts them 5 per tick with the overflow line. Tests: `tests/unit/test_daemon_skills.py` (a non-dev process does not start the gateway), `tests/functional/test_track_t.py` (a dev reply to tenant B posts with B's token; an uncredentialed tenant gets no reply), `tests/functional/test_slack_install.py` (a raising handler is an ephemeral 200, `UnknownAction` still 404), `tests/unit/test_delivery.py` (the backlog survives until Slack is connected). Gates green (340 unit / 104 functional / 7 regression).

**End-to-end verification (2026-09-13).** One black-box regression walk of the whole chain, `tests/regression/test_slack_end_to_end.py` (marked `regression`), against real Postgres and the real FastAPI app through `TestClient`, with only the network faked: `oauth.v2.access`, `chat.postMessage`, `chat.update`, Linear's GraphQL and the Anthropic client are injected, so the run is offline and deterministic. Two companies in two workspaces do every step at once and each step asserts the other saw nothing: sign-up over `POST /onboarding/tenant` → `GET /slack/install` → the real `GET /slack/oauth/callback` with a signed state (installation row + encrypted `tenant_secrets.slack_bot_token` + `connections[slack]`, no token in the table, the response or a log) → `ingest.runner` fixtures → `scheduler.tick_15m` (signal engine → skills → delivery → executors) posting the high signal to that tenant's channel with that tenant's token and writing the ledger row, a second tick delivering only the backlog and never a ref twice → `build.assign_owner` proposing `assign-acm-158`, announced with `approval_approve`/`approval_decline` carrying the approval id → a signed `POST /slack/interactivity` deciding it through the gate, running the Linear executor with THAT tenant's own API key and editing the message in place at the ledger's `ts` (the second press decides nothing, executes nothing and re-renders; a press by tenant A's Slack user against a tenant-B-only approval is `GONE` with no state change, and B's founder pressing A's button gets the link hint) → a `message.im` on `POST /slack/events` acknowledged in well under Slack's 3 s, idempotent on `event_id`, drained through `tenant_jobs` and answered in the DM with the right token → `app_uninstalled` revoking the install, the secret and the connection, after which the next tick is a clean `skipped` that posts nothing and writes no ledger row (so the ref is not blacklisted), inbound events and presses from the revoked workspace are clean 200s, and tenant B still delivers with its own token. Two operational claims sit in the same file: with the three `STARTUPOS_SLACK_*` vars unset the app still imports in a fresh interpreter and `/health` is 200 while `/slack/install`, `/slack/oauth/callback`, `/slack/events` and `/slack/interactivity` all answer 503 (`/slack/status` deliberately still answers, with `configured: false`, because Settings has to render it); and `db/schema.sql` + `db/rls.sql` apply twice more over a populated database with no row, column, policy or grant lost — forced RLS and the SECURITY DEFINER lookups still hold afterwards. `make regression` now passes `STARTUPOS_TEST_DSN` like `make functional`, because the gate needs Postgres. No product defect was found by the walk; the file ends with the list of assumptions only a real Slack workspace can settle (OAuth app settings, Block Kit acceptance, channel membership, real-network latency, rate limits, Events/interactivity subscriptions). Gates green (340 unit / 104 functional / 10 regression).

**Channel gap closed + operator runbook (2026-09-13).** The end-to-end walk left one real defect standing: `tenants.slack_channel` was `NOT NULL DEFAULT '#general'` and outranks the install's `default_channel`, but nothing in the API or the web app ever wrote it — so the channel a founder picks is captured and then ignored, and every tenant posts to `#general` until an operator edits the row. Fixed without changing the precedence order (explicit → `tenants.slack_channel` → install `default_channel` → skip): (1) `db/schema.sql` makes the column NULLABLE with no default — unset means "use the install's channel" — and a one-shot, self-guarding `DO` block (the guard is "the column still has a default", false ever after) drops NOT NULL and the default and turns every row still holding the untouched `'#general'` into NULL; nothing had ever written the column, so every such value was that default. `auth/slack_install.py::save_installation` still does not touch it — `default_channel` stays the source of truth until a founder overrides it. (2) `POST /onboarding/cadence` takes an optional `slack_channel`, validated by the new `common.tenants.normalize_slack_channel` (`#name`, lower-cased like Slack does, or a `C`/`G`/`D` channel id; empty/null clears the override) so a typo is a 422 in Settings rather than a `channel_not_found` the daemon meets at 7am. The same endpoint became a partial update — every field now means "leave this as it is" when omitted, and an omitted `tier2_tokens_allowed` keeps the month's budget — because Settings edits one field at a time and silently resetting a founder's timezone or budget would be a worse bug than the one being fixed; the wizard, which always sends all four, is unaffected. (3) `GET /slack/status` reports the channel actually in effect (`channel`), the override (`slack_channel`), the install's (`default_channel`), the mode (`pulse_channel`) and `delivers`, computed with delivery's own helpers so the two cannot drift; `web/app/settings/page.tsx` renders it and lets the founder change it next to the connection status. Tests: `tests/unit/test_tenant_channel.py` (the validator), two in `tests/unit/test_delivery.py` (unset → the install's channel; an override beats it), `tests/functional/test_slack_channel.py` (the routes, the 422s, clearing, the rest of the cadence surviving, a revoked install, and the migration — the legacy shape is re-created, the schema re-applied, and delivery then resolves the install's channel), and the regression walk now sets the channel through the API instead of an operator `UPDATE`. Known gap, documented not coded around: Slack returns the channel picked at install only for the `incoming-webhook` scope, which this app deliberately does not request, so in a real install `default_channel` is `#general` and Settings is where the channel is really chosen. `docs/SLACK-SETUP.md` is the operator runbook for creating the Slack app (credentials, redirect URL, the seven scopes, events, interactivity, App Home, distribution, the founder flow, a verification checklist and a first-day troubleshooting table) and states plainly that Slack cannot verify the request URL until the API is reachable over public HTTPS — today port 8000 is closed in the VM's NSG and there is no TLS. Gates green (365 unit / 118 functional / 10 regression).
