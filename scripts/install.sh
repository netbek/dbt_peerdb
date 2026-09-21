#!/usr/bin/env bash
# Fetches pinned git reference sources into vendor/ for upgrade diffing.
# Usage: install.sh (no args; pins are inline)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(readlink -f "${SCRIPT_DIR}/..")"

source "${SCRIPT_DIR}/common.sh"

# Clones the repo at the tag, or checks out the tag if already cloned, discarding local changes.
# Usage: git-fetch <dir> <repo-url> <tag> [subdir]
# When subdir is given, checks out only that subdirectory via sparse checkout.
git-fetch() {
    local dir="$1"
    local repo="$2"
    local tag="$3"
    local subdir="${4:-}"
    if [ -d "${dir}/.git" ]; then
        echo "${YELLOW}${dir} already cloned, checking out ${tag}${subdir:+ (${subdir} only)}...${RESET}"
        git -C "${dir}" fetch --tags --force
        if [ -n "${subdir}" ]; then
            git -C "${dir}" sparse-checkout set --cone "${subdir}"
        else
            git -C "${dir}" sparse-checkout disable || true
        fi
        git -C "${dir}" checkout --force "${tag}"
        git -C "${dir}" reset --hard "${tag}"
        git -C "${dir}" clean -fdx
    else
        echo "${YELLOW}Cloning ${repo} at ${tag} into ${dir}${subdir:+ (${subdir} only)}...${RESET}"
        rm -rf "${dir}"
        if [ -n "${subdir}" ]; then
            git clone --branch "${tag}" --depth 1 --filter=blob:none --sparse "${repo}" "${dir}"
            git -C "${dir}" sparse-checkout set --cone "${subdir}"
        else
            git clone --branch "${tag}" --depth 1 "${repo}" "${dir}"
        fi
    fi
}

cd "${ROOT_DIR}"

# Fetch vendor projects
git-fetch vendor/dbt https://github.com/dbt-labs/dbt-core v1.11.14
git-fetch vendor/dbt-adapters https://github.com/dbt-labs/dbt-adapters main dbt-adapters
git-fetch vendor/dbt-clickhouse https://github.com/ClickHouse/dbt-clickhouse v1.10.2
git-fetch vendor/peerdb https://github.com/PeerDB-io/peerdb v0.37.1
