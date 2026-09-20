#!/usr/bin/env bash
# Fetches pinned PyPI reference sources into vendor/ for upgrade diffing.
# Usage: install.sh (no args; pins are inline)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(readlink -f "${SCRIPT_DIR}/..")"

source "${SCRIPT_DIR}/common.sh"

# Fetches one package when the pinned version is absent, else skips.
fetch-or-sync() {
    local dir="$1"
    local dist="$2"
    local version="$3"
    if ls -d "${dir}/${dist}-${version}.dist-info" >/dev/null 2>&1; then
        echo "${YELLOW}${dir} already at ${dist}==${version}, skipping...${RESET}"
    else
        echo "${YELLOW}Fetching ${dist}==${version} into ${dir}...${RESET}"
        rm -rf "${dir}"
        mise exec --fresh-env uv -- pip install --target "${dir}" --no-deps --quiet "${dist}==${version}"
    fi
}

cd "${ROOT_DIR}"

fetch-or-sync vendor/dbt dbt-core 1.11.14
fetch-or-sync vendor/dbt-adapters dbt-adapters 1.22.10
fetch-or-sync vendor/dbt-clickhouse dbt-clickhouse 1.10.2
