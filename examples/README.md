# Executable synthetic workflow examples

Examples are the same reviewed fixtures used by the regression suite, rather than a second toy implementation. They use temporary, synthetic company profiles and mock providers. No real credentials or financial documents are required.

Run these from a source checkout after the documented editable installation:

```sh
python -m pytest -q tests/test_bank_decision_regression.py
python -m pytest -q tests/test_starter_workflow.py tests/test_workflow_safety_regression.py
python -m pytest -q tests/test_starter_accounting.py tests/test_tenant_boundary.py
python -m pytest -q tests/test_reporting_privacy.py
```

The first group exercises financial-event/journal decisions. The second checks local workflow state, partial failures and resumption. The third checks official-readback contracts against synthetic providers and cross-tenant refusal. The last checks private-only reporting and truthful state summaries.

Read the concrete fixtures and assertions in each test before drawing a conclusion. These are not real Money Forward acceptance results and do not prove full invoice/receipt/payroll E2E. Supplying real credentials to these tests is prohibited.
