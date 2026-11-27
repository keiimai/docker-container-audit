# --- 第 3 段階で確定記録を照合し、イメージをビルドして、その中の依存を監査する（R は R/image.R）（README.md「構成」）---

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import time
from pathlib import Path

import container
import contract
from container import bind_mount, file_sha256, run_docker, shown_path, wait_for_bind_sources
from discovery import read_base_inventory, review_text
from evidence import (
    lock_item_rows,
    operation_check_items,
    operation_checks,
    read_lock_entries,
    record,
)
from project import (
    DEFAULT_PROJECT,
    INSPECT_SCRIPT,
    LOCK_FILES,
    default_image,
    dockerfile_files,
    dockerfile_folder,
    lock_inputs,
    required_lock,
    validate_base_image,
)

#: uv の公式イメージ（版とハッシュで固定。Dockerfile.inspect と見本の Dockerfile と同じ）。
UV_IMAGE = "ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168"

#: コンテナの中の uv の設定（Python は取りに行かず、手元のキャッシュも使わない）。
_UV_ENV = ["--env", "UV_PYTHON_DOWNLOADS=never", "--env", "UV_NO_CACHE=1"]


# --- 固定した uv のイメージから、対象のプラットフォームの uv を _tmp/ に取り出す（README.md「Python」）---
def _uv_binary(platform, log):
    platform = container.validate_platform(platform)
    digest = UV_IMAGE.rsplit("@sha256:", 1)[1][:12]
    path = container.ROOT / "_tmp" / "uv" / f"{digest}-{platform.replace('/', '-')}" / "uv"
    if path.is_file():
        return _wait_for_uv(path, platform, log)
    path.parent.mkdir(parents=True, exist_ok=True)
    run_docker(["pull", "--platform", platform, UV_IMAGE], log)
    created = subprocess.run(
        ["docker", "create", "--platform", platform, UV_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode:
        raise RuntimeError(f"{UV_IMAGE} から uv を取り出せません: {created.stderr.strip()}")
    uv_container = created.stdout.strip()
    staging = path.with_name("uv.partial")
    try:
        run_docker(["cp", f"{uv_container}:/uv", str(staging)], log)
    finally:
        subprocess.run(["docker", "rm", "-f", uv_container], capture_output=True, check=False)
    staging.chmod(0o755)
    staging.replace(path)
    return _wait_for_uv(path, platform, log)


# --- 渡す uv が、コンテナの中から読めて動くまで、2 秒おきに最大 2 分待つ（README.md「Python」）---
def _wait_for_uv(path, platform, log, attempts=60, pause=2.0):
    for attempt in range(1, attempts + 1):
        checked = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                platform,
                "--network",
                "none",
                "--mount",
                bind_mount(path, "/audit/uv", True),
                "--entrypoint",
                "/audit/uv",
                UV_IMAGE,
                "--version",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if checked.returncode == 0:
            with Path(log).open("a", encoding="utf-8") as stream:
                stream.write(f"uv readable in a container (attempt {attempt}): {checked.stdout}")
            return path
        time.sleep(pause)
    raise RuntimeError(
        f"Docker から uv を読めません: {path}: {(checked.stderr or checked.stdout).strip()}"
    )


# --- 対象イメージの中で dependencies_inspect.py を動かし、Python と Ubuntu を監査する（ネットワークなし。結果を返す）（README.md「Python」）---
def run_python_image_audit(image, platform, directory, config, record):
    platform = container.validate_platform(platform)
    image_id = container.local_image_id(image, platform)
    record["image_id"] = image_id
    config_path = directory / "python-input.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    log = directory / "python-audit.log"
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"Platform: {platform}\nDocker image ID: {image_id}\n")
    uv = _uv_binary(platform, log)
    wait_for_bind_sources([directory, INSPECT_SCRIPT, uv], image_id, platform, log)
    run_docker(
        [
            "run",
            "--rm",
            "--platform",
            platform,
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "HOME=/tmp",
            "--env",
            "TMPDIR=/tmp",
            "--tmpfs",
            "/tmp:rw,exec,mode=1777",
            "--mount",
            bind_mount(directory, "/report"),
            "--mount",
            bind_mount(INSPECT_SCRIPT, "/audit/script", True),
            "--mount",
            bind_mount(uv, "/audit/uv", True),
            *_UV_ENV,
            "--entrypoint",
            "python3",
            image_id,
            "/audit/script",
            "audit",
            "/report/python-input.json",
            "/report",
        ],
        log,
    )
    report = directory / "python-audit.json"
    status = json.loads(report.read_text(encoding="utf-8")).get("status", "PASS")
    review = ""
    if status == "REVIEW":
        with (directory / "audit-checks.tsv").open(encoding="utf-8", newline="") as stream:
            rows = csv.DictReader(stream, dialect="excel-tab")
            review = review_text(row["check"] for row in rows if row["status"] == "REVIEW")
    return {"report": str(report), "log": str(log), "status": status, "review": review}


# --- 確定記録の中だけで照合し、パスと中身を返す（ハッシュ・build-environment.json・プラットフォーム）（REPRODUCIBILITY.md 3 節）---
def _verify_python_dependency_lock(lock, platform):
    if Path(lock).name != "dependency-lock.tsv":
        raise ValueError("dependency-lock.tsv を指定してください。")
    lock = Path(lock).resolve(strict=True)
    rows = read_lock_entries(lock)
    if (
        len(rows) != len(LOCK_FILES)
        or any(not re.fullmatch(r"[a-f0-9]{64}", digest) for _, digest in rows)
        or {name for name, _ in rows} != set(LOCK_FILES)
    ):
        raise ValueError("確定記録の形式が不正です。")
    for name, digest in rows:
        if not (lock.parent / name).is_file():
            raise ValueError(f"確定後に記録が見つかりません: {name}")
        if file_sha256(lock.parent / name) != digest:
            raise ValueError(f"確定後に記録が変更されました: {name}")
    environment = contract.read_build_environment(lock.parent / "build-environment.json")
    if environment["language"] != "python" or not environment.get("python_version"):
        raise ValueError(f"Python の確定記録ではありません: {lock}")
    validate_base_image(environment["base_image"])
    if environment["apt_snapshot"] is None:
        raise ValueError("確定記録の APT スナップショットが不正です。")
    if file_sha256(lock.parent / "requirements.txt") != environment["inputs"].get(
        "requirements.txt"
    ):
        raise ValueError("確定記録の requirements.txt が、第 1 段階の入力と一致しません。")
    read_base_inventory(
        lock.parent / "base-ubuntu.tsv", environment["base_image"], environment["platform"]
    )
    if environment["platform"] != container.validate_platform(platform):
        raise ValueError(
            f"確定記録のプラットフォームは {environment['platform']} で、{platform} ではありません。"
        )
    return lock, environment


# --- 確定記録のファイルごとの照合を check-items.tsv に残してから、確定記録を照合する（R は items と verify_r_dependency_lock をその場に書く）（REPRODUCIBILITY.md 3 節）---
def verified_python_dependency_lock(items, name, lock, platform):
    items(name, lock_item_rows(lock))
    return _verify_python_dependency_lock(lock, platform)


# --- 記録に書く、確定記録の固定ベースと入力のハッシュを返す（読めなければ None と空）（README.md「結果の共通形式」）---
def lock_summary(lock):
    try:
        environment = contract.read_build_environment(Path(lock).parent / "build-environment.json")
    except (OSError, ValueError):
        return None, {}
    return environment["base_image"], environment["inputs"]


# --- 第 3 段階: 確定記録を前後で照合し、projects/<名前>/dockerfile をビルドする（README.md「第 3 段階の中身」）---
def build_python_image(
    image=None,
    project=DEFAULT_PROJECT,
    platform="linux/arm64",
    *,
    lock=None,
    run_id=None,
):
    platform = container.validate_platform(platform)
    lock = required_lock(lock)
    dockerfile = dockerfile_folder(project)
    image = image or default_image(project)

    def build(directory, record):
        check = operation_checks(directory, "build")
        items = operation_check_items(directory, "build")
        check(
            "Dependency lock",
            lambda: verified_python_dependency_lock(items, "Dependency lock", lock, platform),
            shown_path(lock),
        )
        log = directory / "build.log"
        check(
            "Image build",
            lambda: run_docker(
                [
                    "buildx",
                    "build",
                    "--load",
                    "--platform",
                    platform,
                    "--tag",
                    image,
                    "--file",
                    str(dockerfile / "Dockerfile"),
                    str(dockerfile),
                ],
                log,
            ),
            image,
        )
        check(
            "Dependency lock after build",
            lambda: verified_python_dependency_lock(
                items, "Dependency lock after build", lock, platform
            ),
            shown_path(lock),
        )
        record["image_id"] = check(
            "Image platform", lambda: container.local_image_id(image, platform), detail=str
        )
        return {"image": image, "image_id": record["image_id"], "log": str(log), "status": "PASS"}

    return record(
        "build-python",
        {
            "image": image,
            "project": project,
            "platform": platform,
            "dependency_lock": shown_path(lock),
        },
        [*dockerfile_files(dockerfile), *lock_inputs(lock)],
        build,
        run_id=run_id,
    )
