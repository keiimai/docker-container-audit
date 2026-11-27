# Python の実行手順

Docker Desktop を起動し、Root から Python（`docker_container_audit_python/`）に移動して実行します。入力の用意と結果の見方は [Root の README](../README.md) を参照してください。

## 本番の手順

見本は `example-python` です。別のプロジェクトで実行するときは、各コマンドの `project:` の後をその名前に置き換えます。

### 準備

初回と、検査用イメージの定義を変えたときに実行します。

```sh
bash tools/runner.sh build
```

### 第 1 段階の中身

```sh
bash tools/runner.sh render notebooks/python-dependency-discovery.qmd project:example-python
```

`confirm` が `PASS` になったら、報告書の `dockerfile-inputs` に出た `dependency_lock` のパスを、プロジェクトの `config.yaml` の `image.dependency_lock` に書きます。 [見本の設定](projects/example-python/config.yaml)

### 第 2 段階の中身

```sh
bash tools/runner.sh render notebooks/python-dockerfile-generate.qmd project:example-python
```

ビルド用ファイルが `projects/<名前>/dockerfile/` にそろったことを確認します。

### 第 3 段階の中身

同じ `image.dependency_lock` を指定した設定で実行します。

```sh
bash tools/runner.sh render notebooks/python-image-build.qmd project:example-python
```

## テスト

`bash tools/run-tests.sh` で動作を確認できます。Docker 結合テストは、`AUDIT_TEST_PYTHON_IMAGE` と `AUDIT_TEST_PYTHON_LOCK` にイメージ名と確定記録のパスを指定すると実行します。
