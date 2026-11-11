# Specification

## Independent delivery

`scripts/bootstrap_company.py --slug acme --output /path/to/new-company`
copies a starter to an absent destination whose parent already exists.
Slugs use 1-63 lowercase ASCII letters/digits with internal hyphens or underscores,
start with a letter, and may not be `example`. Parent traversal is rejected.
The output cannot be inside the source starter. Existing files, directories,
and dangling destination symlinks are never overwritten.

The copy allowlist is `src`, `sql`, `tests`, `scripts`, Markdown documentation,
`config/offices/example.json`, `README.md`, `AGENTS.md`, `Makefile`,
`pyproject.toml`, `.gitignore`, and `codex/learning` when present.
Only code/schema/document and synthetic fixture file extensions are delivered.
Git metadata, hidden caches, local profiles, data, exports, credential files,
and build products are excluded. Symlinks in selected content are rejected.
On a copy failure any partial destination is left for inspection; retry with a
new destination rather than overwrite or delete an existing directory.

Bootstrap creates no Git repository. After entering the generated directory,
run `git init -b main` so local-artifact checks can verify Git exclusion. A
commit or remote is not required. Operational inputs, replay outputs, and
manifests must remain under `data/phase12/offices/<slug>/`.

## Profile and identity

The tracked example is schema 2 and explicitly unconfigured. Bootstrap creates
`config/offices/<slug>.json` with the new slug, isolated DB path, and
Keychain namespace; it does not fill in real identities or credentials.
Operator-specific fields are completed locally using the example's field names.
The profile file is private (0600) and ignored by Git.

Runtime identity checks must bind the configured legal entity, provider-specific
principals, Money Forward office, secret-store references, DB/RAG provenance,
and Drive route. A shared Drive account is valid only for the configured route.
An unconfigured example never grants connection or write authority.

The boundary fingerprint includes the entire `accounting_rules` object.
Read-only connection discovery may precede rule configuration, but the full
profile and all required workflow rules must be reviewed before first setup.
Subsequent configuration drift invalidates the DB binding and stops operations.
An explicit reviewed migration is required; no automatic rebinding, fingerprint
editing, reset, or data rebuild is supplied as a compatibility shortcut.

## Public entrypoints

- `make help` describes setup and local commands.
- `make run ARGS="--help"` delegates to `python -m mf_phase12`.
- `make test` runs pytest and Ruff critical-error checks.
- `make audit` invokes `python -m mf_phase12.starter_audit`.

The source-checkout layout is required: SQL and configuration are shipped beside
`src`. Install this copied repository in editable mode, retaining the whole tree.
No other company's repository is needed at runtime.

## Operational sequence

Complete the ignored local profile, diagnose locally in read-only mode, validate
read-only service identity, initialize only a new empty company database, sync that
company's own records and build its own RAG, then prepare and inspect a dry run.
Explicit operations require their existing authorization, scope, duplicate,
read-back, and completion gates. External reads are not implied by local setup.
Use global acknowledgement before the subcommand, for example
`--office-profile config/offices/acme.json --execute setup` and
`--office-profile config/offices/acme.json --execute rag-build --start-date YYYY-MM-DD --end-date YYYY-MM-DD`.
`prepare` and `replay` execute local handlers without production write intent;
they may create local files and update the selected local DB.

## Accounting-rule configuration

All keys below are relative to the profile's `accounting_rules` object. Dotted
keys mean nested objects: `bank.income_debit_account` means
`{"accounting_rules": {"bank": {"income_debit_account": "..."}}}`.
Do not create a second nested `accounting_rules` object: the accessor strips
that prefix from references such as `accounting_rules.folder_credit_rules`.
The distributed object stays empty; configuration is local and company-specific.

`doctor`, `connect`, `setup`, and first read-only `rag-build` use the top-level
identity configuration. Rule-dependent workflows additionally use the keys
below. A missing required value raises `office setting is not configured`.
An empty list does not establish source coverage or make a workflow ready.
Use exact names from the company's current master/read-back, not sample account
names or copied histories. Recheck identity/binding after changing configuration;
do not edit saved fingerprints to force compatibility.

| Keys | Type and requirement |
| --- | --- |
| `identity.moneyforward_principal` | Optional string; if supplied must equal `google_workspace.account` after normalization |
| `runtime.artifact_root` | Optional path string; must resolve to the selected DB's parent. Omit to use that directory |
| `identity.moneyforward_browser_profile` | String naming the authenticated browser profile used for a payable UI handoff |
| `google.keychain_service`, `google.access_token_account` | Strings identifying the Google token's Keychain service and item account, never the token itself; required for Drive/GAS execution |
| `gas.config_path` | Path string to the ignored configuration of the existing shared GAS API executable; see the function contract below |
| `fiscal_year.start_month`, `fiscal_year.start_day` | Integers defining the verified fiscal start date; used by accounting-period operations |
| `bank.income_debit_account`, `bank.income_credit_account`, `bank.income_sub_account` | Exact account/subaccount strings used by the bank-income workflow contract |
| `bank.qualified_evidence_tax_category` | Exact tax-category string from the company's master for qualified evidence |
| `bank.evidence_vendor_aliases` | List of objects with nonempty `statement` and `invoice` strings used for explicit statement-to-evidence vendor matching |
| `card.credit_account`, `card.credit_sub_account` | Exact credit-side strings used to verify existing card journal shapes |
| `card.statement_markers` | List of nonempty strings identifying the selected company's card statements |
| `card.route_suffixes` | Nonempty list of route-name suffix strings; compared after NFKC normalization and separator removal |
| `card.route_name` | String used to label the verified card evidence route |
| `card.fixed_route` | JSON value describing the reviewed route; included in the card decision-cache fingerprint, not a substitute for `card_routes` validation |
| `supplier_primary.vendor_markers` | Nonempty vendor-marker strings for supplier-primary/card duplicate matching |
| `drive.payable_folder_id` | Verified source-folder string; must belong to the profile's permitted Drive sources |
| `drive.payable_route_name` | Payable route label string |
| `drive.payable_route_path` | Ordered list of route-component strings, representing the full verified payable path |
| `drive.expense_route_names`, `drive.processed_route_names` | Lists of exact folder-role names used to distinguish intake from processed evidence |
| `drive.receipt_sources` | List of objects with `parent_folder_id` and `parent_folder_name`; each source must be unique and explicitly allowed by the top-level profile |
| `payroll.payment_due` | Company-reviewed payment-due description, emitted by the workflow contract; do not infer a statutory rule from this value |
| `payroll.manifest_schema` | Exact schema marker string expected in the company's verified payroll manifest |
| `reporting.github_repository`, `reporting.issue_number` | Exact private repository `owner/name` string and integer issue number for explicitly authorized operational reporting; private visibility and identity are verified before sending; neither is configured by bootstrap |
| `recurring_invoices.evidence_source_accounts` | Nonempty, unique list of the explicitly authorized invoice-source account strings |
| `recurring_invoices.start_date` | ISO `YYYY-MM-DD` string defining the authorized Gmail intake coverage start |
| `payable.office_id_hash` | Required 64-hex hash of the authenticated Payable External office ID, using `stable_hash(str(office_id))` (canonical JSON string hashing, no `sha256:` prefix); never store the raw ID |
| `payable.office_identification_code` | Optional additional office-identification selector; it cannot replace the required hash or authenticated office read-back |
| `default_credit_account` | Optional reviewed credit account string. Without it or another proven route, credit classification remains unresolved |
| `invoice_counterparty_credit` | Optional boolean; enables the reviewed invoice-counterparty credit policy, not automatic approval |

The Payable External adapter obtains the office ID only from authenticated
office read-back and compares its hash to `payable.office_id_hash`. The supported
gateway path uses the Accounting credential binding. A standalone gateway JWT
without a verified adapter is refused. Do not add raw `office_id` or token fields
to the profile, or use an identification code alone as proof of authorization.

Company policy arrays below default to no rules. Every row must include
`company_slug` equal to the selected profile's slug. Exact-match routes cannot
be replaced with folder-name guessing; ambiguous matches are rejected.

| Policy array | Row fields and constraints |
| --- | --- |
| `folder_credit_rules` | `company_slug`, exact `folder_id`, `credit_account`, `credit_sub_account`; optional `kind` distinguishes rule types |
| `folder_expense_rules` | `company_slug`, exact `folder_id`, `debit_account`; optional `debit_sub_account` |
| `receipt_fixed_rules`, `receipt_usage_rules` | `company_slug`, nonempty `description_contains` string list, and `candidate` object. Candidate fields are limited to `debit_account`, `debit_sub_account`, `tax_category`, `tax_amount`, `tax_rate` |
| `financial_event_rules` | `company_slug`, nonempty `match`, `candidate`. Match permits `source_event_type`, `cash_flow_direction`, `description_contains`, `vendor_equals`. Candidate requires `debit_account`, `credit_account`, `tax_category`; optional subaccounts, `vendor_name`, `trade_partner_name`. Rules cannot override authoritative amounts/dates |
| `card_routes` | Exactly one row for a configured card workflow: `company_slug`, `credit_account`, `credit_sub_account`, `ledger_account_id`, `ledger_sub_account_id`, matched against current masters |
| `supplier_primary_card_routes` | `company_slug`, `supplier_source_type`, `card_account_key`, `credit_account`, `credit_sub_account`, nonempty `vendor_aliases` string list |
| `recurring_invoices.vendor_queries` | `company_slug`, `vendor_key`, `display_name`, `query`, `terms` (search-term list) |
| `recurring_invoices.routes` | `company_slug`, `vendor_key`, `route`, nonempty `aliases` list; optional `source_account` must be in `recurring_invoices.evidence_source_accounts` |

Before using a policy, compare it against the company's official masters and
verified evidence, run its synthetic/local regression, and inspect unresolved
results. Company rules are not bundled defaults. Keep unconfigured workflows
blocked instead of supplying arbitrary values merely to pass a contract.

## Existing shared GAS contract

This starter bundles the Python client only. The deployed Apps Script
classifier/intake implementation and any OCR service are external prerequisites.
Reuse an existing approved shared deployment and watcher; do not create a new
watcher, trigger, or competing project as part of onboarding.

`accounting_rules.gas.config_path` names an existing ignored local JSON file
containing a nonempty `apiExecutableDeploymentId`. Keep actual deployment
references out of tracked docs/config. The Google credential is resolved through
`google.keychain_service` plus `google.access_token_account`, and Drive read-back
must identify `google_drive.account`. File-based fallback credentials are refused.

The client posts to Apps Script's `scripts/{deployment}:run` with `devMode: false`.
For each function below it appends a final argument containing `profile_slug`
and `boundary_fingerprint`. The existing service must verify this boundary and
its allowed route before work, and return the same two values in a dictionary
under the API envelope `response.result`. Missing or different boundary values
are rejected by the client. Merely echoing an unchecked boundary is insufficient.

| Existing function | Parameters before the appended boundary | Required result |
| --- | --- | --- |
| `classifySharedAccountingCandidates` | One list containing 1-12 packets | Boundary plus `items` linked by `packet_hash`, `ocr_calls`, `gemini_calls`. Packets use `contract_version: 2`: extraction packets carry `decision_required: false`, file/hash and `extraction_input`; decision packets carry verified canonical facts, fixed fields, RAG/master constraints and `decision_required: true`. Results must pass the local fact/decision validators; a success status alone is insufficient |
| `getGmailInvoiceIntakeRuntimeStatus` | One company intake contract object described below | Boundary, matching `intake_contract_hash`, `destinationFoldersReadback: true`, exact `destinationFolderIds`, `principalAccepted: true`, `currentPrincipal`, `coverage.principals`; each required principal must have captured coverage, one existing trigger, ready query receipts, no failures, and no truncated search |
| `saveIncomingInvoiceReceiptAttachments` | The same company intake contract object | Boundary-aware dictionary; the client then invokes the status function again with that contract for authoritative post-scan read-back |

The intake contract has `schema: "company-gmail-intake-v1"`, `company_slug`,
`profile_slug`, `boundary_fingerprint`, `drive_principal`,
`destination_folder_ids`, `evidence_root_id`, `accounts`,
`coverage_start_after` (`YYYY/MM/DD`), and company-specific `queries` generated
from the reviewed recurring-invoice configuration. The server must authorize
those accounts and destinations before scanning or saving. Its status includes
`intake_contract_hash` equal to `stable_hash(contract)` (canonical JSON, full
64-hex digest), exact mailbox coverage without duplicate principals, and
`currentPrincipal` inside the contract's account list. Each principal's
`accountHash` is the first 16 hex characters of SHA-256 of its UTF-8 address.
The separately appended boundary argument is still required for both calls.

Gmail query receipts carry `principal`/`accountEmail`, `accountHash`, `vendorKey`,
`queryHash`, `status`, `resultCount`, and `processedCount`. Treat missing coverage
as incomplete. For payable decision-only calls, the client rejects a second OCR
(`ocr_calls` must be zero) and more than one Gemini call. Do not substitute a
new OCR service or fabricate receipts to satisfy these checks.

GAS calls require production execution intent and the verified local DB binding.
Some intake calls can save attachments; they are not read-only connection tests.
Authorize their exact scope and data transfer only after service permissions,
tenant validation, route selection, and read-back have been verified. Until then
these workflow branches remain external gates, independent of local test success.

## Recorded readiness

`--office-profile config/offices/acme.json --execute verify --strict --detail`
invokes the local read-only readiness inspector against the selected bound DB.
It evaluates actual recorded product/workflow gates, write scopes, the company's
completed production run, source-stage receipts, same-run Cross Audit, and
durable notification. Missing evidence remains `needs_attention`; strict mode
returns nonzero. `make audit` checks the starter itself, not these operational
records. Neither command reauthenticates live services or authorizes new writes.
No absent legacy work-order document is a substitute for recorded evidence.


## Cloud-first ChatGPT orchestration contract

The company copy ships its own \`docs/skills/chatgpt_project_shiwake.md\` and
\`src/mf_phase12/chatgpt_cloud_contract.py\`. The module provides offline
mode/scope validation and required-tool inventory only; it creates no tokens,
DB, second run ledger, plugin registration, network traffic or provider trust.

Call \`requested_workflows("DAILY", ["bank_transfer"])\` for a single new
DAILY workflow, never a combined daily run. For PAYROLL or MONTHLY_CLOSE use
the full separate mode. A trusted company-backed MCP must supply project
mode, company slug, exact workflow scope and run ID. The
\`verify_run_scope\` helper rejects mismatches but **never** grants a write.
Live Money Forward office, authenticated provider principal, Keychain,
DB/RAG and Drive route checks still precede any actual business I/O.

For each independently scoped run, acquire original complete sources, decide
from company facts + verified RAG/master, deduplicate or reuse, verify official
journals and vouchers, Cross Audit read-only, then send one short final
Google Chat notice after durable delivery read-back. Uncertain writes and
notifications are reconciled in the same existing run; no blind retry.
Version/tool/hash changes are compatibility diagnostics except where an
operation's actually required capability or argument is unavailable.

Generated repositories remain self-contained and unconfigured. Never infer
a runtime MCP endpoint or production readiness from the presence of the
starter module, plugin documentation or passing unit tests. Existing
company operations are not switched until verified company-only canary.

## Cloud packaging supplement

The independent copy now also allowlists the company-neutral `mcp_servers`
Python source, `integrations/company-operations` plugin/Skill and its explicitly
required `.codex-plugin/plugin.json`. Bootstrap generates per-slug plugin
names without service access. The tracked canonical workflow Skill and provider-
neutral `chatgpt_cloud_contract.py` remain the only orchestration authority;
there must not be a second conflicting contract. The optional company MCP is
read-only, reuses the existing Keychain and bound DB, and may be used only when
no full company MCP exists. Real run/write/Cross Audit/notification actions are
external onboarding gates and cannot be inferred from package creation.
