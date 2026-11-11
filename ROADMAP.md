# Practical contribution roadmap

The release exists to improve real Money Forward work, not accumulate tool counts.
Do not use real documents or secrets in public reproductions.

## P1 — Complete the existing integration (production gate R1)

Connect source acquisition, journal/voucher operations, official read-back,
scoped Cross Audit and final notification through the existing per-company
backend. Inventory the actual external GAS dependencies; package only reviewed
neutral source or document a supported replacement. No second accounting DB,
new parallel MCP, blind write retry or mock in the production route.
Acceptance: source facts and full expected journal lines survive all transitions;
missing capabilities affect only their operation; official receipts are bound to
the same company, source, run, approval and payload.

## P1 — Tenant-owned live acceptance (production gate R2)

An adopter uses their own authorized environment for read-only identity and
master checks first; a narrowly authorized write test follows only with explicit
permission. Verify source → journal → voucher → official GET → audit and one
final delivery; interrupt after external success and retry without an extra
write. Publish only a sanitized test summary, never actual records or raw IDs.

## P2 — Better accounting decisions and coverage

Provide synthetic fixtures for ambiguous evidence, multiple tax lines, returns,
invoice/bank/card overlap, source version changes and incomplete pagination.
Compare expected amounts/accounts/tax and human intervention counts on held-out
cases. More unit tests alone are not evidence of improved accounting accuracy.

## Contribution start

Reproduce the offline examples, choose one failing case, open a privacy-safe
issue, and send a minimal PR. Follow CONTRIBUTING.md. Production gates remain
open until the exact deployed version passes; source publication closes none of
these product tasks.
