# Independent Money Forward company starter

> **Cloud-first migration target (2026-10-19):** ChatGPT is the ordinary accounting orchestrator; company-specific authenticated MCP/API and existing DB/RAG remain the durable backend. Local CLI is maintenance-only. Template generation does not deploy or connect a ChatGPT plugin. See [single generic Project Skill](skills/chatgpt_project_shiwake.md), company identity gates, and readiness criteria below.

Copy this repository to start accounting automation for one new company.
Each copy contains its own code and schema: it does not import a shared runtime
or another company's repository. The distributed profile is **not configured**.
Bootstrap performs no network, Keychain, OAuth, database, or RAG operation.

## Cloud plugin and optional MCP package

Bootstrap also delivers a unique per-company ChatGPT router and an optional
read-only company MCP. **Reuse a company's existing full authorized accounting
MCP rather than running a competing second service.** The optional MCP is
only for read-only onboarding when no complete connector exists; it cannot
supply journal writes, scoped run tracking, Cross Audit or durable Chat delivery.
See [ChatGPT Cloud onboarding](CHATGPT_CLOUD_SETUP.md) for exact gates.

## 1. Generate an independent copy

Use Python 3.11 or newer. Select a new destination whose parent already exists;
bootstrap refuses existing destinations, traversal, and output inside its source.

```sh
python3 scripts/bootstrap_company.py --slug acme --output /path/to/new-company
cd /path/to/new-company
git init -b main
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -c constraints-test.txt -e '.[test]'
make help PYTHON=python
```

Bootstrap does not initialize Git. The explicit `git init` above is required:
local artifact validation checks that company files are untracked and ignored
with `git ls-files` and `git check-ignore`. No remote, commit, or push is needed.

Keep the complete directory and use an editable install. SQL and configuration
are loaded beside `src`; moving only the installed Python package is not supported.
The accounting starter uses SQLite and the Python classifier. The Keychain
adapter requires macOS. PDF inspection requires Poppler's `pdftotext` and
`pdfinfo` on PATH. Scanned documents still need verified facts/OCR input; a
successful text extraction alone does not authorize classification or posting.

## 2. Configure the ignored local profile

Edit `config/offices/acme.json` (created with mode 0600). Keep the tracked
`config/offices/example.json` unconfigured. Only the example is allowed in Git.

Replace every `REPLACE_ME_*` value with the new company's verified settings:

| Field | Set locally |
| --- | --- |
| `legal_name`, `display_name` | Exact company identity and operator label |
| `moneyforward.expected_office_names` | Verified office names, including the legal name |
| `moneyforward.keychain_service` | A dedicated namespace owned by this company |
| `moneyforward.db_path` | Its own path under `data/phase12/offices/acme/` |
| `google_workspace.account` | The accounting/intake principal |
| `google_drive.account` | The Drive administrator; may be shared |
| `google_drive.*folder*`, `evidence_root_id` | Verified company-specific intake, processed, and temporary OCR routes |
| `accounting_rules` | Only this company's reviewed rules; empty does not enable rule-dependent workflows |

Use the [accounting-rule configuration guide](SPECIFICATION.md#accounting-rule-configuration)
for exact nested keys, types, rule-row fields, and workflow prerequisites.
Read-only `connect` can run before accounting rules are complete. Before the
first `setup`, finish and review the full profile, including every rule key
needed by the intended workflows, against current-company masters.
The DB binding locks the entire `accounting_rules` object as part of the boundary
fingerprint. Adding or changing rules after setup invalidates that binding.
Later profile drift must stop operations and requires an explicit reviewed
migration; this starter supplies no automatic binding rewrite or reset.
Never edit stored fingerprints to make changed configuration appear compatible.

Store OAuth credentials and tokens in the configured secret store, not in this
JSON, environment examples, tests, command arguments, or Git. Use the company's
own approved read-only authorization and credential-setup process. Do not grant
write scopes during initial read-only onboarding.

Set `configured` to `true` only after reviewing all settings. This flag records
configuration completion, not successful external authentication or production
readiness. A company is never identified solely from an email or Workspace.
Shared Drive administration is supported only with the correct company route.

## 3. Diagnose, connect read-only, then initialize a new DB

```sh
python -m mf_phase12 --office-profile config/offices/acme.json doctor
python -m mf_phase12 --office-profile config/offices/acme.json connect
```

Doctor is a local read-only diagnostic. Before configuration it reports
`not_configured`; it does not create a DB or establish external readiness.
Connect performs Money Forward read-only identity discovery using the company's
credentials. A missing, ambiguous, or incompatible identity must stop setup.

After that verification and final review of all needed workflow rules, initialize
**only the new company's empty database**:

```sh
python -m mf_phase12 --office-profile config/offices/acme.json --execute setup
```

Here `--execute` acknowledges local database creation and binding following
read-only office verification. It does not authorize Money Forward writes.
Never point setup at an existing company's DB or copy an existing DB/RAG.
If setup reports a binding mismatch, preserve the files and investigate.

## 4. Sync the company's own records and build its RAG

Choose an explicit accounting date range. This example is illustrative; adjust
both dates to the authorized records for the new company.

```sh
python -m mf_phase12 --office-profile config/offices/acme.json --execute rag-build \
  --start-date 2026-01-01 --end-date 2026-01-31
```

This operation reads official company records and writes the company's own local
DB/RAG. It is not a read-only local diagnostic. Do not reuse another company's
masters, journal patterns, learning history, or fixed rules, and do not reset an
existing RAG to resolve identity problems.

## 5. Dry-run before authorizing operations

Save the company's candidate batch in an ignored local input file and inspect
the CLI contract before preparing it. Keep all inputs and outputs below the
selected company's DB directory:

```sh
python -m mf_phase12 --office-profile config/offices/acme.json run contract
python -m mf_phase12 --office-profile config/offices/acme.json prepare \
  --input-json data/phase12/offices/acme/batch.local.json \
  --output-dir data/phase12/offices/acme/dry-run
```

`prepare` and `replay` invoke their actual local handlers without production
write intent. They can write local artifacts and local DB state; they do not
post to Money Forward, call OAuth, or modify Drive. Inspect their returned
classification, unresolved items, and generated artifacts. Local completion is
not a posted journal or successful external read-back. Follow each command's
help for its input format and replay/resume requirements.
Before any live operation, verify required company rules, OAuth scopes,
identity/binding, source evidence, duplicate checks, and exact approvals.
Execution acknowledgement never overrides these gates.

Completion requires authoritative read-back, duplicate-safe replay, and final
audit. Keep unknown external outcomes pending until reconciled; do not retry a
possibly successful write blindly.

## Optional existing shared infrastructure

Some production receipt and invoice workflows call an existing shared GAS
classifier/intake service. This repository includes its client, not a deployed
OCR service, GAS project, or watcher. Configure the existing approved service
only when that workflow needs it, using the [GAS function contract](SPECIFICATION.md#existing-shared-gas-contract).
Keep its deployment reference in ignored local configuration and its token in
Keychain. Do not deploy another watcher or assume shared account access proves
the company's route. Missing service configuration or boundary-aware responses
remain external gates; do not bypass them to make a workflow appear complete.

## Local verification and handoff

```sh
make test PYTHON=python
make audit PYTHON=python
```

Tests use synthetic data and isolated temporary databases. The audit checks the
starter locally; neither establishes production readiness. If included, the
autonomy runner under `codex/learning/` adds release verification and reporting.
Run it in an isolated verification environment with the chosen Python first on
PATH; never point verification at an operational database.

After authorized operations have recorded evidence in the company's own DB,
inspect the recorded completion gates read-only:

```sh
python -m mf_phase12 --office-profile config/offices/acme.json --execute verify --strict --detail
```

For this command, `--execute` invokes the actual local verifier rather than the
generic dry-run response. It does not authorize an external write. The verifier
opens the selected, bound DB read-only and evaluates recorded classification,
workflow, OAuth-scope, same-run official read-back, Cross Audit, and durable
notification evidence. `--strict` returns nonzero while any gate is incomplete.
`recorded_gates_passed` is not a fresh authentication check or a production-write
authorization; current per-operation identity, scope, and read-back gates remain.

Read [requirements](REQUIREMENTS.md), [specification](SPECIFICATION.md),
and [operating gates](WORK_ORDER.md). No CI, scheduler, deployment, live
write, or credential transfer is performed by bootstrap.


## Optional portable local DAILY state

This is a **partial, opt-in backend capability**, not a complete accounting
connector. Keep the existing company-authorized production route unchanged.
The same bundled MCP can expose local run state without a second service.

Prerequisites: a complete source checkout, `pip install -e '.[cloud]'`, a locally
configured profile, and an **already initialized and correctly bound** company
database. Bootstrap and local metadata tools do not establish OAuth/office trust
or initialize an absent database. Never copy a production DB into a test checkout.

Set `MF_CLOUD_ACCOUNTING_PROFILE` to the absolute path of your local, ignored
profile in this checkout's `config/offices` directory. Set
`MF_CLOUD_LOCAL_WORKFLOW_ENABLED=1` only when local run-state mutation is intended.
With this unset, the original provider-read-only/historical-status behavior remains.
Use the existing approved host connection; setup does not deploy a new server.

Local tools: `run_begin`, `run_status`, `run_resume`, `run_record_source_snapshot`,
`run_record_item`. New DAILY runs require exactly one `selected_workflows` entry.
The source snapshot is a caller-provided manifest, **not** official provider evidence.
`REVIEW`, `NOT_READY`, `NOT_APPLICABLE`, auth waits and retries may be recorded;
`VERIFIED` is rejected until an independently implemented official-read-back path exists.
PAYROLL and MONTHLY_CLOSE are not implemented by this local-state subset.

The existing SQLite audit log is authoritative for this subset. A stale, damaged
or missing JSON cache is rebuilt from the same validated DB record. A failed
transaction leaves the previous state intact. This does not resolve an unknown
Money Forward write effect: such a run stays blocked for official reconciliation.
No resumption path performs a provider write or sends a notification.

`make test-cloud` requires the real installed MCP SDK and checks tool dispatch,
argument validation and redacted failure reasons. It uses no network listener,
real tenant credentials, or provider. It is distinct from a real ChatGPT-host
connection test and real Money Forward E2E acceptance.

Independent copies need `git init -b main` for local-data exclusion checks but do
not need a dummy initial commit. Where no Git HEAD exists, implementation
provenance uses `source-sha256:<digest>` over code and SQL only. Company settings,
local files and credentials are not included. Provenance never grants permission.


### Retry contract for local run creation

Generate and retain an opaque `request_id` in the caller before the first
`run_begin` request. Retry the **same** request ID with the same instruction,
accounting date, mode and selected workflow after a lost response. It returns
that one committed run; reusing the ID with different immutable inputs is rejected.
Changing the request ID intentionally starts a different local run.
Only `selected_workflows` is implemented in `source_scope`; narrower account,
folder or period selectors are rejected rather than silently ignored.

A source snapshot requires an explicit complete `coverage_hash` representing
its facts/version/coverage, not merely stable document IDs. A change in the
observed facts must change that hash. Local manifests remain caller attestations,
not official read-back evidence. The backend cannot detect remote source changes
without new observations supplied by the authorized source reader.


The exact optional cloud acceptance environment is pinned with
`python -m pip install -c constraints-cloud.txt -e '.[test,cloud]'`.
Core-only development may continue using `constraints-test.txt` and `.[test]`.
Optional real SDK tests skip explicitly without the cloud extra; `make test-cloud`
instead fails if the real SDK is absent. Do not report a skipped check as passing.
