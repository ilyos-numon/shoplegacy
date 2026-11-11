-- Refunds and tax-invoice records on invoices, and stored billing keys for
-- monthly automatic card payments.

-- Invoices: a paid invoice can be refunded in full; the operator records the
-- tax invoice number issued outside the app.
ALTER TABLE public.invoices DROP CONSTRAINT invoices_status_check;
ALTER TABLE public.invoices ADD CONSTRAINT invoices_status_check
  CHECK (status IN ('issued', 'paid', 'void', 'refunded'));
ALTER TABLE public.invoices
  ADD COLUMN refunded_at timestamptz,
  ADD COLUMN refund_reason text,
  ADD COLUMN tax_invoice_ref text,
  ADD COLUMN tax_invoice_issued_at timestamptz;

-- Billing keys, one per org. billing_key is stored encrypted by the app.
CREATE TABLE public.billing_keys (
  org_id uuid PRIMARY KEY REFERENCES public.orgs(org_id) ON DELETE CASCADE,
  provider text NOT NULL,
  billing_key text NOT NULL,
  customer_key text NOT NULL,
  card_label text,
  plan text NOT NULL CHECK (plan IN ('pilot', 'org')),
  charge_day int NOT NULL DEFAULT 1 CHECK (charge_day BETWEEN 1 AND 28),
  next_charge_on date NOT NULL,
  status text NOT NULL CHECK (status IN ('active', 'stopped')),
  failures int NOT NULL DEFAULT 0 CHECK (failures >= 0),
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  stopped_at timestamptz
);

ALTER TABLE public.billing_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.billing_keys FORCE ROW LEVEL SECURITY;
-- Only the app login role reads billing keys; members see them through the API.
REVOKE ALL ON public.billing_keys FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.billing_keys TO shoplegacy_app;
CREATE POLICY billing_keys_app_all ON public.billing_keys FOR ALL TO shoplegacy_app
  USING (true) WITH CHECK (true);

-- paid -> refunded (full refund only).
CREATE FUNCTION public.invoice_refund(
  p_invoice_id text, p_reason text, p_refunded_at timestamptz
) RETURNS public.invoices
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
  IF v_old.status <> 'paid' THEN
    RAISE EXCEPTION 'invoice_not_refundable';
  END IF;
  UPDATE public.invoices i
  SET status = 'refunded', refunded_at = p_refunded_at, refund_reason = p_reason
  WHERE i.invoice_id = p_invoice_id
  RETURNING * INTO v_row;
  INSERT INTO public.audit_log (org_id, actor_id, action, table_name, row_id, old_json, new_json)
  VALUES (v_row.org_id, NULL, 'invoice.refund', 'invoices', p_invoice_id,
          to_jsonb(v_old), to_jsonb(v_row));
  RETURN v_row;
END $$;

-- Records the tax invoice number for a paid or refunded invoice.
CREATE FUNCTION public.invoice_mark_tax(
  p_invoice_id text, p_ref text, p_issued_at timestamptz
) RETURNS public.invoices
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
  IF v_old.status NOT IN ('paid', 'refunded') THEN
    RAISE EXCEPTION 'invoice_not_payable';
  END IF;
  UPDATE public.invoices i
  SET tax_invoice_ref = p_ref, tax_invoice_issued_at = p_issued_at
  WHERE i.invoice_id = p_invoice_id
  RETURNING * INTO v_row;
  INSERT INTO public.audit_log (org_id, actor_id, action, table_name, row_id, old_json, new_json)
  VALUES (v_row.org_id, NULL, 'invoice.tax_invoice', 'invoices', p_invoice_id,
          jsonb_build_object('tax_invoice_ref', v_old.tax_invoice_ref,
                             'tax_invoice_issued_at', v_old.tax_invoice_issued_at),
          jsonb_build_object('tax_invoice_ref', v_row.tax_invoice_ref,
                             'tax_invoice_issued_at', v_row.tax_invoice_issued_at));
  RETURN v_row;
END $$;

-- Stores (or replaces) an org's billing key and makes it active again.
-- The audit row never carries the key itself.
CREATE FUNCTION public.billing_key_set(
  p_org uuid, p_provider text, p_billing_key text, p_customer_key text, p_card_label text,
  p_plan text, p_charge_day int, p_next date, p_created_by uuid
) RETURNS public.billing_keys
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_row public.billing_keys;
BEGIN
  INSERT INTO public.billing_keys AS b
    (org_id, provider, billing_key, customer_key, card_label, plan, charge_day,
     next_charge_on, status, failures, created_by)
  VALUES (p_org, p_provider, p_billing_key, p_customer_key, p_card_label, p_plan, p_charge_day,
          p_next, 'active', 0, p_created_by)
  ON CONFLICT (org_id) DO UPDATE
  SET provider = EXCLUDED.provider, billing_key = EXCLUDED.billing_key,
      customer_key = EXCLUDED.customer_key, card_label = EXCLUDED.card_label,
      plan = EXCLUDED.plan, charge_day = EXCLUDED.charge_day,
      next_charge_on = EXCLUDED.next_charge_on, status = 'active', failures = 0,
      created_by = EXCLUDED.created_by, created_at = now(), stopped_at = NULL
  RETURNING * INTO v_row;
  INSERT INTO public.audit_log (org_id, actor_id, action, table_name, row_id, old_json, new_json)
  VALUES (p_org, p_created_by, 'billing_key.set', 'billing_keys', p_org::text, NULL,
          jsonb_build_object('card_label', v_row.card_label, 'plan', v_row.plan,
                             'next_charge_on', v_row.next_charge_on));
  RETURN v_row;
END $$;

-- active -> stopped. Stopping a stopped key does nothing.
CREATE FUNCTION public.billing_key_stop(p_org uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_status text;
BEGIN
  SELECT b.status INTO v_status FROM public.billing_keys b WHERE b.org_id = p_org FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'billing_key_not_found';
  END IF;
  IF v_status <> 'active' THEN
    RETURN;
  END IF;
  UPDATE public.billing_keys b SET status = 'stopped', stopped_at = now()
  WHERE b.org_id = p_org;
  INSERT INTO public.audit_log (org_id, actor_id, action, table_name, row_id, old_json, new_json)
  VALUES (p_org, NULL, 'billing_key.stop', 'billing_keys', p_org::text,
          jsonb_build_object('status', 'active'), jsonb_build_object('status', 'stopped'));
END $$;

-- Active keys whose next charge date has come.
CREATE FUNCTION public.billing_keys_due(p_today date) RETURNS SETOF public.billing_keys
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT * FROM public.billing_keys b
  WHERE b.status = 'active' AND b.next_charge_on <= p_today
  ORDER BY b.org_id
$$;

-- After a successful charge: the next date, and the failure count starts over.
CREATE FUNCTION public.billing_key_advance(p_org uuid, p_next date) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
  UPDATE public.billing_keys b SET next_charge_on = p_next, failures = 0
  WHERE b.org_id = p_org;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'billing_key_not_found';
  END IF;
END $$;

-- After a declined charge. The third failure in a row stops automatic payment.
CREATE FUNCTION public.billing_key_fail(p_org uuid) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_old public.billing_keys;
  v_failures int;
BEGIN
  SELECT * INTO v_old FROM public.billing_keys b WHERE b.org_id = p_org FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'billing_key_not_found';
  END IF;
  v_failures := v_old.failures + 1;
  IF v_failures >= 3 AND v_old.status = 'active' THEN
    UPDATE public.billing_keys b
    SET failures = v_failures, status = 'stopped', stopped_at = now()
    WHERE b.org_id = p_org;
    INSERT INTO public.audit_log (org_id, actor_id, action, table_name, row_id, old_json, new_json)
    VALUES (p_org, NULL, 'billing_key.stop', 'billing_keys', p_org::text,
            jsonb_build_object('status', 'active', 'failures', v_old.failures),
            jsonb_build_object('status', 'stopped', 'failures', v_failures));
  ELSE
    UPDATE public.billing_keys b SET failures = v_failures WHERE b.org_id = p_org;
  END IF;
  RETURN v_failures;
END $$;

REVOKE ALL ON FUNCTION public.invoice_refund(text, text, timestamptz)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.invoice_mark_tax(text, text, timestamptz)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.billing_key_set(uuid, text, text, text, text, text, int, date, uuid)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.billing_key_stop(uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.billing_keys_due(date) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.billing_key_advance(uuid, date) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.billing_key_fail(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.invoice_refund(text, text, timestamptz) TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.invoice_mark_tax(text, text, timestamptz) TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.billing_key_set(uuid, text, text, text, text, text, int, date, uuid)
  TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.billing_key_stop(uuid) TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.billing_keys_due(date) TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.billing_key_advance(uuid, date) TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.billing_key_fail(uuid) TO shoplegacy_app;
