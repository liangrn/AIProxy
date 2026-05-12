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

exec python -m uvicorn app.main:app --host "$LISTEN_HOST" --port "$LISTEN_PORT"
