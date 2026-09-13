# StartupOS — Spec vs. Code Audit (2026-09-12)

Independent audit of `startupos/` against `StartupOS_Research_Brief.md`, `StartupOS_Architecture_Brief.md` and `startupos/CONTRACTS.md`. Every status was confirmed by reading the named code path or test. **Partial** = code exists but the brief's acceptance is not met.

**Verdict:** Sprints 1–2 (Architecture Brief §7 Weeks 1–2) are substantially built and gated. Week 3 (Sales, the wedge) is absent. Week 4 has the API surface but fails the SaaS test — *a second tenant cannot be created and served without an operator editing `.env` and running `make` targets.* One production defect found and fixed in this audit: the signal engine and context pack were never scheduled by the daemon (now: engine runs at the top of every 15-minute tick; pack compiles nightly at 02:00).

Gates at audit time: ruff clean · 200 unit · 31 functional · 7 regression · web eslint clean.

## 1. Build plan (§7)

| Week | Item | Status | Evidence | Gap |
|---|---|---|---|---|
| 1 | VM + Docker Compose | Done | `docker-compose.yml`, `scripts/deploy_azure.sh` | — |
| 1 | Postgres + pgvector | Partial | `db/schema.sql` embedding is `DOUBLE PRECISION[]`; compose image is pgvector | No embeddings written; retrieval is FTS only (`brain/retrieve.py`) |
| 1 | `brain/` repo seeded | Done (UnitOne) | `brain/repo/*.md`, `brain/sync.py` | New tenants get 5 template slices, not a repo |
| 1 | Ingest Linear/Brex/Slack | Done | `ingest/{linear,slack,brex,vercel}.py`, `runner.py` | Slack backfill capped at 500 msgs, not 90 days |
| 1 | Signal engine (10 rules) | Done | `signals/rules.py` (12 rules) + golden test | **Was unscheduled — fixed 2026-09-12** |
| 1 | Nightly context pack | Done | `brain/pack.py` | **Was unscheduled — fixed 2026-09-12** (02:00 job) |
| 1 | Evening digest to Slack | Partial | `signals/digest.py --post`, `cockpit.evening_digest` | Scheduled skill returns text, never posts |
| 2 | Daemon + scheduler + Slack gateway | Done | `daemon/main.py`, `scheduler.job_table`, `gateway/slack.py` | Single-tenant per process |
| 2 | Morning pulse | Partial | `skills/cockpit/morning_pulse.py` | Web only; `pulse_channel` read but not honoured |
| 2 | Build + Finance skills | Done | `build.assign_owner`, `build.nudge_stale`, `finance.ap_queue` | — |
| 2 | Approval queue + Linear executor | Done | `daemon/approvals.py`, `executors/`, end-to-end test | — |
| 2 | Sep 3 artifact rewired to API | Partial | `api/routers/modules.py`, `web/app/m/[name]` | No ⌘K, no agent roster |
| 3 | Apollo ingest | Missing | no `ingest/apollo.py` | `sequences` fillable only by fixtures |
| 3 | Reply-detected + enrollment skills | Missing | no `daemon/skills/sales/` | — |
| 3 | Weekly review | Missing | `scheduler.weekly_review` placeholder | — |
| 3 | Budgets + run ledger in Cockpit | Done | `daemon/budget.py`, `/runs/summary`, `/cockpit.spend` | — |
| 4 | Sign-up | Done | `POST /onboarding/tenant` (bootstrap or Google id_token) | No website → identity extraction |
| 4 | Source picker | Partial | `POST /onboarding/connections` | `kv:` refs unimplemented; keys are global env — no per-tenant credentials |
| 4 | Brain compile + five cards | Partial | `onboarding.compile_cards` | Does not run ingest backfill; no Tier-1 extraction |
| 4 | First-pulse flow | Partial | wizard polls `/cockpit` | Nothing runs the pulse for a non-default tenant |
| 4 | Slack install | Missing | — | Global `SLACK_APP_TOKEN` only |

## 2. Use cases (§4)

| Use case | Status | Evidence / gap |
|---|---|---|
| Cockpit · Morning pulse | Partial | T2 + T0 fallback; web only |
| Cockpit · Evening digest | Partial | not posted to Slack |
| Cockpit · Ask the OS | Done | `ask.answer`, `/asks` queue, `service_asks` |
| Cockpit · Chief of Staff | Done | `cockpit.chief_of_staff`, `signals/lookahead.py` |
| Sales · Reply → draft / enrollment / campaign wave / weekly review | Missing | rule `sales.reply_detected` only; no Apollo ingest, no skills |
| Finance · AP due 7d | Done | rule + `finance.ap_queue` (record-only, Brex deep link is not per-bill) |
| Finance · Cash and runway | Done | rule `finance.cash_low`, `lookahead.runway` |
| Finance · Who paid us | Partial | signal only; no payer→invoice match, no `invoices` table |
| Finance · Invoice draft | Missing | — |
| Build · Stale urgent | Done | rule + `build.nudge_stale` (channel falls back to `#eng`) |
| Build · Unassigned High → owner | Done | rule + `build.assign_owner` + Linear exec |
| Build · Release notes | Missing | — |
| Build · Failed deploy log | Partial | signal only; no incident row in brain |
| Customers · Ask untouched | Done | rule + `customers.prioritize_ask` |
| Customers · Account brief / Scan → summary | Missing | — |
| Marketing · Asset w/o follow-up · LinkedIn batch | Partial | rules only |
| Marketing · Post publishing | Missing | — |
| Web · Analytics off / stale | Partial | rule only |
| Security · Duplicates · Controls posture | Partial | rule / tiles only |
| Security · Findings → issues | Missing | — |

## 3. User flows (§5)

Day 0: sign-up Partial (no extraction) · connect sources Partial (global env keys, no backfill kick-off) · compile Partial (no ingest, no extraction) · five cards **Done** · first pulse Partial (no trigger for new tenant) · cadence **Done** (Slack install missing).
Everyday: 07:00 pulse Partial (web only) · approve queue **Done** · Ask the OS **Done** · signals to Slack Missing · 18:00 digest Partial · Friday review Missing.

## 4. Principles and token economics (§1, §3)

No LLM in ingest/polling/rendering **Done** (only `daemon/llm.py` imports `anthropic`) · one gateway **Done** · three tiers **Done** · per-tenant budgets + degradation **Done** · prompt caching **Done** (cache-creation premium counted) · run ledger in Cockpit **Done** · batching Partial (`nudge_stale` capped at 5 but one call per signal) · BYO key **Missing** · nightly compile **fixed 2026-09-12**.

## 5. Architecture and tenancy (§2)

Brain repo ⇄ `brain_docs` Done (single tenant) · Postgres FTS Done · pgvector Missing · ingest workers Partial (4 of 8 sources, single-tenant loop) · signal engine Done (now scheduled) · context pack Done (now scheduled) · daemon/skills Partial (8 skills, single tenant, text-only Slack) · approval gate + executors Done (DB CHECK + FOR UPDATE + allow-list) · API Done (25 routes, tenant from principal only) · web Partial · Key Vault Missing · RLS Done (25 tables, FORCE, isolation tests) · auth Done.

## 6. Research brief — modules and gtm-engine skills

gtm-engine skills as daemon skills: **0 of 9**. Cockpit Done · Sales shell only · Finance AP + cash (no AR) · Marketing/Web/Social rules only · Build two skills · Commerce stub · Customers one skill · Security tiles + one rule. No Stripe, no tier enforcement beyond budget allowances.

## 7. Tests and quality

Unit 178 functions / 18 files; functional 31 / 9 files; regression 7 / 3 files. No tests: Vercel live path, `runner --loop`, `daemon/main.py`, Slack `start`, `pack._summarize`, `web/` (no JS/e2e). Placeholders: `weekly_review`; `settings.secret` `kv:`; `nudge_stale` `#eng`; `ap_queue` fixed Brex URL; `PRICING_PER_MILLION`/`ALLOWED_BY_TIER` marked assumptions; `rls.sql` literal role password (overridden by `apply_schema.py` when `STARTUPOS_APP_PASSWORD` set).

## 8. Top 10 gaps — ranked for "second tenant without a human" and "opens with work already done"

1. ~~Daemon, ingest loop and Slack gateway are single-tenant~~ **Fixed 3a** (Slack app token still per install → 3b). Was: single-tenant (`settings.tenant_id`). → iterate tenants in `job_table`, `runner --loop`, gateway.
2. ~~Signal engine and context pack never run in production.~~ **Fixed 2026-09-12.**
3. ~~Per-tenant credentials impossible~~ **Fixed 3a.** Was: (`secret()` env-only). → `tenant_secrets` (encrypted) resolved by `kv:` refs; ingest takes `(tenant_id, connection)`.
4. ~~Onboarding compile does not ingest.~~ **Fixed 3a.** → enqueue backfill per connected source, then engine + pack, return real counts.
5. No Tier-1 extraction on day 0. → `onboarding.extract` T1 skill via the `asks` queue pattern.
6. ~~Pulse/digest never reach Slack; no signal → Slack.~~ **Fixed 3b.** → honour `pulse_channel`; post high-severity signals on tick.
7. Sales has zero skills and no Apollo ingest (the wedge). → `ingest/apollo.py` + `sales.reply_draft` (T2) + `sales.enroll_proposal` (T1).
8. ~~Slack install + Block Kit buttons absent.~~ **Fixed 3b.** → OAuth install route with per-tenant bot token; interactive approve/decline.
9. ~~First pulse for a new tenant needs a human.~~ **Fixed 3a.** → on fifth-card confirm, enqueue CoS + pulse for that tenant.
10. BYO key + weekly review placeholders. → `tenants.anthropic_key_ref`; real weekly-review skill.

## Proposed Sprint 3 (in this order)

**3a — Multi-tenant runtime (gaps 1, 3, 4, 9) — SHIPPED 2026-09-12 (see decision log; PE review fixed two cross-tenant credential defects):** tenant iteration in daemon/ingest/gateway; `tenant_secrets` with envelope encryption; onboarding compile triggers backfill → engine → pack → CoS → pulse. Acceptance: create a second tenant through the API with its own Linear key and see a real pulse without touching `.env`.
**3b — Delivery (gap 6, 8) — SHIPPED 2026-09-13 (see decision log):** pulse/digest/high signals to Slack per `pulse_channel`; Slack install + buttons.
**3c — Sales wedge (gap 7):** Apollo ingest, reply draft, enrollment proposal, weekly review.
