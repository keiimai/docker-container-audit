# コミット前の検査と lint

---

## 1. コミット前の検査

`.githooks/pre-commit` が、コミットの前にステージしたファイルを検査し、指摘があればコミットを止める（git の [githooks の説明書](https://git-scm.com/docs/githooks)）。フックは、リポジトリを写したり clone したりするたびに、リポジトリの根で 1 回有効にする（有効にしないと、何も止まらない）。

```bash
chmod +x .githooks/pre-commit
git config --local core.hooksPath .githooks
```

git 2.54 以降なら、次の 2 行で、マージのコミットにも同じ検査を掛ける（[git hook](https://git-scm.com/docs/git-hook)）。

```bash
git config --local hook.precommit-checks.command .githooks/pre-commit
git config --local --replace-all hook.precommit-checks.event pre-merge-commit
```

追跡中の全ファイルに同じ検査を掛けるには、`bash .githooks/pre-commit --all` を走らせる。

表の上の 7 つは bash と git だけで走る。下の 3 つは、コミットする場所の [pre-commit](https://pre-commit.com/) が `.pre-commit-config.yaml` に従って走らせる（`pre-commit` のコマンドか、`python3 -m pre_commit` で動くもの）。pre-commit は、初めて走るときに道具を取ってきて、`.git/pre-commit-cache/` に置く（消してよく、次のコミットで作り直す）。pre-commit が見つからなければ、下の 3 つを飛ばして知らせ、上の 7 つの指摘だけで止める。

| 検査 | 止めるもの |
|---|---|
| `uv.lock` の追跡 | 根に `uv.lock` があるのに、入れ忘れたコミット |
| `data/`・`outputs/`・`models/` の下 | データと成果物（`data/README.md`・`.gitkeep` は通す） |
| `.env` | 秘密情報を書くファイル（`.env.example` は通す） |
| `data/` の外のデータの形式 | CSV・Excel・SPSS・Parquet・pickle・NumPy・圧縮ファイル（zip・gz・tar など）など（`docs/definition/`・`configs/`・`tests/` は通す） |
| `.gitignore` が外すファイル | 追跡しているのに `.gitignore` に当たるもの。`git add -f` で入れたものと、`.gitignore` に行を足す前から追跡していたもの（`docs/definition/`・`configs/`・`tests/` のデータの形式は通す） |
| 大文字小文字だけが違う名前 | macOS・Windows と Linux での衝突 |
| 衝突の印 | `<<<<<<<` などを残したまま仕上げたマージ・cherry-pick・revert（`git rebase --continue` は git がフックを呼ばないので、掛からない） |
| [pre-commit-hooks](https://github.com/pre-commit/pre-commit-hooks) | 2 MB を超えるファイル・秘密鍵・Python・YAML（`CITATION.cff` を含む）の構文・行末の空白と末尾の改行（Markdown と表の形のデータの行末の空白は見ない。直してから止めるので、`git add` し直す） |
| [nbstripout](https://github.com/kynan/nbstripout) | ノートブックの出力（消してから止めるので、`git add` し直す） |
| `tools/precommit_checks.py` | よく知られたトークン（[detect-secrets](https://github.com/Yelp/detect-secrets) の決まった形の規則と、[gitleaks](https://github.com/gitleaks/gitleaks/blob/master/config/gitleaks.toml) の `github-fine-grained-pat`）・秘密情報らしい名前に書いた値（[gitleaks](https://github.com/gitleaks/gitleaks/blob/master/config/gitleaks.toml) の `generic-api-key` の規則。コードでは引用符の中の値）・JSON の構文・`src/` の関数が記録の流れ（`repro.run()` など）を呼ぶこと（→ [`REPRODUCIBILITY.md`](REPRODUCIBILITY.md) 2 節） |

---

## 2. lint と整形

`ruff.toml` が、[ruff](https://docs.astral.sh/ruff/rules/) の決まりを決める（1 行 100 字まで・見る規則・docstring の中のコードも整える）。`notebooks/`・`data/`・`models/`・`outputs/` の外の Python を見る。Python の版は書いていないので、古い書き方を新しい書き方に直す規則（`UP`）は、ruff の既定の版に合わせて働く。版を決めたプロジェクトは、`ruff.toml` の頭に `target-version`（例: `"py312"`）を足す。lint と整形は、リポジトリの根で走らせる（コミット前の検査には入っていない）。

```bash
ruff check .
ruff format .
```
