# 再現性の記録

記録層は、その数値がどのコード・どの入力・どの環境・どの設定から出たかを記録する。環境は作らない（作り方は、このテンプレートの外で決める）。

---

## 1. 構成と保証の範囲

| ファイル | 役割 |
|---|---|
| `src/repro.py` | 記録層。実行の記録・乱数の種の固定（`set_seed()`）・設定の読み込み（`config_load()`・`read_yaml()`）・BOM 付きの CSV の書き出し（`write_csv()`）・描画（`render()`）・台帳（`runs()`）・疎通確認（`smoke()`） |
| `tests/test_repro.py` | 記録層のテスト（リポジトリの根で `python -m pytest tests`。`src/` は自分で読み込み先に足す。イメージの中でしか確かめられない 1 件は、外では飛ばす） |
| `notebooks/README.md`・`notebooks/_metadata.yml`・`notebooks/_project_author.yml`・`notebooks/styles.css` | ノートブックの書き方の約束と、HTML の書式（それぞれの役目 → [`notebooks/README.md`](../notebooks/README.md)「構成」） |

| | 保証するか |
|---|---|
| 同じ環境で同じ結果が出る | 記録層は乱数の種を固定し、環境（依存・ロケール・スレッド数・ハッシュの種）を記録する。環境を揃えるのは、環境を作る側である。解析が種を渡しているかと、2 回走らせて一致するかは、プロジェクトが確かめる |
| 成果物がどのコード・どの入力・どの設定から出たか辿れる | する（コミット・`inputs` に渡したファイルと成果物の SHA-256・既定値まで含めた設定を記録する） |
| データの差し替えに気づける | する（`inputs` に渡したファイルの SHA-256）。差し替える前のデータは復元しない（データは版管理しない） |
| ダミーの数字が結果として出回らない | 記録の `data_mode` と、成果物に付けた実行 ID（名前の頭かフォルダ）で見分ける |
| 別の CPU で下位桁まで一致する | しない（arm64 と amd64 では、数値計算の実装が違う） |

---

## 2. 記録の流れ

入口のノートブックが、`repro.run()` と `repro.finish()` で実行を挟む（書き方は `notebooks/README.md`）。`import repro` できるように、環境の側で `src/` を読み込み先に足しておく（`PYTHONPATH=src` か、パッケージとして入れる）。

- 設定は `repro.config_load()` で読む。記録層が読むのは `seed`・`experiment_name`・`data.mode`・`repro`（`enabled`・`quiet`）だけで、ほかのキーはそのまま持ち越して記録する。省いたキーは既定になる（→ `src/repro.py` の `config_defaults()`）。`seed` は整数でなければ止める。`repro.quiet: true` にすると、始めと終わりの表示を出さない
- 成果物は `state.artifacts`、台帳の値は `state.ledger` に入れてから、`repro.finish(state)` を呼ぶ。`artifacts` のうちファイルでないもの（無い・フォルダ）は、警告して記録に載せない
- 失敗を記録に残すには、`repro.fail(state, error)` を呼ぶ（記録を `status: failed` で終え、`error` に理由（例外の文）を残す。例外の型とトレースバックは実行ログに残る。例: `except Exception as exc: repro.fail(state, exc); raise`）
- 台帳（`runs.csv`）の列になるのは、`state.ledger` のうち 1 つの数・文字・論理値だけである。どの実行にもある列と同じ名前は、列にしない（知らせは出ない）
- 記録を取らないときは設定を `repro.enabled: false` にする（`repro.finish()`・`repro.fail()` は何もしない）
- 作業ツリーの汚れ（コミットしていない変更）は、ノートブックの出力だけの違いを数えない（[nbstripout](https://github.com/kynan/nbstripout) が入っていれば、それを通して比べる）
- `set_seed` は、Python の [`random`](https://docs.python.org/3/library/random.html)・NumPy の大域の乱数・入っていれば [PyTorch](https://docs.pytorch.org/docs/stable/notes/randomness.html) を固定する
- 記録を置く根は `find_root()` が決める（`src/` の 1 つ上。環境変数 `REPRO_ROOT` で変えられる）
- 実行 ID は `<YYYYMMDD_HHMMSS>_<名前>_<4 桁の数>`（時刻帯 → 下）で、`outputs/` に書く成果物は、名前の頭に実行 ID を付けるか、実行 ID のフォルダ（`outputs/figures/<run_id>/` など）に入れる（`state.run_id`）
- 実行 ID は、`repro.run(cfg, inputs, run_id=...)` の引数でも渡せる（引数 → 環境変数 `REPRO_RUN_ID` → 新しく作る、の順）。描画の前などに実行 ID を知りたいときは `repro.new_run_id(名前)` で作り、その値を `run_id` に渡す。`run()` が途中で失敗したときは、開いた実行ログを閉じて進行中の状態を外してから例外を投げ直す（作りかけの記録のフォルダは残る）
- `<名前>` は、ノートブックを描いている間はノートブックの名前（`render()` が環境変数 `REPRO_RUN_ID` で実行 ID を渡す）、それ以外（セルを手で走らせたときや `smoke()`）は設定の `experiment_name` である。記録の `experiment_name`（`runs(experiment=)` で絞る名前）は、どのノートブックが読んでも設定の値のまま残る。次の段が読む `data/interim/`・`data/processed/`・`models/` のファイルは決まった名前で書き、前の段の記録の `artifacts` と次の段の `inputs` の SHA-256 で辿る
- 実行 ID と記録の時刻は、根の `.env` の `TZ` の時刻帯で付ける（`.env` に無ければ環境変数 `TZ`、それも無ければ `Asia/Tokyo`。`.env` のほかの行は読まない）。`.env` は Git では追跡しない（`python setup.py` が、書き方の見本の `.env.example` を写して作る）。時刻帯は、プロジェクトの初めに `.env` で決めて途中で変えず、共同研究者とも同じにする（実行 ID の名前には時差が入らないので、途中で変えると、実行 ID が実行した順に並ばなくなる）。記録の環境（`environment` の `env` の `TZ`）には、使った時刻帯が残る
- 記録の欄の名前と順、時刻の書き方（`2026-09-26T09:30:00+0900`）、台帳の列と書き方は、R のテンプレートの記録層と同じである（`environment` の中身と、Python だけの `log` を除く）。R と Python の記録を、同じ道具で並べて読める
- 描画した HTML は、記録の成果物に載らない（SHA-256 は `report.json` に残る）

```
outputs/repro/<run_id>/<run_id>_run.json      記録
outputs/repro/<run_id>/<run_id>.log           実行ログ（run() から finish() までの警告と、失敗したときのトレースバック）
outputs/repro/<run_id>/<run_id>.config.yaml   解決済みの設定（既定値まで含む）
outputs/repro/runs.csv                        実行台帳（runs() が作り直す。BOM 付きの UTF-8）
outputs/reports/<run_id>/                     レポート（HTML・report.json。.ipynb は実行結果付きの写しも）
```

記録と解決済みの設定は始めた時点で書かれ（記録は `status: running`）、記録は終わりに上書きされる。`render()` は、成功したときだけ HTML を `outputs/reports/<run_id>/` に写す。`render()` が失敗すると（描画の段で落ちても）、記録を `failed` に書き換え、名前かフォルダにその実行 ID を含む途中の成果物（`outputs/` の下）を `outputs/repro/<run_id>/partial/` に移す。JupyterLab などで途中でやめると、記録は `status: running` のまま残る。

`render()` は、ノートブック（`.ipynb`・`.qmd`）を [Quarto](https://quarto.org/docs/tools/jupyter-lab.html) で 1 回だけ実行させる（作業フォルダ → [`notebooks/README.md`](../notebooks/README.md)）。ノートブックから相対パスで指すファイル（参考文献・画像）も読める。

台帳は `runs()` で作り直す（`runs(experiment="smoke", limit=10, show=True)` で絞って表示する）。記録層の経路を 1 回通すには `smoke()` を走らせる。

---

## 3. 記録の読み方と再現

1. `git_dirty` が `false` なら、[`git checkout <git_commit>`](https://git-scm.com/docs/git-checkout) でそのコードに戻る（`true` なら、そのコードはもう無い。`git_commit`・`git_dirty` が null なら、Git の外で走らせたか、Git が失敗した）
2. `environment` の `python_version`・`platform`・`uv_lock_sha256`・`build_info` で、同じ環境を作る（`uv_lock_sha256` と `build_info` は、根に `uv.lock` があるときと、`/opt/build-info.json` があるときだけ入る）
3. `inputs` の SHA-256 と、手元のデータを突き合わせる
4. 同じノートブックを、そのコミットのまま `render()` で描き直し（設定もそのコミットのものを読む）、`artifacts` の SHA-256 と突き合わせる。書き出した時刻を中に持つ形式（SAV・XLSX など）は、読み込んだ中身で比べる

---

## 4. 注意

- [`PYTHONHASHSEED`](https://docs.python.org/3/using/cmdline.html#envvar-PYTHONHASHSEED) とスレッド数（`OMP_NUM_THREADS`・`OPENBLAS_NUM_THREADS` など）は、起動前に環境の側で決める（最終結果を出す実行では、スレッド数を 1 にする。1 でなければ `run()` が警告する）
- 乱数の引数（`random_state`・`seed` など）を持つ関数には、設定の `seed` から作った値を必ず渡す（`set_seed` は届かない）。`seed` は設定の 1 か所に書く
- 並列化（[joblib](https://joblib.readthedocs.io/en/stable/auto_examples/parallel_random_state.html)・[`multiprocessing`](https://docs.python.org/3/library/multiprocessing.html)）では、`set_seed` の種は子のプロセスに届かない（種の無い乱数になるか、どの仕事も同じ乱数になる）。するなら [`numpy.random.SeedSequence(seed).spawn(n)`](https://numpy.org/doc/stable/reference/random/parallel.html) で仕事ごとに種を配り、しないなら逐次で回す
- 設定は `repro.config_load()`・`repro.read_yaml()` で読む（UTF-8 として読み、YAML 1.1 と R の yaml で論理値や文字に化ける書き方（引用符の無い y・n も。[論理値](https://yaml.org/type/bool.html)・[小数](https://yaml.org/type/float.html)の決まり）は、止めるか警告する）
- 人が開く CSV は、BOM 付きの UTF-8 で書く（`repro.write_csv()`。行の辞書のリストか pandas の DataFrame を渡す。pandas だけで書くなら `to_csv(..., encoding="utf-8-sig")`）。日本語版 Excel は BOM の無い CSV を Shift_JIS として読み、日本語が化ける（[Microsoft の説明](https://support.microsoft.com/ja-jp/excel/opening-csv-utf-8-files-correctly-in-excel)）。Python で読み直すときは `encoding="utf-8-sig"` を付ける
- 論文に載る結果は、記録を始めて終えるセル（`repro.run()`・`repro.finish()`）で包んだノートブックから出す。コミットしてから、記録を取って（`repro.enabled: true`）、`render()` で描く（図は、描き方でバイトが変わる）
- テストで小数を比べるときは、許容誤差を付ける（[`pytest.approx`](https://docs.pytest.org/en/stable/reference/reference.html#pytest-approx)）

---

## 5. 依存

記録層が使うパッケージは、環境の側で入れておく。版の固定は、環境を作る側が決める。根に [`uv.lock`](https://docs.astral.sh/uv/concepts/projects/layout/) があれば、`run()` は読まれるパッケージの版が同じかを確かめ（違えば止める）、その SHA-256 を記録する。

| 使う所 | パッケージ |
|---|---|
| 記録層 | [numpy](https://numpy.org/doc/stable/)・[pyyaml](https://pyyaml.org/wiki/PyYAMLDocumentation)・[threadpoolctl](https://github.com/joblib/threadpoolctl) |
| 描画 | [Quarto](https://quarto.org/)・[ipykernel](https://ipykernel.readthedocs.io/en/stable/)（`.ipynb` を実行するとき） |
| テスト・検査 | [pytest](https://docs.pytest.org/en/stable/)・[pre-commit](https://pre-commit.com/)（コミットする場所に入れる）・[ruff](https://docs.astral.sh/ruff/)（lint と整形）（→ [`CHECKS.md`](CHECKS.md)） |
| 任意 | [nbstripout](https://github.com/kynan/nbstripout)（作業ツリーの汚れを、ノートブックの出力を除いて比べる） |
