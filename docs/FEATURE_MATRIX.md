# Capability and evidence matrix

This table describes 0.1.0a2, not the private owner's production system.

| Area | Distributed implementation | Evidence / limitation |
| --- | --- | --- |
| Independent company setup | Bootstrap, unconfigured profile, full Python/SQL source | Offline generated-copy tests; no initial commit needed |
| Money Forward API transport | Existing guarded accounting/invoice/payable clients | Synthetic contract tests; own product OAuth/identity/scopes required |
| Sales-invoice native linkage | Existing invoice/accounting probe with fail-closed page/identity validation and unconfirmed-link state | [Three-stage contract](SALES_INVOICE_NATIVE_LINK.md); synthetic complete/incomplete-page regressions, no live native-link acceptance |
| Journal planning and classification | Existing decision, RAG/master and deterministic validation code | No bundled real journal history; no claimed accounting accuracy percentage |
| Journal execution and read-back | Existing approval, intent, durable claim, POST and comparison functions | Synthetic pipeline acceptance; raw provider responses stay local |
| Unknown-effect POST and replay | Existing uncertain-delivery/reconciliation path | Offline fault/replay tests; never blind retry |
| Local DAILY run state | Same company DB, durable audit and derived-cache recovery | Tested process restart and retained request ID; not provider attestation |
| MCP entrypoint | Default provider reads, opt-in local metadata tools | Real SDK dispatch tested; NOT complete journal/voucher writer or finalizer |
| Evidence intake / GAS | Python client and contracts included | External service implementations are not all bundled; full workflow activation blocked |
| Official source → journal → voucher → audit → final notification | Partial implementations and contracts | Whole live chain NOT acceptance-tested for this distribution |
| Payroll / attendance / annual close / filing | Related source/target contracts only | NOT certified for unattended operation |
| GitHub reporting | Private repository match required before business reporting | Public OSS issues must never receive live business reports |

Synthetic provider counters are not actual API transactions. Tests of the real
MCP library are not tests of a real ChatGPT connection. Each user must complete
PRODUCTION_GATES.md for the exact configured workflow before production use.

Source files are retained for contributor inspection even where integration is
incomplete. No missing provider capability is auto-substituted with a mock at
runtime. A mock is used only by explicitly synthetic examples/tests.
