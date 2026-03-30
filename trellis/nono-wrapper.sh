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

# ── Env forwarding ──────────────────────────────────────────────────
# nono strips env vars from the child process for security. We need to
# forward auth-critical vars so the Claude CLI can find its OAuth tokens.
# This mirrors autonav's buildCleanEnv() allowlist pattern.
#
# Without these, the sandboxed claude process can't authenticate:
#   CLAUDE_CONFIG_DIR  — where to find .claude.json (OAuth tokens)
#   CLAUDECODE         — signals SDK context to the CLI
#   SECURITYSESSIONID  — macOS Security framework (keychain reads)
#   SSH_AUTH_SOCK      — git operations via SSH agent
#
# Note: --credential anthropic does NOT work for OAuth (returns 400).
# The claude-code profile grants read-only keychain access; the CLI
# just needs these env vars to know where to look.
FORWARD_ENV=()
[ -n "${CLAUDE_CONFIG_DIR:-}" ]  && FORWARD_ENV+=(CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR}")
[ -n "${CLAUDECODE:-}" ]         && FORWARD_ENV+=(CLAUDECODE="${CLAUDECODE}")
[ -n "${SECURITYSESSIONID:-}" ]  && FORWARD_ENV+=(SECURITYSESSIONID="${SECURITYSESSIONID}")
[ -n "${SSH_AUTH_SOCK:-}" ]      && FORWARD_ENV+=(SSH_AUTH_SOCK="${SSH_AUTH_SOCK}")

# Forward all CLAUDE_* prefixed vars (config, auth, SDK settings)
for var in $(env | grep '^CLAUDE_' | cut -d= -f1); do
    FORWARD_ENV+=("${var}=${!var}")
done

exec env "${FORWARD_ENV[@]}" \
    nono run \
    --silent \
    --no-diagnostics \
    --trust-override \
    --profile claude-code \
    --allow "${HOME}/.claude-personal" \
    --read /dev \
    ${NONO_FLAGS:-} \
    -- claude "$@"
