# ChatGPT Cloud-first Money Forward — company Project Skill

**Scope:** one generated company repository only. Replace this generic template
with the company's approved resource map after copying. Never use this starter's
example profile for a live provider, and never import another tenant's runtime.
This document is the single business Skill for a copied company's ChatGPT
accounting project; its local CLI remains for maintenance, testing and recovery.

## Responsibility and execution boundary

- **ChatGPT cloud:** receives the instruction, reads source documents, makes
  explainable accounting decisions, orchestrates workflows and reports results.
- **Existing authenticated company MCP/API:** official office/master/journal
  reads and writes, RAG and durable run state, idempotency, authoritative
  read-back, notification ledger. A client running on a local host is a
  *backend*, not the ordinary accounting operator.
- **Native connected Gmail/Drive/Docs tools:** source acquisition using an exact,
  verified company resource map. A shared Drive administrator is not proof that
  the accounting company or its evidence folder is authorized.
- **Local CLI and code agents:** only development, diagnosis, recovery, and
  regression tests. Never silently delegate ordinary accounting to a local
  agent, GitHub runner or ad-hoc scheduler.

There is **one accounting Skill router and one authorized company-bound
accounting MCP**. A new company requires its own verified backend credentials,
office, database/RAG and resource-map bindings. Never point a copied company to
a different company's MCP, OAuth session, Keychain, database, RAG, or Drive route.

## Cloud accounting modes

| Mode | User intent | Business scope |
| --- | --- | --- |
| DAILY | New receipts, invoices, bank/card entries, journal processing | Five independent workflows, each with its **own run ID**: sales_invoice, payable_invoice, all_receipt_folders, bank_transfer, card |
| PAYROLL | Explicit payroll/bonus review, calculation, correction or journal | payroll_bonus only; never auto-launch in DAILY |
| MONTHLY_CLOSE | Explicit monthly review or close | Read-only reconciliation, statements and Cross Audit; no journal writes |

Tax/insurance is **DEFERRED**, not a hidden DAILY failure. Never copy an
existing company's accounting decisions, payroll rates, counterparties,
account aliases, journal patterns or Drive mappings into the starter.

## Every new DAILY run

1. Resolve the intended company and **one** selected workflow; if the user
   requests all new accounting, start five **independent** DAILY runs, one per
   workflow. Do not create an aggregate sixth run or daily extra notification.
2. Inspect only the actual tools required for that workflow. A differing
   release version, hash, optional field or unrelated tool is a diagnostic
   finding, **not** a global freeze. Actual missing capability blocks only
   its affected operation.
3. Before business I/O, get an **authoritative** authenticated Money Forward
   office read-back and verify company-specific principal, credential store,
   DB/RAG binding, permitted source/destination routes and exact selected
   workflow. Missing/ambiguous/mismatched identity means fail closed.
4. Begin the workflow in the existing company run ledger. The returned
   project mode, profile slug, run ID and workflow scope must exactly equal
   the request **before any write**. The pure helper
   mf_phase12.chatgpt_cloud_contract.verify_run_scope only checks scope;
   it never substitutes for live office or OAuth authorization.
5. Obtain the complete source inventory for that workflow; do not report
   a missing page or inaccessible inbox as 0 items. Use original facts,
   company RAG, latest official masters and existing journals; do not
   decide from a vendor name, filename, top-1 RAG hit, or AI proposal alone.
6. For each item: source identity + original hash/economic event → exact
   duplicate check → approved journal or existing verified reuse → original
   multi-line journal official GET → required voucher/Drive GET. On an
   uncertain write response, do **not** blind-retry. Use the same run ID
   and authoritative read-back to resolve it.
7. Separate VERIFIED, terminal REVIEW/NOT_READY/NOT_APPLICABLE,
   waiting_for_auth and retry_pending. A terminal REVIEW may remain when
   no actionable item is left; never call a run complete while auth/retry
   or required voucher/read-back remains missing.
8. Run the scope-appropriate **read-only Cross Audit** and record its
   receipt in that same company run. Send **at most one** concise Google Chat
   completion notification per independently verified workflow run; confirm
   durable receipt. For zero completed journals, obey the approved company
   policy rather than sending repeated empty chats.
9. A failed final notification resumes **only notification**, not the
   verified accounting POST, voucher, OCR or Drive action. Replaying a
   completed run must create no additional write or duplicate message.

### Company-specific exceptions

Invoice linkage, payable registration, OCR policy, card transaction
journalization, payroll sources, message format and learning triggers must
come from this company's approved policies. A procedure proven for one
company is **not** a default for another.

### PAYROLL and MONTHLY_CLOSE

PAYROLL runs only on explicit request, from the company's finalized
attendance/wage/original documents and official payroll read-back. Never
guess person-level amounts or recompute confirmed payroll from another
company's rule. MONTHLY_CLOSE is read-only by default, reconciles the
company's statements and finalized records, and must not auto-post.

## Connection, migration and release gates

An unconfigured example, local tests, a merged PR, a plugin manifest, a
GitHub Actions pass, or a healthy machine does **not** mean ChatGPT can use a
company-bound MCP. Confirm in the *actual ChatGPT conversation*: selected
connector, exposed required actions, official matching office, current
OAuth scope, DB/RAG/Drive binding, exact run scope and durable read-back.
Refresh an existing company's plugin if tool exposure is stale; don't
create a duplicate bridge to bypass missing permissions.

**Activation requires:** isolated company backend reachable through the
approved ChatGPT connection, matching live office identity, supported
per-workflow run/read-back/notification operations, synthetic tenant-isolation
tests, and one authorized company-only live canary. Until verified, preserve
the old operational route and mark cloud execution **not ready**.


## Bundled local-state capability limit

The portable MCP now has opt-in local DAILY manifests and exception tracking.
`run_begin`/`run_resume` availability alone does **not** satisfy the activation
contract above. The local backend does not create authoritative source read-backs,
post or verify journals, perform Cross Audit, or send completion notifications.
A local `REVIEW` or `NOT_APPLICABLE` is not completed accounting. Do not route
ordinary business through this subset in place of a fully verified company backend.
