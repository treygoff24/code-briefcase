#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: scripts/check.sh [--quick|--full]

Runs the repository quality gate.

  --quick   Run commit-time checks: Black, Ruff, and mypy.
  --full    Run quick checks plus the full pytest suite. This is the default.
USAGE
}

mode="full"
case "${1:-}" in
    "" | "--full")
        mode="full"
        ;;
    "--quick")
        mode="quick"
        ;;
    "-h" | "--help")
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$repo_root"

if [[ -n "${PYTHON:-}" ]]; then
    python_bin="$PYTHON"
elif [[ -x "./.venv/bin/python" ]]; then
    python_bin="./.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    python_bin="python3"
else
    python_bin="python"
fi

targets=(code_briefcase tests scripts)

run() {
    printf '\n==> %s\n' "$*"
    "$@"
}

run "$python_bin" -m black --check "${targets[@]}"
run "$python_bin" -m ruff check "${targets[@]}"
run "$python_bin" -m mypy "${targets[@]}"

if [[ "$mode" == "full" ]]; then
    run "$python_bin" -m pytest
fi
