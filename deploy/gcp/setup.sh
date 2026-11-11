#!/usr/bin/env bash
# GCP 준비 (여러 번 돌려도 된다: 이미 있는 것은 건너뜀).
#   API 켜기 → Artifact Registry 저장소 → 런타임 SA(shoplegacy-run)·비밀 접근 권한
#   → 배포 SA(shoplegacy-deploy)·역할 → WIF 풀/프로바이더(GitHub 저장소 조건) → Secret Manager 비밀 6개 (이름만)
# 비밀 값은 이 스크립트에 없다. 값은 deploy/gcp/apply-env.sh 가 .env.production 에서 읽어 넣는다 (DB 주소·무작위 비밀은 거기서 만든다).
#
# 필요한 환경변수: GCP_PROJECT_ID GCP_REGION GITHUB_REPO(<owner>/<repo>), 선택 ARTIFACT_REPO(기본 shoplegacy)
# 사용: GCP_PROJECT_ID=<PROJECT_ID> GCP_REGION=asia-northeast3 ARTIFACT_REPO=shoplegacy GITHUB_REPO=<owner>/<repo> bash deploy/gcp/setup.sh [--dry-run]
#   --dry-run  gcloud 를 실행하지 않고 실행할 명령만 출력
set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

ARTIFACT_REPO="${ARTIFACT_REPO:-shoplegacy}"
missing=()
for name in GCP_PROJECT_ID GCP_REGION GITHUB_REPO; do
  if [ -z "${!name:-}" ]; then missing+=("$name"); fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "필수 환경변수가 비어 있습니다: ${missing[*]}" >&2
  exit 1
fi

PROJECT="$GCP_PROJECT_ID"
REGION="$GCP_REGION"
RUN_SA_NAME="shoplegacy-run"
DEPLOY_SA_NAME="shoplegacy-deploy"
RUN_SA="${RUN_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
DEPLOY_SA="${DEPLOY_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
POOL="github-pool"
PROVIDER="github-provider"
SECRETS=(SHOPLEGACY_DATABASE_URL SHOPLEGACY_JWT_SECRET SHOPLEGACY_FEEDBACK_SECRET SHOPLEGACY_PII_KEYS SHOPLEGACY_LLM_API_KEY SHOPLEGACY_TOSS_SECRET_KEY)

run() {
  echo "+ $*"
  if [ "$DRY_RUN" -eq 0 ]; then "$@"; fi
}

# 있으면 0. dry-run 에서는 gcloud 를 부르지 않고 "없음"으로 보고 만드는 명령을 출력한다
exists() {
  if [ "$DRY_RUN" -eq 1 ]; then return 1; fi
  "$@" >/dev/null 2>&1
}

skip() { echo "= 이미 있음: $*"; }

# IAM 바인딩. 새 서비스 계정은 describe 에 보인 뒤에도 몇십 초 동안 바인딩이
# "Service account ... does not exist" 로 실패할 수 있어(IAM 전파 지연) 그 오류일 때만 10초 간격 최대 6회 다시 시도한다
bind() {
  echo "+ $*"
  if [ "$DRY_RUN" -eq 1 ]; then return 0; fi
  local attempt out
  for attempt in 1 2 3 4 5 6; do
    if out="$("$@" 2>&1)"; then return 0; fi
    if [ "$attempt" -lt 6 ] && printf '%s' "$out" | grep -q 'does not exist'; then
      echo "  · 서비스 계정이 아직 IAM 에 퍼지지 않음, 10초 뒤 다시 시도 ($attempt/6)"
      sleep 10
      continue
    fi
    printf '%s\n' "$out" >&2
    return 1
  done
}

echo "# 1. API 켜기"
run gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com \
  iamcredentials.googleapis.com iam.googleapis.com sts.googleapis.com \
  --project="$PROJECT"

echo "# 2. Artifact Registry 저장소"
if exists gcloud artifacts repositories describe "$ARTIFACT_REPO" --location="$REGION" --project="$PROJECT"; then
  skip "artifact repo $ARTIFACT_REPO"
else
  run gcloud artifacts repositories create "$ARTIFACT_REPO" --repository-format=docker \
    --location="$REGION" --description="shoplegacy images" --project="$PROJECT"
fi

echo "# 3. 런타임 서비스 계정"
if exists gcloud iam service-accounts describe "$RUN_SA" --project="$PROJECT"; then
  skip "service account $RUN_SA"
else
  run gcloud iam service-accounts create "$RUN_SA_NAME" --display-name="shoplegacy runtime" --project="$PROJECT"
fi
[ "$DRY_RUN" -eq 1 ] || for _ in 1 2 3 4 5 6; do exists gcloud iam service-accounts describe "$RUN_SA" --project="$PROJECT" && break; sleep 5; done
bind gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${RUN_SA}" \
  --role=roles/secretmanager.secretAccessor --condition=None --quiet

echo "# 4. 배포 서비스 계정"
if exists gcloud iam service-accounts describe "$DEPLOY_SA" --project="$PROJECT"; then
  skip "service account $DEPLOY_SA"
else
  run gcloud iam service-accounts create "$DEPLOY_SA_NAME" --display-name="shoplegacy deploy (GitHub Actions)" --project="$PROJECT"
fi
# 새 서비스 계정은 IAM 에 보이기까지 몇 초 걸린다
[ "$DRY_RUN" -eq 1 ] || for _ in 1 2 3 4 5 6; do exists gcloud iam service-accounts describe "$DEPLOY_SA" --project="$PROJECT" && break; sleep 5; done
for role in roles/run.admin roles/artifactregistry.writer; do
  bind gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${DEPLOY_SA}" \
    --role="$role" --condition=None --quiet
done
# 배포 SA 가 런타임 SA 로 서비스를 띄울 수 있게
bind gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" --member="serviceAccount:${DEPLOY_SA}" \
  --role=roles/iam.serviceAccountUser --project="$PROJECT"

echo "# 5. Workload Identity Federation (GitHub Actions)"
if [ "$DRY_RUN" -eq 1 ]; then
  PROJECT_NUMBER="<PROJECT_NUMBER>"
else
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
fi
if exists gcloud iam workload-identity-pools describe "$POOL" --location=global --project="$PROJECT"; then
  skip "workload identity pool $POOL"
else
  run gcloud iam workload-identity-pools create "$POOL" --location=global \
    --display-name="GitHub Actions" --project="$PROJECT"
fi
if exists gcloud iam workload-identity-pools providers describe "$PROVIDER" --workload-identity-pool="$POOL" \
  --location=global --project="$PROJECT"; then
  skip "workload identity provider $PROVIDER"
else
  run gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --workload-identity-pool="$POOL" \
    --location=global --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository=='${GITHUB_REPO}'" --project="$PROJECT"
fi
bind gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}" \
  --project="$PROJECT"

echo "# 6. Secret Manager 비밀 (이름 = 환경변수 이름, 값은 deploy/gcp/apply-env.sh 가 넣는다)"
for name in "${SECRETS[@]}"; do
  if exists gcloud secrets describe "$name" --project="$PROJECT"; then
    skip "secret $name"
  else
    run gcloud secrets create "$name" --replication-policy=automatic --project="$PROJECT"
  fi
done

echo
echo "# GitHub 저장소 Variables 에 적을 값"
echo "GCP_PROJECT_ID=$PROJECT"
echo "GCP_REGION=$REGION"
echo "ARTIFACT_REPO=$ARTIFACT_REPO"
echo "GCP_WIF_PROVIDER=projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
echo "GCP_DEPLOY_SA=$DEPLOY_SA"
if [ "$DRY_RUN" -eq 1 ]; then echo "(dry-run: gcloud 를 실행하지 않았습니다)"; fi
