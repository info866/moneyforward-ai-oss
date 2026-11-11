# Money Forward AI Automation — private OSS candidate

[日本語](README.ja.md) · [Setup](docs/SETUP.md) · [Release audit](docs/RELEASE_AUDIT.md)

**PRIVATE / UNRELEASED / NOT PRODUCTION-CERTIFIED.** This is a Money Forward-specific accounting automation development candidate, not a finished autonomous accountant and not an official Money Forward product.

## What is present

The repository contains existing first-party accounting code, SQLite schema, per-company profile/bootstrap, read-only onboarding MCP sample and offline regression tests. The tests cover source-to-journal decisions, evidence/read-back checks, workflow state, partial failure, tenant boundaries and replay safety using synthetic providers. Source existence and passing offline tests do not prove live provider behavior.

Each user must use their own authorized Money Forward account and locally kept credentials. No company identity, customer records, production secrets, private database or source repository history is included in the candidate tree.

## Local setup

Python 3.11+ and Git are required. The supported installation is a complete source checkout with editable installation; do not move only the Python package away from its SQL/config tree.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -c constraints-test.txt -e '.[test]'
make test
make audit
make run ARGS="--help"
```

`make test` uses synthetic fixtures. Do not provide real credentials while testing. The unconfigured example refuses real business operations. The canonical setup and company-generation instructions are in [docs/SETUP.md](docs/SETUP.md). Optional MCP dependencies are not needed for core/offline tests.

## Honest capability boundary

| Area | Candidate status |
| --- | --- |
| Journal/evidence/tenant checks and local run state | Source and synthetic tests present |
| Duplicate/replay and partial-failure scenarios | Offline regression coverage; inspect test results |
| Company-owned read-only API onboarding | Source present; real integration not certified here |
| Five DAILY workflows from source intake to final delivery | Independent adopter live E2E still unverified |
| Payroll/attendance UI operations and final calculation | Not certified or automatically configured |
| Unattended cloud operation, decision learning, annual close/filing | Not established by this release candidate |

The optional MCP is read-only and is not a replacement for a full authorized accounting connector. A passing source test, MCP listing or mock response is not a successful journal write.

## Contribution and release

See [CONTRIBUTING](CONTRIBUTING.md), [AGENTS](AGENTS.md), [SECURITY](SECURITY.md), [operating model](docs/OPERATING_MODEL.md) and [release gates](docs/RELEASE_GATES.md). No GitHub Actions or automatic production deployment is used. Work on a branch; never copy real records or secrets into a PR. The repository stays private until the owner explicitly approves publication after every release blocker is resolved.

MIT license text has been prepared in [LICENSE](LICENSE); the final source-rights and licensing decision remains an owner release gate. See [third-party notices](THIRD_PARTY_NOTICES.md).
