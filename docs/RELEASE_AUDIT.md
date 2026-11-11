# 公開準備監査 — 2026-11-11

## 最終判定

**BLOCKED / Privateを維持。** 機密情報の混入を減らす是正と開発素材の整備は実施したが、独立した利用者の実務E2Eと配布物だけで完結する実行経路は未検収。公開基準を引き下げて合格にはしていない。

## 監査開始時の事実

対象は文書8ファイルのみで、ソース・テスト・ライセンス・導入用パッケージが未収録だった。全11コミットのauthor/committerメタデータに、匿名化されていない名前または会社に関連するメールがあった。値そのものは本レポートへ転載しない。

開始時に全ブランチ/タグ、Gitの履歴とユニークなblob、Issue/コメント/PR、Release/asset、fork、Actions run/artifact、webhook、deploy key、environment、Pages/Wiki/Discussions設定を確認した。1ブランチ、1 Issue、他の対象コレクションは0。Actionsは実行0件だったが設定上は有効だった。

## 是正した内容

- 個人情報のある元の準備履歴を別の**非公開アーカイブ**に保全。履歴削除やforce-pushはせず、候補は新規リポジトリと独立履歴で分離した。旧履歴は候補へ一切importしていない。
- 新候補のcommit identityは一般名とGitHub noreply。GitHub Actionsは設定上も無効。
- 既存の会社分離済みコードから111ファイルを選定。ソース、SQL、未設定の設定例、bootstrap、架空データのテストを収録。コピー元のGit履歴・会社原本・DB・認証・実行ログは収録しない。
- README日英、導入手順、依存ライセンス一覧、MIT本文、変更履歴、貢献手順、実行可能なテスト例を整備。
- ライセンスと文書が法人別コピー生成でも脱落しないようbootstrapを修正。
- 既存監査コマンドへ全到達可能履歴/indexの検査と外部非公開denylist対応を追加。検出した秘密値そのものは出力しない。
- 会計結果のGitHub報告先を、名前が一致したPrivateリポジトリに限定。Public/不明/別名は送信前に拒否。
- 報告本文の固定的な「7/7検証済み・未処理0」等を廃止し、実際のrun/stage/remaining情報を使用。未処理を完成に偽装しない。

## 検証済みの範囲

| 検査 | 実結果 | 解釈 |
| --- | --- | --- |
| 元の分離済みコードの回帰テスト | 307件合格 | Git HEADが必要な2ケースはローカルの空commit作成後に再検証。assertionは弱めていない |
| 是正後の隔離回帰テスト | 324件合格 | ネットワークと本来のユーザーディレクトリ/KeychainアクセスをOS sandboxで遮断 |
| 新規venvのeditable install | 成功、pip check正常 | 同じMac上の新環境。別会社での実API成功ではない |
| Ruff / CLI help / 静的依存監査 | 合格 | 安全性・全機能の完全証明ではない |
| Gitleaks 8.30.1 | 既知secretパターン検出なし | 公式配布バイナリのチェックサム照合済み |
| 非公開の既知固有名・メール・会社ドメイン照合 | 候補内の一致なし | denylist自体はリポジトリ外で保管 |
| 別AIの読み取り専用ソースレビュー | 実在と確認できる固定の個人/会社値は未検出、公開判定はBLOCKED | 約100 PythonファイルのAST・重点経路を確認。全経路の逐行証明ではない。後続修正前の詳細はINDEPENDENT_REVIEW.json |
| 本番会計・給与・認証・DBの変更 | 0 | 監査のための無断実書込みなし |

生成したテスト入力は合成fixtureを利用する。`.test` / `.invalid` / `example.com` の例、Money Forward等の公式製品名・APIドメイン、法人種別の正規化文字列は残っているが、所有者の私的な会社データとして扱っていない。GitHubの所有者ハンドルや許可されたnoreplyは公開時に見えるため、完全匿名の配布という意味ではない。

## 未解決の公開条件

### R1: 配布物だけでは全業務が完結しない

一部の資料取得・分類は別途GAS実装が必要。同梱MCPは読取専用であり、完全なクラウド会計run、仕訳/証憑write、Cross Audit、最終通知を提供しない。外部の既存環境が必須となる経路を、第三者が設定できる配布物または実証済み連携として整える必要がある。給与/勤怠/無人起動/決算も実証済みと表示しない。

### R2: 実務品質の受入未完了

独立した利用者環境での原本→仕訳→証憑→公式読戻し→監査までの実API検証、部分停止後の再開と再実行時の追加writeゼロが未検収。今回の合格数は模擬環境に限定する。既存の本番実績をこの候補の検収に転用しない。

### R3: 最終権利確認・公開承認

MIT本文と実際のテスト依存のライセンス一覧を用意した。元ソースの権利、任意の追加依存、Money ForwardのAPI契約・製品権限、最終公開範囲を所有者が確認する必要がある。公開可否を機械的に自動承認しない。

## 再検証

```sh
python -m pip install -c constraints-test.txt -e '.[test]'
make test
make audit
PYTHONPATH=src python -m mf_phase12.starter_audit --history
# The private denylist must be outside this checkout, never supplied in an issue.
PYTHONPATH=src python -m mf_phase12.starter_audit --history --denylist /path/outside/repository/private-denylist.json
```

GitHub上の全refs、Issue/PR/asset、公開設定の検査はローカル監査とは別。ファイルの追加や公開承認時には、同じ範囲を再検査する。個人情報の絶対的な不存在や将来の無事故を保証するものではない。

## 公式参照

- GitHubの機密履歴削除ガイド: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository
- MITライセンス本文: https://choosealicense.com/licenses/mit/
- Money Forward API利用規約: https://biz.moneyforward.com/agreement/api/

既知の履歴を書き換えてもclone/cached SHA等に残り得るため、候補には旧Gitオブジェクトを渡していない。各利用者の正規認証、会員情報の取扱い、公式との非提携表示を維持する。本監査は法的適合性の確約ではない。
