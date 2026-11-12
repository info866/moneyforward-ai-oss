# External engineer handoff — source-complete developer preview

**Scope:** Public `info866/moneyforward-ai-oss` on `main`, version `0.1.0a2`. This is the independently installable, *offline-tested developer source* being handed to external engineers. It is **not** a certified live accounting product, a connected hosted service, or a production deployment. The [original production gates](PRODUCTION_GATES.md) are still open. This handoff creates no new permission or provider approval.

The exact reviewed version is the Git `main` commit and source-release manifest read at handoff (`git rev-parse HEAD` and `python scripts/build_source_release.py --verify <archive>`). Do not replace a commit SHA with a version label when comparing results. No GitHub Release has been created.

## 1. What is already bundled and reproducible

| Capability | Included / verified offline | Outside current proof |
|---|---|---|
| Standalone company bootstrap | Generated independent unconfigured company copy, SQLite/schema, ignored configuration, synthetic tests | New adopter's real legal identity, credentials, source accounts, approval |
| Invoice sales discovery | Invoice API clients, paginated identity/coverage guards, native-link probe, three logical processing stages, no duplicate generic sales POST | Actual product-to-product linkage, native journal identity, exact provider payloads |
| Accounting planning and guarded write | Approved request hash, journal execution claim, durable uncertainty state, official GET comparison, tax witness comparison, synthetic replay | Authoritative source-to-tax-witness provenance from an authenticated bill/receipt |
| Local DAILY state | Opt-in same-company local ledger/SQLite state, restarted-run synthetic tests | Complete provider-backed source → journal/voucher → audit → notification chain |
| Cloud MCP | Real SDK dispatch subset, read-only and local opt-in state tools | Full independent workflow writer / provider read-back / notification finalizer |
| Source distribution | Clean source archive with manifest, checksum verification, fresh extracted install, Gitleaks review | Production activation or a deployed app/hosted service |

Public tests and examples use synthetic-from-origin fixtures only. They do not connect to Money Forward, call a production account, or represent live invoices/payroll/transactions. A pass in a mock transport must not be described as `LIVE` or real product accuracy.

## 2. Reproduce acceptance from a fresh public checkout

Use a clean checkout with Python 3.13 on macOS (Python 3.11+ is declared, but other combinations need separate verification). Do not import a private repo, private GAS project or production database.

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -c constraints-cloud.txt -e '.[test,cloud]'
make test test-cloud test-accounting audit
python examples/accounting_pipeline_demo.py
python examples/portable_state_demo.py
make source-release
python scripts/build_source_release.py --verify dist/source.tar.gz
```

Build `dist/source.tar.gz` only once at a new output path (the builder intentionally refuses to overwrite a pre-existing archive). Independently extract the archive into a **different** directory and use a new virtual environment to repeat the suite and demos. Confirm source manifest commit/tree/hash, archive SHA-256 and GitHub `main` read-back. Offline demo evidence must say `OFFLINE_SYNTHETIC`, `live_api_exercised=false` and `production_ready=false`. Do not sum overlapping test subsets as independent counts.

Review all-ref Git metadata, issues/PR/comments, files and assets before any new outward source publication. Use the existing Gitleaks workflow manually; **GitHub Actions remain disabled**. Review copyright and `THIRD_PARTY_NOTICES.md` before accepting external code. Do not ingest secrets or identifying records, even transiently into fixtures or issue comments.

## 3. Unfinished integrations — handoff to external engineers

The following are independent acceptance tracks. They must **not** be reported as complete merely because the offline package passes. Reuse existing code, database, MCP and run boundaries rather than adding parallel services.

1. **Original document and source provenance** — [Issue #7](https://github.com/info866/moneyforward-ai-oss/issues/7). Prove that an independent adopter can enumerate original bills/receipts, document hashes and complete pagination through authorized provider/Drive paths *without a private GAS runtime*. Missing pages, changed sources, wrong office or 403 must be blocked. Caller-supplied manifests are not official source receipts.
2. **Expected journal and tax witness** — [Issue #5](https://github.com/info866/moneyforward-ai-oss/issues/5). Bind approved source identity, expected net/tax/gross (including mixed rates and rounding), journal request hash and the matching GET; never derive expected tax values from the response being verified. Missing or forged provenance cannot mark `VERIFIED`.
3. **Uncertain external effects** — [Issue #6](https://github.com/info866/moneyforward-ai-oss/issues/6). Extend the already-tested no-blind-retry/claim/reconciliation primitives. Confirm process restart, concurrency, missing/ambiguous GET, approval/input/office drift and zero extra POST in the adopter's authorized test tenant.
4. **Voucher, Cross Audit, notification** — [Issue #8](https://github.com/info866/moneyforward-ai-oss/issues/8). On the **same run**, independently GET journal and relevant voucher/source evidence, persist a read-only audit receipt, and durably send one final notification. Notification-only resume must not re-POST any journal or voucher. The OSS MCP is not yet a complete writer/finalizer.
5. **Actual API compatibility and release gate** — [Production gates](PRODUCTION_GATES.md). Validate endpoints, authorization scopes, pagination metadata, HTTP 429 behavior, official links and product account behavior from the official docs and an independently authorized adopter's tenant; use read-only discovery first. `mfc/invoice/data.read` is the read scope in [Money Forward's official Invoice API guide](https://biz.moneyforward.com/support/invoice/guide/api-guide/a03.html). The [official common API specification](https://developers.biz.moneyforward.com/docs/common/api_common_specifications/) describes pagination patterns **generically**, not a guarantee that every Invoice endpoint has identical metadata. [Invoice API docs](https://invoice.moneyforward.com/docs/api/v3/index.html) should decide endpoint-specific details. Do not silently change defaults to fabricate successful coverage.

Payroll, tax filing, payment approval and other financially consequential changes have their own authorization requirements. They are **not** implicitly included in an invoice/journal acceptance.

## 4. External engineer's staged go/no-go sequence

1. Set up a **separate** authorized company profile, product scopes and fresh empty company database following [SETUP](SETUP.md). Never reuse the maintainer's production tokens, DB, logs or source files. Reject office mismatch before any write.
2. Verify the complete read-only invoice → native accounting link → journal GET chain on the adopter's own known invoices, including one already-linked invoice, one missing link, pagination across 2+ pages, a 429, a malformed/incomplete page, and a same-invoice replay. A missing link is `REVIEW`, not a reason to create a standalone journal.
3. Only after a **separate explicit approval** for a scoped test write, prove source-bound expected journal and tax, pre-write idempotency check, exact official GET read-back and zero additional mutation on replay/restart. Unknown write result means reconcile, never blind POST retry.
4. Validate source/voucher link, read-only Cross Audit and exactly-once final notification with the same run ID. Preserve records of what is not verified; no fake success or silent skipped workflow.
5. Recheck the exact final commit and source archive, license/dependency/security review, and [production gates](PRODUCTION_GATES.md) before any production activation. Independently verify actual consumer setup from a clean machine and documented instructions. Use redacted evidence summaries in public PRs; keep raw provider responses private to the authorized adopter.

A missing entitlement or API function is a bounded `BLOCKED / NOT VERIFIED` condition, not permission to use undocumented endpoints, a second credential service, a second workflow or the private maintainer implementation. Separate source publication, adopter installation, provider connection, and live bookkeeping sign-off.

## 5. Work allocation and support boundary

Source baseline and tests are handed over **as-is**; this repository is the source of truth. External engineers own endpoint/tenant-specific validation and any remaining integration PRs, under ordinary maintainer review. There is no standing permission to deploy changes back to any maintainer company. Use the existing [issues](https://github.com/info866/moneyforward-ai-oss/issues) and [contribution guide](../CONTRIBUTING.md), keep all code changes company-neutral, and include exact before/after tests and read-back evidence. Please do not close Issues #5–#8 until their individual conditions, including authorized live checks where required, are met.
