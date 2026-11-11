# Security policy

## Report a vulnerability privately

Use [GitHub's private vulnerability report form](https://github.com/akinduroifedayo/shadow/security/advisories/new) for this repository. Describe the affected version, reproduction steps, expected behavior, and observed behavior. Use fake secrets and a minimal conversation sample.

Keep vulnerabilities out of public issues and pull requests until the maintainer coordinates disclosure with you. Do not attach provider keys, access tokens, private conversations, or unredacted profile data.

## Scope

Security reports include bypasses of replay tool blocking, unintended file reads or writes, unexpected sends or command execution, secret exposure, cross-profile data access, and unsafe handling of stored test records. Include failures of the live isolation check or Start consent boundary.

Shadow runs inside Hermes and uses its provider access and tools. State the Hermes version, Shadow commit, operating system, and relevant capability grants. For a Hermes vulnerability outside Shadow, report it through Hermes's own security process.

The maintainer uses the private advisory to confirm impact, coordinate a fix, and agree on disclosure. This project does not promise a response deadline or a bounty.
