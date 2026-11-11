# Bundled data

## regions_kr.csv

Korean sido (시·도) and sigungu (시·군·구) used for shop regions and region search.

- Columns: `code,sido,sigungu` (UTF-8, header row). `code` is the 10-digit legal-dong code
  (법정동코드). `sigungu` is empty for a sido row. Sorted by code.
- Rows: 16 sido + 269 sigungu. A city with general districts appears both as the city
  (e.g. `수원시`) and as each district (e.g. `수원시장안구`), as in the source.
  세종특별자치시 has the sido row `3600000000` and one sigungu row `3611000000` (`세종시`).
- Source: 국토교통부, 「국토교통부_전국 법정동_20260630」, 공공데이터포털 dataset 15063424,
  https://www.data.go.kr/data/15063424/fileData.do
  (compiled by the publisher from 행정표준코드관리시스템, code.go.kr).
- Source reference date: 2026-06-30 (file updated 2026-07-29). It already reflects the
  launch of 전남광주통합특별시, so there are 16 sido.
- License: 공공데이터포털 "이용허락범위 제한 없음" (no restriction on use, commercial use allowed).
- Attribution: "출처: 국토교통부, 전국 법정동 (공공데이터포털, 2026-06-30 기준)"
- Converted on: 2026-11-02

### Rebuild

Download the CSV from the dataset page, then:

```sh
uv run python scripts/fetch_regions.py <downloaded.csv> src/shoplegacy/data/regions_kr.csv
```

The script reads the source as UTF-8 (BOM allowed) or CP949, keeps only codes ending in
`00000000` (sido) or `00000` with a non-empty 시군구명 (sigungu), and does not download
anything itself.
