#!/usr/bin/env bash
# Install pre-commit hooks
# Usage: install-precommit.sh (no args)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(readlink -f "${SCRIPT_DIR}/..")"

source "${SCRIPT_DIR}/common.sh"

# Install pre-commit hooks
install_precommit() {
    cd "${ROOT_DIR}"
    if [ -d .git ]; then pre-commit install --overwrite > /dev/null 2>&1; fi
}

install_precommit
