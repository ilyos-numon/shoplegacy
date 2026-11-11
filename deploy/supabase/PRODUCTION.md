# Supabase 운영 데이터베이스 절차

운영자가 위에서부터 차례로 따라 합니다. 실제 값(프로젝트 ref·비밀번호·키)은 이 파일에 적지 않습니다. 화면 위치·메뉴 이름은 Supabase 판마다 다를 수 있으니 막히면 공식 문서(https://supabase.com/docs)를 봅니다. 3·4단계는 `deploy/gcp/apply-env.sh` 가 자동으로 하므로, 여기서는 확인과 수동 복구 방법만 적습니다.

## 채울 값

| 항목 | 값 (적지 않음 — 비밀번호 관리자에 보관) |
|---|---|
| 프로젝트 ref (`<project-ref>`) | |
| 데이터베이스 비밀번호 (postgres) | |
| 세션 풀러 주소 (→ `SUPABASE_POOLER_URL`) | |
| Project URL (→ `SUPABASE_URL`) | |
| 공개 키 `sb_publishable_…` 또는 anon 키 (→ `SUPABASE_ANON_KEY`) | |
| 공개 도메인 (`<도메인>` = Cloud Run 서비스 주소 호스트) | |

`shoplegacy_app` 비밀번호와 `SHOPLEGACY_DATABASE_URL` 은 `apply-env.sh` 가 만들어 Secret Manager 에만 넣으므로 따로 적지 않습니다. 비밀 키(`sb_secret_…`, 옛 service_role)는 이 앱이 쓰지 않습니다.

## 1. 프로젝트 만들기

- [ ] Supabase 대시보드에서 새 프로젝트를 만듭니다. 리전은 **Northeast Asia (Seoul) `ap-northeast-2`** (Cloud Run `asia-northeast3` 과 같은 도시).
- [ ] 이미 다른 리전(도쿄·싱가포르 등)에 만든 프로젝트를 쓴다면 그대로 써도 됩니다. 요청마다 DB 왕복 지연이 늘어 화면이 조금 느려질 뿐 동작은 같습니다. 리전은 나중에 바꿀 수 없으니 새로 만들 때만 서울을 고릅니다.
- [ ] 데이터베이스 비밀번호는 생성기로 길게 만들고 비밀번호 관리자에 보관합니다.
- [ ] 백업·PITR 을 쓰려면 Pro 이상 요금제가 필요합니다 (7단계).
- [ ] Project Settings → API Keys 에서 Project URL·공개 키를 위 표대로 옮겨 적습니다. 새 형식 키(`sb_publishable_…`)는 JWT 가 아니라 그대로 씁니다.

## 2. 저장소와 연결 (선택)

`apply-env.sh` 는 `--db-url` 로 직접 붙으므로 link 가 없어도 됩니다. 수동으로 확인할 때만:

```sh
supabase login
supabase link --project-ref <project-ref>
```

## 3. 마이그레이션 적용

`supabase/migrations/` 의 모든 파일을 순서대로 적용합니다 (이번 판 기준 9개: 스키마·`shoplegacy_app` 역할, RLS, 구성원 이메일, 공개 카드, 판매 보강, 카드 검색, 작업 대기열(procrastinate), 나머지, 후속 보완). `apply-env.sh` 1/6 단계가 `supabase db push --db-url <SUPABASE_POOLER_URL> --yes` 로 합니다.

- [ ] 첫 파일이 `shoplegacy_app` 역할을 만들고 `GRANT anon, authenticated TO shoplegacy_app` 을 합니다. 접속 계정에 그 권한이 없으면 `apply-env.sh` 가 0/6 단계에서 멈춥니다. 그때는 SQL Editor 에서 첫 파일의 역할 부분을 먼저 실행하고 다시 돌립니다.
- [ ] `supabase migration list --db-url <SUPABASE_POOLER_URL>` 에서 Local 과 Remote 열이 모두 같은지 확인합니다.
- [ ] 작업 대기열 표가 생겼는지 SQL Editor 에서 확인합니다:

```sql
select count(*) from procrastinate_jobs;
```

`supabase/seed.sql` 은 로컬 전용입니다. 운영에는 적용하지 않습니다 (`db push` 는 seed 를 넣지 않습니다).

### 통합 DB(`shoplegacy` 스키마)를 쓸 때

여러 앱이 한 Supabase 프로젝트를 나눠 쓰는 통합 DB 에서는 shoplegacy 표가 `shoplegacy` 스키마에 있고, 앱은 `shoplegacy_app` 역할의 search_path 로 그 스키마를 찾습니다 (코드의 SQL 에는 스키마 한정자가 없습니다).

- [ ] `apply-env.sh` 를 `--skip-migrate` 로 돌립니다. 권한 확인(0/6)과 `supabase db push`(1/6)만 건너뛰고, `shoplegacy_app` 비밀번호·비밀 6개·Cloud Run 반영·`/readyz` 확인은 그대로 합니다.
- [ ] 통합 프로젝트에서는 `supabase db push` 를 bid-radar 폴더에서만 합니다. 이 저장소에서 통합 프로젝트로 `db push` 하지 않습니다.

## 4. 앱 접속 계정 (`shoplegacy_app`)

마이그레이션은 이 역할을 고정된 기본 비밀번호로 만듭니다. `apply-env.sh` 2/6 단계가 바로 새 무작위 비밀번호로 바꾸고 세션 풀러 주소로 `SHOPLEGACY_DATABASE_URL` 을 만들어 Secret Manager 에 넣습니다. 수동으로 할 때는:

```sql
alter role shoplegacy_app login password '<shoplegacy_app 비밀번호>';
```

```
postgresql://shoplegacy_app.<project-ref>:<shoplegacy_app 비밀번호>@<세션 풀러 호스트>:5432/postgres
```

- **직접 연결(`db.<project-ref>.supabase.co:5432`)은 쓰지 않습니다.** 이 주소는 IPv6 전용이라 Cloud Run(IPv4 외부 연결)에서 붙지 않습니다. 반드시 Connect → **Session pooler (포트 5432)** 주소를 씁니다.
- **트랜잭션 풀러(6543)는 쓰지 않습니다.** 앱은 트랜잭션마다 `SET LOCAL ROLE anon|authenticated` 로 RLS 역할을 바꾸고, 작업 대기열은 `LISTEN` 으로 세션을 붙잡습니다.
- [ ] 이 주소는 Secret Manager 비밀 `SHOPLEGACY_DATABASE_URL` 로만 넣습니다.
- [ ] 연결 확인: `psql "<SHOPLEGACY_DATABASE_URL>" -Atc "select current_user"` → `shoplegacy_app`.
- [ ] 기본 비밀번호가 남아 있지 않은지: `apply-env.sh` 를 `--skip-db` 없이 한 번 이상 돌렸으면 바뀌어 있습니다.

## 5. JWT 서명 키 (ECC)

앱은 로그인 토큰을 `SHOPLEGACY_JWKS_URL`(`https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json`)의 공개키로 검증합니다 (ES256).

- [ ] Project Settings → JWT Keys 에서 현재 서명 키가 **ECC (P-256)** 인지 확인합니다. 레거시 HS256 비밀이 현재 키면 ECC 키를 만들어 현재 키로 올립니다.
- [ ] 확인: `curl -s https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json` 결과에 `"alg":"ES256"` 키가 있습니다.
- 레거시 HS256 을 그대로 써야 한다면 `.env.production` 의 `SHOPLEGACY_JWT_SECRET` 에 Supabase JWT secret 을 넣고 `apply-env.sh --skip-db` 로 반영합니다.
- 증상: 로그인 직후 다시 로그인 화면으로 돌아오면 JWKS 검증이 실패한 것입니다. 위 두 항목을 확인합니다.

## 6. Auth 설정

Authentication → URL Configuration / Sign In:

- [ ] Site URL: `https://<도메인>`
- [ ] Redirect URLs: `https://<도메인>/login` (로컬 `http://localhost:*` 항목은 넣지 않습니다)
- [ ] 가입 확인 메일(Confirm email): 로컬 `supabase/config.toml` 과 같이 끄는 것을 권장합니다. 커스텀 SMTP 없이 Supabase 기본 메일을 쓰면 발송 한도가 낮아 확인 메일이 늦거나 오지 않습니다.
- [ ] 로그인 뒤 조직이 없는 사용자는 `/onboarding` 에서 조직을 만듭니다 (별도 설정 없음).

## 7. 백업·PITR·복구 리허설

- [ ] Database → Backups: Pro 요금제의 **일일 백업(7일 보존)** 이 보이는지 확인합니다. 무료 요금제라면 `scripts/backup.sh` 를 관리자 주소(`SHOPLEGACY_BACKUP_DSN`, 세션 풀러 postgres 계정)로 돌려 `pg_dump` 파일을 저장소 밖에 보관합니다.
- [ ] PITR(Point in Time Recovery) 애드온을 켭니다. WAL 보존 **7일** 을 권장합니다 (비용은 애드온 요금표 확인).
- [ ] 복구 리허설 (출시 전 1회, 이후 분기마다):
  1. 별도 스테이징 프로젝트를 하나 만듭니다 (운영 프로젝트에 직접 복원하지 않습니다).
  2. 운영 프로젝트의 백업 또는 PITR 시점을 스테이징으로 복원합니다 (대시보드의 "Restore to new project" 류 기능, 없으면 `supabase db dump` 또는 `scripts/backup.sh` 덤프 → `pg_restore` 로 스테이징에 넣기).
  3. 스테이징에서 주요 표 행 수를 운영과 비교합니다.
  4. 걸린 시간과 결과를 운영 기록에 남기고 스테이징 프로젝트를 지웁니다.
- [ ] `SHOPLEGACY_PII_KEYS`(개인정보 암호화 키)를 잃으면 백업을 복원해도 암호화된 값을 읽지 못합니다. 키를 비밀번호 관리자에도 보관합니다.

## 8. 롤백

- 앱 롤백: Cloud Run 의 이전 리비전으로 트래픽 100% 를 돌립니다.

```sh
gcloud run revisions list --service=shoplegacy --region=asia-northeast3 --project=<PROJECT_ID>
gcloud run services update-traffic shoplegacy --to-revisions=<이전 리비전>=100 --region=asia-northeast3 --project=<PROJECT_ID>
```

- 마이그레이션은 되돌리지 않습니다 (새 마이그레이션은 표·열을 더하는 방향으로 씁니다). 데이터가 손상됐다면 7단계의 PITR 또는 백업으로 손상 직전 시점을 복원합니다.
- 모니터링: Reports → Database 에서 연결 수를 봅니다. 앱 인스턴스는 최대 1개라 연결 수가 풀러 한도를 넘지 않아야 정상입니다. 계속 늘면 Cloud Run 로그와 `/readyz` 응답을 확인합니다.
