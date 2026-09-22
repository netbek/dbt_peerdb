#!/usr/bin/env bash
set -euo pipefail

RED=$(tput setaf 1)
GREEN=$(tput setaf 2)
YELLOW=$(tput setaf 3)
CYAN=$(tput setaf 6)
RESET=$(tput sgr0)

# Clones the repo at the tag, or checks out the tag if already cloned, discarding local changes.
# Usage: git_fetch <dir> <repo-url> <tag> [subdir]
# When subdir is given, checks out only that subdirectory via sparse checkout.
git_fetch() {
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
