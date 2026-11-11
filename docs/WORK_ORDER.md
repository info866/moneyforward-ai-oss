# Starter acceptance and operating gates

## Local acceptance

- Run bootstrap tests for invalid slugs, traversal, existing destinations,
  safe copy selection, symlink rejection, and a second independent generation.
- Confirm copied packages import using only the copied `src` tree.
- Confirm the local profile is schema 2, unconfigured, and excluded by Git rules.
- Initialize the generated repository with `git init -b main`, then verify its
  artifact helper accepts a synthetic ignored file under the company's own path.
- Run all synthetic tests, critical Python checks, and the starter audit.
- Check that docs, config, code, and fixtures contain no actual company
  identities, credentials, office IDs, Drive IDs, historical accounting data,
  or source-repository references.
- Confirm bootstrap performed no network requests, secret-store access,
  DB initialization, RAG rebuild, or repository operation.

These are acceptance criteria, not a claim that they have already passed.

## Operator gates after copying

1. Initialize the generated Git repository; complete and review its ignored
   local profile and the selected workflow's accounting rules for exactly one company.
2. Run the read-only local doctor and resolve every identity/setup finding.
3. Verify the provider's read-only office identity and the shared-storage route.
4. Finalize the complete profile and required workflow rules, then initialize a
   new, empty, company-owned DB only. The fingerprint locks all accounting rules;
   later drift requires explicit reviewed migration, not automatic rebinding or reset.
5. Sync only this company's authorized records and construct its own RAG.
6. Validate dry-run classifications, duplicate checks, and replay/resume behavior.
7. Authorize exact operations only after all required external gates pass.
8. Require authoritative read-back and final audit before marking operations complete.
   Run `--office-profile config/offices/acme.json --execute verify --strict --detail`
   to inspect recorded gates in the selected DB read-only. A passing recorded
   result does not recheck live authentication or waive per-operation guards.

Until the operator supplies evidence, credential availability, live identity,
OAuth scopes, Drive permissions, service read-back, and production readiness
remain unverified. No tests or documentation may silently mark these complete.


## Cloud-first migration acceptance

- [ ] Generated company contains exactly one canonical ChatGPT Project Skill
  and its own local cloud-run scope contract; no shared runtime import.
- [ ] Synthetic tests reject cross-company run response, mixed/duplicate
  DAILY scope, implicit payroll, untrusted provider claims, and extra writes.
- [ ] Each company's authenticated ChatGPT connection independently exposes
  source, RAG, run, Money Forward official GET, journal/voucher where required,
  scoped Cross Audit and final notification actions.
- [ ] Exact company office, OAuth, DB/RAG and Drive route read-backs precede
  first write; no other company's connector is substituted.
- [ ] Five DAILY workflows are separately startable/resumable and the
  resulting per-run Google Chat receipt is durable and idempotent.
- [ ] PAYROLL remains explicit, MONTHLY_CLOSE remains read-only, and
  deferred tax/insurance does not block DAILY.
- [ ] Production activation is withheld until an authorized company-only
  canary passes source → post/reuse → journal/voucher/Drive official read-back
  → Cross Audit → one final Chat read-back. Repository merge alone does
  not close any of these external gates.

## Packaged ChatGPT client acceptance

- Confirm bootstrap emits isolated plugin identities for two different slugs.
- Confirm MCP source is read-only, remains unconfigured until a verified local
  company profile exists, and does not expose journal/delete/notification writes.
- Run `tests/test_chatgpt_bootstrap.py` in addition to the existing
  `tests/test_chatgpt_cloud_contract.py`, then full `make test`/`make audit`.
- No such local test establishes authorized run/write tools, company OAuth,
  browser-facing plugin installation or production Chat delivery.
