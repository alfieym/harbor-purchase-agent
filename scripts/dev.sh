#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PIDS=""

cleanup() {
  trap - INT TERM EXIT
  for pid in $PIDS; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

cd "$ROOT/backend"
uv run python -m harbor &
PIDS="$PIDS $!"

cd "$ROOT/frontend"
npm run dev &
PIDS="$PIDS $!"

wait
