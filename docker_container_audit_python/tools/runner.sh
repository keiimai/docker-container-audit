#!/usr/bin/env bash
# --- Python の子の作業を Python の検査用のイメージの中で行う入口（build・render・lock・run）（R は tools/runner.sh）（README.md「本番の手順」）---
# --- 子の根と親、イメージの名前、定義のファイル、Docker の置き場所 ---
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd -P)"
parent="$(cd "$root/.." && pwd -P)"
platform="linux/arm64"
image="${AUDIT_RUNNER_IMAGE_PREFIX:-container-audit-runner}-python:local"
definition_files=(Dockerfile.runner pyproject.toml uv.lock .python-version)
PATH="$PATH:/usr/local/bin:/opt/homebrew/bin:$HOME/.docker/bin:/Applications/Docker.app/Contents/Resources/bin"

# --- 使い方を出して止める ---
usage() {
  echo "使い方: bash tools/runner.sh build" >&2
  echo "        bash tools/runner.sh render notebooks/<名前>.qmd [名前:値 ...]" >&2
  echo "        bash tools/runner.sh lock" >&2
  echo "        bash tools/runner.sh run COMMAND [ARGS...]" >&2
  exit 2
}

# --- 定義のファイルをこの順につないだ SHA-256（README.md「本番の手順」）---
definition_hash() {
  (cd "$root" && cat "${definition_files[@]}") |
    if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi | cut -c1-64
}

# --- 検査用のイメージの中でコマンドを動かす（イメージが無いか、定義が変わっていれば止める）（README.md「本番の手順」）---
run_in_runner() {
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "検査用のイメージ $image がありません。先に bash tools/runner.sh build で作ってください。" >&2
    exit 1
  fi
  built="$(docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$image" \
    | sed -n 's/^AUDIT_RUNNER_DEFINITION=//p')"
  if [ "$built" != "$definition" ] && [ -z "$allow_changed_definition" ]; then
    echo "検査用の定義（${definition_files[*]}）が、イメージ $image を作ったときから変わっています。" >&2
    echo "bash tools/runner.sh build で作り直してください。" >&2
    exit 1
  fi
  image_id="$(docker image inspect -f '{{.Id}}' "$image")"
  tty=()
  if [ -t 0 ] && [ -t 1 ]; then tty=(-it); fi
  exec docker run --rm ${tty[@]+"${tty[@]}"} ${scratch[@]+"${scratch[@]}"} --platform "$platform" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$parent":"$parent" -w "$root" \
    -e "AUDIT_RUNNER_IMAGE=$image" -e "AUDIT_RUNNER_IMAGE_ID=$image_id" \
    -e AUDIT_TEST_PYTHON_IMAGE -e AUDIT_TEST_PYTHON_LOCK -e AUDIT_TEST_PLATFORM \
    "$image" "$@"
}

# --- 引数を読み、今の定義のハッシュを出す ---
[ "$#" -ge 1 ] || usage
scratch=()
allow_changed_definition=
action="$1"
shift
definition="$(definition_hash)"

# --- 動作ごとに分ける ---
case "$action" in
  # --- 検査用のイメージを作る（送るファイルは Dockerfile.runner.dockerignore で絞る）---
  build)
    exec docker buildx build --load --platform "$platform" -t "$image" \
      --build-arg "AUDIT_RUNNER_DEFINITION=$definition" \
      -f "$root/Dockerfile.runner" "$@" "$root"
    ;;
  # --- notebooks/ の直下の .qmd を記録層の描画で描く（REPRODUCIBILITY.md 2 節）---
  render)
    [ "$#" -ge 1 ] || usage
    qmd="$1"
    shift
    case "$qmd" in
      notebooks/*/*) echo "描けるのは notebooks/ の直下の .qmd だけです（パスは子の根から書く）: $qmd" >&2; exit 2 ;;
      notebooks/*.qmd) ;;
      *) echo "描けるのは notebooks/ の直下の .qmd だけです（パスは子の根から書く）: $qmd" >&2; exit 2 ;;
    esac
    [ -f "$root/$qmd" ] || { echo "$qmd がありません（パスは子の根から書く）。" >&2; exit 1; }
    for parameter in "$@"; do
      case "$parameter" in
        *:*) ;;
        *) echo "設定は 名前:値 の形で書いてください: $parameter" >&2; exit 2 ;;
      esac
    done
    run_in_runner env PYTHONPATH=src python -c \
      'import repro, sys; repro.render(sys.argv[1], sys.argv[2:])' "$qmd" "$@"
    ;;
  # --- 依存の固定を作り直す（定義を変えた後でも、作ってある検査用の中で動かす）（README.md「本番の手順」）---
  lock)
    [ "$#" -eq 0 ] || usage
    allow_changed_definition=1
    run_in_runner uv lock
    ;;
  # --- コマンドを動かす（テストでは子の _tmp/ を、Docker 結合テストでは _tmp/test-runtime/ だけを tmpfs に替える）（README.md「テスト」）---
  run)
    [ "$#" -gt 0 ] || usage
    if [ -z "${AUDIT_TEST_PYTHON_IMAGE:-}" ]; then
      scratch=(--tmpfs "$root/_tmp:rw,exec")
    else
      scratch=(--tmpfs "$root/_tmp/test-runtime:rw,exec")
    fi
    run_in_runner "$@"
    ;;
  *) usage ;;
esac
