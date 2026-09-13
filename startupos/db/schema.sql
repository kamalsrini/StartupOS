-- StartupOS company brain — schema v1
-- Postgres 16. Idempotent. Applied by `make db` / scripts/apply_schema.py.
-- Embeddings are stored as double precision[] in v1 (no pgvector dependency for tests);
-- production compose uses pgvector and a migration flips brain_docs.embedding to vector(1536).

CREATE TABLE IF NOT EXISTS tenants (
  id            TEXT PRIMARY KEY,                 -- slug, e.g. 'unitone'
  name          TEXT NOT NULL,
  website       TEXT,
  timezone      TEXT NOT NULL DEFAULT 'America/Los_Angeles',
  tier          TEXT NOT NULL DEFAULT 'founder',  -- founder | team | growth
  pulse_hour    SMALLINT NOT NULL DEFAULT 7,      -- local hour for the morning pulse
  pulse_channel TEXT NOT NULL DEFAULT 'web',      -- web | slack | both
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS pulse_hour SMALLINT NOT NULL DEFAULT 7;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS pulse_channel TEXT NOT NULL DEFAULT 'web';
-- Sprint 3a (Track T): the scheduler and ingest iterate tenants WHERE status = 'active'; suspended/deleted tenants
-- keep their rows but get no jobs and no ingest passes.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';  -- active | suspended | deleted

CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  email         TEXT NOT NULL,
  name          TEXT,
  role          TEXT NOT NULL DEFAULT 'owner',    -- owner | admin | member (v1: everyone in a company is owner)
  google_sub    TEXT,                             -- Google OIDC subject; NULL until first Google sign-in
  slack_user_id TEXT,                             -- maps Slack button presses to a real identity
  status        TEXT NOT NULL DEFAULT 'active',   -- active | invited | disabled
  last_login_at TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, email)
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_sub TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS slack_user_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS users_google_sub ON users (google_sub) WHERE google_sub IS NOT NULL;
CREATE INDEX IF NOT EXISTS users_email ON users (lower(email));

-- Browser sessions: the cookie carries only the opaque session id (HMAC-signed); state lives here so it can be revoked.
CREATE TABLE IF NOT EXISTS sessions (
  id            TEXT PRIMARY KEY,                 -- random 32-byte urlsafe
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  user_id       TEXT NOT NULL REFERENCES users(id),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL,
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  user_agent    TEXT,
  revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id) WHERE revoked_at IS NULL;

-- Personal API tokens (CLI, Slack gateway, curl). Only the sha256 hash is stored; the token is shown once.
CREATE TABLE IF NOT EXISTS api_tokens (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  user_id       TEXT NOT NULL REFERENCES users(id),
  name          TEXT NOT NULL,
  token_hash    TEXT NOT NULL UNIQUE,             -- sha256 hex of the full token 'sos_<id>_<secret>'
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at  TIMESTAMPTZ,
  revoked_at    TIMESTAMPTZ
);

-- Source connections. Credentials are NEVER stored here: secret_ref points at env var / Key Vault name.
CREATE TABLE IF NOT EXISTS connections (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  source        TEXT NOT NULL,                    -- linear | slack | brex | apollo | vercel | posthog | github | gdrive | gmail | stripe
  secret_ref    TEXT NOT NULL,                    -- e.g. 'env:LINEAR_API_KEY' or 'kv:unitone-linear'
  config        JSONB NOT NULL DEFAULT '{}',      -- channels, team ids, project ids
  status        TEXT NOT NULL DEFAULT 'connected',-- connected | needs_reauth | disabled
  last_sync_at  TIMESTAMPTZ,
  last_error    TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, source)
);

-- Per-tenant secrets (Sprint 3a, Track S). Envelope-encrypted by common/secrets.py: the value is AES-GCM'd
-- under a per-row random data key, which is itself wrapped with STARTUPOS_MASTER_KEY. Only common/secrets.py
-- reads or writes ciphertext; the plaintext is never logged, never returned by the API, never in fixtures.
CREATE TABLE IF NOT EXISTS tenant_secrets (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  name          TEXT NOT NULL,                    -- e.g. 'linear_api_key' (referenced as 'kv:linear_api_key')
  ciphertext    BYTEA NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, name)
);

-- brain/ Markdown mirror. One row per file; path is the repo-relative path.
CREATE TABLE IF NOT EXISTS brain_docs (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  path          TEXT NOT NULL,                    -- identity.md, icp.md, voice.md, pricing.md, team.md, customers.md, decisions.md, finance.md, gtm.md
  slice         TEXT NOT NULL,                    -- identity | icp | voice | pricing | team | customers | decisions | finance | build | gtm
  content       TEXT NOT NULL,
  version       INTEGER NOT NULL DEFAULT 1,
  source        TEXT NOT NULL DEFAULT 'human',    -- human | extracted | decision
  embedding     DOUBLE PRECISION[],
  tsv           TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, path)
);
CREATE INDEX IF NOT EXISTS brain_docs_tsv ON brain_docs USING GIN (tsv);

-- Typed ingested state -------------------------------------------------------
CREATE TABLE IF NOT EXISTS issues (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  id            TEXT NOT NULL,                    -- Linear identifier, e.g. UNI-158
  uuid          TEXT,
  title         TEXT NOT NULL,
  status        TEXT,                             -- Backlog | In Progress | In Review | Done | Canceled
  status_type   TEXT,                             -- backlog | started | completed | canceled
  priority      INTEGER,                          -- 0 none, 1 urgent, 2 high, 3 medium, 4 low
  assignee      TEXT,
  project       TEXT,
  team          TEXT,
  labels        TEXT[] NOT NULL DEFAULT '{}',
  url           TEXT,
  created_at    TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ,
  raw           JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS projects (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  id            TEXT NOT NULL,
  name          TEXT NOT NULL,
  status        TEXT,
  target_date   DATE,
  lead          TEXT,
  url           TEXT,
  updated_at    TIMESTAMPTZ,
  raw           JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS bills (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  id            TEXT NOT NULL,                    -- Brex expense id
  vendor_id     TEXT,
  vendor_name   TEXT,
  amount        NUMERIC(14,2) NOT NULL,
  currency      TEXT NOT NULL DEFAULT 'USD',
  status        TEXT,                             -- DRAFT | SUBMITTED | APPROVED | OUT_OF_POLICY | CANCELED | VOID | SETTLED
  payment_status TEXT,                            -- CLEARED | SCHEDULED | AWAITING_PAYMENT ...
  due_at        TIMESTAMPTZ,
  purchased_at  TIMESTAMPTZ,
  payment_send_at TIMESTAMPTZ,
  invoice_number TEXT,
  memo          TEXT,
  raw           JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS vendors (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  id            TEXT NOT NULL,
  name          TEXT NOT NULL,
  email         TEXT,
  rail          TEXT,                             -- US_ACH | INTL_SWIFT_WIRE
  country       TEXT,
  status        TEXT,
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS accounts_bank (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  id            TEXT NOT NULL,
  name          TEXT,
  nickname      TEXT,
  account_type  TEXT,
  priority      TEXT,                             -- PRIMARY | NON_PRIMARY
  last4         TEXT,
  available     NUMERIC(14,2) NOT NULL DEFAULT 0,
  inflow_mtd    NUMERIC(14,2) NOT NULL DEFAULT 0,
  outflow_mtd   NUMERIC(14,2) NOT NULL DEFAULT 0,
  observed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS transactions (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  id            TEXT NOT NULL,
  type          TEXT,                             -- WIRE | ACH | ...
  status        TEXT,
  amount        NUMERIC(14,2) NOT NULL,           -- signed: + incoming, - outgoing
  currency      TEXT NOT NULL DEFAULT 'USD',
  counterparty  TEXT,
  occurred_at   TIMESTAMPTZ NOT NULL,
  raw           JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS cards (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  id            TEXT NOT NULL,
  holder        TEXT,
  display_name  TEXT,
  last4         TEXT,
  status        TEXT,
  limit_total   NUMERIC(14,2),
  limit_spent   NUMERIC(14,2),
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS deployments (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  id            TEXT NOT NULL,
  project       TEXT NOT NULL,
  state         TEXT,                             -- READY | ERROR | BUILDING
  target        TEXT,                             -- production | preview
  commit_message TEXT,
  created_at    TIMESTAMPTZ,
  url           TEXT,
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS sequences (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  id            TEXT NOT NULL,
  name          TEXT NOT NULL,
  status        TEXT,
  contacts      INTEGER DEFAULT 0,
  delivered     INTEGER DEFAULT 0,
  opened        INTEGER DEFAULT 0,
  clicked       INTEGER DEFAULT 0,
  replied       INTEGER DEFAULT 0,
  bounced       INTEGER DEFAULT 0,
  observed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS accounts (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  id            TEXT NOT NULL,                    -- slug
  name          TEXT NOT NULL,
  stage         TEXT,                             -- target | engaged | poc | customer | advisory | hold
  champion      TEXT,
  icp_score     INTEGER,
  notes         TEXT,
  last_touch_at TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS messages (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  id            TEXT NOT NULL,                    -- slack ts or gmail id
  source        TEXT NOT NULL,                    -- slack | gmail
  channel       TEXT,
  author        TEXT,
  text          TEXT NOT NULL,
  occurred_at   TIMESTAMPTZ NOT NULL,
  thread_id     TEXT,
  tsv           TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  raw           JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, source, id)
);
CREATE INDEX IF NOT EXISTS messages_tsv ON messages USING GIN (tsv);
CREATE INDEX IF NOT EXISTS messages_time ON messages (tenant_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS documents (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  id            TEXT NOT NULL,
  source        TEXT NOT NULL,                    -- gdrive | upload | web
  title         TEXT,
  content       TEXT NOT NULL,
  url           TEXT,
  tsv           TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', coalesce(title,'') || ' ' || content)) STORED,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, source, id)
);
CREATE INDEX IF NOT EXISTS documents_tsv ON documents USING GIN (tsv);

-- Change log ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  source        TEXT NOT NULL,
  entity        TEXT NOT NULL,                    -- issues | bills | transactions | deployments | messages | sequences
  entity_id     TEXT NOT NULL,
  kind          TEXT NOT NULL,                    -- created | updated | deleted
  diff          JSONB NOT NULL DEFAULT '{}',      -- {field: [old, new]}
  occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_tenant_time ON events (tenant_id, occurred_at DESC);

-- Signals (deterministic rule hits) -----------------------------------------
CREATE TABLE IF NOT EXISTS signals (
  id            TEXT NOT NULL,                    -- rule_id:entity_id (idempotent within a tenant)
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  module        TEXT NOT NULL,                    -- sales | finance | build | customers | marketing | web | social | security | cockpit
  rule_id       TEXT NOT NULL,
  severity      TEXT NOT NULL,                    -- high | medium | low | info
  kind          TEXT NOT NULL,                    -- reply | click | open | bounce  (maps to UI signal-icon)
  title         TEXT NOT NULL,
  meta          TEXT,
  entity        TEXT,
  entity_id     TEXT,
  suggested_skill TEXT,                           -- e.g. build.assign_owner
  href          TEXT,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at   TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS signals_open ON signals (tenant_id, module) WHERE resolved_at IS NULL;

-- Compiled context packs -----------------------------------------------------
CREATE TABLE IF NOT EXISTS context_packs (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  content       TEXT NOT NULL,
  token_estimate INTEGER NOT NULL,
  cache_key     TEXT NOT NULL,                    -- sha256 of content
  compiled_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS context_packs_latest ON context_packs (tenant_id, compiled_at DESC);

-- Agent runs (the ledger) ----------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  trigger       TEXT NOT NULL,                    -- schedule | signal | ask | approval
  skill         TEXT NOT NULL,
  tier          INTEGER NOT NULL,                 -- 0 | 1 | 2
  model         TEXT,
  tokens_in     INTEGER NOT NULL DEFAULT 0,
  tokens_cached INTEGER NOT NULL DEFAULT 0,
  tokens_out    INTEGER NOT NULL DEFAULT 0,
  cost_usd      NUMERIC(10,5) NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'ok',       -- ok | error | degraded
  acted_by      TEXT,                             -- users.id for ask/decide-triggered runs; NULL for scheduled
  outcome       TEXT,
  started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS runs_tenant_time ON runs (tenant_id, started_at DESC);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS acted_by TEXT;

-- Approvals (the gate) -------------------------------------------------------
CREATE TABLE IF NOT EXISTS approvals (
  id            TEXT NOT NULL,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  module        TEXT NOT NULL,
  type          TEXT NOT NULL,                    -- 'Linear · assign', 'Slack · nudge', ...
  target        TEXT NOT NULL,
  preview       TEXT NOT NULL,
  exec          JSONB,                            -- {server, tool, input} or null for record-only
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | declined | executed | failed
  created_by_run TEXT REFERENCES runs(id),
  signal_id     TEXT,
  decided_by    TEXT,                             -- users.id (or 'slack:<id>' before mapping, 'system')
  decided_at    TIMESTAMPTZ,
  decline_reason TEXT,
  result        JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Executor allow-list is enforced in the DB too: only Linear and Slack may ever be executed. Brex never.
  CONSTRAINT approvals_exec_server_allowed CHECK (exec IS NULL OR (exec->>'server') IN ('Linear','Slack')),
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT approvals_signal_fkey FOREIGN KEY (tenant_id, signal_id) REFERENCES signals (tenant_id, id)
);
-- Sprint 3a (Track T): signal and approval ids are derived from rule + entity ids (marketing.analytics_off:posthog,
-- assign-acm-158) and repeat across tenants, so both keys are per tenant. Migrate a pre-3a database in place.
DO $$
BEGIN
  IF (SELECT array_length(conkey, 1) FROM pg_constraint WHERE conrelid = 'signals'::regclass AND contype = 'p') = 1 THEN
    ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_signal_id_fkey;
    ALTER TABLE signals DROP CONSTRAINT signals_pkey;
    ALTER TABLE signals ADD PRIMARY KEY (tenant_id, id);
  END IF;
  IF (SELECT array_length(conkey, 1) FROM pg_constraint WHERE conrelid = 'approvals'::regclass AND contype = 'p') = 1 THEN
    ALTER TABLE approvals DROP CONSTRAINT approvals_pkey;
    ALTER TABLE approvals ADD PRIMARY KEY (tenant_id, id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'approvals'::regclass AND conname = 'approvals_signal_fkey') THEN
    ALTER TABLE approvals ADD CONSTRAINT approvals_signal_fkey FOREIGN KEY (tenant_id, signal_id) REFERENCES signals (tenant_id, id);
  END IF;
END$$;
CREATE INDEX IF NOT EXISTS approvals_pending ON approvals (tenant_id, module) WHERE status = 'pending';

-- Asks queue: the API never calls a model. A question from the web/API lands here; the daemon services it
-- (every 30s) with ask.answer or the Chief of Staff, and writes the answer back. Slack answers inline.
CREATE TABLE IF NOT EXISTS asks (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  user_id       TEXT NOT NULL,
  mode          TEXT NOT NULL DEFAULT 'answer',   -- answer | cos
  question      TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
  answer        JSONB,                            -- {text} for answer; the CoS output object for cos
  run_id        TEXT REFERENCES runs(id),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  answered_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS asks_pending ON asks (tenant_id, created_at) WHERE status = 'pending';

-- Onboarding job chain (Sprint 3a, Track O) ---------------------------------------------------------
-- POST /onboarding/compile enqueues, in order, backfill:<source> per connected source → signals → context_pack →
-- chief_of_staff → morning_pulse. daemon/jobs.py services the queue every 15 s through the SECURITY DEFINER
-- tenant_jobs_claim() (db/rls.sql): one queued job at a time, FOR UPDATE SKIP LOCKED, in created_at order, and a job
-- runs only when no earlier job of the same tenant is still queued/running. Each job then executes on a connection
-- bound to its tenant. Failures record error/attempts and retry up to 3 times; `run_after` carries the backoff.
CREATE TABLE IF NOT EXISTS tenant_jobs (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  kind          TEXT NOT NULL,                    -- backfill:<source> | signals | context_pack | chief_of_staff | morning_pulse | slack_event
  payload       JSONB NOT NULL DEFAULT '{}',      -- {source} for backfills; `result` is appended when the job finishes
  status        TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
  attempts      INTEGER NOT NULL DEFAULT 0,
  error         TEXT,
  run_after     TIMESTAMPTZ NOT NULL DEFAULT now(),  -- retry backoff: not claimable before this
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ,
  CONSTRAINT tenant_jobs_status_check CHECK (status IN ('queued', 'running', 'done', 'failed'))
);
CREATE INDEX IF NOT EXISTS tenant_jobs_queue ON tenant_jobs (status, created_at);
CREATE INDEX IF NOT EXISTS tenant_jobs_tenant ON tenant_jobs (tenant_id, created_at DESC);

-- Slack install (Sprint 3b, Track I) -----------------------------------------------------------------
-- One Slack workspace ↔ one tenant. The bot token is NOT here: it goes to `tenant_secrets` as
-- `slack_bot_token` and the tenant's `connections` row for `slack` points at it (`kv:slack_bot_token`), so
-- delivery and the executors resolve it through common.secrets.credential_for_source unchanged.
-- `team_id` is UNIQUE: a second tenant installing into a workspace that is already connected is refused
-- (the callback redirects with ?slack=error&reason=workspace_taken) rather than hijacking the first tenant.
-- Inbound Slack requests resolve their tenant ONLY through this table (slack_lookup_install, db/rls.sql).
CREATE TABLE IF NOT EXISTS slack_installations (
  tenant_id       TEXT PRIMARY KEY REFERENCES tenants(id),
  team_id         TEXT NOT NULL UNIQUE,             -- Slack workspace id (T…)
  team_name       TEXT,
  bot_user_id     TEXT,                             -- the bot's own user id (U…) — used to ignore its own messages
  default_channel TEXT NOT NULL DEFAULT '#general', -- where deliveries land when the tenant names nothing else
  installed_by    TEXT,                             -- StartupOS user id that ran the install
  installed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at      TIMESTAMPTZ                       -- app_uninstalled / tokens_revoked; a revoked row is "not connected"
);
CREATE INDEX IF NOT EXISTS slack_installations_live ON slack_installations (team_id) WHERE revoked_at IS NULL;

-- Budgets --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS budgets (
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  month         DATE NOT NULL,                    -- first of month
  tier2_tokens_allowed BIGINT NOT NULL,
  tier2_tokens_used    BIGINT NOT NULL DEFAULT 0,
  tier1_tokens_used    BIGINT NOT NULL DEFAULT 0,
  cost_usd      NUMERIC(10,4) NOT NULL DEFAULT 0,
  state         TEXT NOT NULL DEFAULT 'normal',   -- normal | conserve (>=90%) | exhausted
  PRIMARY KEY (tenant_id, month)
);

-- Slack delivery (Sprint 3b, Track D) ----------------------------------------------------------------
-- Where a tenant's Slack posts go. `tenants.pulse_channel` stays web|slack|both and says WHERE the founder
-- wants the pulse; `tenants.slack_channel` names WHICH Slack channel. 'web' means never post.
--
-- The column is NULLABLE with NO default: unset means "use the install's `default_channel`", i.e. the channel the
-- founder picked in Slack while installing. It shipped NOT NULL DEFAULT '#general', which — since it takes
-- precedence over the install — silently overrode that choice for every tenant forever. The DO block migrates
-- once: it drops NOT NULL and the default, then turns rows still holding the untouched '#general' into NULL.
-- Nothing ever wrote the column before this migration, so every '#general' in it is that untouched default.
-- Re-applying the schema is a no-op: the guard (a default still present) is false from then on.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS slack_channel TEXT;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'tenants'
       AND column_name = 'slack_channel' AND column_default IS NOT NULL
  ) THEN
    ALTER TABLE tenants ALTER COLUMN slack_channel DROP NOT NULL;
    ALTER TABLE tenants ALTER COLUMN slack_channel DROP DEFAULT;
    UPDATE tenants SET slack_channel = NULL WHERE slack_channel = '#general';
  END IF;
END $$;

-- One row per outbound post StartupOS makes. `ref` is what makes a delivery unique — signal:<signal_id>,
-- pulse:<YYYY-MM-DD>, digest:<YYYY-MM-DD>, approval:<approval_id> — and the UNIQUE constraint IS the dedupe:
-- a conflicting insert means "already delivered", not a query the caller has to remember to run. A row is
-- claimed `pending` before the post so two daemons racing on the same ref can never both post; it is then
-- finished `sent` (with the message `ts`, which Track B's chat.update needs), `skipped` or `failed`.
-- The tenant FK cascades: a delivery is a log line about a tenant, so deleting the tenant (tests, an offboard)
-- must not be blocked by its Slack history.
CREATE TABLE IF NOT EXISTS deliveries (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,                    -- pulse | digest | signal | approval
  ref           TEXT NOT NULL,                    -- signal:<id> | pulse:<date> | digest:<date> | approval:<id>
  channel       TEXT,
  ts            TEXT,                             -- Slack message ts (chat.update / permalinks)
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | skipped | failed
  error         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT deliveries_status_check CHECK (status IN ('pending', 'sent', 'skipped', 'failed')),
  UNIQUE (tenant_id, kind, ref)
);
CREATE INDEX IF NOT EXISTS deliveries_tenant_kind ON deliveries (tenant_id, kind, created_at DESC);
