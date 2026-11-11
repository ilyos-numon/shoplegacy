-- shoplegacy schema. Policies live in 20261102000200_rls.sql.

-- App login role. It never bypasses RLS; the Store switches to anon/authenticated
-- per transaction with SET LOCAL ROLE.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'shoplegacy_app') THEN
    CREATE ROLE shoplegacy_app LOGIN PASSWORD 'shoplegacy_app' NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;
GRANT anon, authenticated TO shoplegacy_app;

CREATE TABLE public.orgs (
  org_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.org_members (
  org_id uuid REFERENCES public.orgs ON DELETE CASCADE,
  user_id uuid NOT NULL,
  role text NOT NULL CHECK (role IN ('owner', 'admin', 'reviewer', 'interviewer')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, user_id)
);

CREATE TABLE public.shops (
  shop_id text PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES public.orgs,
  created_by uuid NOT NULL,
  name text NOT NULL,
  owner_name text NOT NULL,
  trade text NOT NULL,
  area_type text NOT NULL,
  years int NOT NULL,
  closed_year int NOT NULL,
  supplier_names_json text NOT NULL,
  created_at timestamptz NOT NULL
);

CREATE TABLE public.sessions (
  session_id text PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES public.orgs,
  shop_id text NOT NULL REFERENCES public.shops,
  trade text NOT NULL,
  number int NOT NULL,
  started_at timestamptz NOT NULL,
  deadline timestamptz NOT NULL,
  ended_at timestamptz,
  node_index int NOT NULL,
  current_json text
);

CREATE TABLE public.answers (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES public.orgs,
  session_id text NOT NULL REFERENCES public.sessions,
  question_id text NOT NULL,
  axis text NOT NULL,
  question text NOT NULL,
  answer text NOT NULL,
  created_at timestamptz NOT NULL,
  session_number int
);

CREATE TABLE public.cards (
  card_id text PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES public.orgs,
  shop_id text NOT NULL UNIQUE REFERENCES public.shops,
  status text NOT NULL CHECK (status IN ('draft', 'on_hold', 'rejected', 'published', 'unpublished')),
  sections_json text NOT NULL,
  warnings_json text NOT NULL,
  evidence_json text NOT NULL DEFAULT '{}',
  version int NOT NULL DEFAULT 0,
  note text,
  sessions int NOT NULL,
  updated_at timestamptz NOT NULL
);

CREATE TABLE public.card_versions (
  card_id text REFERENCES public.cards ON DELETE CASCADE,
  version int NOT NULL,
  org_id uuid NOT NULL REFERENCES public.orgs,
  status text NOT NULL,
  sections_json text NOT NULL,
  evidence_json text NOT NULL,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (card_id, version)
);

CREATE TABLE public.consents (
  consent_id text PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES public.orgs,
  shop_id text NOT NULL REFERENCES public.shops,
  kind text NOT NULL CHECK (kind IN ('recording', 'storage', 'ai_processing', 'publication')),
  method text NOT NULL CHECK (method IN ('verbal', 'written', 'link')),
  note text,
  granted_by uuid NOT NULL,
  granted_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  revoke_reason text
);

CREATE TABLE public.audit_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  org_id uuid,
  actor_id uuid,
  action text NOT NULL,
  table_name text NOT NULL,
  row_id text NOT NULL,
  old_json jsonb,
  new_json jsonb,
  at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX org_members_user_id_idx ON public.org_members (user_id);
CREATE INDEX shops_org_id_idx ON public.shops (org_id);
CREATE INDEX sessions_shop_id_idx ON public.sessions (shop_id);
CREATE INDEX answers_session_id_idx ON public.answers (session_id);
CREATE INDEX cards_org_id_idx ON public.cards (org_id);
CREATE INDEX consents_shop_id_idx ON public.consents (shop_id);
CREATE INDEX audit_log_org_id_idx ON public.audit_log (org_id);

-- Audit trigger, after the supa_audit record_version pattern
-- (https://github.com/supabase/supa_audit, Apache-2.0): one row per change with
-- old/new records as jsonb. TG_ARGV holds the primary-key column names.
CREATE FUNCTION public.audit_row() RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $$
DECLARE
  v_new jsonb := CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN to_jsonb(NEW) END;
  v_old jsonb := CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) END;
  v_rec jsonb := coalesce(v_new, v_old);
  v_row_id text;
BEGIN
  SELECT string_agg(v_rec ->> k, ':') INTO v_row_id FROM unnest(TG_ARGV) AS k;
  INSERT INTO public.audit_log (org_id, actor_id, action, table_name, row_id, old_json, new_json)
  VALUES ((v_rec ->> 'org_id')::uuid, auth.uid(), TG_OP, TG_TABLE_NAME,
          coalesce(v_row_id, ''), v_old, v_new);
  RETURN coalesce(NEW, OLD);
END $$;

CREATE TRIGGER shops_audit AFTER INSERT OR UPDATE OR DELETE ON public.shops
  FOR EACH ROW EXECUTE FUNCTION public.audit_row('shop_id');
CREATE TRIGGER cards_audit AFTER INSERT OR UPDATE OR DELETE ON public.cards
  FOR EACH ROW EXECUTE FUNCTION public.audit_row('card_id');
CREATE TRIGGER consents_audit AFTER INSERT OR UPDATE OR DELETE ON public.consents
  FOR EACH ROW EXECUTE FUNCTION public.audit_row('consent_id');
CREATE TRIGGER org_members_audit AFTER INSERT OR UPDATE OR DELETE ON public.org_members
  FOR EACH ROW EXECUTE FUNCTION public.audit_row('org_id', 'user_id');

-- Helpers. SECURITY DEFINER so policies can read org_members without recursing
-- into its own policy (the owner bypasses RLS).
CREATE FUNCTION public.current_org_ids() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT m.org_id FROM public.org_members m WHERE m.user_id = auth.uid()
$$;

CREATE FUNCTION public.has_role(p_org uuid, p_roles text[]) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.org_members m
    WHERE m.org_id = p_org AND m.user_id = auth.uid() AND m.role = ANY (p_roles)
  )
$$;

CREATE FUNCTION public.user_id_by_email(p_email text) RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT u.id FROM auth.users u WHERE lower(u.email) = lower(p_email) LIMIT 1
$$;

-- True when the caller created the org and it has no members yet
-- (lets the creator insert the first owner row).
CREATE FUNCTION public.is_first_owner(p_org uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.orgs o
    WHERE o.org_id = p_org AND o.created_by = auth.uid()
  ) AND NOT EXISTS (SELECT 1 FROM public.org_members m WHERE m.org_id = p_org)
$$;

REVOKE ALL ON FUNCTION public.current_org_ids() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.has_role(uuid, text[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.user_id_by_email(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.is_first_owner(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.current_org_ids() TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.has_role(uuid, text[]) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.user_id_by_email(text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.is_first_owner(uuid) TO authenticated;
