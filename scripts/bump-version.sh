#!/usr/bin/env bash
# Bumps the package version across manifests for a GitHub-only release.
# Takes a bump type and reads the current version from pyproject.toml via uv.
# Usage: bump-version.sh [major|minor|patch] (e.g. bump-version.sh patch)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(readlink -f "${SCRIPT_DIR}/..")"

VALID_BUMP="major minor patch"

die() {
    echo "error: $1" >&2
    exit 1
}

usage() {
    echo "Usage: $0 [major|minor|patch]"
    echo "Example: $0 patch"
}

[ "${1:-}" = "--help" ] && {
    usage
    exit 0
}

BUMP="${1:-}"
if [ -z "${BUMP}" ]; then
    usage >&2
    die "bump type is required"
fi
if ! echo "${VALID_BUMP}" | grep -qw "${BUMP}"; then
    die "invalid bump '${BUMP}', must be one of: ${VALID_BUMP}"
fi

# Replaces one fixed string in a file, failing when the string is absent
# so drift or double runs error instead of passing silently.
replace() {
    local file="$1"
    local old="$2"
    local new="$3"
    grep -qF -- "${old}" "${file}" || die "${file} has no '${old}' (already bumped or drifted?)"
    local pattern="${old//./\\.}"
    sed -i "s|${pattern}|${new}|" "${file}"
    echo "${file}: '${old}' -> '${new}'"
}

cd "${ROOT_DIR}"

OLD="$(uv version --short)"
uv version --bump "${BUMP}"
NEW="$(uv version --short)"

if [ "${NEW}" = "${OLD}" ]; then
    echo "already at ${OLD}, nothing to do"
    exit 0
fi
echo "bumped ${OLD} -> ${NEW}"

replace "package.json" "\"version\": \"${OLD}\"" "\"version\": \"${NEW}\""
replace "dbt_project.yml" "version: ${OLD}" "version: ${NEW}"
replace "README.md" "        version: ${OLD}" "        version: ${NEW}"

git add pyproject.toml uv.lock package.json dbt_project.yml README.md
git commit -m "chore(release): ${NEW}"
