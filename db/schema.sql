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
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  email         TEXT NOT NULL,
  name          TEXT,
  role          TEXT NOT NULL DEFAULT 'owner',    -- owner | admin | member
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, email)
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
  id            TEXT PRIMARY KEY,                 -- rule_id:entity_id (idempotent)
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
  resolved_at   TIMESTAMPTZ
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
  outcome       TEXT,
  started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS runs_tenant_time ON runs (tenant_id, started_at DESC);

-- Approvals (the gate) -------------------------------------------------------
CREATE TABLE IF NOT EXISTS approvals (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL REFERENCES tenants(id),
  module        TEXT NOT NULL,
  type          TEXT NOT NULL,                    -- 'Linear · assign', 'Slack · nudge', ...
  target        TEXT NOT NULL,
  preview       TEXT NOT NULL,
  exec          JSONB,                            -- {server, tool, input} or null for record-only
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | declined | executed | failed
  created_by_run TEXT REFERENCES runs(id),
  signal_id     TEXT REFERENCES signals(id),
  decided_by    TEXT,
  decided_at    TIMESTAMPTZ,
  decline_reason TEXT,
  result        JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS approvals_pending ON approvals (tenant_id, module) WHERE status = 'pending';

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
