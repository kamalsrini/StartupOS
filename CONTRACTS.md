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
