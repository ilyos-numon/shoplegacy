#!/usr/bin/env bash
# 운영 환경 반영: ENV_FILE(.env.production) 값을 Supabase·Secret Manager·Cloud Run 에 넣는다.
#   권한 확인 → Supabase 마이그레이션 → 앱 로그인 역할(shoplegacy_app) 비밀번호·SHOPLEGACY_DATABASE_URL
#   → Secret Manager 비밀 6개 → service.yaml 렌더(평문 env 채움, 임시 파일) → gcloud run services replace → 공개(allUsers) → /readyz 확인
# Supabase 값은 SUPABASE_URL·SUPABASE_ANON_KEY 로 받아 SHOPLEGACY_SUPABASE_URL·SHOPLEGACY_SUPABASE_ANON_KEY·SHOPLEGACY_JWKS_URL 로 렌더한다.
# 키는 새 형식(sb_publishable_…)일 수 있다. JWT 가 아니므로 해석하지 않고 그대로 넘긴다.
# 첫 배포와 env·비밀 변경 때 쓴다. 코드 변경 배포는 GitHub Actions(.github/workflows/deploy.yml)가 이미지만 바꾼다.
# 값은 화면에 출력하지 않는다. 여러 번 돌려도 된다 (비어 있는 JWT·피드백·개인정보 키 비밀은 버전이 없을 때만 만든다).
#
# 사용: [ENV_FILE=.env.production] [IMAGE=<이미지 주소>] bash deploy/gcp/apply-env.sh [--skip-db] [--skip-migrate] [--dry-run]
#   --skip-db  권한 확인·마이그레이션·shoplegacy_app 비밀번호를 건너뛰고 기존 SHOPLEGACY_DATABASE_URL 비밀을 그대로 쓴다
#   --skip-migrate  권한 확인과 supabase db push 만 건너뛴다 (통합 DB 의 shoplegacy 스키마처럼 마이그레이션을 다른 곳에서 할 때). 비밀번호·비밀·배포·확인은 그대로
#   --dry-run  외부 명령(supabase·psql·gcloud·curl)을 실행하지 않고 할 일만 출력
# 이미지: IMAGE 환경변수, 없으면 지금 서비스가 쓰는 이미지. 둘 다 없으면 이미지를 먼저 push 하라고 안내하고 끝낸다.
set -euo pipefail

SKIP_DB=0
SKIP_MIGRATE=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --skip-db) SKIP_DB=1 ;;
    --skip-migrate) SKIP_MIGRATE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env.production}"
IMAGE_ARG="${IMAGE:-}"

if [ ! -f "$ENV_FILE" ]; then
  echo "✗ 환경 파일이 없습니다: $ENV_FILE (deploy/env.production.example 을 복사해 채우세요)" >&2
  exit 1
fi
# KEY=VALUE 줄만 읽는다 (셸로 실행하지 않으므로 값에 < > & 같은 문자가 있어도 된다). 값을 감싼 따옴표 한 쌍은 벗긴다
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
    key="${BASH_REMATCH[2]}"
    val="${BASH_REMATCH[3]}"
    if [[ "$val" =~ ^\"(.*)\"$ || "$val" =~ ^\'(.*)\'$ ]]; then val="${BASH_REMATCH[1]}"; fi
    export "$key=$val"
  fi
done < "$ENV_FILE"
unset line key val

need() {
  local n
  for n in "$@"; do
    if [ -z "${!n:-}" ]; then echo "✗ $ENV_FILE 에 $n 이 비어 있습니다" >&2; exit 1; fi
  done
}
need GCP_PROJECT_ID SUPABASE_URL SUPABASE_ANON_KEY SHOPLEGACY_LLM_API_KEY

P="$GCP_PROJECT_ID"
R="${GCP_REGION:-asia-northeast3}"
SVC="${CLOUD_RUN_SERVICE:-shoplegacy}"

SUPABASE_URL="${SUPABASE_URL%/}"
REF="$(printf '%s' "$SUPABASE_URL" | sed -E 's#^https://([a-z0-9]+)\.supabase\.co$#\1#')"
if [ "$REF" = "$SUPABASE_URL" ]; then
  echo "✗ SUPABASE_URL 은 https://<project-ref>.supabase.co 형식이어야 합니다" >&2
  exit 1
fi

if [ "$SKIP_DB" -eq 0 ]; then
  need SUPABASE_POOLER_URL
  case "$SUPABASE_POOLER_URL" in
    *pooler.supabase.com*) ;;
    *) echo "✗ SUPABASE_POOLER_URL 이 Session pooler 주소(…pooler.supabase.com:5432)가 아닙니다" >&2; exit 1 ;;
  esac
  case "$SUPABASE_POOLER_URL" in
    *YOUR-PASSWORD*) echo "✗ SUPABASE_POOLER_URL 의 [YOUR-PASSWORD] 를 DB 비밀번호로 바꿔 주세요" >&2; exit 1 ;;
  esac
fi

# 외부 명령: dry-run 이면 출력만
run() {
  echo "+ $*"
  if [ "$DRY_RUN" -eq 0 ]; then "$@"; fi
}

if [ "$DRY_RUN" -eq 1 ]; then echo "(dry-run: 외부 명령을 실행하지 않습니다)"; fi
echo "프로젝트 $P · 리전 $R · 서비스 $SVC · 환경 파일 $ENV_FILE"

# 이미지
if [ -n "$IMAGE_ARG" ]; then
  IMG="$IMAGE_ARG"
elif [ "$DRY_RUN" -eq 1 ]; then
  IMG="<현재 서비스 이미지>"
  echo "+ gcloud run services describe $SVC --region=$R --project=$P --format=value(spec.template.spec.containers[0].image)"
else
  IMG="$(gcloud run services describe "$SVC" --region="$R" --project="$P" \
    --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
fi
if [ -z "$IMG" ]; then
  echo "✗ 배포할 이미지가 없습니다. 서비스가 아직 없으면 이미지를 먼저 빌드·push 하고 IMAGE 로 주세요:" >&2
  echo "  IMAGE=$R-docker.pkg.dev/$P/<ARTIFACT_REPO>/$SVC:<tag> bash deploy/gcp/apply-env.sh" >&2
  exit 1
fi
echo "이미지: $IMG"

# 공개 주소
if [ -n "${SHOPLEGACY_PUBLIC_BASE_URL:-}" ]; then
  BASE_URL="${SHOPLEGACY_PUBLIC_BASE_URL%/}"
else
  if [ "$DRY_RUN" -eq 1 ]; then
    PROJECT_NUMBER="<PROJECT_NUMBER>"
    echo "+ gcloud projects describe $P --format=value(projectNumber)"
  else
    PROJECT_NUMBER="$(gcloud projects describe "$P" --format='value(projectNumber)')"
  fi
  BASE_URL="https://$SVC-$PROJECT_NUMBER.$R.run.app"
fi
echo "SHOPLEGACY_PUBLIC_BASE_URL: $BASE_URL"

echo "0/6 마이그레이션 권한 확인 (anon·authenticated 역할 관리 권한)"
if [ "$SKIP_DB" -eq 1 ]; then
  echo "  · --skip-db: 건너뜀"
elif [ "$SKIP_MIGRATE" -eq 1 ]; then
  echo "  · --skip-migrate: 건너뜀"
elif [ "$DRY_RUN" -eq 1 ]; then
  echo "+ psql <SUPABASE_POOLER_URL> -Atc \"select pg_has_role(current_user,'anon','MEMBER WITH ADMIN OPTION') and pg_has_role(current_user,'authenticated','MEMBER WITH ADMIN OPTION')\"  → t 기대"
else
  can="$(psql "$SUPABASE_POOLER_URL" -Atc "select pg_has_role(current_user,'anon','MEMBER WITH ADMIN OPTION') and pg_has_role(current_user,'authenticated','MEMBER WITH ADMIN OPTION')")"
  if [ "$can" != "t" ]; then
    echo "✗ 이 DB 계정은 anon·authenticated 역할을 부여할 수 없습니다 (마이그레이션의 GRANT anon, authenticated TO shoplegacy_app 이 실패)." >&2
    echo "  Supabase 대시보드 SQL Editor 에서 supabase/migrations 의 첫 파일(…_schema.sql) 역할 부분" >&2
    echo "  (CREATE ROLE shoplegacy_app … ; GRANT anon, authenticated TO shoplegacy_app;) 을 먼저 실행한 뒤 다시 돌리세요." >&2
    exit 1
  fi
  echo "  ✓ 권한 있음"
fi

echo "1/6 Supabase 마이그레이션 (스키마·RLS·작업 대기열)"
if [ "$SKIP_DB" -eq 1 ]; then
  echo "  · --skip-db: 건너뜀"
elif [ "$SKIP_MIGRATE" -eq 1 ]; then
  echo "  · --skip-migrate: 건너뜀 (통합 DB 는 마이그레이션을 따로 적용)"
else
  echo "+ supabase db push --db-url <SUPABASE_POOLER_URL> --yes"
  if [ "$DRY_RUN" -eq 0 ]; then
    (cd "$ROOT" && supabase db push --db-url "$SUPABASE_POOLER_URL" --yes)
  fi
fi

echo "2/6 앱 로그인 역할(shoplegacy_app) 비밀번호·SHOPLEGACY_DATABASE_URL"
DB_URL_NEW=""
if [ "$SKIP_DB" -eq 1 ]; then
  echo "  · --skip-db: 기존 SHOPLEGACY_DATABASE_URL 비밀 유지"
elif [ "$DRY_RUN" -eq 1 ]; then
  echo "+ psql <SUPABASE_POOLER_URL> (alter role shoplegacy_app login password <새 무작위 값>)"
  echo "+ psql <SHOPLEGACY_DATABASE_URL> -Atc 'select current_user'  → shoplegacy_app 확인"
else
  ROLE_PW="$(openssl rand -hex 24)"
  printf "alter role shoplegacy_app login password '%s';\n" "$ROLE_PW" \
    | psql "$SUPABASE_POOLER_URL" -v ON_ERROR_STOP=1 -q -f - >/dev/null
  DB_URL_NEW="postgresql://shoplegacy_app.$REF:$ROLE_PW@${SUPABASE_POOLER_URL##*@}"
  unset ROLE_PW
  if [ "$(psql "$DB_URL_NEW" -Atc 'select current_user')" != "shoplegacy_app" ]; then
    echo "✗ shoplegacy_app 으로 접속하지 못했습니다" >&2
    exit 1
  fi
  echo "  ✓ shoplegacy_app 으로 접속됨"
fi

echo "3/6 Secret Manager 비밀 6개"
has_version() {
  [ -n "$(gcloud secrets versions list "$1" --filter='state=ENABLED' --format='value(name)' --limit=1 --project="$P" 2>/dev/null)" ]
}
# put 이름 값 : 비밀이 없으면 만들고, 최신 값과 다를 때만 새 버전을 더한다
put() {
  local name="$1" value="$2"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "+ gcloud secrets versions add $name --data-file=- --project=$P   (값은 표준입력, 최신 값과 다를 때만)"
    return
  fi
  gcloud secrets describe "$name" --project="$P" >/dev/null 2>&1 \
    || gcloud secrets create "$name" --replication-policy=automatic --project="$P" >/dev/null
  if [ "$(gcloud secrets versions access latest --secret="$name" --project="$P" 2>/dev/null || true)" = "$value" ]; then
    echo "  · $name 유지 (같은 값)"
    return
  fi
  printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --project="$P" >/dev/null
  echo "  ✓ $name"
}
# keep_or_put 이름 값 : 값이 있으면 넣고, 비었으면 버전이 없을 때만 "unused" 를 넣는다 (Cloud Run 은 없는 비밀을 참조하면 배포 실패)
keep_or_put() {
  local name="$1" value="${2:-}"
  if [ -n "$value" ]; then put "$name" "$value"; return; fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "+ $name: 값이 비어 있음 → 버전이 없을 때만 unused"
    return
  fi
  if has_version "$name"; then echo "  · $name 유지"; else put "$name" "unused"; fi
}
# generate_or_put 이름 값 명령... : 값이 있으면 넣고, 비었으면 버전이 없을 때만 명령으로 무작위 값을 만든다
generate_or_put() {
  local name="$1" value="$2"
  shift 2
  if [ -n "$value" ]; then put "$name" "$value"; return; fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "+ $name: 값이 비어 있음 → 버전이 없을 때만 새 무작위 값"
    return
  fi
  if has_version "$name"; then echo "  · $name 유지"; else put "$name" "$("$@")"; fi
}
# Fernet 키 형식 (urlsafe base64, 32바이트)
fernet_key() { openssl rand -base64 32 | tr '+/' '-_'; }

if [ -n "$DB_URL_NEW" ]; then
  put SHOPLEGACY_DATABASE_URL "$DB_URL_NEW"
elif [ "$DRY_RUN" -eq 1 ]; then
  if [ "$SKIP_DB" -eq 1 ]; then echo "+ SHOPLEGACY_DATABASE_URL: 기존 버전 확인"; else put SHOPLEGACY_DATABASE_URL "<새 SHOPLEGACY_DATABASE_URL>"; fi
elif has_version SHOPLEGACY_DATABASE_URL; then
  echo "  · SHOPLEGACY_DATABASE_URL 유지"
else
  echo "✗ SHOPLEGACY_DATABASE_URL 비밀에 값이 없습니다. --skip-db 없이 다시 돌리세요" >&2
  exit 1
fi
unset DB_URL_NEW
generate_or_put SHOPLEGACY_JWT_SECRET "${SHOPLEGACY_JWT_SECRET:-}" openssl rand -base64 48
generate_or_put SHOPLEGACY_FEEDBACK_SECRET "${SHOPLEGACY_FEEDBACK_SECRET:-}" openssl rand -hex 32
generate_or_put SHOPLEGACY_PII_KEYS "${SHOPLEGACY_PII_KEYS:-}" fernet_key
put SHOPLEGACY_LLM_API_KEY "$SHOPLEGACY_LLM_API_KEY"
keep_or_put SHOPLEGACY_TOSS_SECRET_KEY "${SHOPLEGACY_TOSS_SECRET_KEY:-}"

echo "4/6 Cloud Run 설정 렌더 (저장소 파일은 바꾸지 않음)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/shoplegacy-service.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
OUT="$TMP_DIR/service.yaml"
# 평문 env 로 채울 수 있는 키 = 예시 파일 ENV 구역의 키 중 ENV_FILE 에 값이 있는 것
FILE_KEYS="$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Z][A-Z0-9_]*)=.*/\2/p' "$ENV_FILE" | sort -u | tr '\n' ' ')"
# Supabase 입력(DEPLOY 구역) → 앱 변수. ENV_FILE 에 SHOPLEGACY_* 값이 따로 있으면 그 값이 우선
SHOPLEGACY_SUPABASE_URL="${SHOPLEGACY_SUPABASE_URL:-$SUPABASE_URL}"
SHOPLEGACY_SUPABASE_ANON_KEY="${SHOPLEGACY_SUPABASE_ANON_KEY:-$SUPABASE_ANON_KEY}"
SHOPLEGACY_JWKS_URL="${SHOPLEGACY_JWKS_URL:-$SUPABASE_URL/auth/v1/.well-known/jwks.json}"
export SHOPLEGACY_SUPABASE_URL SHOPLEGACY_SUPABASE_ANON_KEY SHOPLEGACY_JWKS_URL
FILE_KEYS="$FILE_KEYS SHOPLEGACY_SUPABASE_URL SHOPLEGACY_SUPABASE_ANON_KEY SHOPLEGACY_JWKS_URL"
export FILE_KEYS IMG P BASE_URL REF OUT ROOT DRY_RUN
python3 - <<'PY'
import json, os, re, sys
root = os.environ['ROOT']
section, env_keys = None, []
for line in open(os.path.join(root, 'deploy/env.production.example'), encoding='utf-8'):
    m = re.match(r'^# === (\w+)', line)
    if m:
        section = m.group(1)
        continue
    m = re.match(r'^([A-Z][A-Z0-9_]*)=', line)
    if m and section == 'ENV':
        env_keys.append(m.group(1))
file_keys = set(os.environ['FILE_KEYS'].split())
src = ''.join(l for l in open(os.path.join(root, 'deploy/gcp/service.yaml'), encoding='utf-8')
              if not l.lstrip().startswith('#'))
src = (src.replace('${IMAGE}', os.environ['IMG']).replace('<PROJECT_ID>', os.environ['P'])
          .replace('https://<도메인>', os.environ['BASE_URL']).replace('<project-ref>', os.environ['REF']))
filled = []
for k in env_keys:
    if k == 'SHOPLEGACY_PUBLIC_BASE_URL' or k not in file_keys:
        continue
    v = os.environ.get(k, '').strip()
    if not v:
        continue
    pat = re.compile(r'(- name: ' + k + r'\n\s+value: ).*\n')
    if pat.search(src):
        src = pat.sub(lambda m: m.group(1) + json.dumps(v, ensure_ascii=False) + '\n', src, count=1)
    else:
        src = src.replace('          env:\n', '          env:\n            - name: %s\n              value: %s\n'
                          % (k, json.dumps(v, ensure_ascii=False)), 1)
    filled.append(k)
left = sorted(set(re.findall(r'<PROJECT_ID>|<PROJECT_NUMBER>|<도메인>|<project-ref>|\$\{\w+\}', src)))
if left:
    if os.environ['DRY_RUN'] != '1':
        sys.exit('✗ service.yaml 에 자리표시가 남았습니다: ' + ', '.join(left))
    print('  (dry-run: 입력이 자리표시라 남은 자리표시 ' + ', '.join(left) + ')')
open(os.environ['OUT'], 'w', encoding='utf-8').write(src)
print('  ENV_FILE 에서 채운 평문 env: ' + (', '.join(filled) if filled else '(없음)'))
PY

echo "5/6 Cloud Run 반영·공개"
run gcloud run services replace "$OUT" --region="$R" --project="$P"
run gcloud run services add-iam-policy-binding "$SVC" --region="$R" --project="$P" \
  --member=allUsers --role=roles/run.invoker

echo "6/6 확인"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "+ curl $BASE_URL/readyz  → 200 기대"
  echo "(dry-run 끝: 외부 명령을 실행하지 않았습니다)"
  exit 0
fi
URL="$(gcloud run services describe "$SVC" --region="$R" --project="$P" --format='value(status.url)')"
echo "  서비스 주소: $URL"
if [ "${URL%/}" != "$BASE_URL" ]; then
  echo "  ! 서비스 주소가 SHOPLEGACY_PUBLIC_BASE_URL 과 다릅니다. $ENV_FILE 에 SHOPLEGACY_PUBLIC_BASE_URL=$URL 을 적고 --skip-db 로 다시 돌리세요" >&2
fi
code=""
for _ in 1 2 3 4 5 6; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$URL/readyz" || true)"
  if [ "$code" = "200" ]; then break; fi
  sleep 5
done
echo "  /readyz → $code"
if [ "$code" != "200" ]; then exit 1; fi
echo "완료: $URL"
