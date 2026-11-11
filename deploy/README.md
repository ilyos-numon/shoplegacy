# 운영 배포 절차

가게유산을 Cloud Run 에 올리는 실제 순서입니다. 이 폴더의 파일에는 실제 프로젝트 ID·주소·키가 없고 `<PROJECT_ID>`·`<PROJECT_NUMBER>`·`<project-ref>`·`<도메인>` 같은 자리표시만 있습니다. 실제 값은 저장소 밖 `.env.production`(git 제외)에만 둡니다.

구성: Cloud Run 서비스 `shoplegacy` 1개(서울 `asia-northeast3`, 최소 0·최대 1 인스턴스, 512Mi, 요청 처리 중에만 CPU — 요청이 없으면 인스턴스가 0 으로 줄어 유휴 비용이 거의 없음, 주소는 Cloud Run 이 주는 `…run.app` 그대로) + Supabase 운영 프로젝트 + Secret Manager 비밀 6개 + GitHub Actions(WIF) 자동 배포. **예약 실행·도메인 매핑·부하분산기·별도 워커 서비스는 없습니다.**

- 음성 받아쓰기 작업 대기열(procrastinate)은 앱 안 스레드가 처리합니다 (`SHOPLEGACY_STT_WORKER=thread`). 인터뷰 화면이 결과를 묻는 동안 CPU 가 배정되므로 따로 워커를 띄우지 않습니다.
- 음성 인식은 **가짜(`fake`)** 입니다. 운영 데모 인터뷰는 `녹음 파일 올리기 · 직접 입력` 을 눌러 답을 글로 적습니다 (아래 `STT 직접 입력`).
- 결제는 가짜(`fake`), 문자는 로그만(`log`) 남깁니다. 실제 결제·발송은 없습니다.

| 파일 | 하는 일 |
|---|---|
| `deploy/env.production.example` | 운영 환경변수 전체 목록. SECRET(비밀 6개)·ENV(평문)·DEPLOY(배포 입력)·UNUSED(운영에서 비움) 구역. 복사해 `.env.production` 을 만듭니다 |
| `deploy/gcp/setup.sh` | API·Artifact Registry·런타임/배포 서비스 계정·WIF·비밀 6개(이름만) 만들기 (여러 번 돌려도 됨, `--dry-run`) |
| `deploy/build-image.sh` | `git archive` 로 shoplegacy HEAD 와 handoff(ci.yml 이 고정한 커밋)만 담은 임시 컨텍스트에서 `linux/amd64` 이미지 빌드 (`--push`) |
| `deploy/gcp/apply-env.sh` | `.env.production` 을 읽어 마이그레이션·`shoplegacy_app` 비밀번호·비밀 6개·평문 env 를 반영하고 서비스를 공개 (`--skip-db`·`--dry-run`) |
| `deploy/gcp/service.yaml` | Cloud Run 서비스 매니페스트의 원본 (공개해도 되는 고정 값만). `apply-env.sh` 가 임시 파일로 채워 씁니다 |
| `.github/workflows/deploy.yml` | main 에 push 되고 `ci` 가 성공하면 이미지를 빌드해 **이미지만** 교체 |
| `deploy/supabase/PRODUCTION.md` | Supabase 운영 DB 세부 절차 (리전·세션 풀러·`shoplegacy_app`·JWT 서명 키·Auth·백업·롤백) |
| `scripts/check_deploy.py` | 위 파일들이 서로 맞는지, 스케일 설정·STT/결제/문자 제공자 값 검사: `uv run python scripts/check_deploy.py --example` → `DEPLOY_CONFIG_OK` (`--env .env.production` 으로 실제 파일도 검사) |

준비물: `gcloud`(`gcloud auth login`), Docker, Supabase CLI, `psql`, `openssl`, `python3`, `uv`, 저장소 옆에 `../handoff` 저장소, GitHub 저장소 관리자 권한, 결제 계정이 연결된 GCP 프로젝트, **이미 만든 Supabase 운영 프로젝트**.

> 외부에서 살아 있는지 볼 때는 `/readyz` 를 씁니다 (DB 까지 확인). Cloud Run 프론트가 외부의 `/healthz` 요청을 가로채므로 쓰지 않습니다. `/health` 는 컨테이너 시작 확인(`startupProbe`) 전용입니다.

> **결제 계정 연결 한도**: 결제 계정 하나에 연결할 수 있는 프로젝트 수에는 한도가 있습니다. 새 프로젝트에 결제 연결이 "한도 초과"로 실패하면 쓰지 않는 기존 빈 프로젝트를 `GCP_PROJECT_ID` 로 씁니다 (다른 서비스가 있는 프로젝트는 피합니다).

---

## 1. GCP 준비: 배포 서비스 계정·WIF (`setup.sh`)

먼저 2단계대로 `.env.production` 의 DEPLOY 구역(`GCP_PROJECT_ID`·`GCP_REGION`·`ARTIFACT_REPO`·`GITHUB_REPO`)을 채운 뒤, 그 값을 셸로 불러옵니다. `setup.sh` 는 파일을 읽지 않고 환경변수만 봅니다.

```sh
set -a; source .env.production; set +a   # setup.sh 가 쓰는 GCP_PROJECT_ID 등을 내보냄
bash deploy/gcp/setup.sh --dry-run   # 실행할 명령만 확인
bash deploy/gcp/setup.sh
```

API 켜기 → Artifact Registry `shoplegacy` → 런타임 SA `shoplegacy-run`(비밀 읽기) → 배포 SA `shoplegacy-deploy`(`run.admin`·`artifactregistry.writer`·런타임 SA 사용) → WIF 풀 `github-pool`/프로바이더 `github-provider`(`GITHUB_REPO` 저장소만 허용) → 비밀 6개(이름만) 순서입니다. 마지막에 출력되는 `GCP_WIF_PROVIDER`·`GCP_DEPLOY_SA` 는 5단계에서 씁니다.

- 새 서비스 계정은 만든 직후 몇십 초 동안 IAM 바인딩이 `Service account … does not exist` 로 실패할 수 있습니다. `setup.sh` 는 이 오류일 때만 10초 간격으로 최대 6회 다시 시도합니다. 그래도 실패하면 잠시 뒤 다시 돌립니다 (이미 있는 것은 건너뜁니다).
- `.env.production` 값에 셸 특수문자(공백·`&`·`$`)가 있으면 `source` 가 깨집니다. 그런 값은 작은따옴표로 감쌉니다. `apply-env.sh` 는 셸로 실행하지 않고 직접 읽으므로 상관없습니다.

## 2. `.env.production` 채우기

```sh
cp deploy/env.production.example .env.production   # git 에 올라가지 않음 (.env.*)
```

`apply-env.sh` 가 읽는 값입니다. 표에 없는 ENV 구역 값도 채워 두면 Cloud Run 평문 env 로 들어갑니다(비우면 `service.yaml` 의 기본값).

| 변수 | 필수 | 어디서 | 들어가는 곳 |
|---|---|---|---|
| `GCP_PROJECT_ID` | 예 | GCP 콘솔 | 스크립트 |
| `GCP_REGION` | | 비우면 `asia-northeast3` | 스크립트 |
| `GITHUB_REPO` | 예 (`setup.sh`) | `<owner>/<repo>` | WIF 조건 |
| `ARTIFACT_REPO`·`CLOUD_RUN_SERVICE` | | 비우면 `shoplegacy` | 스크립트 |
| `SUPABASE_URL` | 예 | `https://<project-ref>.supabase.co` | 평문 env `SHOPLEGACY_SUPABASE_URL`·`SHOPLEGACY_JWKS_URL` |
| `SUPABASE_ANON_KEY` | 예 | Supabase → API Keys 의 공개 키 (새 형식 `sb_publishable_…` 그대로) | 평문 env `SHOPLEGACY_SUPABASE_ANON_KEY` |
| `SUPABASE_POOLER_URL` | 예 (`--skip-db` 면 아니오) | Supabase → Connect → **Session pooler** (`postgres.<project-ref>@…pooler.supabase.com:5432`, `[YOUR-PASSWORD]` 를 DB 비밀번호로) | 마이그레이션·`shoplegacy_app` 비밀번호 설정 (저장하지 않음) |
| `SHOPLEGACY_LLM_API_KEY` | 예 | Qwen(DashScope 국제) API 키 | Secret Manager |
| `SHOPLEGACY_JWT_SECRET`·`SHOPLEGACY_FEEDBACK_SECRET`·`SHOPLEGACY_PII_KEYS` | | 비우면 버전이 없을 때만 무작위로 만듦 | Secret Manager |
| `SHOPLEGACY_TOSS_SECRET_KEY` | | 비우면 `unused` (결제 fake) | Secret Manager |
| `SHOPLEGACY_PUBLIC_BASE_URL` | | 비우면 `https://shoplegacy-<PROJECT_NUMBER>.asia-northeast3.run.app` | 평문 env |

- **DB 연결은 Session pooler(5432)만 됩니다.** Connect 화면의 Direct 주소 `db.<project-ref>.supabase.co:5432` 는 IPv6 전용이라 Cloud Run 에서 붙지 않고, 트랜잭션 풀러(6543)는 쓰지 않습니다 (`SET LOCAL ROLE`·작업 대기열 LISTEN 이 세션을 씁니다). `apply-env.sh` 는 `SUPABASE_POOLER_URL` 의 호스트로 `shoplegacy_app` 전용 `SHOPLEGACY_DATABASE_URL` 을 만듭니다.
- **Supabase 새 형식 키**: 공개 키 `sb_publishable_…`·비밀 키 `sb_secret_…` 은 JWT 가 아닙니다. 앱은 공개 키만 로그인 화면에 넘기고, 비밀 키(`sb_secret_…`, 옛 service_role)는 쓰지 않으므로 `.env.production` 에 넣지 않아도 됩니다.
- **로그인 토큰 검증은 JWKS(ES256)** 입니다. `SHOPLEGACY_JWKS_URL` 은 `https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json` 으로 자동으로 채워집니다. Supabase 의 JWT 서명 키가 ECC(P-256)여야 합니다 (`deploy/supabase/PRODUCTION.md` 5단계). 레거시 HS256 만 쓰는 프로젝트라면 `SHOPLEGACY_JWT_SECRET` 에 Supabase JWT secret 을 넣습니다.
- 검사: `uv run python scripts/check_deploy.py --env .env.production`

## 3. 첫 이미지 빌드·push (`build-image.sh`)

서비스가 아직 없으므로 첫 이미지는 로컬에서 올립니다. 이미지는 `../handoff` 경로 의존성이 필요해 `build-image.sh` 가 shoplegacy HEAD 와 handoff 고정 커밋만 담은 임시 컨텍스트를 만들어 `linux/amd64` 로 빌드합니다 (Apple Silicon 에서도 그대로). 커밋하지 않은 변경은 들어가지 않습니다.

```sh
gcloud auth configure-docker asia-northeast3-docker.pkg.dev
TAG=$(git rev-parse --short HEAD)
export IMAGE=asia-northeast3-docker.pkg.dev/<PROJECT_ID>/shoplegacy/shoplegacy:$TAG
bash deploy/build-image.sh --push
```

태그는 커밋 해시로 둡니다. 자동 배포가 이 태그를 보고 이미지에 들어가는 파일이 바뀌었는지 판단합니다. 로컬 확인만 할 때는 `bash deploy/build-image.sh` (태그 `shoplegacy:dev`).

## 4. 첫 배포·공개 (`apply-env.sh`)

```sh
IMAGE="$IMAGE" bash deploy/gcp/apply-env.sh --dry-run   # 할 일만 출력 (외부 명령 실행 안 함)
IMAGE="$IMAGE" bash deploy/gcp/apply-env.sh
```

순서: 권한 확인(`anon`·`authenticated` 역할 관리 권한) → Supabase 마이그레이션(`supabase db push`) → `shoplegacy_app` 새 비밀번호·`SHOPLEGACY_DATABASE_URL` → Secret Manager 비밀 6개(JWT·피드백·개인정보 키는 버전이 없을 때만 무작위로 만들고, 나머지는 값이 바뀌었을 때만 새 버전) → `service.yaml` 을 임시 파일로 렌더(이미지·`<PROJECT_ID>`·공개 주소·`<project-ref>`·`.env.production` 의 평문 env) → `gcloud run services replace` → `allUsers` 에 `run.invoker`(공개) → `/readyz` 200 확인. 값은 화면에 출력하지 않고, 저장소 파일은 바꾸지 않습니다.

- 권한 확인에서 멈추면 안내대로 SQL Editor 에서 첫 마이그레이션의 역할 부분을 먼저 실행하고 다시 돌립니다.
- 끝에 서비스 주소가 `SHOPLEGACY_PUBLIC_BASE_URL` 과 다르다는 안내가 나오면 `.env.production` 에 그 주소를 적고 `bash deploy/gcp/apply-env.sh --skip-db` 를 한 번 더 돌립니다.
- Supabase Auth 의 Site URL·Redirect URLs 를 서비스 주소로 맞춥니다 (`deploy/supabase/PRODUCTION.md` 6단계).
- `SHOPLEGACY_PII_KEYS` 는 개인정보 암호화 키입니다. 잃어버리면 암호화된 값을 못 읽으니 첫 배포 뒤 비밀번호 관리자에도 보관합니다.

## 5. GitHub Variables → 자동 배포

저장소 Settings → Secrets and variables → Actions → **Variables** 에 적습니다 (WIF 를 쓰므로 GCP 키 파일은 넣지 않습니다).

| Variable | 값 |
|---|---|
| `GCP_PROJECT_ID` | `<PROJECT_ID>` |
| `GCP_REGION` | `asia-northeast3` |
| `ARTIFACT_REPO` | `shoplegacy` |
| `GCP_WIF_PROVIDER` | `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-pool/providers/github-provider` (setup.sh 출력) |
| `GCP_DEPLOY_SA` | `shoplegacy-deploy@<PROJECT_ID>.iam.gserviceaccount.com` (setup.sh 출력) |

handoff 저장소가 비공개면 Secrets 에 `HANDOFF_REPO_TOKEN`(handoff 읽기 권한 토큰)도 넣습니다. 공개면 필요 없습니다.

이후 main 에 push 하고 `ci` 워크플로가 성공하면 `deploy` 워크플로가 돌아갑니다.

- 커밋 해시 태그로 이미지를 빌드·push 하고 `deploy-cloudrun` 의 `service`+`image` 로 **이미지만** 바꿉니다. 평문 env·비밀·스케일 설정은 4단계에서 넣은 그대로 남습니다 (공개 저장소의 `service.yaml` 로 덮어쓰지 않으므로 `.env.production` 에서 온 값이 지워지지 않습니다).
- 지금 서비스 이미지의 태그(커밋)와 비교해 이미지에 들어가는 파일(`Dockerfile`·`pyproject.toml`·`uv.lock`·`README.md`·`src/`)이 그대로면 배포를 건너뜁니다. 문서·테스트만 바뀐 커밋은 배포되지 않습니다.
- `ci` 가 실패하거나 취소되면 배포하지 않습니다. Actions 화면에서 수동 실행(workflow_dispatch)하면 변경 여부와 상관없이 배포합니다.
- 마지막에 `<서비스 주소>/readyz` 가 200 인지 확인합니다.

## 6. env·비밀 바꾸기 (`apply-env.sh --skip-db`)

`.env.production` 을 고친 뒤:

```sh
bash deploy/gcp/apply-env.sh --skip-db
```

권한 확인·마이그레이션·`shoplegacy_app` 비밀번호는 건너뛰고 기존 `SHOPLEGACY_DATABASE_URL` 비밀을 그대로 씁니다. `IMAGE` 를 주지 않으면 지금 서비스가 쓰는 이미지를 그대로 씁니다. 새 마이그레이션이 생겼을 때는 `--skip-db` 없이 돌립니다 (`shoplegacy_app` 비밀번호도 새로 바뀝니다). `service.yaml` 의 고정 값(스케일·메모리·비용 상한 기본값)을 바꿨을 때도 이 명령으로 반영합니다.

## 7. 비용·확인

- 인스턴스 최소 0·최대 1, 요청 처리 중에만 CPU. 예약 실행·도메인 매핑·부하분산기가 없어 고정비가 없고, 무료 사용량 안에서는 월 비용이 거의 0 입니다. Secret Manager 활성 버전(6개)과 Artifact Registry 저장 용량은 무료 범위를 넘으면 소액이 붙으니 오래된 이미지는 가끔 지웁니다. 정확한 금액은 공식 요금표 확인.
- 결제 계정에 예산 알림을 겁니다 (아래 `예산 알림` 절).
- 확인:

```sh
gcloud run services describe shoplegacy --region=asia-northeast3 --project=<PROJECT_ID> --format='value(status.url)'
curl -s <서비스 주소>/readyz        # 200 {"status":"ok","db":"ok"}
```

### 콜드스타트

요청이 없으면 인스턴스가 0 으로 줄어, 한동안 아무도 안 들어온 뒤 첫 요청은 컨테이너 시작(수 초)을 기다립니다. `startup-cpu-boost` 가 켜져 있고 시작 확인은 `/health` 를 5초 간격 최대 12회 봅니다. 첫 화면이 늦게 뜨는 것은 정상입니다. 시연 직전에는 `/readyz` 를 한 번 불러 깨워 둡니다.

- **Supabase 리전**: 운영 프로젝트가 서울(`ap-northeast-2`)이 아닌 리전(예: 도쿄·싱가포르)에 있으면 요청마다 DB 왕복 지연이 늘어 화면이 조금 느립니다. 동작에는 문제가 없습니다. 새로 만든다면 서울을 고릅니다.

### STT 직접 입력

운영 음성 인식은 `SHOPLEGACY_STT_PROVIDER=fake` 입니다 (512Mi 인스턴스에 받아쓰기 모델을 올리지 않습니다). 인터뷰 화면에서 `녹음 파일 올리기 · 직접 입력` 을 누르고 답을 글로 적어 진행합니다. 녹음 동의를 끄면 처음부터 직접 입력으로 진행합니다. 녹음 파일은 인스턴스 임시 디스크(`/app/data/audio`)에만 있고 인스턴스가 내려가면 지워집니다.

## 8. 스모크

자동 검사: `SHOPLEGACY_SMOKE_URL=<서비스 주소> uv run pytest tests/e2e/test_deploy_smoke.py -q` → `7 passed` (로그인·쓰기 없음).

| 확인 | 방법 | 기대 | 결과 |
|---|---|---|---|
| 연결 점검 | `curl -s <서비스 주소>/readyz` (외부에서 `/healthz` 는 쓰지 않음) | 200, `{"status":"ok","db":"ok"}` | [ ] |
| 첫 화면 | 브라우저로 `<서비스 주소>/` | 제목 `가게유산` | [ ] |
| 로그인 화면 | `<서비스 주소>/login` | 200, 로그인 양식 | [ ] |
| 공개 카드 | `<서비스 주소>/cards` | 200, 카드 목록 화면 | [ ] |
| 정적 파일 | `curl -sI <서비스 주소>/static/style.css` | 200, `text/css` | [ ] |
| 없는 주소 | `<서비스 주소>/no-such-page` | 404 | [ ] |
| 가입·로그인 | 새 이메일로 가입 후 로그인 | 로그인 뒤 로그인 화면으로 되돌아가지 않음 (JWKS 검증 통과), 조직이 없으면 `/onboarding` | [ ] |
| 인터뷰 직접 입력 | 인터뷰 화면 → `녹음 파일 올리기 · 직접 입력` → 답 입력 | 마이크 없이 글 입력 칸이 열리고 답을 보낼 수 있음 | [ ] |

## 비용 상한

공개 주소로 열어도 비용이 위로 막히게 하는 장치입니다. 인프라 값은 `service.yaml` 고정 값이고, 앱 값은 평문 env 입니다(`service.yaml` 기본값, `.env.production` 에 적으면 그 값). 바꾸면 `apply-env.sh --skip-db` 로 반영합니다.

| 장치 | 설정 | 기본값 | 넘으면 |
|---|---|---|---|
| 최대 인스턴스 수 | `autoscaling.knative.dev/maxScale` | 1 (검사기가 1 초과를 막음) | 요청이 대기열에서 기다리거나 429 |
| 인스턴스당 동시 요청 | `containerConcurrency` | 20 | 다음 요청은 대기 |
| 메모리·CPU | `resources` | 512Mi · 1 CPU (요청 처리 중에만) | 메모리 초과 시 인스턴스 재시작 |
| 요청 처리 상한 | `timeoutSeconds` | 660 (실시간 받아쓰기 600초 + 여유) | 연결 끊김 |
| 요청 속도 제한 | `SHOPLEGACY_RATE_LIMIT` | `60/minute` | 429 |
| LLM 호출 제한 시간(초) | `SHOPLEGACY_LLM_TIMEOUT` | 20 | 그 호출 실패 처리 |
| 음성 업로드 최대 크기 | `SHOPLEGACY_STT_MAX_BYTES` | 26214400 (25MiB) | 업로드 거절 |
| 실시간 받아쓰기 최대 시간(초) | `SHOPLEGACY_STT_STREAM_MAX_SECONDS` | 600 | 스트림 종료 (동시 수는 `SHOPLEGACY_STT_STREAM_MAX_CONCURRENT`=2) |
| 인터뷰 한 회 시간(초) | `SHOPLEGACY_SESSION_SECONDS` | 1200 | 그 회차 마감 |

- 인스턴스 최대 1 × 동시 요청 20, 요청이 없으면 인스턴스 0, 요청 처리 중에만 CPU. 고정비가 없고, 넘더라도 인스턴스 1개 상한이 위를 막습니다.
- 하루 LLM 호출 수 예산 같은 코드 상한은 없습니다. 아래 예산 알림으로 봅니다.

## 예산 알림

코드 상한과 별개로 결제 계정에 예산 알림을 겁니다 (관리형·무료). 금액과 결제 계정 ID 는 이 파일에 적지 않습니다.

```sh
gcloud billing budgets create --billing-account=<BILLING_ACCOUNT_ID> \
  --display-name="shoplegacy monthly" --budget-amount=<월 상한 금액>KRW \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
  --filter-projects=projects/<PROJECT_ID>
```

- [ ] 결제 콘솔 → 예산 및 알림에 위 예산이 보이고, 알림 받을 이메일(결제 관리자)이 맞다
- [ ] 알림이 오면 먼저 Cloud Run 요청 수·LLM 제공자 사용량과 위 `비용 상한` 표 값을 확인합니다

## 롤백

이전 리비전으로 트래픽을 돌립니다. 자세한 내용은 `deploy/supabase/PRODUCTION.md` 8단계.

```sh
gcloud run revisions list --service=shoplegacy --region=asia-northeast3 --project=<PROJECT_ID>
gcloud run services update-traffic shoplegacy --to-revisions=<이전 리비전>=100 --region=asia-northeast3 --project=<PROJECT_ID>
```

고친 커밋을 배포한 뒤 `gcloud run services update-traffic shoplegacy --to-latest --region=asia-northeast3 --project=<PROJECT_ID>` 로 최신 리비전에 트래픽을 되돌립니다.
