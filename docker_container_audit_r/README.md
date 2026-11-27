# R の実行手順

Docker Desktop を起動し、Root から R（`docker_container_audit_r/`）に移動して実行します。入力の用意と結果の見方は [Root の README](../README.md) を参照してください。

## 本番の手順

見本は `example-r` です。別のプロジェクトで実行するときは、各コマンドの `project:` の後をその名前に置き換えます。

### 準備

初回と、検査用イメージの定義を変えたときに実行します。

```sh
bash tools/runner.sh build
```

### 第 1 段階の中身

```sh
bash tools/runner.sh render notebooks/r-dependency-discovery.qmd project:example-r
```

`confirm` が `PASS` になったら、報告書の `dockerfile-inputs` に出た `dependency_lock` のパスを、プロジェクトの `config.yaml` の `image.dependency_lock` に書きます。 [見本の設定](projects/example-r/config.yaml)

### 第 2 段階の中身

```sh
bash tools/runner.sh render notebooks/r-dockerfile-generate.qmd project:example-r
```

ビルド用ファイルが `projects/<名前>/dockerfile/` にそろったことを確認します。

### 第 3 段階の中身

同じ `image.dependency_lock` を指定した設定で実行します。

```sh
bash tools/runner.sh render notebooks/r-image-build.qmd project:example-r
```

## テスト

`bash tools/run-tests.sh` で動作を確認できます。Docker 結合テストは、`AUDIT_TEST_R_IMAGE` と `AUDIT_TEST_R_LOCK` にイメージ名と確定記録のパスを指定すると実行します。
