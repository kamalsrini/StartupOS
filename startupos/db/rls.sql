-- Tenant isolation — Row Level Security. Applied after schema.sql by scripts/apply_schema.py.
--
-- Model: every tenant-scoped table carries tenant_id. The application connects as role `startupos_app`
-- (NOBYPASSRLS) and sets `app.tenant_id` on the connection (common/db.get_conn). Policies compare
-- tenant_id to that setting; a connection with no tenant set sees nothing. The `postgres` superuser
-- bypasses RLS by design and is for migrations and operators only — services never use it.
--
-- Idempotent: safe to re-run.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'startupos_app') THEN
    CREATE ROLE startupos_app LOGIN NOBYPASSRLS PASSWORD 'startupos_app';
  END IF;
END$$;

GRANT USAGE ON SCHEMA public TO startupos_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO startupos_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO startupos_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO startupos_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO startupos_app;

-- tenants: a connection sees only its own tenant row.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_self ON tenants;
CREATE POLICY tenant_self ON tenants
  USING (id = current_setting('app.tenant_id', true))
  WITH CHECK (id = current_setting('app.tenant_id', true));

-- Every other tenant-scoped table.
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users','sessions','api_tokens','connections','tenant_secrets','brain_docs','issues','projects','bills','vendors',
    'accounts_bank','transactions','cards','deployments','sequences','accounts','messages','documents',
    'events','signals','context_packs','runs','approvals','budgets','asks','tenant_jobs','deliveries',
    'slack_installations'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) '
      'WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
  END LOOP;
END$$;

-- Login needs to find a user BEFORE a tenant is known (email / google_sub / token hash lookup).
-- That lookup runs through SECURITY DEFINER functions owned by the superuser, which return only the
-- (tenant_id, user_id) pair — never a row scan across tenants from application code.
CREATE OR REPLACE FUNCTION auth_lookup_google(p_sub TEXT, p_email TEXT)
RETURNS TABLE (tenant_id TEXT, user_id TEXT, status TEXT) LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT u.tenant_id, u.id, u.status FROM users u
  WHERE (p_sub IS NOT NULL AND u.google_sub = p_sub) OR (u.google_sub IS NULL AND lower(u.email) = lower(p_email))
  ORDER BY (u.google_sub = p_sub) DESC NULLS LAST LIMIT 1
$$;
CREATE OR REPLACE FUNCTION auth_lookup_token(p_hash TEXT)
RETURNS TABLE (tenant_id TEXT, user_id TEXT, token_id TEXT) LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT t.tenant_id, t.user_id, t.id FROM api_tokens t WHERE t.token_hash = p_hash AND t.revoked_at IS NULL LIMIT 1
$$;
CREATE OR REPLACE FUNCTION auth_lookup_session(p_id TEXT)
RETURNS TABLE (tenant_id TEXT, user_id TEXT, expires_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ) LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT s.tenant_id, s.user_id, s.expires_at, s.revoked_at FROM sessions s WHERE s.id = p_id LIMIT 1
$$;
CREATE OR REPLACE FUNCTION auth_lookup_slack(p_slack_user_id TEXT)
RETURNS TABLE (tenant_id TEXT, user_id TEXT) LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT u.tenant_id, u.id FROM users u WHERE u.slack_user_id = p_slack_user_id AND u.status = 'active' LIMIT 1
$$;
-- Sprint 3a (Track T): the daemon and the ingest loop run as startupos_app and must enumerate tenants to
-- schedule per-tenant jobs. Same pattern as the auth lookups: a SECURITY DEFINER function returning only the
-- cadence columns, never a cross-tenant scan from application code.
CREATE OR REPLACE FUNCTION tenants_active()
RETURNS TABLE (id TEXT, name TEXT, timezone TEXT, tier TEXT, pulse_hour SMALLINT, pulse_channel TEXT, status TEXT)
LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT t.id, t.name, t.timezone, t.tier, t.pulse_hour, t.pulse_channel, t.status
  FROM tenants t WHERE t.status = 'active' ORDER BY t.created_at, t.id
$$;
REVOKE ALL ON FUNCTION tenants_active() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tenants_active() TO startupos_app;
-- Sprint 3a (Track O): the job service (daemon/jobs.py) runs as startupos_app and must claim the next queued job
-- across tenants — a cross-tenant write the RLS policy forbids. Same pattern again: a SECURITY DEFINER claim that
-- returns exactly one row (or none) and enforces the queue rules in one statement:
--   * oldest queued job first (created_at, id), FOR UPDATE SKIP LOCKED so several daemons never claim the same row;
--   * per-tenant ordering: a job is claimable only when no earlier job of its tenant is still queued or running
--     (done/failed jobs never block — the chain continues past a failed backfill);
--   * `run_after` (retry backoff) must have passed; a job left 'running' for over 30 minutes (a daemon died
--     mid-job) is claimable again, so nothing stays stuck — its attempts still count towards the limit of 3;
--   * p_tenants narrows the claim to some tenants (the pinned dev scheduler); NULL = every tenant.
-- The claimed job is then executed and finished (done/failed/re-queued) on a connection bound to its tenant.
CREATE OR REPLACE FUNCTION tenant_jobs_claim(p_tenants TEXT[] DEFAULT NULL)
RETURNS SETOF tenant_jobs LANGUAGE sql SECURITY DEFINER VOLATILE AS $$
  UPDATE tenant_jobs j
     SET status = 'running', attempts = j.attempts + 1, started_at = now(), finished_at = NULL
   WHERE j.id = (
     SELECT c.id FROM tenant_jobs c
      WHERE ((c.status = 'queued' AND c.run_after <= now())
             OR (c.status = 'running' AND c.started_at < now() - interval '30 minutes'))
        AND (p_tenants IS NULL OR c.tenant_id = ANY(p_tenants))
        AND NOT EXISTS (
          SELECT 1 FROM tenant_jobs e
           WHERE e.tenant_id = c.tenant_id
             AND (e.status = 'queued' OR (e.status = 'running' AND e.started_at >= now() - interval '30 minutes'))
             AND (e.created_at, e.id) < (c.created_at, c.id))
      ORDER BY c.created_at, c.id
      LIMIT 1
      FOR UPDATE OF c SKIP LOCKED)
  RETURNING j.*
$$;
REVOKE ALL ON FUNCTION tenant_jobs_claim(TEXT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tenant_jobs_claim(TEXT[]) TO startupos_app;
-- Sprint 3b (Track I): an inbound Slack request (events, interactivity) arrives with no session and no tenant —
-- the ONLY thing that identifies the company is the Slack `team_id`. Resolving it is a cross-tenant read that RLS
-- forbids from application code, so it goes through the same SECURITY DEFINER pattern as the auth lookups and
-- returns one row with no credential in it (the bot token lives in tenant_secrets, encrypted). A revoked install
-- is still returned, with `revoked_at` set, so the caller answers "this workspace is not connected" rather than
-- silently accepting events for a tenant that uninstalled.
CREATE OR REPLACE FUNCTION slack_lookup_install(p_team_id TEXT)
RETURNS TABLE (tenant_id TEXT, team_id TEXT, team_name TEXT, bot_user_id TEXT, default_channel TEXT,
               installed_by TEXT, installed_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ)
LANGUAGE sql SECURITY DEFINER STABLE AS $$
  SELECT i.tenant_id, i.team_id, i.team_name, i.bot_user_id, i.default_channel,
         i.installed_by, i.installed_at, i.revoked_at
  FROM slack_installations i WHERE i.team_id = p_team_id LIMIT 1
$$;
REVOKE ALL ON FUNCTION slack_lookup_install(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION slack_lookup_install(TEXT) TO startupos_app;
REVOKE ALL ON FUNCTION auth_lookup_google(TEXT, TEXT), auth_lookup_token(TEXT), auth_lookup_session(TEXT), auth_lookup_slack(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_lookup_google(TEXT, TEXT), auth_lookup_token(TEXT), auth_lookup_session(TEXT), auth_lookup_slack(TEXT) TO startupos_app;
