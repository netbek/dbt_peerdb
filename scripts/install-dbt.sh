#!/usr/bin/env bash
# Install dbt dependencies for tests
# Usage: install-dbt.sh (no args)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(readlink -f "${SCRIPT_DIR}/..")"

source "${SCRIPT_DIR}/common.sh"

# Install dbt dependencies for tests
install_dbt() {
    cd "${ROOT_DIR}/tests/fixtures/dbt"
    dbt deps
}

install_dbt
