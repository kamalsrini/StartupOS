---
name: startupos-sdlc-state
description: Current sprint state for the StartupOS codebase — what shipped, gate results, open PE findings, next tasks. Read at the start of every StartupOS session after the briefs.
type: project
---

**Repo:** `StartupAgents/startupos/` (main). Sprint 2 committed on the Mac as `92e1bbe` (2026-09-04). Sync pattern: `git apply` (working tree only) from a patch dropped in `StartupAgents/_to_delete/`, then the user commits — the Cowork sandbox cannot delete git lock files (`.git/index.lock`, `.git/rebase-apply/`), so `git am`/`commit` may fail there. Never hand the user archives.

**Runtime:** deployed on the Azure VM via docker compose since 2026-09-04 (see startupos_deploy.md) — but the VM still runs the Sprint 1 code with no auth. Redeploy after adding to the VM `.env`: `STARTUPOS_SESSION_SECRET`, `STARTUPOS_BOOTSTRAP_TOKEN` (blank after first login), `STARTUPOS_APP_PASSWORD` (compose derives `STARTUPOS_APP_DSN`), `STARTUPOS_PUBLIC_URL`, `STARTUPOS_WEB_URL`, optional `GOOGLE_CLIENT_ID/SECRET`. Without `STARTUPOS_SESSION_SECRET` and an APP DSN the API now refuses to start (by design). Keep NSG port 8000 closed until then.

**Sprint 2 (2026-09-04) — shipped, gates green (ruff · 200 unit · 30 functional · 7 regression · web eslint):**
- Tenant isolation: Postgres RLS on all 24 tenant tables, `startupos_app` NOBYPASSRLS role, `app.tenant_id` bound per connection (`common/db.get_conn(tenant_id=)`), SECURITY DEFINER `auth_lookup_google/token/session/slack` for pre-auth lookups; isolation tests prove cross-tenant reads see nothing and writes are refused.
- Auth (`auth/`): Google OIDC (PyJWT vs Google JWKS, iss/aud/exp/nonce/email_verified), httpOnly SameSite=Lax signed-cookie sessions backed by `sessions` (revocable, 14d TTL), API tokens `sos_<id>_<secret>` stored as sha256 only, bootstrap owner via `STARTUPOS_BOOTSTRAP_TOKEN` (constant-time compare, throttled 5/min/IP), Slack user → user mapping. One company-wide role for now (everyone `owner`). Tenant ALWAYS derived from the user; `?tenant=`/`X-Tenant-Id` removed. `approvals.decided_by`, `runs.acted_by` record identity. Web: `/login`, logout, 401→/login.
- Chief of Staff hub agent: `daemon/prompts/chief_of_staff.md`, `signals/lookahead.py` (Tier-0 facts: bills vs cash, runway, due issues, untouched high issues, POC milestones from customers.md front matter, campaign ages, deploy cadence, stale approvals, budget, disconnected sources), skill `cockpit.chief_of_staff` (T2 high_priority; strict JSON; writes `cos.risk:*` signals per spoke and resolves stale ones; proposals only within executor allow-list; Tier-0 fallback marks run `degraded`), scheduled 06:30 tenant-local before the pulse; pulse reads the brief; `/cockpit` returns `cos_brief` + `cos_risks`; Slack "what should I worry about".
- Asks queue: `asks` table; `POST /asks {question, mode: answer|cos}` → daemon `service_asks` every 30s → `GET /asks/{id}`. API never calls a model.
- Live check on real UnitOne data: unauth 401; bootstrap → session → `/modules/build` 115 open / 28 urgent-high as the RLS role; API token works; CoS run ok: 5 risks (UNI-158 corpus bottleneck, UNI-126 stale urgent, 18 stale in-progress, JCI Alts draft 93d, Brex disconnected), 5 asks, $0.052 (7.9k in, 6.3k cached, 1.8k out).

**PE findings fixed:** `settings.app_dsn` etc. were missing (Track D's report was right); API silently fell back to superuser DSN → now refuses outside `STARTUPOS_DEV=1`; bootstrap throttle added; compose services moved to the RLS role with password set at migrate; anthropic SDK 1.3 rejects `temperature` → dropped; `events.kind='created'` relabeled `first_seen` for the model (backfills looked like 162 new issues).

**Open findings / next tasks:**
1. Add the new env vars on the VM, redeploy (`./scripts/deploy_azure.sh`), bootstrap the first owner, then blank the bootstrap token.
2. Google OAuth client not yet created (needs GCP console); bootstrap + API tokens cover single-operator use meanwhile.
3. Invites (add a second user to a tenant) — Sprint 3. Roles stay flat until the user asks.
4. Web: Cockpit should render `cos_brief`/`cos_risks` and an "Ask the Chief of Staff" box using `/asks` polling — API is ready, UI not yet.
5. `brain/repo/customers.md` has no `milestones:` front matter yet → POC lookahead is empty; add JCI milestones.
6. assign_owner over-assigns to Alexey; pricing constants in `daemon/llm.py` unverified; Apollo ingest (Week 3); Vercel token team-scope; Slack/Brex keys.
7. Tests share one test DB — never run two `make check` concurrently.
