#!/usr/bin/env bash
# Initialise and update vendor submodules
# Usage: install-vendor.sh (no args)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(readlink -f "${SCRIPT_DIR}/..")"

source "${SCRIPT_DIR}/common.sh"

# Initialise and update vendor submodules.
install_vendor() {
    cd "${ROOT_DIR}"
    echo "${YELLOW}Updating vendor submodules...${RESET}"
    git submodule sync --recursive
    git submodule update --init --recursive
    git submodule update --remote vendor/dbt-adapters
    echo "${GREEN}Vendor submodules ready.${RESET}"
}

install_vendor
