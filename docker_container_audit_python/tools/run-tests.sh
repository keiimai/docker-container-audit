#!/usr/bin/env bash
# --- Python の子のテスト・lint と、操作側の環境の固定をまとめて確かめる（R は tools/run-tests.sh）（CHECKS.md 1 節）---
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
if [ "${AUDIT_RUNNER:-}" != python ]; then
  exec bash "$root/tools/runner.sh" run bash tools/run-tests.sh
fi

# --- 文字コードを UTF-8 にする（パスの日本語を読むため。R は読めないと止まる）---
case "${LC_ALL:-${LANG:-}}" in
  *UTF-8* | *utf8* | *UTF8*) ;;
  *) if [ "$(uname -s)" = Darwin ]; then export LANG=en_US.UTF-8; else export LANG=C.UTF-8; fi ;;
esac

# --- テストの一時ファイルは子の _tmp/ に置く（CHECKS.md 1 節）---
test_tmp="$root/_tmp/test-runtime"
mkdir -p "$test_tmp"

# --- 工程を走らせ、不合格を集める ---
failed=()
step() {
  local name="$1"
  shift
  echo "=== ${name}"
  if "$@"; then echo "--- ${name}: 合格"; else echo "--- ${name}: 不合格" >&2; failed+=("$name"); fi
}

# --- 操作側の Python の環境で、uv.lock・lint・整形・pytest を走らせる（README.md「テスト」）---
venv="${UV_PROJECT_ENVIRONMENT:-.venv}"
if [ ! -x "$venv/bin/python" ]; then
  echo "$venv がありません。検査用のイメージ（bash tools/runner.sh build）を作ってください。" >&2
  exit 1
fi
if command -v uv >/dev/null 2>&1; then
  step "Python: uv.lock と pyproject.toml" uv lock --check
fi
step "Python: lint（骨格の ruff.toml）" "$venv/bin/ruff" check .
step "Python: 整形の確認" "$venv/bin/ruff" format --check .
step "Python: pytest" env TMPDIR="$test_tmp" GIT_CEILING_DIRECTORIES="$test_tmp" \
  "$venv/bin/python" -m pytest tests -q -rs -p no:cacheprovider

# --- 不合格があれば終了コード 1 で終える ---
if [ "${#failed[@]}" -gt 0 ]; then
  echo "不合格: ${failed[*]}" >&2
  exit 1
fi
echo "Python の子のテストはすべて合格しました。"
