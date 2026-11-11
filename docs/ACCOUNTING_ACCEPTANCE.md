# Accounting execution acceptance and tax witness

The developer preview includes `tests/test_accounting_pipeline_e2e.py` and
`examples/accounting_pipeline_demo.py`. They use the actual bundled request
builder, MoneyForwardAccountingClient with a synthetic transport, SDK approval,
journal write-claim/execution and GET verification/reconciliation functions.
The fixture starts at the explicitly planned and approved execution boundary;
it does not claim raw document acquisition/OCR/classification, master sync,
voucher attachment, final audit, notification or whole DAILY completion.

Expected outcomes are literal independent single/multi-line journal amounts,
accounts and taxes. Tests exercise wrong amounts/category/account/date/line,
missing and revoked approvals, tenant mismatch, an uncertain POST committed by
the fake provider, rejection of blind retry, GET reconciliation and replay with
zero further mutations. Every result is OFFLINE_SYNTHETIC; no actual provider
request or real company record is used.

## Corrected tax-amount comparison

A reproducible defect accepted net 1001 + tax 99 when the independent source
required net 1000 + tax 100, because both had gross 1100 and the same tax ID.
The verification APIs now accept `expected_tax_values`, one entry per branch:

```python
[{"debitor": 100, "creditor": 0}]
```

These amounts must originate from independently reviewed source facts or the
approved plan, NEVER from the very GET response being verified. They are
verification-only inputs, not added Money Forward POST fields. The API compares
explicit finite Decimal values without inventing a tax rate or rounding rule.
A nonzero tax response without this source witness cannot become verified.
Missing, boolean, nonfinite or mismatched witnesses fail closed, including the
unknown-effect reconciliation path. `tax_amounts_verified` explicitly reports
whether the independent source amounts were actually compared.

Legacy taxable callers that omit this new argument now remain unverified; they
must wire their independent source facts before activation. This distribution
does not silently derive source tax from the provider response or claim those
live CLI integrations complete. R1/R2 in PRODUCTION_GATES.md remain open.

Run `make test-accounting`. The three synthetic journals in the demo are not
three live accounting transactions, and journal verification is not whole-run
or voucher completion.
