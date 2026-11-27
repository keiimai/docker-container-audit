# --- イメージの保存・読み戻しと SIF の作成・監査を行う（R は R/archive.R）（README.md「構成」）---

from __future__ import annotations

import contextlib
import csv
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

import container
import contract
from container import (
    bind_mount,
    file_sha256,
    run_docker,
    shown_path,
    size_and_sha256,
    wait_for_bind_sources,
)
from discovery import read_base_inventory, read_table_tsv
from evidence import (
    operation_check_items,
    operation_checks,
    record,
    status_detail,
    worst_status,
    write_tool_versions,
)
from image import lock_summary, run_python_image_audit, verified_python_dependency_lock
from project import INSPECT_SCRIPT, lock_inputs, read_requirements, required_lock


# --- 第 3 段階: イメージの中で依存を調べ直し、確定記録と照合して監査する（README.md「第 3 段階の中身」）---
def audit_python_image(
    image,
    platform="linux/arm64",
    *,
    lock=None,
    run_id=None,
    operation="audit-python",
    sif=None,
    sif_record=None,
):
    if operation not in ("audit-python", "audit-python-archive", "audit-python-sif"):
        raise ValueError(
            "監査の操作名は audit-python・audit-python-archive・audit-python-sif のどれかです。"
        )
    if (operation == "audit-python-sif") != bool(sif and sif_record):
        raise ValueError(
            "SIF を監査するには、SIF のファイルと sif の工程の記録先（sif.tsv がある）を指定してください。"
        )
    platform = container.validate_platform(platform)
    lock = required_lock(lock)
    base_image, input_hashes = lock_summary(lock)
    source = image
    if sif:
        sif = container.ROOT / sif
        if not sif.is_file() or sif.suffix != ".sif":
            raise ValueError(f"監査する SIF のファイルを指定してください: {sif}")
        sif_record = container.ROOT / sif_record
        if not (sif_record / "sif.tsv").is_file():
            raise ValueError(
                f"sif の工程の記録先（sif.tsv がある）を指定してください: {sif_record}"
            )
        sif = sif.resolve(strict=True)
        sif_record = sif_record.resolve(strict=True)
        image = sif_image_name(source, platform)

    def run(directory, record):
        check = operation_checks(directory, "audit")
        items = operation_check_items(directory, "audit")
        if sif:
            _prepare_sif_image(sif, sif_record, source, image, platform, directory, check, items)
        _, environment = check(
            "Dependency lock",
            lambda: verified_python_dependency_lock(items, "Dependency lock", lock, platform),
            shown_path(lock),
        )
        folder = lock.parent
        pins = read_table_tsv(folder / "python-pins.tsv")
        config = {
            "requirements": read_requirements(folder / "requirements.txt"),
            "ubuntu": read_table_tsv(folder / "ubuntu-packages.tsv"),
            "confirmed": {
                "python_version": environment["python_version"],
                "packages": {row["package"]: row["version"] for row in pins},
                "hashes": {row["package"]: f"{row['version']} {row['sha256']}" for row in pins},
                "pylock_sha256": file_sha256(folder / "pylock.toml"),
            },
        }
        base = environment["base_image"]

        def inventory():
            config["baseline"] = read_base_inventory(folder / "base-ubuntu.tsv", base, platform)
            baseline = directory / "baseline"
            baseline.mkdir(exist_ok=True)
            (baseline / "base-ubuntu.tsv").write_text(
                f"# {base} {platform}\nPackage\tArchitecture\tVersion\tStatus\n"
                + "".join(
                    f"{row['package']}\t{row['architecture']}\t{row['version']}\tinstalled\n"
                    for row in config["baseline"]
                ),
                encoding="utf-8",
            )
            return f"{base}、Ubuntu パッケージ {len(config['baseline'])}"

        check("Base inventory", inventory, detail=str)
        summary = directory / "audit-checks.tsv"

        def statuses():
            if not summary.is_file():
                return []
            with summary.open(encoding="utf-8", newline="") as stream:
                return [row["status"] for row in csv.DictReader(stream, delimiter="\t")]

        def audit():
            try:
                result = run_python_image_audit(image, platform, directory, config, record)
            except (RuntimeError, OSError, ValueError, csv.Error) as error:
                if not summary.is_file():
                    raise
                detail = None
                with contextlib.suppress(OSError, KeyError, ValueError, csv.Error):
                    detail = status_detail(statuses())
                if detail is None:
                    raise
                raise RuntimeError(f"{detail}。{error}") from error
            found["result"] = result
            return statuses()

        found = {}
        check("Audit checks", audit, detail=status_detail, status=worst_status)
        return found["result"]

    parameters = {
        "image": image,
        "platform": platform,
        "dependency_lock": shown_path(lock),
    }
    if base_image is not None:
        parameters.update(base_image=base_image, input_hashes=input_hashes)
    inputs = [INSPECT_SCRIPT, *lock_inputs(lock)]
    if sif:
        parameters.update(
            source_image=source, sif=shown_path(sif), sif_record=shown_path(sif_record)
        )
        inputs.extend(p for p in (sif, sif_record / "sif.tsv") if p.is_file())
    return record(operation, parameters, inputs, run, run_id=run_id)


# --- 第 3 段階: 作った SIF の中身を、audit と同じ条件で監査する（README.md「Apptainer の SIF」）---
def audit_python_sif(
    sif, image, platform="linux/arm64", *, lock=None, sif_record=None, run_id=None
):
    return audit_python_image(
        image,
        platform,
        lock=lock,
        run_id=run_id,
        operation="audit-python-sif",
        sif=sif,
        sif_record=sif_record,
    )


# --- 保存する .tar の名前（<イメージ>_<アーキテクチャ>.tar。置き場所は outputs/images/<実行 ID>/）（README.md「第 3 段階の中身」）---
def _image_archive_name(image, platform):
    return f"{re.sub(r'[^A-Za-z0-9._-]', '_', image)}_{platform.split('/')[1]}.tar"


def _artifact_location(name, run_id):
    path = Path(name)
    if path.name == str(name) and run_id:
        path = container.ROOT / "outputs" / "images" / run_id / path
    elif not path.is_absolute() and path.parts[:2] == ("outputs", "images"):
        path = container.ROOT / path
    return path.parent.resolve() / path.name


# --- 出力先のパスを確かめる（拡張子・プロジェクトの中・まだ無いこと）（README.md「第 3 段階の中身」）---
def _artifact_path(name, extension, run_id):
    if not str(name).endswith(extension):
        raise ValueError(f"ファイル名の末尾を {extension} にしてください。")
    if Path(name).name == str(name) and (not run_id or "/" in str(run_id)):
        raise ValueError("outputs/images/<実行 ID>/ に置くには実行 ID が要ります。")
    path = _artifact_location(name, run_id)
    root = container.ROOT.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("出力先はプロジェクト内を指定してください。") from exc
    if path.parent.is_relative_to(root / "outputs" / "images"):
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path.parent.resolve(strict=True)
    if path.exists():
        raise FileExistsError(f"出力先が既にあります。別の名前を指定してください。\n{path}")
    return path


# --- 完成したファイルを指定した出力先にハードリンクで置く（README.md「第 3 段階の中身」）---
def _publish_artifact(staging, output):
    try:
        os.link(staging, output)
    except OSError as error:
        message = "出力先に保存できません。既存ファイルと出力先を確認してください。"
        raise OSError(error.errno, message, output) from error


# --- イメージを .tar で保存し、SHA-256 を記録する（既定の置き場所は outputs/images/<実行 ID>/）（README.md「第 3 段階の中身」）---
def save_python_image(image, output=None, platform="linux/arm64", *, run_id=None):
    platform = container.validate_platform(platform)
    name = output or _image_archive_name(image, platform)

    def save(directory, record):
        check = operation_checks(directory, "save")
        items = operation_check_items(directory, "save")
        try:
            path = _artifact_path(name, ".tar", run_id)
        except (ValueError, OSError) as error:
            check("Archive written", detail=str(error), status="FAIL")
            raise
        image_id = check(
            "Image platform", lambda: container.local_image_id(image, platform), detail=str
        )
        record["image_id"] = image_id
        log = directory / "save.log"

        def write():
            with container.work_directory("export-image-") as work:
                staging = Path(work) / "image.tar"
                if container.local_image_id(image, platform) != image_id:
                    raise RuntimeError("Docker タグの実体が保存の開始時と一致しません。")
                run_docker(
                    [
                        "image",
                        "save",
                        "--platform",
                        platform,
                        "--output",
                        str(staging),
                        "--",
                        image,
                    ],
                    log,
                )

                def unchanged():
                    try:
                        found = container.local_image_id(image, platform)
                    except (OSError, RuntimeError, subprocess.CalledProcessError):
                        items("Image unchanged during save", [(image, image_id, None)])
                        raise
                    items("Image unchanged during save", [(image, image_id, found)])
                    if found != image_id:
                        raise RuntimeError("Docker タグの実体が保存の開始時と一致しません。")

                check("Image unchanged during save", unchanged, detail=image_id)
                if not staging.is_file() or staging.stat().st_size == 0:
                    raise RuntimeError("出力ファイルが空です。")
                _publish_artifact(staging, path)
            return path

        check("Archive written", write, detail=size_and_sha256)
        readback = readback_image_name(image, platform)
        check(
            "Archive loads back",
            lambda: _readback_archive(path, image_id, readback, platform, log, items),
            detail=str,
        )
        digest = file_sha256(path)
        (directory / "archive.tsv").write_text(
            f"path\tsha256\n{path.relative_to(container.ROOT.resolve()).as_posix()}\t{digest}\n",
            encoding="utf-8",
        )
        return {
            "archive": str(path),
            "sha256": digest,
            "image_id": image_id,
            "readback": readback,
            "log": str(log),
            "paths": [str(path)],
            "status": "PASS",
        }

    return record(
        "save-python",
        {"image": image, "platform": platform, "tar": shown_path(_artifact_location(name, run_id))},
        [],
        save,
        run_id=run_id,
    )


# --- 外へ書く名前（絶対パス・..）とリンクが無いかを確かめてから .tar を展開し、項目の数を返す（README.md「Apptainer の SIF」）---
def _safe_extract(archive, directory, items=None):
    directory = Path(directory)
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        rows = []
        for member in members:
            name = Path(member.name)
            if member.issym() or member.islnk():
                found = "link"
            elif (
                name.is_absolute()
                or ".." in name.parts
                or not (directory / name).resolve().is_relative_to(directory.resolve())
            ):
                found = "outside"
            else:
                found = "safe"
            rows.append((member.name.rstrip("/"), "safe", found))
        if items is not None:
            items("Archive entries safe", rows)
        unsafe = [name for name, _, found in rows if found != "safe"]
        if unsafe:
            raise ValueError(
                f".tar に安全でない中身（外への書き込みかリンク）があります: {unsafe[0]}"
            )
        stream.extractall(directory, members=members, filter="data")
    return len(members)


# --- 読み戻すときの名前（audit-readback/<イメージ>:<アーキテクチャ>。元のタグには触れない）（README.md「第 3 段階の中身」）---
def readback_image_name(image, platform="linux/arm64"):
    repository = re.sub(r"[^A-Za-z0-9._-]", "_", image).lower()
    return f"audit-readback/{repository}:{container.validate_platform(platform).split('/')[1]}"


# --- SIF から作るイメージの名前（audit-sif/<イメージ>:<アーキテクチャ>。読み戻しと同じ規則）（README.md「Apptainer の SIF」）---
def sif_image_name(image, platform="linux/arm64"):
    repository = re.sub(r"[^A-Za-z0-9._-]", "_", image).lower()
    return f"audit-sif/{repository}:{container.validate_platform(platform).split('/')[1]}"


# --- SIF を取り出す Docker のボリュームの名前（記録の実行 ID から作り、記号は - にする）（README.md「Apptainer の SIF」）---
def _sif_volume_name(run_id):
    return "audit-sif-" + re.sub(r"[^A-Za-z0-9_.-]", "-", run_id)


# --- 元のイメージの Env・WorkingDir・User を、docker import の --change の形で返す（README.md「Apptainer の SIF」）---
def _sif_import_changes(image, platform):
    found = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--platform",
            platform,
            "--format",
            "{{json .Config}}",
            "--",
            image,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode:
        raise RuntimeError(
            f"{platform} のイメージの設定を読めません: {image}: {found.stderr.strip()}"
        )
    config = json.loads(found.stdout or "null") or {}
    changes = [
        f"ENV {name}={json.dumps(value, ensure_ascii=False)}"
        for name, _, value in (item.partition("=") for item in config.get("Env") or [])
    ]
    if config.get("WorkingDir"):
        changes.append(f"WORKDIR {config['WorkingDir']}")
    if config.get("User"):
        changes.append(f"USER {config['User']}")
    return changes


# --- SIF の監査で使う docker のコマンドを組み立てる（README.md「Apptainer の SIF」）---
def _sif_commands(sif, volume, apptainer, platform, name, changes):
    sif = Path(sif)
    work = f"type=volume,source={volume},destination=/work"
    return {
        "create": ["volume", "create", volume],
        "extract": [
            "run",
            "--rm",
            "--platform",
            platform,
            "--network",
            "none",
            "--mount",
            bind_mount(sif, f"/input/{sif.name}", True),
            "--mount",
            work,
            "--entrypoint",
            "sh",
            apptainer,
            "-c",
            contract.sif_extract_script(),
            "sh",
            f"/input/{sif.name}",
            container.validate_platform(platform).split("/")[1],
        ],
        "export": [
            "run",
            "--rm",
            "--platform",
            platform,
            "--network",
            "none",
            "--mount",
            work + ",readonly",
            "--entrypoint",
            "tar",
            apptainer,
            "-C",
            "/work/rootfs",
            "-cf",
            "-",
            ".",
        ],
        "import": [
            "import",
            "--platform",
            platform,
            *(part for change in changes for part in ("--change", change)),
            "-",
            name,
        ],
        "remove": ["volume", "rm", "--force", volume],
    }


# --- 取り出したファイルシステムを docker import で取り込み、イメージの ID を返す（README.md「Apptainer の SIF」）---
def _import_sif_rootfs(commands, name, platform, log):
    with Path(log).open("a", encoding="utf-8") as stream:
        exporter = subprocess.Popen(
            ["docker", *commands["export"]], stdout=subprocess.PIPE, stderr=stream
        )
        imported = subprocess.run(
            ["docker", *commands["import"]],
            stdin=exporter.stdout,
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=False,
        )
        exporter.stdout.close()
        if exporter.wait() or imported.returncode:
            raise RuntimeError(
                f"SIF のファイルシステムの取り込み（docker import）に失敗しました。ログ {log}"
            )
    return container.local_image_id(name, platform)


# --- SIF を照合して取り出し、元のイメージの設定を付けたイメージにする（README.md「Apptainer の SIF」）---
def _prepare_sif_image(sif, sif_record, source, name, platform, directory, check, items):
    log = Path(directory) / "sif-audit.log"

    def unchanged():
        rows = read_table_tsv(Path(sif_record) / "sif.tsv")
        hashes = [row["sha256"] for row in rows if row["item"] == "sif"]
        expected = hashes[0] if len(hashes) == 1 else None
        found = file_sha256(sif)
        items("SIF unchanged", [(shown_path(sif), expected, found)])
        if found != expected:
            raise ValueError(f"SIF の SHA-256 が sif の工程の記録（sif.tsv）と一致しません: {sif}")
        return found

    check("SIF unchanged", unchanged, detail=str)
    apptainer = contract.apptainer_image(platform)
    volume = _sif_volume_name(Path(directory).parent.name)
    commands = _sif_commands(sif, volume, apptainer, platform, name, [])
    wait_for_bind_sources([sif], apptainer, platform, log)
    try:
        run_docker(commands["create"], log)
        check("SIF file system extracted", lambda: run_docker(commands["extract"], log), apptainer)

        def imported():
            changes = _sif_import_changes(source, platform)
            return _import_sif_rootfs(
                _sif_commands(sif, volume, apptainer, platform, name, changes), name, platform, log
            )

        check("SIF image imported", imported, detail=str)
    finally:
        with log.open("a", encoding="utf-8") as stream:
            subprocess.run(
                ["docker", *commands["remove"]],
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            )


# --- そのプラットフォームの分のイメージ ID と層（RootFS）を返す（README.md「第 3 段階の中身」）---
def _platform_image_layers(image, platform):
    found = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--platform",
            platform,
            "--format",
            "{{.Id}} {{json .RootFS.Layers}}",
            "--",
            image,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode:
        raise RuntimeError(
            f"{platform} のイメージを確認できません: {image}: {found.stderr.strip()}"
        )
    image_id, _, layers = found.stdout.strip().partition(" ")
    return {"id": image_id, "layers": json.loads(layers)}


# --- 保存した .tar を別の名前で読み戻し、元のイメージと ID と層が同じかを確かめる（README.md「第 3 段階の中身」）---
def _readback_archive(archive, image_id, name, platform, log, items=None):
    with container.work_directory("readback-") as work:
        layout = Path(work) / "layout"
        layout.mkdir()
        _safe_extract(archive, layout)
        index_path = layout / "index.json"
        if not index_path.is_file():
            raise ValueError(f"OCI の形の .tar（index.json がある）を指定してください: {archive}")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for item in index.get("manifests", []):
            annotations = item.setdefault("annotations", {})
            annotations["io.containerd.image.name"] = f"docker.io/{name}"
            annotations["org.opencontainers.image.ref.name"] = name.rsplit(":", 1)[1]
        index_path.write_text(json.dumps(index), encoding="utf-8")
        manifest_path = layout / "manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in manifest:
                item["RepoTags"] = [name]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        packed = Path(work) / "readback.tar"
        with Path(log).open("a", encoding="utf-8", buffering=1) as stream:
            packed_result = subprocess.run(
                ["tar", "-C", str(layout), "-cf", str(packed), "."],
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            stream.write(f"tar の終了コード {packed_result.returncode}\n")
            if packed_result.returncode:
                raise RuntimeError(f"読み戻しに失敗しました。ログ {log}")
            with packed.open("rb") as source:
                docker_load = subprocess.run(
                    ["docker", "load"], stdin=source, stdout=stream, stderr=subprocess.STDOUT
                )
            stream.write(f"docker load の終了コード {docker_load.returncode}\n")
            if docker_load.returncode:
                raise RuntimeError(f"読み戻しに失敗しました。ログ {log}")
    original = _platform_image_layers(image_id, platform)
    loaded = _platform_image_layers(name, platform)
    if items is not None:
        items("Archive loads back", _readback_item_rows(original, loaded))
    if original != loaded:
        raise RuntimeError(
            f"読み戻したイメージが元のイメージの {platform} の分と一致しません: {name}"
        )
    return f"{name} {loaded['id']}、層 {len(loaded['layers'])}"


# --- 元のイメージと読み戻したイメージの ID と層を 1 つずつ照合した行にする（片方だけの層も行にする）（README.md「第 3 段階の中身」）---
def _readback_item_rows(original, loaded):
    count = max(len(original["layers"]), len(loaded["layers"]))

    def layer(value, index):
        return value["layers"][index] if index < len(value["layers"]) else None

    return [
        ("image ID", original["id"], loaded["id"]),
        *((f"layer {i + 1}", layer(original, i), layer(loaded, i)) for i in range(count)),
    ]


# --- 固定した Apptainer のイメージの apptainer の版を返す（読めなければ unavailable）（README.md「Apptainer の SIF」）---
def _apptainer_version(apptainer, platform):
    reported = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            platform,
            "--network",
            "none",
            "--entrypoint",
            "apptainer",
            apptainer,
            "--version",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    line = (reported.stdout.strip().splitlines() or [""])[0].strip()
    return line if reported.returncode == 0 and line else "unavailable"


# --- .tar を展開し、目次（index.json）を対象のプラットフォームのイメージ 1 つに絞る（README.md「Apptainer の SIF」）---
def _sif_layout(archive, work, platform, check=None, items=None):
    check = check or (lambda name, action, **_: action())
    layout = Path(work) / "layout"
    layout.mkdir()

    def select():
        index_path = layout / "index.json"
        if not index_path.is_file():
            raise ValueError(f"OCI の形の .tar（index.json がある）を指定してください: {archive}")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        architecture = platform.split("/")[1]

        def blob(digest):
            path = layout / "blobs" / digest.replace(":", "/", 1)
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

        def item_platform(item):
            if item.get("platform") is not None:
                return item["platform"]
            config = blob(blob(item.get("digest", "")).get("config", {}).get("digest", ""))
            return {"architecture": config.get("architecture"), "os": config.get("os")}

        keep = [
            item
            for item in index.get("manifests", [])
            if item_platform(item).get("architecture") == architecture
            and item_platform(item).get("os") == "linux"
            and "io.containerd.manifest.subject" not in item.get("annotations", {})
        ]
        if items is not None and keep:
            status = "PASS" if len(keep) == 1 else "FAIL"
            items(
                "Archive platform image",
                [(item["digest"], platform, platform, status) for item in keep],
            )
        if len(keep) != 1:
            raise ValueError(f"{platform} のイメージが .tar に 1 つだけ入っていません: {archive}")
        index["manifests"] = keep
        index_path.write_text(json.dumps(index), encoding="utf-8")
        return keep[0]["digest"]

    check(
        "Archive entries safe",
        lambda: _safe_extract(archive, layout, items),
        detail=lambda count: f"{count} 項目",
    )
    return check("Archive platform image", select, detail=str)


# --- 保存した .tar から固定した Apptainer で SIF を作り、アーキテクチャと SHA-256 を記録する（既定の置き場所は outputs/images/<実行 ID>/）（README.md「Apptainer の SIF」）---
def save_python_sif(archive, output=None, platform="linux/arm64", *, run_id=None):
    platform = container.validate_platform(platform)
    given = str(archive)
    archive = (container.ROOT / archive).resolve(strict=True)
    if not archive.is_file() or archive.suffix != ".tar":
        raise ValueError(f"保存した .tar のファイルを指定してください: {given}")
    name = output or archive.with_suffix(".sif").name
    apptainer = contract.apptainer_image(platform)

    def build(directory, record):
        try:
            archive.relative_to(container.ROOT.resolve())
        except ValueError as exc:
            raise ValueError(
                f"保存した .tar は、プロジェクトの中のものを指定してください: {archive}"
            ) from exc
        output = _artifact_path(name, ".sif", run_id)
        log = directory / "sif.log"
        check = operation_checks(directory, "sif")
        items = operation_check_items(directory, "sif")
        found = {}

        def write():
            with container.work_directory("export-sif-") as work:
                convert(Path(work))
            return output

        def convert(work):
            digest = found["digest"] = _sif_layout(archive, work, platform, check, items)
            log.write_text(f"Manifest {digest}\n", encoding="utf-8")
            wait_for_bind_sources([work], apptainer, platform, log)

            def run(*args):
                run_docker(
                    [
                        "run",
                        "--rm",
                        "--platform",
                        platform,
                        "--network",
                        "none",
                        "--mount",
                        bind_mount(work, "/work"),
                        "--entrypoint",
                        "apptainer",
                        apptainer,
                        *args,
                    ],
                    log,
                )

            check(
                "SIF build", lambda: run("build", "/work/image.sif", "oci:/work/layout"), apptainer
            )
            write_tool_versions(directory, [("apptainer", _apptainer_version(apptainer, platform))])

            def listed():
                run("sif", "list", "/work/image.sif")
                pattern = rf"Squashfs/.*{platform.split('/')[1]}\)"
                lines = [
                    line.strip()
                    for line in log.read_text(encoding="utf-8").splitlines()
                    if re.search(pattern, line)
                ]
                if not lines:
                    raise RuntimeError(f"SIF の中身が {platform} ではありません。ログ {log}")
                return lines[0]

            check("SIF file system architecture", listed, detail=str)
            staging = work / "image.sif"
            if not staging.is_file() or staging.stat().st_size == 0:
                raise RuntimeError("出力ファイルが空です。")
            _publish_artifact(staging, output)

        check("SIF written", write, detail=size_and_sha256)
        digest = found["digest"]
        root = container.ROOT.resolve()
        rows = [
            ("archive", archive.relative_to(root).as_posix(), file_sha256(archive)),
            ("sif", output.relative_to(root).as_posix(), file_sha256(output)),
            ("manifest", "", digest.removeprefix("sha256:")),
            ("apptainer", apptainer, ""),
        ]
        (directory / "sif.tsv").write_text(
            "item\tpath\tsha256\n" + "".join("\t".join(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        return {
            "sif": str(output),
            "sha256": rows[1][2],
            "log": str(log),
            "paths": [str(output)],
            "status": "PASS",
        }

    return record(
        "save-python-sif",
        {
            "image": given,
            "platform": platform,
            "sif": shown_path(_artifact_location(name, run_id)),
            "apptainer": apptainer,
        },
        [archive],
        build,
        run_id=run_id,
    )
