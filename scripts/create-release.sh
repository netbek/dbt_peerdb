#!/usr/bin/env bash
# Creates a GitHub release for the current project version.
# Expects the version tag to exist locally and on origin already.
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
    echo "The version tag must exist locally and on origin first."
    exit 0
}

[ $# -eq 0 ] || die "takes no arguments (version comes from pyproject.toml)"

command -v gh >/dev/null 2>&1 || die "gh not found"

cd "${ROOT_DIR}"

[ -z "$(git status --porcelain)" ] || die "uncommitted changes, commit or stash first"

VERSION="$(uv version --short)"
[ -n "${VERSION}" ] || die "could not read version from pyproject.toml"

git rev-parse -q --verify "refs/tags/${VERSION}" >/dev/null \
    || die "local tag '${VERSION}' missing, create it first"
git ls-remote --tags origin | grep -qw "${VERSION}" \
    || die "tag '${VERSION}' not on origin, push it first"
if gh release view "${VERSION}" >/dev/null 2>&1; then
    die "release '${VERSION}' already exists"
fi

echo "${YELLOW}Creating GitHub release ${VERSION}...${RESET}"
gh release create "${VERSION}" --generate-notes
git fetch --tags
