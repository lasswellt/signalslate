---
description: Second-opinion review of the current changes by Gemini (via Antigravity CLI)
---

Run the project's critic and report what it found.

!`bash .claude/critic.sh $ARGUMENTS`

The critic is an independent reviewer — it did not write this code and is asked
to be skeptical rather than encouraging. Treat its output as claims to verify,
not findings to act on blindly: for each one, check the cited file:line yourself
before agreeing or dismissing it. Say plainly which findings you confirmed, which
you think are wrong and why, and then ask before changing anything.

Note: this must run outside the sandbox (`dangerouslyDisableSandbox: true`) —
the sandbox's allowedDomains has no Google AI host, so a sandboxed call fails
with a confusing network error rather than a clean one.
