#!/usr/bin/env bash
# Creates a GitHub release for the current project version.
# Creates the version tag if it does not exist yet.
# Usage: create-release.sh (no args; version comes from pyproject.toml via uv)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(readlink -f "${SCRIPT_DIR}/..")"

source "${SCRIPT_DIR}/common.sh"

die() {
    echo "error: $1" >&2
    exit 1
}

[ "${1:-}" = "--help" ] && {
    echo "Usage: $0"
    echo "Creates a GitHub release for the version in pyproject.toml."
    echo "Creates the version tag if it does not exist yet."
    exit 0
}

[ $# -eq 0 ] || die "takes no arguments (version comes from pyproject.toml)"

command -v gh >/dev/null 2>&1 || die "gh not found"

cd "${ROOT_DIR}"

[ -z "$(git status --porcelain)" ] || die "uncommitted changes, commit or stash first"

VERSION="$(uv version --short)"
[ -n "${VERSION}" ] || die "could not read version from pyproject.toml"

if gh release view "${VERSION}" >/dev/null 2>&1; then
    die "release '${VERSION}' already exists"
fi

echo "${YELLOW}Creating GitHub release ${VERSION}...${RESET}"
gh release create "${VERSION}" --generate-notes
git fetch --tags
