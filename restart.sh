#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

./stop.sh
sleep 1
exec ./run.sh
