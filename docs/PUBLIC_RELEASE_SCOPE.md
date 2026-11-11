# Initial public source scope: 0.1.0a2 development preview

## Decision and why it changes the earlier report

The owner requested an OSS codebase that other Money Forward users can inspect,
run locally, improve, and eventually validate with their own provider credentials.
The prior assistant incorrectly made completion of every production workflow a
prerequisite for sharing development source. This release explicitly corrects
that scope; it does NOT claim those workflows are complete or remove their gates.

Two independent answers must always be reported:

1. **Source publication readiness**: complete and licensed distribution, meaningful
   reproducible examples/tests, no detected sensitive content in the reviewed
   scope, honest capabilities, and a workable contribution process.
2. **Production activation readiness**: a specifically authorized tenant's live
   source-to-journal/voucher/read-back/audit workflow, recovery, and zero duplicate
   mutations. This remains **NOT VERIFIED / BLOCKED**.

The owner authorized the Public visibility change on 2026-11-11, and the repository
is now public. This does not mean production activation or a GitHub Release was approved.

## What contributors receive

The complete existing company-neutral Python source and SQL, per-company
bootstrap/config contract, real API adapter implementation, journal planning,
write approvals/claims/read-back verification, local run state, synthetic tests,
examples, and source-release builder. No private repository is a runtime import.
Use the full source distribution with editable installation, not a bare wheel.

Supported demonstrable entrypoints are offline tests/examples, source inspection,
company-copy generation, CLI diagnostics/help, and the opt-in local MCP state
and default provider-read interfaces. Real provider access requires each user's
own account, locally configured profile, product authorization and acceptance.

## Not advertised as working end-to-end

The bundled MCP does not provide complete automatic source discovery, all
journal/voucher writes, Cross Audit or final notification. Some legacy integration
branches need an operator-supplied GAS service. These are visible source and
extension work, not hidden installation dependencies for the offline quickstart.
Unattended payroll/attendance, annual close and filing are not certified.
See FEATURE_MATRIX.md and PRODUCTION_GATES.md; R1/R2 are not closed.

## No privacy or permission relaxation

Only reviewed first-party source and synthetic fixtures are included. Secret,
company, all-ref history, metadata and contribution checks apply to the entire
public repository, not only the tar archive. Test providers never authorize real
writes. Existing office isolation, scoped approvals, uncertain-effect blocking,
read-back requirements and production gates remain unchanged.

## References

GitHub's project-starting guidance distinguishes initial source release from
maturity and calls for a license, README and contribution guidance:
https://opensource.guide/starting-a-project/
The production checklist remains available verbatim in PRODUCTION_GATES.md.
