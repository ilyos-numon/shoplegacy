-- Public card tags. anon has no grant on shops, but a published card shows its
-- shop's tags. SECURITY DEFINER returns only the tag columns (never name,
-- owner_name or supplier names) and only while the shop has a published card.
CREATE FUNCTION public.public_card_tags(p_shop_id text)
RETURNS TABLE (trade text, area_type text, years int, closed_year int)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT s.trade, s.area_type, s.years, s.closed_year
  FROM public.shops s
  WHERE s.shop_id = p_shop_id
    AND EXISTS (
      SELECT 1 FROM public.cards c
      WHERE c.shop_id = s.shop_id AND c.status = 'published'
    )
$$;

REVOKE ALL ON FUNCTION public.public_card_tags(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.public_card_tags(text) TO anon, authenticated;
