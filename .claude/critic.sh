#!/usr/bin/env bash
# Second-opinion code review for signalslate, using Gemini as the critic.
#
#   .claude/critic.sh                   # review uncommitted changes vs HEAD
#   .claude/critic.sh HEAD~3            # review everything since a ref
#   .claude/critic.sh --staged          # review only staged changes
#   CRITIC_CLI=gemini .claude/critic.sh # use Gemini CLI instead of Antigravity
#
# Defaults to `agy` (Antigravity CLI), which runs Gemini under the account's
# Gemini Pro subscription. `gemini` (Gemini CLI) uses an AI Studio API key on
# the free tier, capped at 20 requests/day -- enough to smoke-test, not enough
# to review with. Keep it as a fallback, not the default.
#
# Notes:
#   - Must run OUTSIDE Claude Code's sandbox (dangerouslyDisableSandbox: true).
#     The sandbox's allowedDomains has no Google AI host, so a sandboxed call is
#     network-blocked with a confusing error rather than a clean failure.
#   - gemini reads its key from ~/.gemini/.env; agy uses ~/.gemini/oauth_creds.json.
#     Neither needs anything exported here.

set -uo pipefail

CRITIC_CLI="${CRITIC_CLI:-agy}"
MAX_DIFF_BYTES="${MAX_DIFF_BYTES:-180000}"
# agy defaults to a 5m print timeout, which a large diff blows through and
# returns only partial output. Give it room; override for a huge review.
PRINT_TIMEOUT="${PRINT_TIMEOUT:-20m}"
cd "$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "not a git repo" >&2; exit 1; }

# ':(exclude).claude' keeps the critic from reviewing its own tooling.
EXCL=(':(exclude).claude')
case "${1:-}" in
  --staged) DIFF=$(git diff --staged -- . "${EXCL[@]}"); WHAT="staged changes" ;;
  "")       DIFF=$(git diff HEAD -- . "${EXCL[@]}")
            # include new files, which `git diff HEAD` does not show
            for f in $(git ls-files --others --exclude-standard -- . "${EXCL[@]}"); do
              DIFF+=$'\n'"--- /dev/null"$'\n'"+++ b/$f"$'\n'"$(sed 's/^/+/' "$f")"
            done
            WHAT="uncommitted changes (tracked + new files)" ;;
  *)        DIFF=$(git diff "$1" -- . "${EXCL[@]}"); WHAT="changes since $1" ;;
esac

if [ -z "${DIFF// }" ]; then echo "No changes to review."; exit 0; fi

if [ "${#DIFF}" -gt "$MAX_DIFF_BYTES" ]; then
  echo "Diff is ${#DIFF} bytes; truncating to $MAX_DIFF_BYTES for the critic." >&2
  DIFF="${DIFF:0:$MAX_DIFF_BYTES}"$'\n\n[diff truncated]'
fi

command -v "$CRITIC_CLI" >/dev/null || { echo "$CRITIC_CLI not found on PATH" >&2; exit 1; }

read -r -d '' PROMPT <<'EOF'
You are a critical code reviewer giving a SECOND OPINION on changes another AI
agent wrote. It was confident; your job is to find what it missed. Be specific
and skeptical, not encouraging.

This is SignalSlate: a Python service (FastAPI-style api/, pipeline/, auth/)
with pytest tests. Recent work targets Phase 1 auth gaps.

Report ONLY things that matter, most severe first. For each finding give:
  - file:line (or file if line is unclear)
  - what is wrong, concretely
  - why it breaks: inputs or state that produce the wrong result

Prioritise, in order:
  1. Correctness and security bugs, especially auth/authz: missing scope checks,
     bypassable guards, wrong boolean logic, unvalidated input, secrets in code.
  2. Tests that assert the wrong thing, are tautological, or would still pass if
     the behaviour they claim to cover were broken.
  3. Error handling and edge cases: None/empty, concurrency, partial failure.
  4. Only then, clarity or duplication -- and only if genuinely harmful.

Ignore formatting and style preferences. If you find nothing serious, say so
plainly in one line rather than inventing minor issues.

The diff to review was provided above.
EOF

echo "Critic: $CRITIC_CLI · reviewing $WHAT (${#DIFF} bytes)" >&2

# Both CLIs take the prompt as -p's value and append it to stdin, so the diff
# goes in on stdin and the instructions arrive last.
#   agy  --mode plan keeps it read-only (it is agentic and will otherwise try to
#        edit); --dangerously-skip-permissions is required because headless mode
#        cannot answer permission prompts and auto-denies, producing no output.
#        Plan mode is what bounds the risk here: tools auto-approve, edits do not.
#   gemini --skip-trust: gemini has its own trusted-folder gate, separate from
#        Claude Code's, and blocks on it otherwise.
case "$CRITIC_CLI" in
  agy) printf 'Diff under review:\n\n%s\n' "$DIFF" | \
         agy --mode plan --dangerously-skip-permissions \
             --print-timeout "$PRINT_TIMEOUT" -p "$PROMPT" 2>&1 ;;
  *)   printf 'Diff under review:\n\n%s\n' "$DIFF" | "$CRITIC_CLI" --skip-trust -p "$PROMPT" 2>&1 ;;
esac | grep -vE '^\[STARTUP\]|^Ripgrep is not available|Loaded cached credentials|^Data collection|^\s+at |^\s*status: [0-9]+|^\s*\}$|_ApiError'
