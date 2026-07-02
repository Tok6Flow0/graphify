#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHARED_ROOT="/Users/aaronsamuel/Documents/Codex/2026-07-02/do-some-deep-reading-on-this/work/graphify"
GRAPHIFY_HELPER="$SHARED_ROOT/scripts/graphify_update.py"

RUNNER_PYTHON="$SHARED_ROOT/graphify-runner-venv/bin/python3"
RUNNER_BINARY="$SHARED_ROOT/graphify-runner-venv/bin/graphify"

if [ "${1:-}" = "update" ]; then
  if [ -f "$GRAPHIFY_HELPER" ]; then
    GRAPH_TARGET="${2:-.}"
    GRAPHIFY_PYTHON="python3"
    if [ -x "$RUNNER_PYTHON" ]; then
      GRAPHIFY_PYTHON="$RUNNER_PYTHON"
    fi
    PYTHONPATH="$SHARED_ROOT${PYTHONPATH:+:$PYTHONPATH}" exec "$GRAPHIFY_PYTHON" "$GRAPHIFY_HELPER" "$GRAPH_TARGET" "$REPO_ROOT"
  fi
fi

if [ -x "${REPO_ROOT}/.venv/bin/graphify" ]; then
  exec "${REPO_ROOT}/.venv/bin/graphify" "$@"
fi

if [ -x "${REPO_ROOT}/graphify-runner-venv/bin/graphify" ]; then
  exec "${REPO_ROOT}/graphify-runner-venv/bin/graphify" "$@"
fi

if command -v graphify >/dev/null 2>&1; then
  SYSTEM_GRAPHIFY="$(command -v graphify)"
  if [ -n "${SYSTEM_GRAPHIFY}" ]; then
    exec "$SYSTEM_GRAPHIFY" "$@"
  fi
fi

if [ -x "$RUNNER_BINARY" ]; then
  exec "$RUNNER_BINARY" "$@"
fi

export PYTHONPATH="$SHARED_ROOT:${PYTHONPATH:-}"
exec python3 -m graphify "$@"
