# Sales invoices — native accounting linkage (developer-preview contract)

This is a company-neutral implementation contract for the existing sales-invoice
path. It documents intended safe behavior; it does **not** certify unattended
end-to-end operation or live provider acceptance.

## Three logical stages

1. **Invoice API**: read the invoice list, details, partner, amount and tax facts;
   normalize and evaluate against verified accounting patterns and current master data.
2. **Money Forward native accounting linkage**: favor the invoice product's
   standard integration and its existing sales-accrual journal. Do not manufacture
   a separate journal when the native candidate/registration state is unknown.
3. **Accounting API official GET**: inspect the registered journal and validate
   the source relationship, debit/credit lines, amounts, tax treatment, partner
   and duplicate state before marking it verified/reusable.

These are operational stages, not three HTTP requests. The same existing DAILY
sales-invoice workflow should own all stages; no second scheduler, database,
MCP service, or journal-writing route is introduced by this document.

## Decision boundary

- **Verified native journal and full expected match**: reuse the existing
  journal; no additional journal POST.
- **Unconfirmed linkage, missing registered journal, ambiguity or mismatch**:
  hold only that invoice for review or authorized confirmation; do **not**
  fallback to a generic Accounting journal POST.
- **No pending entries in a user interface** does not imply no native journal:
  a standard integration may already have registered it.
- The probe status `registered_journal_not_confirmed` means only that a
  registered *native-linked* journal is unconfirmed. It is **not** evidence
  that a pending UI candidate exists.
- Preserve provenance from the accounting journal back to the original invoice
  (and product-managed supporting document when relevant). Do not require
  duplicate PDF export to Google Drive or re-upload solely for this sales path.
- Before resuming an uncertain operation, prefer provider read-back and
  duplicate checks over retrying a possible write.

## Evidence and release boundary

The open-source probe and synthetic tests illustrate matching and uncertainty
handling. They are not live proof that Money Forward product-to-product linkage
has been configured or that a journal is registered in a particular office.
Independent users must validate with their own authorized tenant and satisfy
[production gates](PRODUCTION_GATES.md). No company records, credentials,
private operational workflow, or original private Git history are bundled.
