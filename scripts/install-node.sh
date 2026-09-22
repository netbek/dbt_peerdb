#!/usr/bin/env bash
# Install Node dependencies
# Usage: install-node.sh (no args)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(readlink -f "${SCRIPT_DIR}/..")"

source "${SCRIPT_DIR}/common.sh"

# Install Node dependencies
install_node() {
    cd "${ROOT_DIR}"
    rm -fr node_modules && pnpm install
}

install_node
