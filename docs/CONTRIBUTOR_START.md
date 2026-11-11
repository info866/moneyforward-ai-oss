# Help build reproducible Money Forward accounting automation

**Unofficial project · developer preview 0.1.0a2 · not production-certified**

This project welcomes skilled Python, financial systems, MCP and reliability engineers who want to solve something harder than exposing an accounting API as tools: making accounting effects **reproducible and independently verifiable**.

## What already runs in synthetic tests

- A first-party accounting request/approval/POST/GET comparison chain.
- Durable journal write intents, explicit uncertain-write reconciliation and replay guards.
- Synthetic mixed-tax multi-line journals, expected tax value comparison, and a terminal example.
- Company-bound local run state and same-request recovery without a second run.

These tests **do not prove** a live Money Forward accounting close, document/voucher completeness, payroll, an authorized API canary or autonomous operation. Read [the feature matrix](FEATURE_MATRIX.md) first.

## Start contributing in about five commands

From a complete source checkout, with Python 3.11+:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -c constraints-cloud.txt -e '.[test,cloud]'
make test test-cloud test-accounting audit
python examples/accounting_pipeline_demo.py
```

The demo explicitly reports `OFFLINE_SYNTHETIC` and `replay_extra_mutations: 0`. No actual Money Forward account or paid AI API is required.

## Pick a concrete challenge

The [issues labeled help wanted](https://github.com/info866/moneyforward-ai-oss/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22) describe supported files, expected before/after behavior and acceptance tests. Beginners can start with [good first issues](https://github.com/info866/moneyforward-ai-oss/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22). More advanced contributors can work on official source coverage, tax provenance, voucher read-back and interruption/replay safety.

**Evidence for a useful PR:** a minimal failure on synthetic records, the exact expectation, a passing test after the fix, no weakened business guards, and a short note distinguishing synthetic and live evidence. We value validated defect fixes over commit/Star counts. Keep the exact failing baseline, input set, environment and evidence level (MOCK / TESTED / LIVE / REPRODUCIBLE). Prioritize accounting completion over cosmetic security tooling. See [the original specification](SPECIFICATION.md) when reviewing existing contracts.

## Privacy, authorization and maintainer contact

Each contributor uses **their own** Money Forward account/permissions if an explicitly authorized live experiment is needed. Never send tax, employee or company records, OAuth tokens, private log lines, tenant identifiers, or account credentials to this repository or its maintainers. Make fixtures synthetic *from origin*. Live writes are **not** part of standard PR verification.

Open an ordinary technical Issue/PR under the GitHub project account. For security findings use GitHub's private reporting channel where available; do not post a public exploit. No personal maintainer biography or direct email is required. Contributions and PR reviews do not automatically change any production system.

The maintainer will evaluate the technical evidence; **response times and automatic acceptance are not promised**.

## 日本語

このOSSはMoney Forward専用です。APIのツール数ではなく、**実際の仕訳内容、証憑照合、二重登録防止、中断からの再開**を検証可能にすることが目的です。まず上記の合成テスト・デモを実行してください。実会社データは提出せず、「誤りを再現する架空入力→期待値→修正→検証」をIssue/PRで共有してください。現在の公開版は全業務の本番自律稼働を保証していません。

See [CONTRIBUTING](../CONTRIBUTING.md), [SETUP](SETUP.md), [PRODUCTION_GATES](PRODUCTION_GATES.md) and [SECURITY](../SECURITY.md).
