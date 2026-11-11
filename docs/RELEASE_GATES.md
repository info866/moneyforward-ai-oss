# Source publication gates — development preview

Scope: **0.1.0a2 developer preview**, not production-certified autonomous accounting.
See PUBLIC_RELEASE_SCOPE.md for the explicit correction to the earlier conflated
publication policy. The original gates are preserved in PRODUCTION_GATES.md.

## Mandatory before declaring the source candidate ready

- [ ] All reachable branches, tags, commits, author/committer metadata, source
  files, Issues/PRs/comments, assets and visible repository settings are screened.
- [ ] Gitleaks and private identity denylist screening report no unresolved findings;
  focused independent review has no unresolved blocking disclosure finding.
- [ ] No real office profile, evidence, payroll, journal data, secrets, original
  private Git objects, webhook values or local path identity is distributed.
- [ ] LICENSE is valid MIT text for owner-controlled first-party code; dependency
  notices and actual tested versions are included. No unreviewed vendor code.
- [ ] A clean source checkout AND an independently extracted source archive install
  and pass core/cloud/accounting tests, static audit and executable examples.
- [ ] The distribution is generated from one clean commit; repeated builds are
  byte-identical and every included file has a checked content hash.
- [ ] README and feature matrix distinguish real source, synthetic verification,
  supported interfaces, external prerequisites and unverified live operations.
- [ ] Contribution/bug/feature/PR templates require safe reproduction and forbid
  uploading credentials or business/personal data.
- [ ] Operational reports cannot silently send company data to the public OSS repo;
  no Actions, production deployment, hosted service or extra credentials are added.
- [ ] The exact tested commit and read-back evidence are recorded in Issue #1.

## Separate actions (not performed by a readiness check)

The owner approves the exact Public visibility change after reviewing the final
source audit. The publication operator rechecks GitHub surfaces and enables the
private vulnerability reporting channel for the resulting public repository.
No script here changes repository visibility, creates a release, posts accounting
entries or promotes the code to a production system.

## Production status

**BLOCKED / NOT VERIFIED**. Every live identity, authorization, source, journal,
voucher, replay, audit, notification and tenant-specific acceptance condition in
PRODUCTION_GATES.md remains binding. Neither a source release nor mock test
success may waive it. SOURCE-READY is never LIVE-READY.

The measured current decision is in release-readiness.json and Issue #1.
