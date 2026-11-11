-- Member list with emails. The app role cannot read auth.users, so this
-- SECURITY DEFINER helper resolves emails, but only for members of p_org.
CREATE FUNCTION public.org_member_emails(p_org uuid)
RETURNS TABLE (user_id uuid, email text, role text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT m.user_id, u.email, m.role
  FROM public.org_members m
  LEFT JOIN auth.users u ON u.id = m.user_id
  WHERE m.org_id = p_org
    AND p_org IN (SELECT public.current_org_ids())
  ORDER BY m.created_at ASC, m.user_id ASC
$$;

REVOKE ALL ON FUNCTION public.org_member_emails(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.org_member_emails(uuid) TO authenticated;
