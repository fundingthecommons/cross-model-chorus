# Security

This tool sits between your drafts and outside model providers, so a flaw in its guards, its routing or its receipt checks can expose material you meant to keep in. Please report anything like that privately.

**How to report.** Use GitHub's private vulnerability reporting on this repository: the "Report a vulnerability" button under the Security tab. Please don't open a public issue for a security problem.

**What helps.** The commit you ran (`git rev-parse --short HEAD`), the exact command with any real names and paths replaced, what you expected, and what happened. Never include a real key, a real draft or a real receipt id from a confidential run.

**In scope.** Anything that lets content leave the machine when the tool says it would not: a guard that misses a credential or a never-send path, a confidential run that reaches a non-TEE voice or an unapproved upstream, a receipt check that passes when it should fail, or a key that can leak to the wrong host.
