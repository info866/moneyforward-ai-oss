# Production activation gates — NOT SATISFIED

The original full-accounting acceptance checklist below is preserved verbatim as
historical policy. It formerly mixed source-publication readiness, live operating
acceptance and the owner's visibility-change approval. Those are now distinct.
No live gate has passed because this document moved. R1/R2 remain OPEN.
Publishing a development preview does not authorize real journal, payroll,
payment, deletion, tax filing, or autonomous deployment operations.

## Preserved original checklist

# Public-release gates — all mandatory

**This is a checklist, not evidence that any check has passed.**

## A. Security and disclosure

- [ ] Confirm the new repository has no production-derived data, raw account IDs, user/employee names, emails, addresses, salary details, invoices, journals, receipts, tax records, real run logs, API responses, or sensitive folder paths.
- [ ] Inspect **all tracked files and full Git history**, including commit messages, reachable branches and tags; reject any copied production commits.
- [ ] Inspect **commit author/committer names and email metadata**, including initial and staging commits. The original staging history was quarantined in a separate private archive after identifying metadata was detected. The release candidate must contain only its fresh pseudonymous/noreply history; never merge old commits.
- [ ] Review Issues/PRs, attachments, Discussions, release assets, action artifacts/logs (if any), and repo metadata for information leakage.
- [ ] Run secret scanning and an independent human/agent review; redaction in README alone is insufficient.
- [ ] Ensure test fixtures are **synthetic from origin**, not merely stripped versions of sensitive records.
- [ ] Ensure no production OAuth tokens, API keys, webhooks, Keychain material, DB backups, or browser profiles are transferred.
- [ ] Verify documentation and code do not reveal private infrastructure or allow production account access.
- [ ] Confirm Money Forward terms, all third-party licenses, notices, trademarks, and the project's chosen open-source license.

## B. Repeatable accounting quality

- [ ] Each advertised workflow has a synthetic end-to-end test with deterministic expected journal lines, amounts, tax categories, and documentary provenance.
- [ ] Every mutation path checks tenant/office, source identity, masters, expected effect, duplicate/previously committed result, and authorized scope.
- [ ] Official read-back of journal lines, vouchers, and relevant source IDs is the only basis for marking external writes VERIFIED.
- [ ] Same-input replay and interrupted-run replay are tested to produce **zero extra external writes**.
- [ ] Unknown-effect POST/timeouts, HTTP 429/5xx, authentication loss, wrong office, unsupported API, missing documents, and partial failures are separately tested. A safe failure must not block unrelated ready items.
- [ ] Complete workflow status must reflect **actual** outstanding items, approvals and notifications; a passed CI/unit test is not a business close.
- [ ] At least one separately authorized real-API validation is documented with **redacted evidence**, without publishing account data. Read-only first, narrow write tests only when explicitly approved.
- [ ] Each included module has a working local setup and documented, reproducible tests for an independent user and their own Money Forward auth.

## C. Operational release

- [ ] Neutral setup instructions work without access to any private owner repo, company, local path, hidden MCP, or proprietary agent skill.
- [ ] Documentation distinguishes currently working features from roadmap items.
- [ ] Security/contact and contribution policy are operational; the license is selected and the notices are correct.
- [ ] External code contributions remain review-gated; there is no automatic production mirroring.
- [ ] The owner reviews the final inventory, source commit and complete history, and approves the exact public release contents.
- [ ] **Separate explicit owner approval** is obtained immediately before changing GitHub visibility to Public.

## Not permitted

Do not make this repository public merely because tests pass or a release date arrives. Do not publish the original private repository or any of its historical commits. Do not enable GitHub Actions for this preparation. Do not weaken data or authorization gates to make acceptance tests pass.

**Current release decision: BLOCKED — private preparation only.**
