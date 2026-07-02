#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

find_shared_root() {
  if [ -n "${GRAPHIFY_SHARED_ROOT:-}" ] && [ -f "${GRAPHIFY_SHARED_ROOT%/}/scripts/graphify_update.py" ]; then
    echo "${GRAPHIFY_SHARED_ROOT%/}"
    return 0
  fi

  local current
  current="$REPO_ROOT"
  while [ "$current" != "/" ]; do
    if [ -f "$current/work/graphify/scripts/graphify_update.py" ]; then
      echo "$current/work/graphify"
      return 0
    fi
    current="$(dirname "$current")"
  done

  return 1
}

SHARED_ROOT=""
if [ -f "${SCRIPT_DIR}/graphify_update.py" ]; then
  SHARED_ROOT="$REPO_ROOT"
else
  if root="$(find_shared_root)"; then
    SHARED_ROOT="$root"
  fi
fi

if [ -n "$SHARED_ROOT" ] && [ -f "$SHARED_ROOT/scripts/graphify_update.py" ]; then
  GRAPHIFY_HELPER="$SHARED_ROOT/scripts/graphify_update.py"
  RUNNER_PYTHON="$SHARED_ROOT/graphify-runner-venv/bin/python3"
  RUNNER_BINARY="$SHARED_ROOT/graphify-runner-venv/bin/graphify"
else
  GRAPHIFY_HELPER=""
  RUNNER_PYTHON=""
  RUNNER_BINARY=""
fi

if [ "${1:-}" = "update" ] && [ -n "$GRAPHIFY_HELPER" ]; then
  GRAPH_TARGET="${2:-.}"
  GRAPHIFY_PYTHON="python3"
  if [ -x "$RUNNER_PYTHON" ]; then
    GRAPHIFY_PYTHON="$RUNNER_PYTHON"
  fi
  PYTHONPATH="$SHARED_ROOT${PYTHONPATH:+:$PYTHONPATH}" exec "$GRAPHIFY_PYTHON" "$GRAPHIFY_HELPER" "$GRAPH_TARGET" "$REPO_ROOT"
fi

if [ -x "${REPO_ROOT}/.venv/bin/graphify" ]; then
  exec "${REPO_ROOT}/.venv/bin/graphify" "$@"
fi

if [ -x "${REPO_ROOT}/graphify-runner-venv/bin/graphify" ]; then
  exec "${REPO_ROOT}/graphify-runner-venv/bin/graphify" "$@"
fi

if [ -x "$RUNNER_BINARY" ]; then
  exec "$RUNNER_BINARY" "$@"
fi

if command -v graphify >/dev/null 2>&1; then
  SYSTEM_GRAPHIFY="$(command -v graphify)"
  if [ -n "${SYSTEM_GRAPHIFY}" ]; then
    exec "$SYSTEM_GRAPHIFY" "$@"
  fi
fi

if [ -n "$SHARED_ROOT" ] && [ -f "$GRAPHIFY_HELPER" ]; then
  export PYTHONPATH="$SHARED_ROOT${PYTHONPATH:+:$PYTHONPATH}"
  exec python3 -m graphify "$@"
fi

echo "graphify is unavailable. Install a local graphify dependency or add scripts/graphify.sh plus scripts/graphify_update.py."
exit 1
