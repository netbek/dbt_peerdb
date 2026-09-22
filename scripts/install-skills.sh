#!/usr/bin/env bash
# Install agent skills
# Usage: install-skills.sh (no args)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(readlink -f "${SCRIPT_DIR}/..")"

source "${SCRIPT_DIR}/common.sh"

# Install agent skills
install_skills() {
    cd "${ROOT_DIR}"
    pnpm exec skills-manager install --force
}

install_skills
