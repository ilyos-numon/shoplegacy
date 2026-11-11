#!/usr/bin/env bash
# Runs the golden set against the real Qwen model.
# Usage: scripts/qwen-eval.sh [--diagnose] [--record] [pytest or diagnose args...]
# Settings: SHOPLEGACY_LLM_* lines from ./.env first; the voice-agent env file
# (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL) only fills what is still empty.
# Secret values are never printed.
set -euo pipefail

cd "$(dirname "$0")/.."

MODE=eval
if [ "${1:-}" = "--diagnose" ]; then
  MODE=diagnose
  shift
fi
if [ "${1:-}" = "--record" ]; then
  # Re-record tests/golden/cassettes/<case>.yaml from the live responses.
  export SHOPLEGACY_QWEN_RECORD=1
  shift
fi

found=0
RUN=(uv run)
in_env_file() { return 1; }
if [ -f .env ]; then
  # Parse .env the way the app does (uv's dotenv parser), not with bash `source`:
  # dotenv values are not shell syntax and can come out empty or mangled in bash.
  # Variables already set in the shell take precedence over .env (uv does not override them).
  found=1
  RUN=(uv run --env-file .env)
  in_env_file() {
    uv run --env-file .env python -I -c "import os,sys; sys.exit(0 if os.environ.get('$1') else 1)"
  }
fi

have() { [ -n "${!1:-}" ] || in_env_file "$1"; }

if [ -n "${VOICE_AGENT_ENV:-}" ] || ! have SHOPLEGACY_LLM_API_KEY; then
  VA_ENV="${VOICE_AGENT_ENV:-../voice-agent/.env}"
  if [ -f "$VA_ENV" ]; then
    found=1
    # shellcheck disable=SC1090
    va() { (set -a; source "$VA_ENV" >/dev/null 2>&1; printf '%s' "${!1:-}"); }
    have SHOPLEGACY_LLM_API_KEY || export SHOPLEGACY_LLM_API_KEY="$(va LLM_API_KEY)"
    have SHOPLEGACY_LLM_BASE_URL || { v="$(va LLM_BASE_URL)"; [ -z "$v" ] || export SHOPLEGACY_LLM_BASE_URL="$v"; }
    have SHOPLEGACY_LLM_MODEL || { v="$(va LLM_MODEL)"; [ -z "$v" ] || export SHOPLEGACY_LLM_MODEL="$v"; }
  fi
fi

if [ "$found" = 0 ]; then
  echo "no env file: .env or ${VOICE_AGENT_ENV:-../voice-agent/.env}" >&2
  exit 3
fi

have SHOPLEGACY_LLM_BASE_URL || export SHOPLEGACY_LLM_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
have SHOPLEGACY_LLM_MODEL || export SHOPLEGACY_LLM_MODEL="qwen3.7-flash"

if ! have SHOPLEGACY_LLM_API_KEY; then
  echo "SHOPLEGACY_LLM_API_KEY is empty; live tests will be skipped" >&2
fi

if [ "$MODE" = diagnose ]; then
  exec "${RUN[@]}" python scripts/qwen-diagnose.py "$@"
fi

exec "${RUN[@]}" pytest tests/live/test_golden_qwen.py -q -rs -s "$@"
