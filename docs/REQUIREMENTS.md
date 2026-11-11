# Requirements

## Purpose

Provide a complete, independent starter that a new company copies and owns.
Updates to this starter must not automatically change any deployed company.
The starter contains accounting code, schema, synthetic tests, setup instructions,
and an explicitly unconfigured example, never another company's data.

## Company boundary

Money Forward office, OAuth authorization, Keychain namespace, DB, RAG,
masters, journal history, and company-specific rules belong to one company.
Sharing a Workspace or a Drive administration account does not merge companies.
A shared Drive foundation is allowed when the company's source and destination
routes are explicitly bound and verified.

Reject missing, ambiguous, or incompatible office, token context, Keychain,
DB/RAG, or Drive route before business I/O. Allow only the minimal identity
read needed to establish authorization. Never guess a tenant or fall back to
another account, database, or folder.

## Delivery requirements

- Bootstrap accepts `--slug` and `--output`; output must not exist.
- A generated copy includes its own source, schema, scripts, tests, and docs.
- Bootstrap makes no service calls and creates no database or RAG.
- Generated schema 2 profiles remain `configured: false` until setup is complete.
- Credentials stay in a company-specific secret store; local profile files are ignored.
- Existing databases and RAGs are preserved. Setup initializes only a new company's empty DB.
- Preserve idempotency, duplicate checks, authoritative read-back, replay/resume,
  and audit gates throughout accounting operations.
- Local test success cannot waive unfinished external verification or authorize writes.

## Out of scope

No production deployment, automatic OAuth consent, live accounting write,
DB/RAG reset, scheduler creation, CI workflow creation, or cross-company runtime
dependency is part of starter generation.


## ChatGPT cloud-first delivery (2026-10-19)

- ChatGPT owns the ordinary accounting conversation, source interpretation,
  decisions, run orchestration and final human-facing summary. Existing
  company-specific MCP/API retains auth, durable DB/RAG, idempotency,
  official read-back and Google Chat delivery receipts.
- One canonical company Project Skill / router; no second operational runner,
  duplicate MCP bridge, credential store, DB, scheduler or notification path.
- Three modes: DAILY (five independent per-workflow run IDs and one verified
  final notice per eligible run), PAYROLL (explicit only), MONTHLY_CLOSE
  (read-only); tax/insurance deferred.
- Every new DAILY run must be scoped to exactly one workflow. Read-back of
  the selected run scope and live company identity is required before writes.
- Missing optional capability or release metadata must not block unrelated
  workflows. Missing company identity, write authorization, duplicate safety
  or authoritative read-back must fail closed for the affected operation.
- This offline starter is NOT a deployed cloud bridge; company onboarding,
  ChatGPT connector installation, live OAuth, external callbacks and canary
  remain independently required.
