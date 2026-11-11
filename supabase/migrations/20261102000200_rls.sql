-- Row level security. RLS guards only the org boundary; per-role
-- write limits (interviewer cannot edit cards, ...) are enforced by require_role.

GRANT USAGE ON SCHEMA public TO anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON
  public.orgs, public.org_members, public.shops, public.sessions, public.answers,
  public.cards, public.card_versions, public.consents
  TO authenticated;
GRANT SELECT, INSERT ON public.audit_log TO authenticated;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO authenticated;
GRANT SELECT ON public.cards TO anon;

ALTER TABLE public.orgs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.orgs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.org_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.org_members FORCE ROW LEVEL SECURITY;
ALTER TABLE public.shops ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.shops FORCE ROW LEVEL SECURITY;
ALTER TABLE public.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.answers FORCE ROW LEVEL SECURITY;
ALTER TABLE public.cards ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cards FORCE ROW LEVEL SECURITY;
ALTER TABLE public.card_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.card_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.consents FORCE ROW LEVEL SECURITY;
ALTER TABLE public.audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_log FORCE ROW LEVEL SECURITY;

-- orgs
CREATE POLICY orgs_member_select ON public.orgs FOR SELECT TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()));
CREATE POLICY orgs_insert_authenticated ON public.orgs FOR INSERT TO authenticated
  WITH CHECK (created_by = auth.uid());

-- org_members
CREATE POLICY org_members_select_same_org ON public.org_members FOR SELECT TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()));
-- One policy for INSERT/UPDATE/DELETE: FOR ALL (its SELECT part adds nothing beyond
-- org_members_select_same_org, since owners/admins are members).
CREATE POLICY org_members_admin_write ON public.org_members FOR ALL TO authenticated
  USING (public.has_role(org_id, '{owner,admin}'))
  WITH CHECK (
    public.has_role(org_id, '{owner,admin}')
    OR (role = 'owner' AND user_id = auth.uid() AND public.is_first_owner(org_id))
  );

-- org-scoped data tables
CREATE POLICY shops_org_rw ON public.shops FOR ALL TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()))
  WITH CHECK (org_id IN (SELECT public.current_org_ids()));
CREATE POLICY sessions_org_rw ON public.sessions FOR ALL TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()))
  WITH CHECK (org_id IN (SELECT public.current_org_ids()));
CREATE POLICY answers_org_rw ON public.answers FOR ALL TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()))
  WITH CHECK (org_id IN (SELECT public.current_org_ids()));
CREATE POLICY card_versions_org_rw ON public.card_versions FOR ALL TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()))
  WITH CHECK (org_id IN (SELECT public.current_org_ids()));
CREATE POLICY consents_org_rw ON public.consents FOR ALL TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()))
  WITH CHECK (org_id IN (SELECT public.current_org_ids()));

-- cards
CREATE POLICY cards_org_rw ON public.cards FOR ALL TO authenticated
  USING (org_id IN (SELECT public.current_org_ids()))
  WITH CHECK (org_id IN (SELECT public.current_org_ids()));
CREATE POLICY cards_public_published_select ON public.cards FOR SELECT TO anon, authenticated
  USING (status = 'published');

-- audit_log
CREATE POLICY audit_log_org_select ON public.audit_log FOR SELECT TO authenticated
  USING (public.has_role(org_id, '{owner,admin}'));
CREATE POLICY audit_log_insert_same_org ON public.audit_log FOR INSERT TO authenticated
  WITH CHECK (org_id IN (SELECT public.current_org_ids()));
