# Security Policy

## Reporting a vulnerability

**Please don't open a public issue for a security problem.**

Report it privately through GitHub:
[**Report a vulnerability**](https://github.com/JIUNG9/rds-pg-migration-toolkit/security/advisories/new)

That opens an advisory visible only to you and the maintainer — no email address to
track down, and nothing public until there's a fix.

> **Maintainer note:** that link requires *Private vulnerability reporting* to be
> switched on under **Settings → Advanced Security**. It's one checkbox and it's
> free on public repos. Until it's enabled the URL 404s for outside reporters, so
> turn it on before pointing anyone at this policy.

### What helps

- What the issue is, and what an attacker gets out of it
- Steps to reproduce, or a proof of concept
- The affected version or commit
- A suggested fix, if you have one in mind

### What to expect

This is a personally maintained project rather than a funded product, so response is
best-effort: acknowledgement within a few days, and a fix prioritised by severity.

## Supported versions

Only the latest release gets fixes. Earlier versions are not maintained.

## Scope

This tool runs against production databases, so the failure that matters most isn't
disclosure — it's **a preflight check that passes when it shouldn't**. In scope:

- A check returning `PASS` or `SKIP` where the underlying condition is actually
  unsafe. A silent `SKIP` is the dangerous one: it reads as "fine" in the report
  while nothing was verified. There was a real instance of this — ARN parsing raised
  `IndexError`, an outer handler swallowed it, and the cross-region replica check was
  skipped without a word.
- Anything that logs or writes a connection string, password, or endpoint into the
  report output.
- A rollback path that can't actually roll back, or a blue-green cutover that reports
  success before the green environment is serving.
- IAM policy examples in the docs that are broader than the toolkit needs.

Out of scope: `--dry-run` output differing cosmetically from a real run.
