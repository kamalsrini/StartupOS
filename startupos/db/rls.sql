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
    'users','sessions','api_tokens','connections','brain_docs','issues','projects','bills','vendors',
    'accounts_bank','transactions','cards','deployments','sequences','accounts','messages','documents',
    'events','signals','context_packs','runs','approvals','budgets','asks'
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
REVOKE ALL ON FUNCTION auth_lookup_google(TEXT, TEXT), auth_lookup_token(TEXT), auth_lookup_session(TEXT), auth_lookup_slack(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_lookup_google(TEXT, TEXT), auth_lookup_token(TEXT), auth_lookup_session(TEXT), auth_lookup_slack(TEXT) TO startupos_app;
