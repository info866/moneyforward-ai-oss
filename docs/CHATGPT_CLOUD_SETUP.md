# Company-owned ChatGPT / Money Forward integration

This distribution is an independent, unconfigured starter. It does not install or connect a hosted service and does not include anybody else's Money Forward credentials.

Use the operator's own authorized Money Forward account, office, product subscriptions and scopes. Use an already authorized full accounting MCP when available; do not deploy the optional read-only sample as a competing write path. The supplied company MCP exposes only onboarding reads and cannot supply full journal/voucher writes, durable workflow runs, Cross Audit or final notifications.

The source includes local run/replay logic and guards, but does not establish unattended cloud operation. See the capability matrix and release audit. No normal accounting job may be dispatched without a verified company profile and separate operation authorization.

1. Generate an independent copy with `scripts/bootstrap_company.py` and initialize its own Git repository.
2. Set the ignored local company profile, keeping the tracked example unconfigured.
3. Store the operator's own credentials outside Git. Do not paste secrets into issues or commands.
4. Use `make run ARGS="--help"`, local tests and doctor before any external operation.
5. Test the optional company MCP only in the operator's authorized context. A tool listing is not evidence that accounting or payroll completed.
6. Keep DAILY, explicit PAYROLL, and read-only MONTHLY_CLOSE scopes separate. The canonical router is in `docs/skills/chatgpt_project_shiwake.md`.
7. Live posting, evidence attachment, payroll modification, approvals, payments and filing require separate authorization and full provider read-back.

No connector registration, extra database, scheduler or new credential store is created by this repository audit. A missing operation is a release blocker, not permission to switch tenants or bypass approval.
