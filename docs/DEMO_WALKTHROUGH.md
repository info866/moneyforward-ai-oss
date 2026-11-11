# 75-second developer demo — operator-free recording script

**Status: storyboard and reproducible terminal script; not an uploaded video or evidence of a live Money Forward operation.**

This allows the maintainer or any contributor to record a screen-only walkthrough with no webcam, personal identity, invoices, finance data or real API key.

## Truthful demonstration (synthetic fixtures)

Run in a fresh checkout with Python 3.11+ after the documented installation:

```sh
python examples/accounting_pipeline_demo.py
python examples/portable_state_demo.py
```

The first command's checked output includes:

```json
{
  "evidence_level": "OFFLINE_SYNTHETIC",
  "verified_journals": 3,
  "synthetic_provider_mutations": 3,
  "synthetic_post_attempts": 3,
  "replay_extra_mutations": 0,
  "blind_retry_blocked": true,
  "uncertain_reconciled": true,
  "live_api_exercised": false,
  "accounting_workflow_completed": false,
  "production_ready": false
}
```

Counts are only for a mock transport that exercises real first-party request/approval/write/read-back Python functions. These are not real Money Forward API transactions.

## 75-second narration/caption plan

| Timing | Narration (Japanese) | On-screen evidence |
| --- | --- | --- |
| 00–12s | 「Money ForwardをAIにつなぐだけでは、実務は安全に完了しません。」 | Title: Money Forward AI Automation — unofficial OSS |
| 12–24s | 「重要なのは仕訳の正確さ、二重登録防止、途中停止からの再開です。」 | Open the repository's synthetic demo and tests |
| 24–40s | 「架空の2税率の仕訳を、実装された登録・読戻し処理で照合します。」 | Run `python examples/accounting_pipeline_demo.py`; highlight `OFFLINE_SYNTHETIC` |
| 40–56s | 「登録直後の通信断では再登録せず、照合から回復します。」 | Highlight `blind_retry_blocked=true` / `uncertain_reconciled=true` |
| 56–66s | 「再実行しても、余計な登録はゼロです。」 | Highlight `replay_extra_mutations=0` |
| 66–75s | 「不足している実API・証憑・監査統合を、一緒に検証・改善しませんか。」 | Show `docs/CONTRIBUTOR_START.md` and `help wanted` Issues |

## Recording rules

1. Show a **temporary synthetic checkout** with anonymous terminal prompt; hide account login, local paths, desktop notifications, and history.
2. Use only the real command output; do not edit numbers or imply a real API result.
3. Retain `OFFLINE_SYNTHETIC` and `production_ready=false` visibly.
4. The project is independent, not officially supported by Money Forward.
5. Before uploading a video, review **every frame, subtitle, embedded metadata and voice** for private information.
6. No automatic publishing, GitHub Actions, live accounting calls, or new service is needed to demonstrate this.

For technical details and acceptance gaps see [ACCOUNTING_ACCEPTANCE](ACCOUNTING_ACCEPTANCE.md) and [FEATURE_MATRIX](FEATURE_MATRIX.md).
