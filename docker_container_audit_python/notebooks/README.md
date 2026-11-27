# notebooks/ — 解析ノートブック

[JupyterLab](https://jupyterlab.readthedocs.io/en/stable/) か、VS Code（[Jupyter の拡張機能](https://marketplace.visualstudio.com/items?itemName=ms-toolsai.jupyter)）で作業する（成果物と出力の置き場所 → 下の「出力の扱い」）。

## 構成

| ファイル | 中身 |
|---|---|
| `_metadata.yml` | Quarto の共通設定（このフォルダの直下のノートブックに効く。著者は `_project_author.yml` から読む（Quarto の[メタデータの取り込み](https://quarto.org/docs/projects/quarto-projects.html#metadata-includes)）。Quarto の[フォルダごとの設定](https://quarto.org/docs/projects/quarto-projects.html#directory-metadata)） |
| `_project_author.yml` | レポートの著者と所属（プロジェクトが書き換える） |
| `styles.css` | HTML の見た目（`_metadata.yml` が読む） |

## 描き方

リポジトリの根で、`src/` を読み込み先に足して走らせる（Quarto は環境の側で入れておく）。

```bash
PYTHONPATH=src python -c "import repro, sys; repro.render(sys.argv[1], sys.argv[2:])" notebooks/<名前>.ipynb
PYTHONPATH=src python -c "import repro, sys; repro.render(sys.argv[1], sys.argv[2:])" notebooks/<名前>.qmd
PYTHONPATH=src python -c "import repro, sys; repro.render(sys.argv[1], sys.argv[2:])" notebooks/<名前>.ipynb "名前:値"
```

ノートブックの後ろに `名前:値` を並べると、ノートブックの `parameters` のセル（`.qmd` なら YAML の `params:`）に書いた既定値を、描くときだけ上書きする（Quarto の[パラメータの説明](https://quarto.org/docs/computations/parameters.html)）。ここで変えた値は記録に残らないので、論文に載る結果に効く値は設定ファイルに書く。

ノートブックは 1 本ずつ描く（同時に描くと、先に描き終えた方が Quarto の作業フォルダ `notebooks/.quarto/` を消し、もう片方が止まることがある）。

## 出力の扱い

| 場所 | 出力 | git |
|---|---|---|
| `notebooks/` | 消してからコミットする（実データの数字が出力に残ったまま履歴に入らないように。コミット前の検査が [nbstripout](https://github.com/kynan/nbstripout) で消す → [`../docs/CHECKS.md`](../docs/CHECKS.md) 1 節） | 追跡する（外すもの → `.gitignore`） |
| `outputs/reports/<run_id>/` | **含む**（これが成果物。HTML と、`.ipynb` なら実行結果付きの写し） | 追跡しない |

## 書き方の約束

- 記録を始めて終えるセルで包んでいないノートブックは、探索と結果を読むための場所（論文に載る数字の条件 → [`../docs/REPRODUCIBILITY.md`](../docs/REPRODUCIBILITY.md) 4 節）
- `import repro` できるように、環境の側で `src/` を読み込み先に足しておく（`PYTHONPATH=src` か、パッケージとして入れる）
- 記録を始めるセル（`repro.run()`）と終えるセル（`repro.finish()`）で解析を挟み（形 → 下）、解析の本体は `src/` の関数にする。`render()` では、記録の実行 ID がレポートのフォルダの名前と同じになり、HTML の題名の帯にも出る（ほかの描き方の帯は「render() で描くと出る」）
- 読むデータのファイルとノートブック自身を、`repro.run()` の `inputs` に渡す（その SHA-256 が記録に残る）
- 1 つの設定ファイルを、複数のノートブックが読んでよい（実行 ID の名前の部分と、記録の `experiment_name` → [`../docs/REPRODUCIBILITY.md`](../docs/REPRODUCIBILITY.md) 2 節）
- パスはリポジトリの根から書き、`repro.find_root()` につなぐ（`repro.find_root() / "data/raw/<名前>.csv"`）。`render()` はリポジトリの根で、JupyterLab と VS Code の Jupyter はノートブックのあるフォルダで実行する。`run()` の `inputs` は根から数える
- 図を出すセルは `plt.show()` で終える（最後の式を `fig` にすると、同じ図が 2 回載る）
- セルの先頭に [`#| label: 名前`](https://quarto.org/docs/reference/cells/cells-jupyter.html) を書く（Quarto の図表の参照と、描画のログでの目印になる。図を参照するなら `fig-` で始める）
- 受け取ったデータは、CSV などに変換せず元の形式のまま読む
- 人が開く CSV の書き方 → [`../docs/REPRODUCIBILITY.md`](../docs/REPRODUCIBILITY.md) 4 節
- 書式は `_metadata.yml` と `styles.css` が決めるので、ノートブックには書かない
- 記録が `failed`・`running` のまま残るとき → [`../docs/REPRODUCIBILITY.md`](../docs/REPRODUCIBILITY.md) 2 節

ノートブックは次の形で書く（記録の流れ → [`../docs/REPRODUCIBILITY.md`](../docs/REPRODUCIBILITY.md) 2 節）。`.ipynb` では 1 つのコードのセルに 1 つの形を書き、先頭の `#| label:` も同じにする。

````markdown
```{python}
#| label: setup
import repro

root = repro.find_root()
cfg = repro.config_load(root / "configs/experiment/<設定の名前>.yaml")
repro.set_seed(cfg["seed"])
```

```{python}
#| label: begin
state = repro.run(cfg, inputs=["data/raw/<受領ファイル>", "notebooks/<名前>.ipynb"])
```

```{python}
#| label: analysis
result = my_analysis(cfg, state.run_id)
```

```{python}
#| label: finish
state.artifacts = result.paths
state.ledger = {"n_analysis": result.n}
repro.finish(state)
```
````

## このプロジェクトの Quarto 報告書

| ファイル | 内容 |
|---|---|
| [`python-dependency-discovery.qmd`](python-dependency-discovery.qmd) | 第 1 段階。`requirements.txt` をもとに Python パッケージの wheel と、それらの共有ライブラリに必要な Ubuntu パッケージを調べる。採用する版の一覧と `dependency-lock.tsv` を確定記録として残す。 |
| [`python-dockerfile-generate.qmd`](python-dockerfile-generate.qmd) | 第 2 段階。確定記録と `request/` を照合し、`dockerfile/` に Dockerfile と関連ファイルを生成する。 |
| [`python-image-build.qmd`](python-image-build.qmd) | 第 3 段階。`dockerfile/` からイメージを作り、中の Python・Ubuntu パッケージを第 1 段階の確定記録と照合する。`.tar` から読み戻したイメージと、Apptainer の SIF の中身も監査する。 |
