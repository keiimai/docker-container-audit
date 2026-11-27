# Container Dependency Audit

R または Python のプログラムを動かす Docker イメージを、依存の確定から監査まで 3 段階で作ります。

1. 第1段階：**依存を調べて確定する**段階では、必要な言語パッケージと Ubuntu パッケージの版を調べます。確定すると、`dependency-lock.tsv` と、`ubuntu-pins.txt`、R の `r-pins.tsv` または Python の `pylock.toml` などの固定一覧ができます。
2. 第2段階：**ビルド用ファイルを生成する**段階では、確定記録から Dockerfile とビルドに使うファイルを `projects/<名前>/dockerfile/` にそろえます。
3. 第3段階：**イメージをビルドして監査する**段階では、導入したパッケージの版を確定記録と照合し、パッケージや共有ライブラリの読み込みを確認します。監査結果と設定に応じて、Docker の保存ファイル（`.tar`）と Apptainer の SIF を作り、それぞれの中身も監査します。

各段階の結果は Quarto の HTML 報告書にまとめ、設定の写しと実行記録も残します。

**Root** はリポジトリの最上位です。使う言語の README に従って、R または Python のフォルダで実行します。 [R の実行手順](docker_container_audit_r/README.md#本番の手順) · [Python の実行手順](docker_container_audit_python/README.md#本番の手順)

## 本番の手順

Mac で Docker Desktop を起動します。Git とネットワーク接続も必要です。R・Python・Quarto は検査用コンテナで動きます。報告書は言語ごとに 1 本ずつ描画します。

### 検査対象のフォルダ

見本は R の `projects/example-r/`、Python の `projects/example-python/` です。別のプロジェクトを調べるときは、見本の `config.yaml` と `request/` を `projects/<名前>/` に写します。設定の `project` と `experiment_name` をその名前に変更し、`image.dependency_lock`、`image.compare_to`、`discovery.review_notes` を空にします。名前は小文字の英数字で始め、小文字の英数字・ハイフン・下線を使えます。

| `request/` の入力        | R                                                                                     | Python                                                          |
| -------------------------- | ------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| 言語の依存                 | `DESCRIPTION`                                                                       | `requirements.txt`                                            |
| 固定ベース                 | `base.txt` にダイジェスト付きの `BASE_IMAGE` と日付付きの `R_REPOSITORY` を書く | `base.txt` にダイジェスト付きの `BASE_IMAGE` を書く         |
| 追加する Ubuntu パッケージ | 必要に応じて `ubuntu-packages.txt` に書く                                           | `ubuntu-packages.txt` に `python3` と必要なパッケージを書く |

### 第 1 段階の中身

依存を調べて確定します。報告書の `confirm` が `PASS` になったら、表 `dockerfile-inputs` の `dependency_lock` に出たパスを、プロジェクトの `config.yaml` の `image.dependency_lock` に書きます。

要確認項目は `dependency-review.csv` で確認します。入力を直すか、確認根拠を `item`・`detail`・`reason` の 3 列の TSV に書いて `discovery.review_notes` にそのパスを指定し、第 1 段階を描き直します。

### 第 2 段階の中身

第 1 段階の確定記録から `projects/<名前>/dockerfile/` に Dockerfile とビルド用ファイルを生成します。同名で内容が異なるファイルを置き換える場合は、`config.yaml` の `dockerfile` の節で `replace: true` を指定します。

### 第 3 段階の中身

第 2 段階と同じ `image.dependency_lock` で、生成した Dockerfile を使ってイメージをビルドし、監査します。イメージ名の既定は `<名前>:local` です。元のイメージの監査が `PASS` または `REVIEW` なら、設定に従って `.tar` の保存と読み戻し、Apptainer の SIF の作成と監査へ進みます。

既存のイメージを監査する場合は、`image.image` にイメージ名を書き、`image.build` を `false` にします。保存や SIF の作成を省くには `image.save` や `image.sif` を `false` にします。前回の第 3 段階の報告書 ID を `image.compare_to` に指定すると、監査結果を比較します。

## 結果の確認

次のパスは R または Python からの相対パスです。`<ID>` は報告書の実行 ID です。

| 生成されるもの | 場所 | 何に使うか |
| --- | --- | --- |
| 確定記録（`dependency-lock.tsv` と固定したパッケージの一覧） | `outputs/evidence/<ID>/confirm/`（第 1 段階） | Dockerfile の生成とイメージの監査 |
| Dockerfile とビルド用ファイル | `projects/<名前>/dockerfile/`（第 2 段階） | Docker イメージのビルド |
| Docker イメージの保存ファイル（`.tar`） | `outputs/images/<ID>/`（第 3 段階） | Docker に読み込んで実行 |
| Apptainer イメージ（`.sif`） | `outputs/images/<ID>/`（第 3 段階） | Apptainer で直接実行 |
| HTML 報告書 | `outputs/reports/<ID>/` | 各段階の結果の確認 |
| 工程の判定表 `steps.csv`（第 1・第 3 段階） | `outputs/tables/<ID>/` | 工程ごとの判定の確認 |
| 実行記録と設定の写し | `outputs/repro/<ID>/` | 実行時の設定や記録の確認 |
| 詳しい照合結果とログ | `outputs/evidence/<ID>/<工程>/` | 判定の理由やエラーの確認 |

`.tar` と `.sif` は、監査結果と第 3 段階の保存設定に応じて生成されます。

- **Docker で使う**場合は、ビルドしたイメージ名（`image.image` の設定値。既定は `<名前>:local`）を `docker run` に指定します。保存した `.tar` から使う場合は、`docker image load -i 'outputs/images/<ID>/<ファイル名>.tar'` で読み込み、表示されたイメージ名を `docker run` に指定します。
- **Apptainer で使う**場合は、`.sif` を直接指定します。たとえば `apptainer exec 'outputs/images/<ID>/<ファイル名>.sif' cat /etc/os-release` で、コンテナ内の OS を確認できます。

第 1・第 3 段階では、実行予定の工程が `PASS` かを確認します。`REVIEW`、`FAIL`、予定外の `NOT_RUN` があれば、判定表の `detail` とログを見ます。第 2 段階の報告書には生成したファイルと SHA-256 が載ります。第 2・第 3 段階を再実行するため、`image.dependency_lock` が指す `confirm/` フォルダは一式を保管してください。

## ライセンスと引用

- コードは [MIT](https://opensource.org/license/mit)（`LICENSE`）です。
- この環境を引用するときは、`CITATION.cff` の情報を使ってください。作者への問い合わせ先も、`CITATION.cff` にあります。

## AI の使用

コードと文書は、AI のコーディング支援を道具として使って書いた。Anthropic の [Claude Opus 5.5](https://www.anthropic.com/claude/opus)（[Claude Code](https://code.claude.com/docs/en/overview) で使った）とOpenAI の [GPT-6.2 Sol](https://developers.openai.com/api/docs/models/gpt-6.2-sol)（[Codex](https://openai.com/codex/) で使った）で開発した。
