# 0.1.0a2 — source-development release candidate

- Explicitly separate source publication from unchanged production activation.
- Include deterministic clean-commit source packaging and manifest verification.
- Provide privacy-safe contributor templates and exact capability/roadmap docs.
- Add accounting execution/read-back/replay acceptance with synthetic providers.
- Reject nonzero tax read-back without independent source amounts, and mismatched
  net/tax splits even where gross and tax category match. Legacy callers must
  provide source-approved expected_tax_values; no inferred tax calculation.
- No Public operation, live accounting completion or production deployment implied.

# Changelog

## 0.1.0a1 — private candidate, unreleased

- Added existing per-company accounting source, SQL schema and offline regression tests.
- Added clean-install documentation, contribution instructions and MIT license text for owner review.
- Added independent Git history with pseudonymous/noreply commit identity.
- Preserved fail-closed tenant, write, read-back and replay boundaries.
- Blocked accounting-status posts to public or identity-mismatched GitHub repositories.
- Replaced hardcoded completion claims in operational reports with actual run/stage/remaining status.
- Added history/index privacy screening, private denylist support and regression checks.

No public release, live business deployment, unattended accounting certification or production improvement is claimed. See `docs/RELEASE_AUDIT.md` for remaining blockers.


## Unreleased — interruption/reproducibility repair

- Reproduce and fix two tests failing in an independent generated repository
  without an initial Git commit; keep code/SQL provenance without fake SHAs.
- Preserve example instructions and machine-readable release gates in generated copies.
- Add opt-in local DAILY run-state subset using the existing bound DB, exact
  scope checks, immutable exception decisions, and restart-safe committed state.
- Restore missing/stale cache from the bound durable record, not from caller claims.
- Preserve cause-specific redacted tool errors through real MCP SDK dispatch.
- Retain provider-read-only default, no automatic accounting or notification writes,
  no synthetic `VERIFIED` promotion, and the unchanged public-release block.
