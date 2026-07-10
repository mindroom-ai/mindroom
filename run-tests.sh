#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

section() {
    printf '\n%s\n' "$1"
}

find_command() {
    local command_name="$1"
    shift

    local command_path
    command_path="$(command -v "$command_name" 2>/dev/null || true)"
    if [[ -n "$command_path" && -x "$command_path" ]]; then
        printf '%s\n' "$command_path"
        return
    fi

    for command_path in "$@"; do
        if [[ -n "$command_path" && -x "$command_path" ]]; then
            printf '%s\n' "$command_path"
            return
        fi
    done

    printf 'Missing required command: %s\n' "$command_name" >&2
    return 127
}

find_real_node() {
    local command_path
    command_path="$(command -v node 2>/dev/null || true)"

    for command_path in "$command_path" "$@"; do
        if [[ -n "$command_path" && -x "$command_path" ]] && \
            "$command_path" -e 'process.exit(process.versions.bun ? 1 : 0)' >/dev/null 2>&1; then
            printf '%s\n' "$command_path"
            return
        fi
    done

    printf 'Missing required command: node (a real Node.js runtime, not the Bun compatibility layer)\n' >&2
    return 127
}

UV_BIN="$(
    find_command uv \
        "${HOME:+${HOME}/.local/bin/uv}" \
        "${HOME:+${HOME}/.cargo/bin/uv}" \
        /opt/homebrew/bin/uv \
        /usr/local/bin/uv
)"
BUN_BIN="$(
    find_command bun \
        "${HOME:+${HOME}/.bun/bin/bun}" \
        /opt/homebrew/bin/bun \
        /usr/local/bin/bun
)"
NODE_BIN="$(
    find_real_node \
        "${NVM_BIN:+${NVM_BIN}/node}" \
        "${VOLTA_HOME:+${VOLTA_HOME}/bin/node}" \
        "${HOME:+${HOME}/.volta/bin/node}" \
        /opt/homebrew/bin/node \
        /usr/local/bin/node \
        /usr/bin/node \
        /run/current-system/sw/bin/node
)"

# Bun follows JavaScript bin shebangs through PATH. Require real Node so
# Vitest and Jest do not run inside Bun's Node compatibility layer.
export PATH="$(dirname "$NODE_BIN")${PATH:+:$PATH}"

section "Toolchain"
printf 'uv: %s\n' "$UV_BIN"
"$UV_BIN" --version
printf 'bun: %s\n' "$BUN_BIN"
"$BUN_BIN" --version
printf 'node: %s\n' "$NODE_BIN"
"$NODE_BIN" --version

section "Core Python tests"
"$UV_BIN" sync --all-extras
"$UV_BIN" run --all-extras pytest

section "Core frontend tests"
(
    cd frontend
    "$BUN_BIN" install --frozen-lockfile
    "$BUN_BIN" run test --run
)

section "SaaS backend tests"
(
    cd saas-platform/platform-backend
    "$UV_BIN" sync
    "$UV_BIN" run pytest
)

section "SaaS frontend tests"
(
    cd saas-platform/platform-frontend
    "$BUN_BIN" install --frozen-lockfile
    "$BUN_BIN" run test
)

section "SaaS frontend API command test"
(
    cd saas-platform/platform-frontend
    "$BUN_BIN" run test:api-check
)

section "All automated test suites passed"
