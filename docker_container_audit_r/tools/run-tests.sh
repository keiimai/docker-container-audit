#!/usr/bin/env bash
# --- R の子のテスト・lint と、操作側の環境の固定をまとめて確かめる（Python は tools/run-tests.sh）（CHECKS.md 1 節）---
# --- 子の根に移る ---
set -uo pipefail
root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$root" || exit 1

# --- 引数は取らない ---
if [ "$#" -gt 0 ]; then
  echo "使い方: bash tools/run-tests.sh" >&2
  exit 2
fi

# --- 検査用の外で呼ばれたら、検査用の中で自分を動かし直す（README.md「本番の手順」）---
if [ "${AUDIT_RUNNER:-}" != r ]; then
  exec bash "$root/tools/runner.sh" run bash tools/run-tests.sh
fi

# --- 文字コードを UTF-8 にする（パスの日本語を読むため。R は読めないと止まる）---
case "${LC_ALL:-${LANG:-}}" in
  *UTF-8* | *utf8* | *UTF8*) ;;
  *) if [ "$(uname -s)" = Darwin ]; then export LANG=en_US.UTF-8; else export LANG=C.UTF-8; fi ;;
esac

# --- テストの一時ファイルは子の _tmp/ に置く（CHECKS.md 1 節）---
mkdir -p "$root/_tmp"

# --- 工程を走らせ、不合格を集める ---
failed=()
step() {
  local name="$1"
  shift
  echo "=== ${name}"
  if "$@"; then echo "--- ${name}: 合格"; else echo "--- ${name}: 不合格" >&2; failed+=("$name"); fi
}

# --- 操作側の R で、renv.lock・lint・テスト（ファイルごと）を走らせる（README.md「テスト」）---
PATH="$PATH:/usr/local/bin:/opt/homebrew/bin:/Library/Frameworks/R.framework/Resources/bin"
if ! command -v Rscript >/dev/null 2>&1; then
  echo "Rscript がありません。検査用のイメージ（bash tools/runner.sh build）を作ってください。" >&2
  exit 1
fi
step "R: renv.lock と環境・パッケージの一覧" Rscript --vanilla tools/lock-r-environment.R --check
step "R: lint（骨格の .lintr）" Rscript --vanilla tools/lint-r.R
for test in tests/testthat/test-*.R; do
  step "R: ${test}" Rscript --vanilla -e \
    'testthat::test_file(commandArgs(TRUE)[[1L]], reporter = "summary", stop_on_failure = TRUE)' "$test"
done

# --- 不合格があれば終了コード 1 で終える ---
if [ "${#failed[@]}" -gt 0 ]; then
  echo "不合格: ${failed[*]}" >&2
  exit 1
fi
echo "R の子のテストはすべて合格しました。"
