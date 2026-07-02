#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SELF="$(realpath "$0")"

if command -v graphify >/dev/null 2>&1; then
  SYSTEM_GRAPHIFY="$(command -v graphify)"
  if [ -n "$SYSTEM_GRAPHIFY" ] && [ "$(realpath "$SYSTEM_GRAPHIFY")" != "$SELF" ]; then
    exec "$SYSTEM_GRAPHIFY" "$@"
  fi
fi

if [ -x "$REPO_ROOT/.venv/bin/graphify" ]; then
  exec "$REPO_ROOT/.venv/bin/graphify" "$@"
fi

if [ -x "$REPO_ROOT/graphify-runner-venv/bin/graphify" ]; then
  exec "$REPO_ROOT/graphify-runner-venv/bin/graphify" "$@"
fi

exec python3 -m graphify "$@"
