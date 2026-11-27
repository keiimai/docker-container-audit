# --- 確定記録からビルド用ファイルを生成する（R は R/generate-dockerfile.R）（README.md「第 2 段階の中身」）---

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import container
from evidence import read_lock_entries
from image import _verify_python_dependency_lock
from project import (
    load_project_config,
    project_folder,
    read_base_file,
    required_lock,
    target_inputs,
)

#: Python の生成に使う Dockerfile のひな形。
_RECIPE = container.ROOT / "dockerfile-templates" / "Dockerfile.generate"

#: R と Python に共通の保存処理。
_PUBLISH = container.ROOT.parent / "tools" / "publish-dockerfile.sh"

#: 確定記録からそのまま写すビルド用ファイル。
_PINS = ("ubuntu-pins.txt", "pylock.toml")

#: 検査用のイメージの中で第 2 段階を実行する使い方。
_USAGE = "使い方: bash tools/runner.sh run python src/generate_dockerfile.py <プロジェクト名> [--replace]"


# --- 確定記録を今の第 1 段階の入力と照合する（README.md「第 2 段階の中身」）---
def _verify_request(project, environment):
    _, paths = target_inputs(project)
    current = {name: container.file_sha256(path) for name, path in paths.items()}
    current.setdefault("ubuntu-packages.txt", None)
    for name, digest in current.items():
        if environment["inputs"].get(name) != digest:
            raise ValueError(f"第 1 段階の確定記録と request/ が一致しません: {name}")
    if read_base_file(paths["base.txt"])["BASE_IMAGE"] != environment["base_image"]:
        raise ValueError("第 1 段階の確定記録と request/base.txt の BASE_IMAGE が一致しません。")


# --- ひな形に固定したベースと APT スナップショットを入れる（README.md「第 2 段階の中身」）---
def _render_dockerfile(environment):
    snapshot = environment["apt_snapshot"]
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", snapshot or ""):
        raise ValueError("確定記録の APT スナップショットが不正です。")
    recipe = _RECIPE.read_text(encoding="utf-8")
    for key, value in (("BASE_IMAGE", environment["base_image"]), ("APT_SNAPSHOT", snapshot)):
        placeholder = f"@@{key}@@"
        if recipe.count(placeholder) != 1 or f"ARG {key}={placeholder}" not in recipe.splitlines():
            raise ValueError(f"Dockerfile のひな形に {key} の差し込み先が 1 つ必要です。")
        recipe = recipe.replace(placeholder, value)
    return recipe


# --- ビルド用ファイルを一時フォルダで作ってからプロジェクトに保存する（README.md「第 2 段階の中身」）---
def generate_python_dockerfile(project, *, replace=False):
    config = load_project_config(project)
    configured_lock = config["image"].get("dependency_lock")
    if configured_lock is not None and not isinstance(configured_lock, str):
        raise ValueError("設定ファイルの image.dependency_lock は文字にしてください。")
    lock = required_lock(configured_lock)
    if not lock.is_file():
        raise ValueError(f"確定記録が見つかりません: {lock}")
    lock_digest = container.file_sha256(lock)
    lock, environment = _verify_python_dependency_lock(lock, config["platform"])
    _verify_request(project, environment)
    digests = dict(read_lock_entries(lock))
    destination = project_folder(project) / "dockerfile"
    with container.work_directory("generate-dockerfile-") as stage:
        (stage / "Dockerfile").write_text(_render_dockerfile(environment), encoding="utf-8")
        for name in _PINS:
            shutil.copyfile(lock.parent / name, stage / name)
            if container.file_sha256(stage / name) != digests.get(name):
                raise ValueError(f"確定後に記録が変更されました: {lock.parent / name}")
        if container.file_sha256(lock) != lock_digest:
            raise ValueError(f"確定後に記録が変更されました: {lock}")
        _verify_request(project, environment)
        command = ["bash", str(_PUBLISH), str(stage), str(destination.relative_to(container.ROOT))]
        if replace:
            command.append("--replace")
        finished = subprocess.run(
            command, cwd=container.ROOT, capture_output=True, text=True, check=False
        )
        if finished.returncode:
            raise RuntimeError(
                finished.stderr.rstrip() or "ビルド用ファイルを保存できませんでした。"
            )
    return destination


# --- 検査用のイメージの中で第 2 段階を実行する（README.md「第 2 段階の中身」）---
def _main(args):
    if args == ["--help"]:
        print(_USAGE)
        return
    if not args or args[0].startswith("-") or args[1:] not in ([], ["--replace"]):
        raise SystemExit(_USAGE)
    if os.getenv("AUDIT_RUNNER") != "python":
        raise SystemExit(f"検査用のイメージの中で実行してください。\n{_USAGE}")
    try:
        destination = generate_python_dockerfile(args[0], replace=len(args) == 2)
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from None
    print(f"ビルド用ファイルを用意しました: {container.shown_path(destination)}")


if __name__ == "__main__":
    _main(sys.argv[1:])
