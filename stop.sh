#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

: "${LISTEN_HOST:=127.0.0.1}"
: "${LISTEN_PORT:=8383}"

pids="$(lsof -tiTCP:"$LISTEN_PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -z "$pids" ]]; then
  echo "CodexProxy is not running on ${LISTEN_HOST}:${LISTEN_PORT}"
  exit 0
fi

kill $pids
echo "Stopped CodexProxy on ${LISTEN_HOST}:${LISTEN_PORT}"
