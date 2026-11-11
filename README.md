# 가게유산 (ShopLegacy)

## 철학
20년 장사한 사장님의 머릿속은 경영서 한 권이다. 문을 닫는 날 그 책도 같이 버려진다.

## 문제
- 폐업하는 소상공인의 운영 노하우(거래처, 단골 응대, 계절 매출 패턴, 실패담)는 기록되지 않는다
- 같은 자리에 들어오는 다음 가게가 같은 실수를 한다
- 창업 준비자는 책과 유튜브는 보지만 "그 동네 그 업종" 이야기는 못 듣는다

## 방향
- 폐업 예정 사장님과 AI가 3~5회 대화 (전화·음성 가능)
- 질문은 입지·거래처·메뉴·가격·손님·실패 여섯 축
- 결과는 익명 처리된 "가게 유산 카드" — 업종·지역·운영 기간 태그
- 창업 준비자는 업종·지역으로 검색해 선배 사장님 경험을 읽는다
- 사장님에게는 소정의 사례비 또는 다음 창업 시 혜택

## 하지 않는 것
- 상권 분석 데이터 판매
- 권리금·부동산 중개

## 기술 방향
- 핸드오프와 같은 인터뷰 → 구조화 엔진, 질문 트리만 소상공인용
- 음성 STT 필수 (사장님은 타이핑 안 함)
- 개인·상호 식별 정보 자동 마스킹

## 주요 기능
- **근거 있는 카드**: 카드 초안의 문장마다 어느 답변에서 나왔는지 연결하고, 근거 없는 문장(환각)은 표시한다. 검수 화면에서 문장 옆 배지를 누르면 원문 답변으로 이동해 대조할 수 있다.
- **검수 흐름**: 카드는 초안 → 보류·반려 → 공개 → 비공개 상태를 오가고, 고칠 때마다 버전이 남는다. 공개 전 품질 게이트(근거 비율, 축 채움, 동의, 가림 처리)를 통과해야 공개된다.
- **동의 관리**: 녹음·보관·AI 처리·공개 동의를 따로 받고, 철회도 기록으로 남긴다. 공개 동의가 없으면 카드를 공개할 수 없다.
- **조직·역할**: 소유자·관리자·검수자·인터뷰어 역할로 권한을 나누고, Postgres RLS 로 조직(고객사)끼리 데이터를 분리한다. 앱 코드가 확인을 빠뜨려도 DB 가 막는다.
- **질문 트리**: 업종별(동네 식당, 카페) 질문 트리가 답변에 따라 꼬리 질문으로 분기하고, 숫자가 필요한 질문에서 숫자가 없으면 "대략 어느 정도였을까요?" 로 한 번 더 묻는다. 여러 회차에 걸쳐 이어서 인터뷰한다.
- **음성 받아 적기**: 사장님은 말로 답하고, STT(faster-whisper)가 받아 적는다.
- **운영 기본기**: structlog 구조화 로그와 요청 ID, `limits` 기반 rate limit, 보안 헤더, Sentry 연동(선택).
- **배포 준비**: Dockerfile·docker compose 로컬 스택, GitHub Actions CI (lint·타입·단위·RLS·골든셋·E2E).

## 기술 선택
원칙: **검증된 오픈소스·관리형 서비스를 쓰고 직접 만들지 않는다.** 로그인·권한·토큰 검증·로깅처럼 틀리면 위험한 부분일수록 그렇다.

| 구성 요소 | 선택 | 이유 |
|---|---|---|
| 로그인·조직 데이터 분리 | Supabase Auth + Postgres RLS | 매직 링크 로그인을 직접 만들지 않고, 데이터 분리를 DB 정책으로 강제 |
| 토큰 검증 | PyJWT | Supabase 액세스 토큰(HS256·JWKS) 검증의 표준 라이브러리 |
| DB 접속 | psycopg 3 (pool) | 트랜잭션마다 `SET LOCAL ROLE` 로 RLS 역할을 바꾸기 쉬움 |
| 로그 | structlog | JSON·콘솔 겸용 구조화 로그, 요청 ID 바인딩 |
| rate limit | limits | FastAPI 전역 의존성으로 건다. 래퍼(slowapi) 대신 엔진을 직접 써서 중첩 라우터에서도 핸들러별 한도가 맞게 걸린다 |
| 보안 헤더 | secure | CSP·HSTS 등 헤더 묶음을 한 곳에서 관리 |
| 오류 추적 | sentry-sdk | DSN 만 넣으면 켜지고, 없으면 꺼짐 |
| DB 테스트 | pytest-postgresql | 마이그레이션된 템플릿 DB 를 테스트마다 복제 |
| E2E | Playwright | 실제 브라우저로 검수·공개 흐름과 스크린샷 확인 |

## 구조
```mermaid
flowchart LR
  B[브라우저<br/>인터뷰·검수·카드 화면] -->|HTTPS + JWT| A[FastAPI<br/>shoplegacy]
  B -->|매직 링크 로그인| SA[Supabase Auth]
  A -->|토큰 검증 JWT/JWKS| SA
  A -->|SET LOCAL ROLE<br/>anon / authenticated| P[(Postgres<br/>RLS)]
  A -->|꼬리 질문·초안·근거 검증| L[LLM]
  A -->|음성 받아 적기| S[STT<br/>faster-whisper]
```

## 화면
| 검수 화면 | 공개 카드 |
|---|---|
| ![검수 화면: 문장별 근거 배지와 원 답변 패널](assets/screenshots/review.png) | ![공개 카드: 근거 배지](assets/screenshots/card.png) |

| 카드 목록 | 로그인 |
|---|---|
| ![공개 카드 목록](assets/screenshots/cards.png) | ![로그인](assets/screenshots/login.png) |

화면의 가게·답변은 모두 합성 데이터다.

### 화면과 디자인
담당자 화면은 휴대폰 한 손 사용을 기준으로 다시 그렸다. 로그인은 링크를 보낸 뒤 같은 자리에 메일함 안내가 펼쳐지고, 기관 만들기는 버튼이 화면 아래에 고정돼 "만든 사람이 소유자"라고 알려 준다. 새로 생긴 화면은 4개다.

| 화면 | 주소 | 내용 |
|---|---|---|
| 동의 | `/shops/{id}/consent` | 가게를 등록한 직후 동의 종류와 방법을 받는다 |
| 검수함 | `/review` | 초안·사장님 답 대기·보류·공개로 카드를 걸러 본다 |
| 구성원 | `/members` | 기관 구성원 추가·제거 |
| 설정 | `/settings` | 보관 기간과 사장님 승인 설정(읽기 전용) 확인 |

아래 메뉴는 역할에 따라 달라진다. 인터뷰어는 가게·공개 카드만, 검토자는 검수함이 더해지고, 관리자와 소유자는 기관 메뉴까지 본다. 요금제 탭은 소유자만 본다.

디자인 토큰(`tokens.css`: 색·글꼴 변수, OS 설정을 따르는 다크 모드)과 글꼴(IBM Plex Sans KR, Gowun Batang, IBM Plex Mono 서브셋 woff2 6개)은 모두 이 저장소에서 제공하고 외부 글꼴·스크립트 요청은 없다. 글꼴은 SIL Open Font License 1.1 이며 사본이 `static/fonts/OFL-*.txt` 에 있다. 움직임에 쓰는 GSAP 3.12.5 는 `static/vendor/gsap.min.js` 로 두고 GSAP 표준 라이선스(`static/vendor/LICENSE-gsap.txt`)를 따른다. 움직임 감소 설정이면 짧은 페이드만 쓴다.

## 실행 방법
Python 3.12, [uv](https://docs.astral.sh/uv/), Docker, [Supabase CLI](https://supabase.com/docs/guides/local-development/cli/getting-started) 가 필요하다. 인터뷰 엔진은 옆 폴더의 `../handoff` 를 경로 의존성으로 설치한다. 데이터는 Postgres 에 저장되고, 로그인·조직·권한은 Supabase Auth 와 Postgres RLS 가 맡는다.

```bash
uv sync                         # 의존성 설치
uv sync --extra stt             # (선택) 실제 음성 받아 적기(faster-whisper)까지 설치
supabase start                  # 로컬 Supabase (Postgres 56322, API 56321). 마이그레이션이 적용된다
supabase status -o env          # JWT_SECRET, ANON_KEY 값을 확인
cp .env.example .env            # SHOPLEGACY_JWT_SECRET ← JWT_SECRET, SHOPLEGACY_SUPABASE_ANON_KEY ← ANON_KEY
# .env 에 아래 줄도 넣는다 (로컬 Supabase 로그인에 필요)
SHOPLEGACY_JWKS_URL=http://localhost:56321/auth/v1/.well-known/jwks.json
uv run --env-file .env uvicorn shoplegacy.main:create_app --factory --port 8830
curl -sf http://localhost:8830/health   # {"status":"ok"}
```

브라우저에서 http://localhost:8830 을 연다. 공개된 카드 목록은 http://localhost:8830/cards 에서 업종·동네 유형으로 걸러 볼 수 있다. 설정은 `SHOPLEGACY_*` 환경변수로 바꾼다 (기본값: `SHOPLEGACY_PROVIDER=fake`, `SHOPLEGACY_STT_PROVIDER=fake`, `SHOPLEGACY_DATA_DIR=./data`). `SHOPLEGACY_PROVIDER=qwen` 이면 `SHOPLEGACY_LLM_API_KEY` 가 필요하다. `SHOPLEGACY_DATABASE_URL` (앱 전용 역할 `shoplegacy_app` 접속 주소) 은 필수이고, `SHOPLEGACY_JWT_SECRET` 또는 `SHOPLEGACY_JWKS_URL` 중 하나가 필요하다. 가능한 변수는 `.env.example` 에 있고, 운영에 쓰는 변수는 아래 "운영 환경변수" 표에 정리했다.

Supabase CLI 2.x 는 사용자 토큰을 ES256 으로 서명한다. `SHOPLEGACY_JWKS_URL` 이 비어 있으면 서버가 토큰을 검증하지 못해 로그인 직후 다시 로그인 화면으로 돌아온다.

처음 로그인하면 기관 이름을 묻는 화면(`/onboarding`)이 뜬다. 기관을 만든 뒤 가게를 등록한다.

### 녹음 파일 받아 적기 (작업 큐)
인터뷰 화면에서 올린 녹음 파일은 바로 받아 적지 않고 작업으로 쌓인다 (`POST /api/stt/jobs` → 202, 화면은 `GET /api/stt/jobs/{id}` 를 폴링). 파일은 개인정보 키로 암호화해 `SHOPLEGACY_AUDIO_STORE` 에 두고, 받아 적기가 끝나면 지운다. 큐는 같은 Postgres 위의 procrastinate 가 맡는다. 실패하면 두 번 다시 시도하고, 그래도 안 되면 작업이 `failed` 가 된다.

워커는 `SHOPLEGACY_STT_WORKER` 로 고른다.

| 값 | 동작 |
|---|---|
| `thread` (기본) | 앱 프로세스 안의 스레드가 작업을 처리한다. 서버 하나로 돌릴 때 |
| `external` | 앱은 작업을 쌓기만 하고, 별도 프로세스가 처리한다. 무거운 whisper 모델을 웹 서버와 나눌 때 |
| `off` | 작업을 쌓기만 하고 아무도 처리하지 않는다. 받아 적기를 잠시 멈출 때 |

```bash
# external 일 때 워커 프로세스 (앱과 같은 .env 를 읽는다)
uv run --env-file .env procrastinate --app shoplegacy.jobs.app worker
```

워커는 같은 프로세스에서 결제 큐(`billing`)도 듣는다. 매일 자동결제 작업이 여기서 돈다 (아래 "청구서와 결제").

#### 녹음 파일을 S3 호환 저장소에 두기 (Supabase Storage)
`SHOPLEGACY_AUDIO_STORE=s3://<버킷>/<접두>` 로 두면 녹음 파일이 S3 호환 저장소에 암호화된 채로 올라갔다가 받아 적기가 끝나면 지워진다. 코드 변경 없이 s3fs 가 맡는다 (`uv sync --extra s3`). 로컬 Supabase 의 Storage S3 를 쓰는 예다. 값은 `supabase status -o env` 에서 아래 이름으로 찾는다.

| `supabase status -o env` 이름 | 넣을 변수 |
|---|---|
| `STORAGE_S3_URL` | `SHOPLEGACY_AUDIO_S3_ENDPOINT` (호스트 부분은 `localhost` 로 바꿔 넣는다) |
| `S3_PROTOCOL_ACCESS_KEY_ID` | `AWS_ACCESS_KEY_ID` |
| `S3_PROTOCOL_ACCESS_KEY_SECRET` | `AWS_SECRET_ACCESS_KEY` |
| `S3_PROTOCOL_REGION` | `AWS_DEFAULT_REGION` |

```bash
uv sync --extra s3
eval "$(supabase status -o env | grep -E '^(STORAGE_S3_URL|S3_PROTOCOL_)')"
export AWS_ACCESS_KEY_ID="$S3_PROTOCOL_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$S3_PROTOCOL_ACCESS_KEY_SECRET"
export AWS_DEFAULT_REGION="$S3_PROTOCOL_REGION"
export SHOPLEGACY_AUDIO_S3_ENDPOINT="$(printf '%s' "$STORAGE_S3_URL" | sed -E 's#//[^:/]+#//localhost#')"
export SHOPLEGACY_AUDIO_STORE=s3://<버킷>/audio      # 버킷은 Supabase Studio 나 s3fs 로 먼저 만든다
```

실제 저장소로 확인하는 E2E 는 `SHOPLEGACY_TEST_S3_ENDPOINT` 가 있을 때만 돈다 (없으면 건너뛴다). 테스트가 임시 버킷을 만들고, 올린 파일이 평문으로 남지 않는지와 작업이 끝나면 지워지는지를 본다.

```bash
# 위의 AWS_* 를 export 한 셸에서
SHOPLEGACY_TEST_S3_ENDPOINT="$SHOPLEGACY_AUDIO_S3_ENDPOINT" uv run pytest tests/e2e/test_audio_store_s3.py -q
```

Supabase Storage 는 여러 객체를 한 번에 지우는 요청(배치 DeleteObjects)을 거부한다. 앱은 파일을 하나씩 지우므로 문제없지만, 버킷을 통째로 비우는 정리 작업은 객체를 하나씩 지워야 한다.

### 실시간 마이크 받아 적기
인터뷰 화면의 마이크 버튼을 누르면 브라우저가 16kHz PCM 음성을 WebSocket `/api/stt/stream` 으로 보내고, 서버가 말이 끊길 때마다 받아 적은 문장을 돌려준다. `SHOPLEGACY_STT_PROVIDER=whisper` (`uv sync --extra stt` 필요) 면 faster-whisper 의 음성 구간 검출로 발화를 나눠 서버 안에서 받아 적고, 음성은 외부로 나가지 않는다. `fake` 면 텍스트 프레임을 그대로 돌려주는 시험용이다. 연결 하나는 `SHOPLEGACY_STT_STREAM_MAX_SECONDS` 초까지, 프로세스당 `SHOPLEGACY_STT_STREAM_MAX_CONCURRENT` 개까지 받는다. 브라우저의 마이크 권한은 `localhost` 또는 HTTPS 에서만 열린다. 리버스 프록시를 두면 WebSocket 업그레이드를 통과시켜야 한다.

### 청구서와 결제
요금제 결제는 청구서 단위다. 운영자가 청구서를 발행하고, 조직 owner 가 요금제 화면(`/billing`)에서 결제하면 청구서가 `paid` 가 되고 요금제가 올라간다. 계약·계좌이체로 받은 경우는 운영자가 직접 결제 완료로 바꾼다. 매달 카드로 내고 싶은 조직은 카드를 등록해 자동결제를 건다. 운영자 명령은 RLS 를 넘어야 하므로 DB 소유자 DSN 으로 돌린다. 명령 결과는 stdout, 로그와 오류 코드는 stderr 로 나간다.

```bash
export SHOPLEGACY_DATABASE_URL=<DB 소유자 DSN>
uv run python -m shoplegacy.maintenance issue-invoice <org_id> pilot 2026-11            # 금액은 요금제 기본값. 청구서 id 출력
uv run python -m shoplegacy.maintenance issue-invoice <org_id> org 2026-11 --amount 450000 --memo "연 계약 할인"
uv run python -m shoplegacy.maintenance mark-paid <invoice_id> --method contract        # 카드 외 수납
uv run python -m shoplegacy.maintenance void-invoice <invoice_id>                       # 발행 취소 (미결제만. 환불된 청구서는 거부)
```

결제 공급자는 `SHOPLEGACY_BILLING_PROVIDER` 로 고른다.

| 값 | 동작 |
|---|---|
| `fake` (기본) | 실제 돈이 오가지 않는다. 결제·카드 등록 버튼이 `/billing/pay-test` 시험 페이지로 가고, 거기서 승인하면 결제 완료(또는 카드 등록)로 처리된다. 개발·데모·E2E 용 |
| `toss` | 요금제 화면에 토스페이먼츠 결제위젯(v2)이 뜨고, 서버가 승인·조회 API 로 결제를 확인한다. 카드 등록은 토스 빌링 인증 창으로 한다. `SHOPLEGACY_TOSS_CLIENT_KEY`·`SHOPLEGACY_TOSS_SECRET_KEY` 가 필요하다. 토스 개발자센터에 웹훅 주소 `<SHOPLEGACY_PUBLIC_BASE_URL>/api/billing/webhook` 을 등록하면 브라우저가 중간에 닫혀도 결제가 반영된다 |

#### 결제위젯
청구서의 "결제" 버튼을 누르면 화면 안에 결제수단과 약관 동의 위젯이 그려지고, 결제하면 토스 결제창을 거쳐 `/billing` 으로 돌아와 서버가 승인한다. 위젯 스크립트·iframe 도메인(`js.tosspayments.com`, `*.tosspayments.com`, `static.tosspayments.com`)은 `/billing` 으로 시작하는 화면의 CSP 에만 열려 있다. 다른 화면에는 결제 도메인이 없다.

#### 자동결제 (카드 등록)
조직 owner 가 요금제 화면의 자동결제 칸에서 요금제와 결제일(매달 1~28일)을 고르고 카드를 등록한다 (`POST /api/orgs/{org_id}/billing-key/auth` → 카드 인증 → `POST /api/orgs/{org_id}/billing-key`). 카드 정보는 토스가 갖고, 앱은 토스가 준 빌링키만 개인정보 키로 암호화해 저장한다. 빌링키는 API 응답과 감사 로그에 나오지 않는다. 첫 결제일은 이번 달 결제일이 안 지났으면 이번 달, 지났으면 다음 달이다. owner 는 같은 화면에서 자동결제를 끌 수 있다 (`DELETE /api/orgs/{org_id}/billing-key`).

결제일이 된 조직은 매일 `0 3 * * *`(서버 시간 03:00)에 procrastinate 주기 작업이 청구한다. 그 달 청구서가 없으면 발행하고, 등록한 카드로 결제해 `paid` 로 바꾼 뒤 다음 결제일을 한 달 뒤로 옮긴다. 카드가 거절되면 실패 횟수를 올리고, 3번 연속 거절이면 자동결제를 멈추고(`stopped`) 감사 로그를 남긴다. 주기 작업은 STT 워커(`thread` 또는 `external`)가 돌 때만 실행되므로, `SHOPLEGACY_STT_WORKER=off` 로 돌리면 호스트 cron 에서 같은 일을 하는 명령을 건다.

```bash
uv run --env-file .env python -m shoplegacy.maintenance charge-due                    # 오늘 결제일인 조직 청구. 줄마다 "<org_id> <결과> <청구서 id> <코드>"
uv run --env-file .env python -m shoplegacy.maintenance charge-due --today 2026-12-01 # 날짜를 정해 다시 돌리기
uv run --env-file .env python -m shoplegacy.maintenance stop-autopay <org_id>         # 운영자가 자동결제 중지
# cron 예시 (워커를 끈 경우): 매일 03:00
0 3 * * * cd /srv/shoplegacy && uv run --env-file .env python -m shoplegacy.maintenance charge-due >> autopay.log 2>&1
```

결과 값은 `paid`(결제됨), `already_paid`(이미 낸 달), `declined`(거절, 코드 표시), `stopped`(3회 거절로 중지), `skipped`(그 달 청구서가 취소·환불된 상태 등이라 청구하지 않음) 중 하나다. 같은 날 두 번 돌려도 이미 낸 달은 다시 청구하지 않는다.

#### 환불
환불은 전액만, 운영자 명령으로만 한다 (owner 셀프 환불 없음). 카드 결제였으면 토스 결제 취소 API 로 먼저 취소하고, 성공하면 청구서를 `refunded` 로 바꾸며 사유와 시각을 남긴다. 계약 수납(`--method contract`)은 돈을 돌려준 뒤 기록만 바꾼다. 공급자가 취소를 거절하면 청구서는 그대로 두고 종료 코드 2 로 끝난다.

```bash
uv run --env-file .env python -m shoplegacy.maintenance refund-invoice <invoice_id> --reason "서비스 해지"
```

환불해도 요금제는 자동으로 내려가지 않는다. 필요하면 운영자가 `set-plan <org_id> free` 로 내린다.

#### 세금계산서 기록
세금계산서는 앱이 발행하지 않는다. 운영자가 국세청 홈택스 등에서 발행한 뒤 승인번호를 청구서에 기록하면 요금제 화면의 청구서 표에 보인다 (결제된 청구서와 환불된 청구서에 기록할 수 있다).

```bash
uv run --env-file .env python -m shoplegacy.maintenance mark-tax-invoice <invoice_id> --ref <승인번호> --issued 2026-11-10
```

#### 결제 후속 마이그레이션 되돌리기
`20261104000900_followups.sql` 은 청구서에 환불·세금계산서 열 4개와 `refunded` 상태를, 그리고 `billing_keys` 표와 함수 7개를 더한다. 이전 버전 앱으로 되돌려야 하면 관리자 DSN 으로 아래를 실행한다. 저장된 빌링키와 환불·세금계산서 기록이 지워지므로 먼저 백업한다. `refunded` 청구서가 있으면 상태 제약을 되돌리기 전에 처리 방침(예: `void`)을 정해 바꿔 둔다.

```sql
BEGIN;
DROP FUNCTION public.invoice_refund(text, text, timestamptz);
DROP FUNCTION public.invoice_mark_tax(text, text, timestamptz);
DROP FUNCTION public.billing_key_set(uuid, text, text, text, text, text, int, date, uuid);
DROP FUNCTION public.billing_key_stop(uuid);
DROP FUNCTION public.billing_keys_due(date);
DROP FUNCTION public.billing_key_advance(uuid, date);
DROP FUNCTION public.billing_key_fail(uuid);
DROP TABLE public.billing_keys;
ALTER TABLE public.invoices
  DROP COLUMN refunded_at,
  DROP COLUMN refund_reason,
  DROP COLUMN tax_invoice_ref,
  DROP COLUMN tax_invoice_issued_at;
-- refunded 청구서가 남아 있으면 아래 제약 추가가 실패한다
ALTER TABLE public.invoices DROP CONSTRAINT invoices_status_check;
ALTER TABLE public.invoices ADD CONSTRAINT invoices_status_check
  CHECK (status IN ('issued', 'paid', 'void'));
-- Supabase CLI 로 적용한 DB 면 적용 기록도 지운다
DELETE FROM supabase_migrations.schema_migrations WHERE version = '20261104000900';
COMMIT;
```

### 테스트
테스트는 로컬 Supabase Postgres 위에 마이그레이션된 템플릿 DB 를 만들고, 테스트마다 그 복제본을 쓴다. 그래서 테스트 전에 `supabase start` 가 떠 있어야 한다. 관리자 접속은 `SHOPLEGACY_TEST_PG_DSN` (기본 `postgresql://postgres:postgres@localhost:56322/postgres`), 테스트 DB 이름 접두사는 `SHOPLEGACY_TEST_DB_PREFIX` (기본 `sl_test`) 로 바꾼다.

```bash
supabase start                      # 테스트 전에 한 번
uv run ruff check src tests && uv run mypy src
uv run pytest tests/unit -q         # 단위
uv run pytest tests/pg -q           # RLS·마이그레이션 검증
uv run pytest tests/golden -q       # 합성 인터뷰 골든셋 (가림 처리·근거 비율)
uv run playwright install chromium  # 처음 한 번
uv run pytest tests/e2e -q          # 실제 서버 + 브라우저. tests/e2e/test_supabase_login.py 는 `supabase start` 상태에서만 돌고 아니면 건너뛴다
uv run pytest tests/live -q         # 실제 LLM·STT·토스 API 스모크. 키·모델이 없으면 건너뛴다
```

결제 E2E 는 실제 토스 대신 테스트가 띄우는 토스 모양의 스텁 서버와 스텁 위젯 스크립트로 돈다. 자동결제 거절 경로는 `SHOPLEGACY_FAKE_DECLINE_BILLING=1` 로 `fake` 공급자가 카드 결제를 거절하게 만들어 확인한다 (테스트 전용, 운영에서 켜지 않는다).

#### 실제 토스 API 로 오류 경로 확인
`tests/live/test_toss_live.py` 는 실제 토스페이먼츠 API 를 불러 없는 결제 조회·없는 결제 취소·가짜 인증키로 빌링키 발급, 이 오류 경로 3개가 정해진 오류 코드로 돌아오는지 본다. 돈이 오가는 호출은 하지 않는다. 키는 저장소나 `.env` 에 넣지 않고 실행할 때만 넘긴다. 토스 개발자센터 문서의 공개 테스트 시크릿 키나 자기 계정의 테스트 시크릿 키를 쓴다.

```bash
SHOPLEGACY_TOSS_SECRET_KEY=<테스트 시크릿 키> uv run pytest tests/live/test_toss_live.py -q
# 키가 없으면 3개 모두 건너뛴다
```

#### 실제 Qwen 모델로 카드 품질 평가
`scripts/qwen-eval.sh` 는 합성 인터뷰 골든셋을 실제 Qwen 모델로 돌려 카드 초안을 만들고, 케이스마다 근거 비율(`supported_ratio`)·가림 처리 누락(전화번호)·빈 축을 잰다. 기준은 케이스별 근거 비율 0.5 이상, 평균 0.7 이상, 개인정보 누출 0, 빈 축 0 이다. 설정은 이 폴더의 `.env` 에 있는 `SHOPLEGACY_LLM_API_KEY`(·`SHOPLEGACY_LLM_BASE_URL`·`SHOPLEGACY_LLM_MODEL`)를 먼저 쓴다. `.env` 는 앱과 같은 방식(`uv run --env-file .env`)으로 읽고 셸에서 `source` 하지 않는다. dotenv 값은 셸 문법이 아니라서 비거나 깨질 수 있기 때문이다. 셸에 이미 있는 값이 `.env` 보다 먼저다. 값이 비어 있을 때만 옆 폴더 `../voice-agent/.env` 의 `LLM_API_KEY`·`LLM_BASE_URL`·`LLM_MODEL` 로 채운다. 다른 파일은 `VOICE_AGENT_ENV` 로 지정한다. 키 값은 출력하지 않는다. 기본 모델은 `qwen3.7-flash` 다.

```bash
supabase start
scripts/qwen-eval.sh                                   # 마지막 줄에 QWEN_EVAL_SUMMARY {...} 요약
SHOPLEGACY_EVAL_OUT=eval.json scripts/qwen-eval.sh     # 케이스별 결과를 파일로도 남긴다
VOICE_AGENT_ENV=/path/to/other.env scripts/qwen-eval.sh
scripts/qwen-eval.sh --diagnose --out eval-diagnose g01 g02   # DB 없이 초안·검증 원응답을 케이스별 JSON 으로 덤프 (케이스를 비우면 전부)
scripts/qwen-eval.sh --record                          # 실평가를 돌리면서 녹화본을 다시 만든다
```

`--diagnose` 는 문장이 왜 근거 없음으로 판정됐는지 보려고 만든 진단 모드다. 축마다 초안 문장, 검증 모델의 원응답, 문장별 판정과 근거 번호를 남긴다.

키 없이 같은 기준을 검사하는 녹화 재생 테스트도 있다. `tests/golden/test_golden_recorded.py` 가 `tests/golden/cassettes/*.yaml`(케이스당 1개, 10개)에 녹화된 실제 Qwen 응답을 vcrpy 로 재생해, 실평가와 같은 기준(케이스별 0.5·평균 0.7·누출 0·빈 축 0)을 단언한다. 녹화본에는 인증 헤더가 들어가지 않는다. 요청 본문까지 맞춰 재생하므로 초안·검증 프롬프트를 바꾸면 `scripts/qwen-eval.sh --record` 로 다시 녹화한다.

```bash
uv run pytest tests/golden/test_golden_recorded.py -q   # 키·네트워크 불필요
```

### Docker
이미지는 `../handoff` 경로 의존성이 필요해서 상위 폴더를 빌드 컨텍스트로 쓴다.

```bash
# 상위(작업 공간) 폴더에서
docker build -t shoplegacy:dev -f shoplegacy/Dockerfile .
# 이 폴더에서: 앱 http://localhost:8831, Postgres localhost:56340
docker compose up --build
docker compose down -v              # 멈추고 DB 볼륨 삭제
```

### 배포 (Cloud Run)
서울 리전 Cloud Run(인스턴스 0~1, 요청 중에만 CPU) + Supabase 운영 DB 로 올린다. 절차 전체는 [`deploy/README.md`](deploy/README.md), DB 는 [`deploy/supabase/PRODUCTION.md`](deploy/supabase/PRODUCTION.md).

```bash
bash deploy/build-image.sh                                  # linux/amd64 이미지 shoplegacy:dev (git archive 컨텍스트)
uv run python scripts/check_deploy.py --example             # 배포 파일 일관성 → DEPLOY_CONFIG_OK
```
첫 배포는 `deploy/gcp/setup.sh` → `build-image.sh --push` → `deploy/gcp/apply-env.sh`, 이후 `main` 에서 `ci` 가 성공하면 이미지만 자동 교체된다. 운영 음성 인식은 가짜(fake)라 인터뷰는 직접 입력으로 진행한다.

## 운영

### 백업과 복원
`scripts/backup.sh` 는 `pg_dump -Fc` 로 DB 전체를 `backups/shoplegacy-<날짜-시각>.dump` 에 저장하고, 최신 14개만 남긴다. 접속 주소는 앱 역할이 아니라 덤프 권한이 있는 관리자 주소를 쓴다. `backups/` 는 git 에 올라가지 않는다.

```bash
SHOPLEGACY_BACKUP_DSN=postgresql://postgres:<비밀번호>@<호스트>:5432/postgres scripts/backup.sh
# 복원 (대상 DB 를 덮어쓴다. 먼저 빈 DB 나 스테이징에서 확인)
pg_restore --clean --if-exists --no-owner -d "$TARGET_DSN" backups/shoplegacy-20261102-0300.dump
```

`pg_dump` 는 서버와 major 버전이 같아야 한다. 스크립트는 덤프 전에 `pg_dump --version` 과 서버의 `server_version_num` 을 비교하고, 다르면 아무 파일도 만들지 않고 종료 코드 3 으로 끝난다. 설치된 클라이언트가 서버(로컬 Supabase·CI 는 17)와 다르면 `SHOPLEGACY_PG_DUMP` 에 다른 명령 접두를 준다. 버전 확인에 쓰는 `psql` 은 `SHOPLEGACY_PSQL` 로 바꾸고, 비우면 `SHOPLEGACY_PG_DUMP` 의 `pg_dump` 를 `psql` 로 바꾼 명령을 쓴다. 덤프는 stdout 으로 받으므로 docker 클라이언트도 그대로 된다.

```bash
# macOS (Docker Desktop): 컨테이너에서 호스트 DB 는 host.docker.internal 로 접속한다
SHOPLEGACY_BACKUP_DSN=postgresql://postgres:postgres@host.docker.internal:56322/postgres \
SHOPLEGACY_PG_DUMP="docker run --rm -i postgres:17 pg_dump" \
SHOPLEGACY_PSQL="docker run --rm -i postgres:17 psql" \
  scripts/backup.sh

# Linux: 호스트 네트워크를 써서 DSN 은 localhost 그대로
SHOPLEGACY_BACKUP_DSN=postgresql://postgres:postgres@localhost:56322/postgres \
SHOPLEGACY_PG_DUMP="docker run --rm -i --network host postgres:17 pg_dump" \
SHOPLEGACY_PSQL="docker run --rm -i --network host postgres:17 psql" \
  scripts/backup.sh

# 복원도 같은 방식. 덤프 파일은 stdin 으로 넘긴다
SHOPLEGACY_PG_RESTORE="docker run --rm -i postgres:17 pg_restore"
$SHOPLEGACY_PG_RESTORE --clean --if-exists --no-owner \
  -d postgresql://postgres:<비밀번호>@host.docker.internal:5432/postgres < backups/shoplegacy-20261102-0300.dump
# 덤프 안의 목록만 보기
docker run --rm -v "$PWD/backups:/b" postgres:17 pg_restore --list /b/shoplegacy-20261102-0300.dump
```

#### 공개 태그 함수 v1 되돌리기
`20261103000800_remaining.sql` 마이그레이션은 4열짜리 `public.public_card_tags(text)` 를 지우고 `public_card_tags_v2` 만 남긴다. 이전 버전 앱으로 되돌려야 하면 관리자 DSN 으로 아래를 실행한다 (`20261102000400_public_card_tags.sql` 과 같은 내용).

```sql
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
```

호스팅된 Supabase 를 쓰면 관리형 일일 백업(유료 플랜, 시점 복구는 별도 옵션)을 우선 쓰고, 이 스크립트는 다른 곳에 사본을 두는 용도로 함께 돌린다. 백업 파일에는 암호화된 개인정보 열이 그대로 들어가므로, 복원하려면 그때 쓰던 `SHOPLEGACY_PII_KEYS` 도 같이 보관해야 한다.

### 보관기한과 자동 삭제
가게를 등록할 때 `등록 시각 + 조직 보관기한(일)` 으로 삭제 예정일이 정해진다. 조직 보관기한 기본값은 1095일(3년)이고, 조직 owner 가 `PUT /api/orgs/{org_id}/retention` (`{"days": 30 이상}`) 으로 바꾼다. 바꾼 기한은 그 뒤에 등록하는 가게부터 적용된다. 응답의 `next_purge_count` 는 다음 삭제 실행 때 지워질 그 조직 가게 수다.

삭제 예정일이 지난 가게는 아래 명령이 지운다 (인터뷰·답변·카드·동의·사장님 검토가 함께 지워지고, 감사 로그에 `shop.purge` 가 남는다). 지운 가게 id 를 한 줄씩 출력한다.

```bash
uv run --env-file .env python -m shoplegacy.maintenance purge --dry-run   # 지울 대상만 확인 (롤백)
uv run --env-file .env python -m shoplegacy.maintenance purge
# cron 예시: 매일 03:30 에 삭제, 04:00 에 백업
30 3 * * * cd /srv/shoplegacy && uv run --env-file .env python -m shoplegacy.maintenance purge >> purge.log 2>&1
0 4 * * *  cd /srv/shoplegacy && SHOPLEGACY_BACKUP_DSN=... scripts/backup.sh >> backup.log 2>&1
```

Supabase 에서는 호스트 cron 대신 pg_cron 으로 `SELECT public.purge_expired_shops(now())` 를 같은 주기로 걸어도 된다.

가게 하나를 바로 지우거나 내려받을 때는 조직 admin 이 `DELETE /api/shops/{shop_id}` (204) 와 `GET /api/shops/{shop_id}/export` (zip: `shop.json`, `sessions.json`, `answers.json`, `cards.json`, `card_versions.json`, `consents.json`, `owner_reviews.json`, `audit.json`, 개인정보는 복호화된 상태) 를 쓴다. 사장님의 열람·삭제 요청에 이 두 경로로 응한다.

요금제는 계약 후 운영자가 바꾼다 (API 없음, 감사 로그 `org.plan`): `SHOPLEGACY_DATABASE_URL=<DB 소유자 DSN> uv run python -m shoplegacy.maintenance set-plan <org_id> <free|pilot|org>` — 앱 DSN 은 RLS 때문에 orgs 를 바꿀 수 없다.

### 개인정보 암호화 키
가게 이름·사장님 이름·거래처·답변 원문은 앱에서 Fernet 으로 암호화해 저장한다. 키를 만들고 `.env` 에 넣는다.

```bash
uv run python -m shoplegacy.maintenance generate-key
# SHOPLEGACY_PII_KEYS=<새 키>
```

키를 바꿀 때는 새 키를 맨 앞에 두고 이전 키를 뒤에 남긴다 (`SHOPLEGACY_PII_KEYS=<새 키>,<이전 키>`). 새로 쓰는 값은 첫 키로 암호화되고, 읽을 때는 목록의 모든 키를 시도한다. 이전 키로 암호화된 값이 남아 있는 동안에는 이전 키를 지우지 않는다. 키가 비어 있으면 암호화 없이 저장하고 기동 로그에 `pii_encryption_disabled` 를 남긴다.

### 운영 환경변수
| 변수 | 기본값 | 설명 |
|---|---|---|
| `SHOPLEGACY_PII_KEYS` | (없음) | 쉼표로 구분한 Fernet 키. 첫 키로 암호화, 전부로 복호화 |
| `SHOPLEGACY_RETENTION_DAYS` | `1095` | 조직 보관기한 기본값(일), 최소 30. 조직별 값이 우선 |
| `SHOPLEGACY_NER` | `none` | `none` 또는 `presidio` (이름 가림에 개체명 인식 추가) |
| `SHOPLEGACY_NER_MODEL` | `ko_core_news_md` | presidio 가 쓰는 spaCy 모델 |
| `SHOPLEGACY_OTEL_ENDPOINT` | (없음) | OTLP/HTTP 주소. 비면 트레이싱 꺼짐 |
| `SHOPLEGACY_OTEL_SERVICE_NAME` | `shoplegacy` | 트레이스 서비스 이름 |
| `SHOPLEGACY_TRUSTED_PROXIES` | (없음) | 쉼표 구분 프록시 IP 또는 `*`. 비면 `X-Forwarded-*` 무시 |
| `SHOPLEGACY_CSP_REPORT_ONLY` | `false` | true 면 CSP 를 보고 전용으로 보낸다 |
| `SHOPLEGACY_SMS_PROVIDER` | `log` | `log` 또는 `solapi`. solapi 면 아래 3개 필수 |
| `SHOPLEGACY_SMS_API_KEY` / `_SECRET` / `_FROM` | (없음) | 문자 발송 계정 |
| `SHOPLEGACY_OWNER_LINK_TTL_HOURS` | `72` | 사장님 검토 링크 유효 시간 |
| `SHOPLEGACY_REQUIRE_OWNER_APPROVAL` | `false` | true 면 사장님 승인 없이는 공개 불가 |
| `SHOPLEGACY_PUBLIC_BASE_URL` | `http://localhost:8830` | 사장님 링크 주소의 앞부분 |
| `SHOPLEGACY_REPORT_MAX_ROWS` | `10000` | 보고서 최대 행 수 |
| `SHOPLEGACY_BACKUP_DSN` | (없음) | `scripts/backup.sh` 전용. 앱은 읽지 않는다 |
| `SHOPLEGACY_PG_DUMP` / `SHOPLEGACY_PG_RESTORE` | `pg_dump` / `pg_restore` | 백업·복원 명령 접두. 예 `docker run --rm -i postgres:17 pg_dump`. 앱은 읽지 않는다 |
| `SHOPLEGACY_PSQL` | `SHOPLEGACY_PG_DUMP` 의 `pg_dump` → `psql` | `scripts/backup.sh` 가 서버 버전을 확인할 때 쓰는 명령 |
| `SHOPLEGACY_FEEDBACK_SECRET` | (없음) | 공개 피드백 지문 키. 비면 `SHOPLEGACY_JWT_SECRET`, 그것도 없으면 기동마다 새로 만들고 `feedback_secret_generated` 경고 |
| `SHOPLEGACY_AUDIO_STORE` | `file://<데이터 폴더>/audio` | 받아 적기 대기 중인 녹음 파일 위치. `file://` 또는 `s3://` 만 |
| `SHOPLEGACY_AUDIO_S3_ENDPOINT` | (없음) | `s3://` 일 때 S3 호환 주소 (예: Supabase Storage S3). 자격은 아래 `AWS_*` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | (없음) | `s3://` 저장소 자격. s3fs(boto)가 표준 이름으로 읽는다. 로컬 Supabase 는 `S3_PROTOCOL_ACCESS_KEY_ID` / `S3_PROTOCOL_ACCESS_KEY_SECRET` 값 |
| `AWS_DEFAULT_REGION` | (없음) | `s3://` 저장소 지역. 로컬 Supabase 는 `S3_PROTOCOL_REGION` 값 |
| `SHOPLEGACY_STT_WORKER` | `thread` | `thread`(앱 안 스레드), `external`(별도 워커 프로세스), `off`(작업을 쌓기만 함) |
| `SHOPLEGACY_STT_STREAM_MAX_SECONDS` | `600` | 실시간 받아 적기 연결 하나의 최대 길이(초), 최소 10 |
| `SHOPLEGACY_STT_STREAM_MAX_CONCURRENT` | `4` | 프로세스당 동시 실시간 연결 수, 최소 1 |
| `SHOPLEGACY_BILLING_PROVIDER` | `fake` | `fake` 또는 `toss`. toss 면 아래 두 키 필수 |
| `SHOPLEGACY_TOSS_CLIENT_KEY` / `SHOPLEGACY_TOSS_SECRET_KEY` | (없음) | 토스페이먼츠 클라이언트 키·시크릿 키 |
| `SHOPLEGACY_TOSS_API_BASE` | `https://api.tosspayments.com` | 토스페이먼츠 API 주소 |
| `SHOPLEGACY_FAKE_DECLINE_BILLING` | (없음) | **테스트 전용.** `1` 이면 `fake` 공급자가 자동결제 카드 결제를 거절한다. 운영에서 켜지 않는다 |
| `SHOPLEGACY_TEST_S3_ENDPOINT` | (없음) | 테스트 전용. 있으면 S3 저장소 E2E 를 그 주소로 돌린다 |

## 현재 한계
- 실제 Qwen(`qwen3.7-flash`)으로 잰 카드 근거 비율은 처음에 케이스별 0.0~0.9 (미달 8건은 0.0~0.43), 평균 0.345 로 10건 중 8건이 기준에 못 미쳤다. 초안이 답변 두 개를 한 문장으로 합쳐 바꿔 말한 것이 원인이었다. 초안을 답변당 한 문장·원문 표현으로 쓰게 바꾼 뒤 실평가 3회 모두 평균 1.0·최저 1.0·개인정보 누출 0·빈 축 0 이었다 (문장 120개 중 116개는 Qwen 검증이 인정, 4개는 원문 포함도로 구제). 대신 카드 문장이 답변 원문에 매우 가까워서, 요약·윤문으로 얻는 읽기 좋음은 줄었다.
- 매직 링크 로그인은 호스팅된 Supabase 프로젝트와 SMTP 설정이 있어야 실제 메일이 간다.
- 사례비 지급은 없다.
- 결제는 토스 결제위젯·자동결제(빌링키)·전액 환불까지 있지만, 실제 가맹점 계정으로 돈이 오가는 결제는 검증하지 않았다. 확인한 것은 스텁 서버로 도는 E2E 와 공개 테스트 키로 부른 실제 API 의 오류 경로뿐이다.
- 부분 환불은 없다. 환불은 전액만, 운영자 명령으로만 한다.
- 세금계산서를 자동으로 발행하지 않는다. 운영자가 따로 발행하고 승인번호만 기록한다.
- 보관기한을 바꿔도 이미 등록된 가게의 삭제 예정일은 그대로다.
- 백업 스크립트는 복원까지 자동으로 확인하지 않는다. 주기적으로 스테이징에 `pg_restore` 해 보는 것은 운영자 몫이다.

## MVP
- 업종 1개(동네 식당), 인터뷰 5건 직접 수행
- 카드 5장 공개 페이지
- 창업 준비자 3명에게 보여주고 "도움됐나" 인터뷰

## handoff 재사용 대응표
| handoff | shoplegacy | 방식 |
|---|---|---|
| `handoff.question_tree.FollowUp` | 그대로 | import |
| `handoff.privacy.scrub_contacts`, `find_pii` | `masking.mask` 안에서 호출 | import |
| `handoff.providers.FakeLLM.follow_up`, `handoff.providers_qwen.QwenLLM.follow_up`, `handoff.providers.ProviderError` | `providers.FakeShopLLM/QwenShopLLM.follow_up` 이 위임 | import |
| `handoff.question_tree.{Axis, QuestionNode, QuestionTree, load_tree}` | `question_tree.py` 6축판 (같은 필드·같은 규칙) | 같은 모양으로 재구현 (Axis Literal 결합 때문) |
| `handoff.session.{Question, SessionView, SessionService}` | `interview.{Question, InterviewSession, InterviewService}` (`leaver→shop_id`, `role→trade`, hooks·queue·KB·summary 제거) | 같은 모양으로 재구현 |
| `handoff.kb.KBEntry` | `interview.AnswerEntry` (필드 동일) | 같은 모양으로 재구현 |
| `handoff.store.SessionStore` | `store.Store` (`sessions`·`answers` 표 열 동일 + `shops`·`cards`) | 같은 모양으로 재구현 |
| `handoff.kb.KnowledgeBase`, rbac, demo, coverage, cards(KnowledgeCard) | 사용 안 함 | - |
