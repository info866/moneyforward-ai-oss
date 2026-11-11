# Money Forward AI Automation

[日本語](README.ja.md) · [Start contributing](docs/CONTRIBUTOR_START.md) · [Setup](docs/SETUP.md) · [Capabilities](docs/FEATURE_MATRIX.md) · [Contributing](CONTRIBUTING.md)

> **Beyond API connectors:** working toward accounting correctness that can be independently tested, safely resumed and audited.

**Contributors wanted:** [Start here](docs/CONTRIBUTOR_START.md) · [Open technical challenges](https://github.com/info866/moneyforward-ai-oss/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22) · [75-second synthetic demo guide](docs/DEMO_WALKTHROUGH.md)

**0.1.0a2 · Developer preview · Publication pending owner approval.**
Money Forward-specific accounting automation source for independent users and
contributors. This is an unofficial community-development project, **not a
production-certified autonomous accountant**. The repository remains private
until the separate publication action is authorized.

## Why this project

Improve the practical accounting pipeline: evidence and journal decisions,
matching/reconciliation, authorization, duplicate prevention, authoritative
read-back, and safe recovery. The code goes beyond an API connector; improvements
must be demonstrated on expected accounting outputs and failure/replay cases.

Every user operates their own Money Forward account and keeps credentials and
business records locally. No shared account, maintainer-hosted API, production
company data, private repository import or cloud agent subscription is required
for the offline development quickstart.

## Reproduce locally

The tested environment is macOS / Python 3.13; Python 3.11+ is required. Some
live adapters use macOS Keychain and external PDF tools. Other OS combinations
are not certified. Use a full source checkout or verified source archive.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -c constraints-cloud.txt -e '.[test,cloud]'
make test test-cloud test-accounting audit
python examples/portable_state_demo.py
python examples/accounting_pipeline_demo.py
```

The examples create only temporary synthetic state; they do not call Money
Forward, access Keychain, or modify real accounting. Their output is explicitly
`OFFLINE_SYNTHETIC`, never proof of live-provider performance. Core-only work may
install `-c constraints-test.txt -e '.[test]'` and omit `test-cloud`.

## Included and not included

The repository includes Python accounting modules, existing SQL schema,
company-owned configuration/bootstrap, guarded provider adapters, approval and
write-claim/read-back logic, opt-in local run recovery, tests and source packaging.
See [feature matrix](docs/FEATURE_MATRIX.md) for exact support boundaries.

**Not certified:** fully unattended source intake, all five DAILY workflows
through final external delivery, payroll/attendance automation, annual close or
filing. Full live MCP/GAS integration and tenant-specific real API E2E remain
open engineering tasks. Publishing source does not mark them complete.

## Independent company and contribution workflow

Follow [SETUP](docs/SETUP.md) to generate an unconfigured independent copy.
Configure only your own authorized products/office locally. Read-only discovery
comes before any separately authorized write. Never publish real records,
credentials, company profiles, screenshots, unredacted logs or API responses.

Use an issue and branch/PR with synthetic reproduction, before/after results,
expected journal lines, replay counts and limitations. Merges never deploy to a
user's production account. GitHub Actions are not used.

## Source release and license

[MIT](LICENSE). Original copyright and license notices must be retained.
[Dependency notices](THIRD_PARTY_NOTICES.md) cover separately installed software.
Money Forward names identify interoperability only; this project is not endorsed
by Money Forward, Inc., and no provider SDK, service, credential or customer data
is redistributed. Each adopter must follow its own API/product agreements.

For project-only maintainer contact, see [MAINTAINERS](MAINTAINERS.md). External outreach materials are held for an authorized Public launch; no official partner relationship has been claimed.

See [source scope](docs/PUBLIC_RELEASE_SCOPE.md), [source release checks](docs/RELEASE_GATES.md),
[production gates](docs/PRODUCTION_GATES.md), [security](SECURITY.md), and
[publication steps](docs/PUBLICATION_CHECKLIST.md). Build a source snapshot only
from a clean committed checkout with `make source-release`; verify the archive
before extraction. A successful build never publishes anything.
