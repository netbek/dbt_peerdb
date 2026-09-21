#!/usr/bin/env bash
# Fetches pinned PyPI and git reference sources into vendor/ for upgrade diffing.
# Usage: install.sh (no args; pins are inline)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(readlink -f "${SCRIPT_DIR}/..")"

source "${SCRIPT_DIR}/common.sh"

# Fetches one package when the pinned version is absent, else skips.
python-fetch() {
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

# Clones the repo at the tag, or checks out the tag if already cloned, discarding local changes.
git-fetch() {
    local dir="$1"
    local repo="$2"
    local tag="$3"
    if [ -d "${dir}/.git" ]; then
        echo "${YELLOW}${dir} already cloned, checking out ${tag}...${RESET}"
        git -C "${dir}" fetch --tags --force
        git -C "${dir}" checkout --force "${tag}"
        git -C "${dir}" reset --hard "${tag}"
        git -C "${dir}" clean -fdx
    else
        echo "${YELLOW}Cloning ${repo} at ${tag} into ${dir}...${RESET}"
        rm -rf "${dir}"
        git clone --branch "${tag}" --depth 1 "${repo}" "${dir}"
    fi
}

cd "${ROOT_DIR}"

# Fetch vendor projects
git-fetch vendor/dbt https://github.com/dbt-labs/dbt-core v1.11.14
python-fetch vendor/dbt-adapters dbt-adapters 1.22.10
git-fetch vendor/dbt-clickhouse https://github.com/ClickHouse/dbt-clickhouse v1.10.2
git-fetch vendor/peerdb https://github.com/PeerDB-io/peerdb v0.37.1
