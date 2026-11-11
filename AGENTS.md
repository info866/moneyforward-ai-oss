# AGENTS.md — Money Forward AI OSS public developer preview

This repository is an independent, **PUBLIC source-only developer preview**. It is not the production system. These instructions apply to coding agents (Codex, Claude Code, and others) working on this repository.

## Goal

Build a Money Forward-specific accounting automation project with testable end-to-end correctness, safe interrupted-run recovery, and independent-user installation. Prioritize **observable real accounting outcomes and replay safety**, not tool count, documentation volume, or cosmetic security checks.

## Mandatory boundaries

- **Do not change repository visibility, create a GitHub Release, publish new integrations or run live accounting without separate explicit owner approval.**
- Never import, copy, publish, or mirror any production repository Git history, live database, company-specific configuration, account/employee records, credentials, logs, or raw provider responses.
- Use only individually reviewed, generic code and synthetic-from-origin fixtures. All imported code must have approved source rights and required attribution.
- Do not run live accounting writes, payroll changes, approvals, payments, deletions, or filing tasks as part of OSS development. Real API read/write verification requires its own authorized, scoped task.
- Do not create GitHub Actions, a second production MCP, a new permanent database, scheduler, secret store, or automatic mirror/backport to private production.
- Never weaken tenant identity, authorization, idempotency, official provider read-back, or independent review gates for test success.
- Follow the least-change principle. Reuse existing reviewed code only where appropriate, rather than duplicating provider clients or orchestration systems.

## Before changes

1. Read README.md, docs/OPERATING_MODEL.md, and docs/RELEASE_GATES.md.
2. Fix the exact failure/reproducibility case with synthetic inputs and documented before/after evidence.
3. Verify no sensitive data or external secrets enter code, diffs, test artifacts, commit metadata, or PRs.
4. Run available local tests and inspect the diff. A passing mock test is only TESTED, not LIVE or REPRODUCIBLE.
5. Record what changed, what was checked, and what remains unverified. Do not report private production changes as part of this repo.

## Accounting safety contract

- Journal/attachment writes must check identity, source, duplicates and authorization.
- Unknown-effect writes cannot be blindly retried; official provider GET reconciliation is required.
- Same-input and same-run replay must have zero additional external writes.
- Only evidence from the actual target, the same run and provider read-back can mark a workflow completed.
- Incorrect or missing data becomes a bounded exception; safe, unrelated items may continue.

## Source publication versus production activation

The source-only developer preview is evaluated under docs/RELEASE_GATES.md.
The original full-accounting checklist is preserved in docs/PRODUCTION_GATES.md;
R1/R2 remain open and must never be labeled live-complete. This explicit scope
correction enables review and contribution to unfinished software without
pretending it is a finished autonomous accountant. Never claim source-readiness
until a clean source archive, independent extraction, tests, disclosure checks
and GitHub read-back pass. The owner explicitly authorized the Public transition on 2026-11-11; any future visibility, release or production transition needs its own authorization.
