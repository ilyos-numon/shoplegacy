-- Transcription jobs, invoices, shop owner review links, and removal of the
-- four-column public_card_tags (public_card_tags_v2 is the only reader left).

-- Transcription jobs. text is stored encrypted by the app.
CREATE TABLE public.stt_jobs (
  job_id text PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES public.orgs(org_id) ON DELETE CASCADE,
  session_id text NOT NULL REFERENCES public.sessions(session_id) ON DELETE CASCADE,
  created_by uuid NOT NULL,
  status text NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
  audio_key text NOT NULL,
  content_type text NOT NULL,
  bytes int NOT NULL CHECK (bytes >= 0),
  text text,
  duration_s double precision,
  provider text,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz
);
CREATE INDEX stt_jobs_session_idx ON public.stt_jobs (session_id, created_at DESC);

-- Invoices. One per (org, plan, period). Written only through the functions below.
CREATE TABLE public.invoices (
  invoice_id text PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES public.orgs(org_id) ON DELETE CASCADE,
  plan text NOT NULL CHECK (plan IN ('pilot', 'org')),
  period_start date NOT NULL,
  period_end date NOT NULL CHECK (period_end > period_start),
  amount_krw int NOT NULL CHECK (amount_krw >= 0),
  status text NOT NULL CHECK (status IN ('issued', 'paid', 'void')),
  method text CHECK (method IN ('card', 'contract')),
  provider text,
  provider_payment_key text,
  order_id text NOT NULL UNIQUE,
  memo text,
  issued_at timestamptz NOT NULL DEFAULT now(),
  paid_at timestamptz,
  UNIQUE (org_id, plan, period_start)
);

-- Shop owner review links, so a link can be revoked before it expires.
CREATE TABLE public.owner_links (
  token_id text PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES public.orgs(org_id) ON DELETE CASCADE,
  card_id text NOT NULL REFERENCES public.cards(card_id) ON DELETE CASCADE,
  issued_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  revoked_by uuid
);
CREATE INDEX owner_links_card_idx ON public.owner_links (card_id, issued_at DESC);

-- Table grants. Supabase grants new tables to anon and authenticated by
-- default, so start from nothing and grant only what the app needs.
REVOKE ALL ON public.stt_jobs, public.invoices, public.owner_links FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON public.stt_jobs TO authenticated;
GRANT SELECT ON public.invoices TO authenticated;
GRANT SELECT, INSERT, UPDATE ON public.owner_links TO authenticated;

ALTER TABLE public.stt_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stt_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invoices FORCE ROW LEVEL SECURITY;
ALTER TABLE public.owner_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.owner_links FORCE ROW LEVEL SECURITY;

CREATE POLICY stt_jobs_org_rw ON public.stt_jobs FOR ALL TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()))
  WITH CHECK (org_id IN (SELECT public.current_org_ids()));

-- Writes go through invoice_* functions, so only reads need a policy.
CREATE POLICY invoices_org_select ON public.invoices FOR SELECT TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()));

CREATE POLICY owner_links_org_rw ON public.owner_links FOR ALL TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()))
  WITH CHECK (org_id IN (SELECT public.current_org_ids()));

-- Worker side of a transcription job. queued -> running; NULL when the job is
-- missing or already taken, so a second claim does nothing.
CREATE FUNCTION public.stt_job_claim(p_job_id text)
RETURNS public.stt_jobs
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_row public.stt_jobs;
BEGIN
  UPDATE public.stt_jobs j
  SET status = 'running', started_at = now()
  WHERE j.job_id = p_job_id AND j.status = 'queued'
  RETURNING * INTO v_row;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  RETURN v_row;
END $$;

CREATE FUNCTION public.stt_job_finish(
  p_job_id text, p_text text, p_duration double precision, p_provider text
) RETURNS void
LANGUAGE sql SECURITY DEFINER
SET search_path = ''
AS $$
  UPDATE public.stt_jobs j
  SET status = 'done', text = p_text, duration_s = p_duration, provider = p_provider,
      error = NULL, finished_at = now()
  WHERE j.job_id = p_job_id AND j.status IN ('queued', 'running')
$$;

CREATE FUNCTION public.stt_job_fail(p_job_id text, p_error text) RETURNS void
LANGUAGE sql SECURITY DEFINER
SET search_path = ''
AS $$
  UPDATE public.stt_jobs j
  SET status = 'failed', error = p_error, finished_at = now()
  WHERE j.job_id = p_job_id AND j.status IN ('queued', 'running')
$$;

-- Operator issues an invoice. order_id is what the payment provider sees.
CREATE FUNCTION public.invoice_issue(
  p_invoice_id text, p_org uuid, p_plan text, p_start date, p_end date, p_amount int,
  p_memo text
) RETURNS public.invoices
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_row public.invoices;
BEGIN
  INSERT INTO public.invoices
    (invoice_id, org_id, plan, period_start, period_end, amount_krw, status, order_id, memo)
  VALUES (p_invoice_id, p_org, p_plan, p_start, p_end, p_amount, 'issued',
          'sl-' || p_invoice_id, p_memo)
  RETURNING * INTO v_row;
  INSERT INTO public.audit_log (org_id, actor_id, action, table_name, row_id, old_json, new_json)
  VALUES (p_org, NULL, 'invoice.issue', 'invoices', p_invoice_id, NULL, to_jsonb(v_row));
  RETURN v_row;
END $$;

-- issued -> paid moves the org to the invoice's plan. Paying an already paid
-- invoice returns it unchanged (webhook and confirm may both arrive); a void
-- invoice cannot be paid. The row is locked so two payments cannot both apply.
CREATE FUNCTION public.invoice_mark_paid(
  p_invoice_id text, p_method text, p_provider text, p_payment_key text,
  p_paid_at timestamptz
) RETURNS public.invoices
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_old public.invoices;
  v_row public.invoices;
  v_plan text;
BEGIN
  SELECT * INTO v_old FROM public.invoices i WHERE i.invoice_id = p_invoice_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'invoice_not_found' USING ERRCODE = 'no_data_found';
  END IF;
  IF v_old.status = 'paid' THEN
    RETURN v_old;
  END IF;
  IF v_old.status <> 'issued' THEN
    RAISE EXCEPTION 'invoice_not_payable';
  END IF;
  UPDATE public.invoices i
  SET status = 'paid', method = p_method, provider = p_provider,
      provider_payment_key = p_payment_key, paid_at = p_paid_at
  WHERE i.invoice_id = p_invoice_id
  RETURNING * INTO v_row;
  SELECT o.plan INTO v_plan FROM public.orgs o WHERE o.org_id = v_row.org_id FOR UPDATE;
  UPDATE public.orgs o SET plan = v_row.plan WHERE o.org_id = v_row.org_id;
  INSERT INTO public.audit_log (org_id, actor_id, action, table_name, row_id, old_json, new_json)
  VALUES (v_row.org_id, NULL, 'invoice.paid', 'invoices', p_invoice_id,
          to_jsonb(v_old), to_jsonb(v_row));
  INSERT INTO public.audit_log (org_id, actor_id, action, table_name, row_id, old_json, new_json)
  VALUES (v_row.org_id, NULL, 'org.plan', 'orgs', v_row.org_id::text,
          jsonb_build_object('plan', v_plan), jsonb_build_object('plan', v_row.plan));
  RETURN v_row;
END $$;

-- issued -> void. A paid invoice cannot be voided; voiding twice does nothing.
CREATE FUNCTION public.invoice_void(p_invoice_id text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_old public.invoices;
  v_row public.invoices;
BEGIN
  SELECT * INTO v_old FROM public.invoices i WHERE i.invoice_id = p_invoice_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'invoice_not_found' USING ERRCODE = 'no_data_found';
  END IF;
  IF v_old.status = 'paid' THEN
    RAISE EXCEPTION 'invoice_paid';
  END IF;
  IF v_old.status = 'void' THEN
    RETURN;
  END IF;
  UPDATE public.invoices i SET status = 'void' WHERE i.invoice_id = p_invoice_id
  RETURNING * INTO v_row;
  INSERT INTO public.audit_log (org_id, actor_id, action, table_name, row_id, old_json, new_json)
  VALUES (v_row.org_id, NULL, 'invoice.void', 'invoices', p_invoice_id,
          to_jsonb(v_old), to_jsonb(v_row));
END $$;

-- Invoice by provider order id (payment webhook, no user). NULL when unknown.
CREATE FUNCTION public.invoice_by_order(p_order_id text) RETURNS public.invoices
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT * FROM public.invoices i WHERE i.order_id = p_order_id
$$;

-- State of a review link for the anonymous link holder: only the state, never
-- the row. 'unknown' covers links issued before this table existed.
CREATE FUNCTION public.owner_link_state(p_token_id text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT coalesce((
    SELECT CASE
      WHEN l.revoked_at IS NOT NULL THEN 'revoked'
      WHEN l.expires_at < now() THEN 'expired'
      ELSE 'active'
    END
    FROM public.owner_links l
    WHERE l.token_id = p_token_id
  ), 'unknown')
$$;

-- Function grants. Supabase grants EXECUTE on new functions to anon and
-- authenticated by default, so revoke from them explicitly as well.
REVOKE ALL ON FUNCTION public.stt_job_claim(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.stt_job_finish(text, text, double precision, text)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.stt_job_fail(text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.invoice_issue(text, uuid, text, date, date, int, text)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.invoice_mark_paid(text, text, text, text, timestamptz)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.invoice_void(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.invoice_by_order(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.owner_link_state(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.stt_job_claim(text) TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.stt_job_finish(text, text, double precision, text)
  TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.stt_job_fail(text, text) TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.invoice_issue(text, uuid, text, date, date, int, text)
  TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.invoice_mark_paid(text, text, text, text, timestamptz)
  TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.invoice_void(text) TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.invoice_by_order(text) TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.owner_link_state(text) TO anon, authenticated;

-- public_card_tags_v2 replaces the four-column function. To roll back, re-run
-- the CREATE FUNCTION and grants from 20261102000400_public_card_tags.sql.
DROP FUNCTION public.public_card_tags(text);
