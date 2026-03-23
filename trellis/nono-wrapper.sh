#!/usr/bin/env bash
# Wraps claude CLI with nono kernel-level sandbox enforcement.
# NONO_FLAGS is set by agent.py with --read, --allow, --allow-command flags.
# Uses --profile claude-code as base (provides ~/.claude, keychain, .gitconfig, tmp).
set -euo pipefail

# Suppress WARN messages for missing optional claude-code profile paths.
# These go to stdout and corrupt the SDK's JSON stream.
mkdir -p "${HOME}/.vscode" 2>/dev/null || true
mkdir -p "${HOME}/Library/Application Support/Code" 2>/dev/null || true
touch "${HOME}/.gitignore_global" 2>/dev/null || true

exec nono run \
    --silent \
    --no-diagnostics \
    --trust-override \
    --profile claude-code \
    --allow "${HOME}/.claude-personal" \
    --read /dev \
    ${NONO_FLAGS:-} \
    -- claude "$@"
