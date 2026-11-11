# Money Forward AI Automation

**0.1.0a2・開発者向けプレビュー。現在は非公開、公開操作は所有者承認待ち。**
Money Forward専用の会計自動化コードを、利用者自身の環境で検証・改良するためのプロジェクトです。Money Forward株式会社の公式製品ではなく、完全自律の本番会計や決算完了を保証する製品でもありません。

## 何を改善するのか

単純なAPI接続だけでなく、証憑・仕訳判断、照合、認可、二重登録防止、公式読戻し、中断復旧の実務処理を対象にします。第三者が自分のMoney Forward契約・事業所・認証を使って検証し、合成データで再現できる改善をPull Requestとして共有する方式です。

本番会社の原本・仕訳・給与・DB・認証や、元の非公開Git履歴は含めません。開発者向けのオフライン検証にはMoney Forward認証もAI API契約も不要です。

## まず動かす

検証済み環境はmacOS / Python 3.13。必要Pythonは3.11以上ですが、全OS・全バージョンでの稼働を認定していません。完全なソース一式を取得し、次を実行します。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -c constraints-cloud.txt -e '.[test,cloud]'
make test test-cloud test-accounting audit
python examples/portable_state_demo.py
python examples/accounting_pipeline_demo.py
```

テスト・デモは一時的な合成会社データだけを使用します。実際のMoney Forward接続・本番登録は行わず、結果にはOFFLINE_SYNTHETICと表示します。実MCP SDKの検査も実会計の検収ではありません。

## 配布するものと未完成部分

会計Pythonソース、SQL、法人別設定・生成スクリプト、認可・書込要求管理・公式読戻し比較、ローカルrun復旧、テスト、導入・貢献手順を含みます。

一方、すべての原本取得、GAS等の外部連携、全機能クラウドMCP、仕訳から最終監査・通知までの無人運転、給与・勤怠・年次決算は完成品として提供していません。既存の本番検収条件は維持し、未達事項は[機能一覧](docs/FEATURE_MATRIX.md)と[本番利用条件](docs/PRODUCTION_GATES.md)に明示します。

**共同開発用ソースを公開できることと、本番会計を任せられることは別です。**

## 参加方法

[導入手順](docs/SETUP.md)で自社用コピーを生成し、自社設定・秘密はGit外に保存します。不具合は架空の入力・期待仕訳・再現手順・実行結果を添え、Issue→branch→PR→レビューの順で提案してください。実帳票、会社名、従業員情報、APIキー、未加工ログは投稿しないでください。

[貢献ルール](CONTRIBUTING.md) / [改善課題](ROADMAP.md) / [公開手順](docs/PUBLICATION_CHECKLIST.md) / [セキュリティ](SECURITY.md)

ライセンスは[MIT](LICENSE)。各利用者が自分のAPI利用条件・製品権限を守って利用します。GitHub Actions、本番への自動配備、共有認証は使用しません。
