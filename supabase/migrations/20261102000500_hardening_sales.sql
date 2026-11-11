-- Plans and retention, shop regions, card feedback, owner reviews, LLM usage,
-- last-owner protection, retention purge and shop-delete cascades.

-- orgs: plan and retention period (days)
ALTER TABLE public.orgs
  ADD COLUMN plan text NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'pilot', 'org')),
  ADD COLUMN retention_days int NOT NULL DEFAULT 1095 CHECK (retention_days >= 30);

-- shops: region (si/do, si/gun/gu) and retention deadline. The app fills
-- retention_until from created_at + orgs.retention_days.
ALTER TABLE public.shops
  ADD COLUMN region_code text,
  ADD COLUMN region_sido text,
  ADD COLUMN region_sigungu text,
  ADD COLUMN retention_until timestamptz;
CREATE INDEX shops_region_code_idx ON public.shops (region_code);
CREATE INDEX shops_retention_until_idx ON public.shops (retention_until);

-- cards: which LLM wrote the draft, and the untouched draft so reviewer-written
-- sentences can be told apart.
ALTER TABLE public.cards
  ADD COLUMN llm_provider text NOT NULL DEFAULT 'fake',
  ADD COLUMN draft_sections_json text NOT NULL DEFAULT '{}';

-- Deleting a shop removes everything recorded about it. audit_log keeps its rows.
ALTER TABLE public.sessions DROP CONSTRAINT sessions_shop_id_fkey,
  ADD CONSTRAINT sessions_shop_id_fkey FOREIGN KEY (shop_id)
    REFERENCES public.shops ON DELETE CASCADE;
ALTER TABLE public.answers DROP CONSTRAINT answers_session_id_fkey,
  ADD CONSTRAINT answers_session_id_fkey FOREIGN KEY (session_id)
    REFERENCES public.sessions ON DELETE CASCADE;
ALTER TABLE public.cards DROP CONSTRAINT cards_shop_id_fkey,
  ADD CONSTRAINT cards_shop_id_fkey FOREIGN KEY (shop_id)
    REFERENCES public.shops ON DELETE CASCADE;
ALTER TABLE public.card_versions DROP CONSTRAINT card_versions_card_id_fkey,
  ADD CONSTRAINT card_versions_card_id_fkey FOREIGN KEY (card_id)
    REFERENCES public.cards ON DELETE CASCADE;
ALTER TABLE public.consents DROP CONSTRAINT consents_shop_id_fkey,
  ADD CONSTRAINT consents_shop_id_fkey FOREIGN KEY (shop_id)
    REFERENCES public.shops ON DELETE CASCADE;

-- Anonymous feedback on published cards: one vote per fingerprint per card per day.
CREATE TABLE public.card_feedback (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  card_id text NOT NULL REFERENCES public.cards ON DELETE CASCADE,
  vote text NOT NULL CHECK (vote IN ('up', 'down')),
  fingerprint text NOT NULL,
  day date NOT NULL DEFAULT current_date,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (card_id, fingerprint, day)
);

-- Shop owner decisions, one per (card, version, link token).
CREATE TABLE public.owner_reviews (
  review_id text PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES public.orgs,
  shop_id text NOT NULL REFERENCES public.shops ON DELETE CASCADE,
  card_id text NOT NULL REFERENCES public.cards ON DELETE CASCADE,
  card_version int NOT NULL,
  token_id text NOT NULL,
  decision text NOT NULL CHECK (decision IN ('approve', 'request_changes')),
  comment text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (card_id, card_version, token_id)
);
CREATE INDEX owner_reviews_org_id_idx ON public.owner_reviews (org_id);

CREATE TABLE public.llm_usage (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES public.orgs ON DELETE CASCADE,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  kind text NOT NULL CHECK (kind IN ('follow_up', 'draft', 'verify')),
  provider text NOT NULL,
  tokens_in int NOT NULL DEFAULT 0,
  tokens_out int NOT NULL DEFAULT 0,
  fallback boolean NOT NULL DEFAULT false
);
CREATE INDEX llm_usage_org_month_idx ON public.llm_usage (org_id, occurred_at);

-- Last-owner protection. Two owners demoting each other at once would each see
-- the other still as owner under READ COMMITTED, so the org row is locked first:
-- the second transaction waits, then re-checks against the committed state.
-- SECURITY DEFINER because FOR UPDATE under the caller's RLS would need the
-- orgs UPDATE policy (owners only), and an admin would silently skip the check.
CREATE FUNCTION public.guard_last_owner() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
  PERFORM 1 FROM public.orgs WHERE org_id = OLD.org_id FOR UPDATE;
  IF NOT FOUND THEN
    -- The org itself is being deleted (cascade); nothing left to protect.
    RETURN coalesce(NEW, OLD);
  END IF;
  IF OLD.role = 'owner' AND (TG_OP = 'DELETE' OR NEW.role <> 'owner')
     AND NOT EXISTS (
       SELECT 1 FROM public.org_members m
       WHERE m.org_id = OLD.org_id AND m.role = 'owner' AND m.user_id <> OLD.user_id
     )
  THEN
    RAISE EXCEPTION 'last_owner' USING ERRCODE = 'check_violation';
  END IF;
  RETURN coalesce(NEW, OLD);
END $$;

CREATE TRIGGER org_members_guard_last_owner BEFORE UPDATE OR DELETE ON public.org_members
  FOR EACH ROW EXECUTE FUNCTION public.guard_last_owner();

-- Vote counts for a published card; (0, 0) for anything else.
CREATE FUNCTION public.card_feedback_counts(p_card_id text)
RETURNS TABLE (up int, down int)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT
    count(*) FILTER (WHERE f.vote = 'up')::int,
    count(*) FILTER (WHERE f.vote = 'down')::int
  FROM public.card_feedback f
  WHERE f.card_id = p_card_id
    AND EXISTS (
      SELECT 1 FROM public.cards c WHERE c.card_id = p_card_id AND c.status = 'published'
    )
$$;

-- Card as the shop owner sees it through a review link, plus the owner's latest
-- decision on the current version. Bypasses RLS, so the app must verify the
-- signed link token before calling it. Never returns shop name or owner name.
CREATE FUNCTION public.owner_card_view(p_card_id text)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT jsonb_build_object(
    'card_id', c.card_id,
    'shop_id', c.shop_id,
    'org_id', c.org_id,
    'trade', s.trade,
    'status', c.status,
    'version', c.version,
    'sections', c.sections_json::jsonb,
    'decision', r.decision,
    'comment', r.comment,
    'decided_at', r.created_at
  )
  FROM public.cards c
  JOIN public.shops s ON s.shop_id = c.shop_id
  LEFT JOIN LATERAL (
    SELECT o.decision, o.comment, o.created_at
    FROM public.owner_reviews o
    WHERE o.card_id = c.card_id AND o.card_version = c.version
    ORDER BY o.created_at DESC, o.review_id DESC
    LIMIT 1
  ) r ON true
  WHERE c.card_id = p_card_id
$$;

-- Records a shop owner's decision (the caller is anonymous; the app has
-- verified the link token). A repeat for the same (card, version, token)
-- raises unique_violation.
CREATE FUNCTION public.owner_review_insert(
  p_review_id text, p_card_id text, p_card_version int, p_token_id text,
  p_decision text, p_comment text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_org uuid;
  v_shop text;
  v_row public.owner_reviews;
BEGIN
  SELECT c.org_id, c.shop_id INTO v_org, v_shop FROM public.cards c WHERE c.card_id = p_card_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'card_not_found' USING ERRCODE = 'no_data_found';
  END IF;
  INSERT INTO public.owner_reviews
    (review_id, org_id, shop_id, card_id, card_version, token_id, decision, comment)
  VALUES (p_review_id, v_org, v_shop, p_card_id, p_card_version, p_token_id, p_decision, p_comment)
  RETURNING * INTO v_row;
  INSERT INTO public.audit_log (org_id, actor_id, action, table_name, row_id, old_json, new_json)
  VALUES (v_org, NULL, 'owner_review.decide', 'owner_reviews', p_review_id, NULL, to_jsonb(v_row));
END $$;

-- Monthly LLM usage of one org; only members of that org get non-zero numbers.
CREATE FUNCTION public.llm_usage_month(p_org uuid, p_month date)
RETURNS TABLE (calls bigint, tokens_in bigint, tokens_out bigint, fallbacks bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT
    count(*)::bigint,
    coalesce(sum(u.tokens_in), 0)::bigint,
    coalesce(sum(u.tokens_out), 0)::bigint,
    count(*) FILTER (WHERE u.fallback)::bigint
  FROM public.llm_usage u
  WHERE u.org_id = p_org
    AND p_org IN (SELECT public.current_org_ids())
    AND u.occurred_at >= date_trunc('month', p_month::timestamp)
    AND u.occurred_at < date_trunc('month', p_month::timestamp) + interval '1 month'
$$;

-- Deletes shops past their retention deadline (children go by cascade) and
-- returns their ids. Runs without a user, so it is granted to the app login
-- role only, never to anon or authenticated.
CREATE FUNCTION public.purge_expired_shops(p_now timestamptz)
RETURNS SETOF text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_shop record;
BEGIN
  FOR v_shop IN
    DELETE FROM public.shops s
    WHERE s.retention_until IS NOT NULL AND s.retention_until < p_now
    RETURNING s.shop_id, s.org_id, s.retention_until
  LOOP
    INSERT INTO public.audit_log (org_id, actor_id, action, table_name, row_id, old_json, new_json)
    VALUES (v_shop.org_id, NULL, 'shop.purge', 'shops', v_shop.shop_id,
            jsonb_build_object('retention_until', v_shop.retention_until), NULL);
    RETURN NEXT v_shop.shop_id;
  END LOOP;
END $$;

-- Public tags plus region_sigungu, same visibility rule as public_card_tags.
-- A new name instead of DROP + CREATE: existing callers and tests depend on the
-- four-column shape of public_card_tags, which stays as it is.
CREATE FUNCTION public.public_card_tags_v2(p_shop_id text)
RETURNS TABLE (trade text, area_type text, years int, closed_year int, region_sigungu text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT s.trade, s.area_type, s.years, s.closed_year, s.region_sigungu
  FROM public.shops s
  WHERE s.shop_id = p_shop_id
    AND EXISTS (
      SELECT 1 FROM public.cards c
      WHERE c.shop_id = s.shop_id AND c.status = 'published'
    )
$$;

-- Function grants. Supabase grants EXECUTE on new functions to anon and
-- authenticated by default, so revoke from them explicitly as well.
REVOKE ALL ON FUNCTION public.guard_last_owner() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.card_feedback_counts(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.owner_card_view(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.owner_review_insert(text, text, int, text, text, text)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.llm_usage_month(uuid, date) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.purge_expired_shops(timestamptz) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.public_card_tags_v2(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.card_feedback_counts(text) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.owner_card_view(text) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.owner_review_insert(text, text, int, text, text, text)
  TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.llm_usage_month(uuid, date) TO authenticated;
GRANT EXECUTE ON FUNCTION public.purge_expired_shops(timestamptz) TO shoplegacy_app;
GRANT EXECUTE ON FUNCTION public.public_card_tags_v2(text) TO anon, authenticated;

-- Table grants. Supabase also grants table privileges to anon and authenticated
-- by default; RLS below is what limits them.
GRANT INSERT ON public.card_feedback TO anon, authenticated;
GRANT SELECT, INSERT ON public.owner_reviews, public.llm_usage TO authenticated;
GRANT UPDATE ON public.orgs TO authenticated;
GRANT USAGE ON SEQUENCE public.card_feedback_id_seq TO anon, authenticated;
GRANT USAGE ON SEQUENCE public.llm_usage_id_seq TO authenticated;

ALTER TABLE public.card_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.card_feedback FORCE ROW LEVEL SECURITY;
ALTER TABLE public.owner_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.owner_reviews FORCE ROW LEVEL SECURITY;
ALTER TABLE public.llm_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.llm_usage FORCE ROW LEVEL SECURITY;

-- No SELECT policy on card_feedback: counts come from card_feedback_counts.
-- The column is qualified with the table name; a bare card_id inside the
-- subquery would bind to c.card_id and accept any published card.
CREATE POLICY card_feedback_anon_insert ON public.card_feedback FOR INSERT TO anon, authenticated
  WITH CHECK (EXISTS (
    SELECT 1 FROM public.cards c
    WHERE c.card_id = card_feedback.card_id AND c.status = 'published'
  ));

-- Inserts go through owner_review_insert, so only reads need a policy.
CREATE POLICY owner_reviews_org_select ON public.owner_reviews FOR SELECT TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()));

CREATE POLICY llm_usage_org_rw ON public.llm_usage FOR ALL TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()))
  WITH CHECK (org_id IN (SELECT public.current_org_ids()));

-- Only owners change plan and retention.
CREATE POLICY orgs_owner_update ON public.orgs FOR UPDATE TO authenticated
  USING (public.has_role(org_id, '{owner}'))
  WITH CHECK (public.has_role(org_id, '{owner}'));
