# --- Docker を動かし、イメージ ID・プラットフォーム・ファイルのハッシュを調べる（R は R/container.R）（README.md「構成」）---

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

#: 子の根（このファイルは <子の根>/src/ にある）。
ROOT = Path(__file__).resolve().parents[1]


# --- docker を走らせ、失敗なら止める（標準出力とエラーをログに追記。R は任意のコマンドの run_command）（REPRODUCIBILITY.md 2 節）---
def run_docker(args, log, env=None):
    with Path(log).open("a", encoding="utf-8") as stream:
        finished = subprocess.run(
            ["docker", *args],
            stdout=stream,
            stderr=subprocess.STDOUT,
            cwd=ROOT,
            env=os.environ | env if env is not None else None,
            check=False,
        )
    if finished.returncode:
        raise RuntimeError(
            f"docker の実行に失敗しました（終了コード {finished.returncode}）。\nログ {log}"
        )


# --- _tmp/ に、名前の頭が prefix の作業フォルダを作り、終われば消す（README.md「記録」）---
@contextlib.contextmanager
def work_directory(prefix):
    scratch = ROOT / "_tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=prefix, dir=scratch) as work:
        yield Path(work)


# --- docker の --mount に渡す bind の指定を作る（CSV の形なので、パスの引用符は二重にする）（README.md「構成」）---
def bind_mount(source, destination, readonly=False):
    escaped = str(Path(source).resolve()).replace('"', '""')
    return ",".join(
        [
            "type=bind",
            f'"source={escaped}"',
            f"destination={destination}",
            *(["readonly"] if readonly else []),
        ]
    )


# --- 作った直後のパスが Docker から見えるまで待つ（Dropbox の同期の間。プロジェクトの外は止める）（README.md「構成」）---
def wait_for_bind_sources(paths, image, platform, log):
    root = ROOT.resolve()
    relative = []
    for path in paths:
        try:
            child = (root / path).resolve(strict=True).relative_to(root)
            if not child.parts:
                raise ValueError
            relative.append(str(child))
        except ValueError as exc:
            raise ValueError(
                "Docker の共有確認にはプロジェクト内のパスを指定してください。"
            ) from exc
    relative = list(dict.fromkeys(relative))
    probe = """visible() {
    remaining=$1
    current=/project
    while :; do
        ls -a -- "$current" >/dev/null 2>&1 || return 1
        component=${remaining%%/*}
        current="$current/$component"
        if [ "$component" = "$remaining" ]; then break; fi
        remaining=${remaining#*/}
    done
    test -e "$current"
}
for attempt in $(seq 1 60); do
    missing=
    for path do
        if ! visible "$path"; then missing=$path; break; fi
    done
    if [ -z "$missing" ]; then
        printf "Docker bind sources visible (attempt %s)\\n" "$attempt"
        exit 0
    fi
    sleep 0.25
done
printf "Docker VM cannot see the bind source: %s\\n" "$missing" >&2
exit 1"""
    run_docker(
        [
            "run",
            "--rm",
            "--network",
            "none",
            "--platform",
            platform,
            "--mount",
            bind_mount(root, "/project", True),
            "--entrypoint",
            "/bin/sh",
            image,
            "-ec",
            probe,
            "sh",
            *relative,
        ],
        log,
    )


# --- ファイルの SHA-256 を返す（REPRODUCIBILITY.md 3 節）---
def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


# --- バイト数と SHA-256 の説明（README.md「結果の共通形式」）---
def size_and_sha256(path):
    return f"{Path(path).stat().st_size} バイト、SHA-256 {file_sha256(path)}"


# --- プラットフォームが linux/amd64 か linux/arm64 かを確かめる（README.md「検査対象のフォルダ」）---
def validate_platform(platform):
    if platform not in ("linux/amd64", "linux/arm64"):
        raise ValueError("platform は linux/amd64 または linux/arm64 を指定してください。")
    return platform


# --- ローカルのイメージの ID を返す（タグを ID にしてから、そのプラットフォームがあるかを確かめる）（README.md「結果の共通形式」）---
def local_image_id(image, platform):
    platform = validate_platform(platform)
    found = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", "--", image],
        capture_output=True,
        text=True,
        check=False,
    )
    image_id = found.stdout.strip()
    if found.returncode or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise RuntimeError(f"イメージ ID を確認できません: {image}: {found.stderr.strip()}")
    selected = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--platform",
            platform,
            "--format",
            "{{.Os}}/{{.Architecture}}",
            "--",
            image_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if selected.returncode or selected.stdout.strip() != platform:
        raise RuntimeError(
            f"{platform} のイメージがローカルにありません: {image}: {selected.stderr.strip()}"
        )
    return image_id


# --- 記録に書くパス（プロジェクトの中ならプロジェクトからの相対パス）（README.md「結果の共通形式」）---
def shown_path(path):
    if path is None:
        return None
    resolved = (ROOT / path).resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)
