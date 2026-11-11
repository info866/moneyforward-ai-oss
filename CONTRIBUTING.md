# Contributing (pre-release)

This repository is currently **PRIVATE** and is not yet accepting unsolicited public contributions. After the owner authorizes a public release, Money Forward users may propose improvements tested against their own authorized accounts.

## Pull request contract

1. Describe the actual Money Forward product/API, supported version, scope and a reproducible use case.
2. Provide **synthetic fixtures** and local tests showing the failure before the change and the result after it.
3. Report the same-input replay behavior, external writes (expected zero for replay), error/recovery behavior, and whether official read-back was verified.
4. Keep fixes minimal; do not introduce a second DB, MCP, OCR service, scheduler, workflow, or dependency without a demonstrated need and review.
5. Never upload credentials, financial statements, real transaction data, personally identifying data, company-specific configuration, or raw API responses/logs.
6. Include licenses and attribution for externally derived code, and distinguish mock/fixture tests from authorized real-API evidence.
7. A contributor's account and credentials must never be shared with the maintainer or used by project automation.

## Acceptance

Review requires observable operational improvement, no regression to safe write/resume/tenant isolation guarantees, independent source review, and evidence from reproducible tests. The maintainer decides whether to merge. A merge does **not** automatically deploy to any user's production Money Forward instance.

There is no GitHub Actions workflow in this staging repository. Tests must be executable locally and may later use an existing authorized, non-Actions verification route.

## Reproducible local verification

Use the complete source checkout with `python -m pip install -c constraints-test.txt -e '.[test]'`, then `make test` and `make audit`. Test fixtures are synthetic and independent of a live Money Forward account. See `examples/README.md` for workflow-specific entrypoints. Check your local Git author identity before committing; use a public handle and GitHub noreply email, not an employer or personal contact address. Do not copy private denylist files into the repo.
