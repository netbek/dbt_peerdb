#!/usr/bin/env bash
# Fetch vendor projects
# Usage: install-vendor.sh (no args)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(readlink -f "${SCRIPT_DIR}/..")"

source "${SCRIPT_DIR}/common.sh"

# Fetch vendor projects
install_vendor() {
    cd "${ROOT_DIR}"
    git_fetch vendor/dbt https://github.com/dbt-labs/dbt-core v1.12.5
    git_fetch vendor/dbt-adapters https://github.com/dbt-labs/dbt-adapters main dbt-adapters
    git_fetch vendor/dbt-clickhouse https://github.com/ClickHouse/dbt-clickhouse v1.10.3
    git_fetch vendor/peerdb https://github.com/PeerDB-io/peerdb v0.37.10
}

install_vendor
