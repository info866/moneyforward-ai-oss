# Operating model — private OSS staging

## Scope of this repository

This is the **future distribution codebase**, not the operational accounting system. It begins with a clean, new Git history. Source is added only after it is proven safe to disclose. The candidate now contains selected company-neutral first-party implementation and offline tests; no production data, source Git history or deployment is included.

### Two separate environments

| Environment | Responsibility | Prohibited exchange |
| --- | --- | --- |
| Private production code and connected Money Forward account | Real journal entry, source verification, run state, company-specific rules, production read-back | Never copy its data, credentials, tenant-specific configuration, local SQLite database, run logs, historical Git objects, or customer documents into this repository |
| This private OSS staging repository | Portable code, neutral docs, synthetic fixtures, test harness, external contribution review | Never connect automatically to a production account or automatically deploy contributed code |

No runtime imports, write-back sync, cross-repository mirroring, or hidden credential dependencies are permitted.

## Contribution and integration path

1. **Identify a reproducible business problem**: expected facts, existing behavior, observable failure, and success condition.
2. **Check existing implementation** to avoid duplicating an API adapter, DB, OCR pipeline, MCP server, or scheduler.
3. **Extract only approved generic source** and rewrite any company-specific configuration to a neutral configuration contract. Start a fresh commit history here; do not transfer original Git history.
4. **Add synthetic fixtures and failure tests** that reproduce the problem without real financial records or API credentials.
5. **Review source and dependency licenses**; retain required third-party notices where code is copied.
6. **Accept pull requests through review**. PRs do not run against private production secrets or tenants.
7. **Selectively backport verified improvements** to the private production repository after separate approval and its own regression/live acceptance. No automatic mirror.

The original system remains authoritative. An OSS PR merge is not a production deployment or evidence of an accounting outcome.

## Technical boundaries

- One user's own authorized identity and Money Forward office per execution context; no cross-tenant fallback.
- API transport and AI decision-making must be separate from deterministic validation and durable status.
- A write with uncertain effect must be reconciled by the **official GET/read-back** before any further write. No blind retry of journal, voucher, approval, or payment mutations.
- Resume the same run, preserving completed steps and original document identity; never classify a duplicate write as success.
- Do not infer support for products whose official API or approved UI capability has not been confirmed.
- Finance/HR/PAYROLL flows require appropriate identity, source, period, employee, and authorization checks. Payment, approval, deletion, and filing remain separately authorized actions.
- Real credentials and business rules remain outside the repository. Ignoring a file in Git is not a substitute for a disclosure audit.

## Operational metrics

Measure actual results, not the number of tools, tests, commits, or agent cycles:

- Officially verified correct journal/attachment rate.
- End-to-end completion rate and elapsed time for each workflow.
- Additional external writes on same-input replay (must be zero).
- Fault recovery/resumption rate without repeating verified effects.
- Human intervention count and reasons.
- Regression rate on held-out synthetic and approved real-world validation cases.

For any metric, preserve the denominator, time window, environment, and evidence level (CODE / TESTED / LIVE / REPRODUCIBLE). Do not claim a business improvement before comparing against the same baseline and checking a subsequent real run.

## Operations and publication

**Private by default.** The repository owner must authorize public visibility separately, after all source-publication [release gates](RELEASE_GATES.md) pass for the declared developer-preview scope. Live activation additionally requires [production gates](PRODUCTION_GATES.md), which remain unfulfilled. The staging branch and README do not themselves authorize publication.
