# Contributor outreach kit — project-only identity

**Status: prepared for a later Public launch; no application, post, email or event registration has been sent.**

## Identity and privacy

Use the project name **Money Forward AI Automation (unofficial community project)** and the existing GitHub project maintainer account only. Do not attribute this project to Money Forward, imply endorsement, introduce a made-up legal entity, or claim an engineering team exists. Do not disclose the owner's real name, profession, personal contact address, actual company, residence or business data.

GitHub repository ownership and commit handles will remain visible after publication; anonymity from the hosting platform cannot be guaranteed. Never publish private intake logs or client data. Public responses should stay technical, link to synthetic reproduction, and invite public PRs.

## One-paragraph positioning — Japanese

Money Forwardの会計自動化を「APIを呼べる」段階から「正しい仕訳を、確認できる証拠とともに、二重登録なく再現できる」段階へ進めるための、非公式オープンソース開発者プレビューです。Python製の仕訳要求・認可・書込記録・読戻し比較・中断復旧ロジックと、架空の会計データで再現できるテストを公開します。現時点では実Money Forward環境での全業務完遂や無人決算を保証しません。Python/MCP/会計API/障害復旧の経験者から、再現テスト付きのIssue・Pull Requestを募ります。

## One-paragraph positioning — English

An unofficial **Money Forward-specific** accounting automation OSS developer preview. The goal is to move beyond API connectivity toward **reproducible journal correctness, independent read-back, no duplicate posts and crash recovery**. The Python source and synthetic fixtures exercise request/approval/write/verification paths, but live end-to-end accounting, vouchers, payroll and unattended closing are **not certified**. We welcome focused issues and PRs with failing-before/fixed-after tests from Python, MCP, financial-systems and reliability engineers.

## Launch announcements — post only after an explicitly approved Public switch

### X / Threads — Japanese

Money ForwardのAI自動会計を、安全で再現可能なOSSとして改善したい。非公式の開発者向けプレビューを公開準備中です。仕訳の金額/税額検証、通信断後の二重登録防止、再開・公式読戻しの実装と合成テストを収録。未完成の実API連携も正直に公開します。Python/MCP/会計API開発者から、再現できる改善PRを募集中。公開後のGitHub: https://github.com/info866/moneyforward-ai-oss #Python #MoneyForward #OSS #MCP

### Short technical post for Zenn / Qiita — Japanese

**タイトル候補：** Money ForwardをAIで自動化するなら、MCP接続より先に「二重仕訳ゼロ」を証明したい

Money Forward専用の非公式OSSを開発しています。仕訳APIのPOSTが成功した直後に通信が切れると、単純な再送は二重登録の原因になります。そこで、書込予定の永続管理、結果不明時の再送禁止、別GETによる照合、中断・復帰の再現テストを重視しています。税込総額が同じでも本体額と税額の内訳が間違う問題も再現し、検証条件を強化しました。

公開プレビューにはソースと架空データによるテストが含まれます。実APIでの全業務稼働は未実証です。会計・Python・MCP・信頼性設計に詳しい方から、再現例・監査コメント・小さなPRを歓迎します。具体的な課題とセットアップは https://github.com/info866/moneyforward-ai-oss にまとめています。

### HN / Reddit (English)

**Title:** Show HN: Unofficial Money Forward accounting OSS — crash-safe journal replay with synthetic tests (developer preview)

We are building a Money Forward-specific Python accounting automation project. The tricky part is not exposing endpoints: it's proving that journal lines, tax splits, durable intents and official GET read-back all agree—even after a timeout following a commit. This preview has deterministic synthetic scenarios and a no-blind-retry path. It is **not** a production-certified unattended accountant. Looking for code review and focused contributions on source provenance, vouchers and safe replay.

Repo after launch: https://github.com/info866/moneyforward-ai-oss. Please follow each community's self-promotion and posting rules; don't cross-post repetitive promotional messages.

## Official Money Forward AI-agent partner inquiry

Official contact: https://developers.biz.moneyforward.com/ai-agent/

The current required Google Form asks for company name, company website, named contact, position, email, project category, Money Forward usage and privacy consent. **Do not submit using invented details**, auto-tick legal declarations, or expose owner details without a separate explicit authorization to disclose them.

**Proposed technical summary for the form once identity handling is approved:**

> Money Forward専用の非公式Python OSSを準備しています。AIが経理業務を進める際の、事業所境界、仕訳・税区分の正確な照合、結果不明POSTの安全な復旧、同一runの再開を重視しています。合成テストと再現できる開発者向けコードは整備済みです。実APIの本番E2Eは未検証であり、技術レビュー、対応API仕様、開発者コミュニティへの適切な紹介経路についてご相談したいと考えています。

If a public launch is not yet approved, do not give the partner programme a private GitHub URL as though it were accessible.

## Targeted outreach sequence (one high-value post per community)

1. Release gate + owner approval: re-scan repository, Issue/PR content and history; approve Public separately; confirm private vulnerability reporting.
2. First launch day: publish Japanese and English README, a **verified** synthetic demo, 5 scoped issues, and topics `moneyforward`, `accounting`, `python`, `mcp`, `ai-agent`, `fintech`, `open-source`.
3. Days 1–3: share the technical failure/recovery example on Qiita or Zenn and one professional social account. Do not imply Money Forward endorsement.
4. Within a week: contact the official partner programme with owner-approved professional contact information; approach developers on relevant public technical threads **only where outreach is permitted and useful**.
5. Weeks 2–4: respond to constructive PRs, review reproducible tests, publish accepted fixes with evidence; no spam and no automatic external business writes.

## What to measure

Verified new contributors, independently reproduced defects, reviewable PRs, merged fixes without regression, and **actual live acceptance evidence if explicitly authorized**. Watchers, impressions and Stars are only discovery signals, not system quality. A merged PR is not a production deployment.

Until Public approval the only permitted actions are private preparation, synthetic testing and safe GitHub metadata updates. The maintainer can delegate writing, technical triage and copy editing to AI, but external platform identities and account-based consents cannot be fabricated.
