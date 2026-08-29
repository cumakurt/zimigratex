#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/bootstrap.sh
. "$ROOT/scripts/bootstrap.sh"

export ZIMIGRATE_ROOT="$ROOT"
run_zimigrate export "$@"
