#!/usr/bin/env bash
# --- 第 2 段階のビルド用ファイルを置く（README.md「第 2 段階の中身」）---
set -euo pipefail

# --- 検査に失敗した理由を表示する（README.md「第 2 段階の中身」）---
fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

if [[ $# -lt 2 || $# -gt 3 || ( $# -eq 3 && $3 != --replace ) ]]; then
  fail '使い方: bash tools/publish-dockerfile.sh <作業フォルダ> <出力先> [--replace]'
fi
stage=$1
destination=$2
project=${destination%/dockerfile}
[[ -d "$stage" && "$project" != "$destination" && -d "$project" && ! -L "$project" ]] ||
  fail '出力先は、既存のプロジェクトの dockerfile/ にしてください。'

exec {project_fd}<"$project"
flock --exclusive "$project_fd" || fail '同じプロジェクトへの生成をロックできません。'
if [[ -L "$destination" || ( -e "$destination" && ! -d "$destination" ) ]]; then
  fail "出力先は通常のフォルダにしてください: $destination"
fi

files=(ubuntu-pins.txt)
if [[ -f "$stage/r-pins.tsv" ]]; then
  files+=(r-pins.tsv install-dependencies.R)
else
  files+=(pylock.toml)
fi
files+=(Dockerfile)
changes=()
for name in "${files[@]}"; do
  source_file="$stage/$name"
  target="$destination/$name"
  [[ -f "$source_file" && ! -L "$source_file" ]] || fail "生成したファイルがありません: $name"
  if [[ -L "$target" || ( -e "$target" && ! -f "$target" ) ]]; then
    fail "生成するファイルの名前が、通常のファイル以外に使われています: $target"
  fi
  if [[ -f "$target" ]]; then
    if cmp -s -- "$source_file" "$target"; then
      continue
    fi
    [[ ${3-} == --replace ]] || fail "内容が異なる既存ファイルがあります: $target。置き換えるには設定の dockerfile.replace を true にするか、生成コマンドで --replace を指定してください。"
  fi
  changes+=("$name")
done
[[ ${#changes[@]} -gt 0 ]] || exit 0

backup="$stage/.before-publish"
mkdir -- "$backup"
for name in "${changes[@]}"; do
  if [[ -f "$destination/$name" ]]; then
    cp -p -- "$destination/$name" "$backup/$name"
  fi
done

# --- 保存に失敗したら元の出力に戻す（README.md「第 2 段階の中身」）---
rollback() {
  local status=$? name
  trap - EXIT HUP INT TERM
  if (( status != 0 )); then
    for name in "${started[@]}"; do
      if [[ -f "$backup/$name" ]]; then
        cp -p --remove-destination -- "$backup/$name" "$destination/$name" ||
          printf '元に戻せません: %s\n' "$destination/$name" >&2
      else
        rm -f -- "$destination/$name" || printf '取り除けません: %s\n' "$destination/$name" >&2
      fi
    done
    if [[ $created == yes ]]; then
      rmdir -- "$destination" || true
    fi
  fi
  exit "$status"
}

created=no
started=()
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
if [[ ! -d "$destination" ]]; then
  mkdir -- "$destination"
  created=yes
fi
for name in "${changes[@]}"; do
  started+=("$name")
  cp -p --remove-destination -- "$stage/$name" "$destination/$name"
done
trap - EXIT HUP INT TERM
