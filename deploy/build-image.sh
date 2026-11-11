#!/usr/bin/env bash
# 운영 이미지 빌드 (로컬·CI 공용). Cloud Run 은 linux/amd64 이미지만 돌리므로 Apple Silicon 에서도 플랫폼을 고정한다.
#   빌드 컨텍스트 = 임시 폴더에 git archive 두 번: shoplegacy HEAD → shoplegacy/, handoff(ci.yml 이 고정한 커밋) → handoff/
#   추적 파일만 들어가므로 .env·data/·.venv 가 이미지에 들어갈 수 없고, 워크스페이스 전체를 업로드하지 않는다.
#   커밋하지 않은 변경은 이미지에 들어가지 않는다 (HEAD 기준).
#
# 사용: [IMAGE=<태그>] [HANDOFF_REF=<커밋>] [HANDOFF_DIR=<경로>] bash deploy/build-image.sh [--push]
#   IMAGE        기본 shoplegacy:dev. push 할 때는 asia-northeast3-docker.pkg.dev/<PROJECT_ID>/shoplegacy/shoplegacy:<git sha>
#   HANDOFF_REF  기본값 = .github/workflows/ci.yml 의 첫 40자 `ref:` 해시
#   HANDOFF_DIR  기본 ../handoff (저장소 옆 폴더)
#   --push       빌드 후 docker push (먼저 gcloud auth configure-docker asia-northeast3-docker.pkg.dev)
set -euo pipefail

PUSH=0
for arg in "$@"; do
  case "$arg" in
    --push) PUSH=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HANDOFF_DIR="${HANDOFF_DIR:-$ROOT/../handoff}"
IMAGE="${IMAGE:-shoplegacy:dev}"
HANDOFF_REF="${HANDOFF_REF:-$(sed -nE 's/^[[:space:]]*ref:[[:space:]]*([0-9a-f]{40}).*/\1/p' "$ROOT/.github/workflows/ci.yml" | head -1)}"

if [ -z "$HANDOFF_REF" ]; then
  echo "✗ handoff 커밋을 찾지 못했습니다 (.github/workflows/ci.yml 의 ref: 또는 HANDOFF_REF)" >&2
  exit 1
fi
if [ ! -d "$HANDOFF_DIR/.git" ] && ! git -C "$HANDOFF_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  echo "✗ handoff 저장소가 없습니다: $HANDOFF_DIR (HANDOFF_DIR 로 경로를 주세요)" >&2
  exit 1
fi
if ! git -C "$HANDOFF_DIR" cat-file -e "${HANDOFF_REF}^{commit}" 2>/dev/null; then
  echo "handoff 커밋 $HANDOFF_REF 이 로컬에 없어 가져옵니다"
  if ! git -C "$HANDOFF_DIR" fetch origin "$HANDOFF_REF"; then
    echo "✗ handoff 커밋 $HANDOFF_REF 을 가져오지 못했습니다. $HANDOFF_DIR 에서 git fetch 후 다시 돌리세요" >&2
    exit 1
  fi
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/shoplegacy" "$TMP/handoff"
git -C "$ROOT" archive HEAD | tar -x -C "$TMP/shoplegacy"
git -C "$HANDOFF_DIR" archive "$HANDOFF_REF" | tar -x -C "$TMP/handoff"

echo "이미지 $IMAGE · shoplegacy $(git -C "$ROOT" rev-parse --short HEAD) · handoff ${HANDOFF_REF:0:7}"
docker build --platform linux/amd64 -f "$TMP/shoplegacy/Dockerfile" -t "$IMAGE" "$TMP"

if [ "$PUSH" -eq 1 ]; then
  docker push "$IMAGE"
fi
echo "완료: $IMAGE"
