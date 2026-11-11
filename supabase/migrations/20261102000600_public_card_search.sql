-- Region data of published cards, for the public card search. anon has no grant
-- on shops, so the region filter cannot join shops directly. SECURITY DEFINER
-- returns only non-identifying columns (never name, owner_name or supplier
-- names) and only for cards whose status is 'published'.
CREATE FUNCTION public.published_card_regions()
RETURNS TABLE (
  card_id text, region_code text, region_sido text, region_sigungu text,
  trade text, area_type text
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT c.card_id, s.region_code, s.region_sido, s.region_sigungu, s.trade, s.area_type
  FROM public.cards c
  JOIN public.shops s ON s.shop_id = c.shop_id
  WHERE c.status = 'published'
$$;

REVOKE ALL ON FUNCTION public.published_card_regions() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.published_card_regions() TO anon, authenticated;
