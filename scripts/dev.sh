#!/usr/bin/env bash
# 로컬 E2E 개발 실행: smee 터널 + 웹훅 서버를 함께 띄운다.
#
#   ./scripts/dev.sh
#
# 종료하면 둘 다 정리된다.

set -euo pipefail
cd "$(dirname "$0")/.."

SMEE_URL="${SMEE_URL:-https://smee.io/UnkcfFZ6Agkj0ID3}"
PORT="${PORT:-8000}"

if [[ ! -f .env ]]; then
  echo ".env 가 없습니다. .env.example 을 복사해서 채우세요." >&2
  exit 1
fi
if [[ ! -f secrets/github-app.pem ]]; then
  echo "secrets/github-app.pem 이 없습니다. GitHub App 개인키를 넣으세요." >&2
  exit 1
fi

cleanup() {
  # 자식 프로세스를 남기면 다음 실행에서 포트가 물린다
  jobs -p | xargs -r kill 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "smee 터널: $SMEE_URL -> http://localhost:$PORT/webhook"
npx --yes smee-client --url "$SMEE_URL" --target "http://localhost:$PORT/webhook" &

echo "웹훅 서버: http://localhost:$PORT"
uv run uvicorn crbot.main:app --host 0.0.0.0 --port "$PORT" --reload &

wait
