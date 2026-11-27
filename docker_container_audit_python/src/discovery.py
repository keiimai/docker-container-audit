# --- 第 1 段階の依存の調査と確定を行う（R は R/discovery.R）（README.md「構成」）---

from __future__ import annotations

import csv
import json
import re
import shutil
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

import container
import contract
from container import file_sha256, run_docker, shown_path
from evidence import hash_item_rows, operation_check_items, operation_checks, record
from project import (
    DEFAULT_PROJECT,
    INSPECT_DOCKERFILE,
    INSPECT_SCRIPT,
    LOCK_FILES,
    project_name,
    read_base_file,
    read_package_list,
    read_requirements,
    target_inputs,
)

#: wheel の無い要件の、調査の要確認項目の説明。
_NO_WHEEL = (
    "対象のプラットフォームに合う wheel が無い要件があります（uv の解決のログを見てください）"
)


# --- dpkg の一覧の行から、導入済みのパッケージの版を返す（README.md「Python」）---
def _ubuntu_inventory(lines):
    entries = {}
    for line in lines:
        name, arch, version, status = line.split("\t")
        if status == "installed":
            entries[name, arch] = version
    if not entries:
        raise RuntimeError("Ubuntu パッケージの一覧が空です。")
    return entries


# --- Ubuntu パッケージごとに、それを使う Python の配布物を返す（notebooks/README.md「出力の扱い」）---
def _language_packages(native):
    used = {package: set(names) for package, names in native.get("used_by", {}).items()}
    for soname, provider in native.get("providers", {}).items():
        used.setdefault(provider, set()).update(native.get("missing", {}).get(soname, []))
    return used


# --- 導入の前後の dpkg の一覧から Ubuntu の計画を作る（入力と wheel の提供元は direct）（notebooks/README.md「出力の扱い」）---
def _installed_plan(base_lines, installed_lines, requested, native=None):
    native = native or {}
    needed_by = {}
    for soname, provider in native.get("providers", {}).items():
        needed_by.setdefault(provider, set()).update(native.get("missing", {}).get(soname, []))
    direct = set(requested) | set(needed_by)
    used = _language_packages(native)
    base = _ubuntu_inventory(base_lines)
    installed = _ubuntu_inventory(installed_lines)
    removed = sorted(f"{name}:{arch}" for name, arch in base.keys() - installed.keys())
    if removed:
        raise RuntimeError(f"APT がベースのパッケージを削除しました: {', '.join(removed)}")
    plan = []
    for (name, arch), version in sorted(installed.items()):
        before = base.get((name, arch))
        plan.append(
            {
                "package": name,
                "architecture": arch,
                "version": version,
                "relation": "direct" if name in direct else ("base" if before else "indirect"),
                "base_version": before,
                "action": "keep" if before == version else ("update" if before else "install"),
                "language_packages": ", ".join(sorted(used.get(name, []))),
            }
        )
    return plan


# --- 調査の環境の定義のハッシュ（入力のハッシュに入れ、確定で照合する）（README.md「Python」）---
def _inspect_hashes():
    return {"Dockerfile.inspect": file_sha256(INSPECT_DOCKERFILE)}


# --- 調査の環境をビルドし、dependency_report の段の結果を destination に書き出す（README.md「Python」）---
def _run_inspect_build(base, platform, destination, log, files, *, apt_snapshot):
    output = str(destination).replace('"', '""')
    with container.work_directory("inspect-context-") as work:
        for name, text in files.items():
            (Path(work) / name).write_text(text, encoding="utf-8")
        run_docker(
            [
                "buildx",
                "build",
                "--target",
                "dependency_report",
                "--platform",
                platform,
                "--build-arg",
                f"BASE_IMAGE={base}",
                "--build-arg",
                f"APT_SNAPSHOT={apt_snapshot}",
                "--build-context",
                f"audit_tools={INSPECT_SCRIPT.parent}",
                "--file",
                str(INSPECT_DOCKERFILE),
                "--output",
                f'type=local,"dest={output}"',
                work,
            ],
            log,
        )


# --- 調査の環境に渡す Ubuntu パッケージの一覧（ubuntu-packages.txt の中身。無ければ空）（README.md「検査対象のフォルダ」）---
def _ubuntu_text(paths):
    listed = paths.get("ubuntu-packages.txt")
    return listed.read_text(encoding="utf-8-sig") if listed else ""


# --- 確定記録に残すビルド環境（固定ベース・プラットフォーム・APT スナップショット・入力のハッシュ）（REPRODUCIBILITY.md 3 節）---
def _lock_environment(paths, platform, apt_snapshot):
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", apt_snapshot or ""):
        raise ValueError("APT スナップショットは YYYYMMDDTHHMMSSZ の形で指定してください。")
    return {
        "schema_version": "1.0",
        "language": "python",
        "base_image": read_base_file(paths["base.txt"])["BASE_IMAGE"],
        "platform": container.validate_platform(platform),
        "apt_snapshot": apt_snapshot,
        "inputs": {
            **{name: file_sha256(path) for name, path in paths.items()},
            **_inspect_hashes(),
        },
    }


# --- 表を TSV で書く（列は contracts/tables.tsv。論理値は TRUE・FALSE、値の無い欄は空）（README.md「Python」）---
def _write_table_tsv(path, table, rows):
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=contract.table_columns(table, "python"),
            dialect="excel-tab",
            quoting=csv.QUOTE_ALL,
            lineterminator="\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(
            {
                key: ("TRUE" if value else "FALSE")
                if isinstance(value, bool)
                else ("" if value is None else value)
                for key, value in row.items()
            }
            for row in rows
        )


# --- 表の TSV を、行の辞書のリストで読む（値の無い欄は空の文字）（README.md「Python」）---
def read_table_tsv(path):
    with Path(path).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, dialect="excel-tab"))


# --- 固定ベースの Ubuntu パッケージの一覧を書く（1 行目は # <固定ベース> <プラットフォーム>）（README.md「Python」）---
def _write_base_inventory(path, base, platform, lines):
    rows = [line for line in lines if len(line.split("\t")) == 4]
    Path(path).write_text(
        f"# {base} {platform}\nPackage\tArchitecture\tVersion\tStatus\n"
        + "".join(row + "\n" for row in rows),
        encoding="utf-8",
    )


# --- 固定ベースの一覧を読み、固定ベースとプラットフォームを確かめて中身を返す（Ubuntu の一覧 1 つ。R は R と Ubuntu の 2 つを確かめる validate_base_inventories）（README.md「Python」）---
def read_base_inventory(path, base, platform):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if lines[:2] != [f"# {base} {platform}", "Package\tArchitecture\tVersion\tStatus"] or not any(
        lines[2:]
    ):
        raise ValueError(
            f"ベース一覧が固定イメージ、プラットフォームまたは一覧の形式と一致しません: {path}"
        )
    packages = []
    for number, line in enumerate(lines[2:], 3):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 4:
            raise ValueError(f"ベース一覧の行が4列ではありません: {path}（{number}行）")
        name, arch, version, status = fields
        if status == "installed":
            packages.append({"package": name, "architecture": arch, "version": version})
    return packages


# --- uv の解決結果（pylock.toml）を配布物ごとの行にする（requested は要件に書いたもの）（README.md「Python」）---
def _python_package_rows(pylock, requested):
    return [
        {
            "package": item["name"],
            "version": item["version"],
            "requested": canonicalize_name(item["name"]) in requested,
            "sha256": "; ".join(
                str(wheel.get("hashes", {}).get("sha256", "")) for wheel in item.get("wheels", [])
            ),
        }
        for item in tomllib.loads(Path(pylock).read_text(encoding="utf-8")).get("packages", [])
    ]


# --- 調査の結果を python-packages.tsv・ubuntu-packages.tsv・review.tsv に書く（README.md「Python」）---
def _write_discovery_tables(directory, plan, review, pylock, requested):
    packages = _python_package_rows(pylock, requested) if pylock else []
    _write_table_tsv(directory / "python-packages.tsv", "python-packages", packages)
    _write_table_tsv(directory / "ubuntu-packages.tsv", "ubuntu-packages", plan)
    _write_table_tsv(directory / "review.tsv", "python-ubuntu-review", review)


# --- 調査の環境の状態（python-status・ubuntu-status）が失敗なら、FAIL の行を書いて止める（README.md「Python」）---
def _stop_on_failed_inspection(check, resolution, log):
    def status(name):
        path = resolution / name
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""

    def failed(label):
        raise RuntimeError(f"{label} の依存の調査に失敗しました。ログ {shown_path(log)}")

    python, ubuntu = status("python-status"), status("ubuntu-status")
    if python != "0":
        if not python and ubuntu != "0":
            check("Ubuntu dependency plan", lambda: failed("Ubuntu"))
        check("Python dependency resolution", lambda: failed("Python"))
    if ubuntu != "0":
        _resolution_check(check, status("resolve-status") != "0", resolution / "pylock.toml")
        check("Ubuntu dependency plan", lambda: failed("Ubuntu"))


# --- wheel の解決の結果を、照合の記録に残す（合う wheel が無ければ REVIEW）（README.md「結果の共通形式」）---
def _resolution_check(check, failed, pylock):
    if failed:
        check(
            "Python dependency resolution",
            detail="対象のプラットフォームに合う wheel が無い要件があります",
            status="REVIEW",
        )
    else:
        packages = tomllib.loads(Path(pylock).read_text(encoding="utf-8")).get("packages", [])
        check("Python dependency resolution", detail=f"{len(packages)} 件の配布物を解決")


# --- 調査の解決と Ubuntu の計画の結果を、照合の記録に残す（README.md「結果の共通形式」）---
def _discovery_checks(check, failed, plan, review, pylock):
    _resolution_check(check, failed, pylock)
    count = {
        action: sum(row["action"] == action for row in plan)
        for action in ("keep", "install", "update")
    }
    check(
        "Ubuntu dependency plan",
        detail=(
            f"維持 {count['keep']}、追加 {count['install']}、更新 {count['update']}、要確認 {len(review)}"
        ),
        status="REVIEW" if review else "PASS",
    )


# --- 要確認の理由を「要確認: 項目, 項目」の形で返す（無ければ空）（README.md「結果の共通形式」）---
def review_text(items):
    items = list(items)
    return f"要確認: {', '.join(items)}" if items else ""


# --- 共有ライブラリの提供元と調査失敗の両方を、確認根拠の対象にする（README.md「第 1 段階の中身」）---
def _native_review(native):
    rows = list(native.get("review", []))
    for detail in dict.fromkeys(native.get("inspection_review", [])):
        subject, separator, _ = detail.removeprefix("ldd failed for ").partition(": ")
        rows.append({"item": subject if separator else "Shared libraries", "detail": detail})
    return rows


# --- uv の解決の失敗が、wheel が無いためかを確かめる（それ以外の失敗は止める）（README.md「Python」）---
def _no_wheel(log):
    text = Path(log).read_text(encoding="utf-8")
    if "has no usable wheels" not in text:
        raise RuntimeError(f"uv の依存の解決に失敗しました: {log}")


# --- 要件の環境マーカーを調査対象の Python で評価する（README.md「Python」）---
def _target_requirements(requirements, python_environment):
    environment = {
        "platform_machine": python_environment.get("platform_machine", ""),
        "python_full_version": python_environment.get("python_full_version", ""),
        "python_version": ".".join(
            python_environment.get("python_full_version", "").split(".")[:2]
        ),
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
        **python_environment,
    }
    for line in requirements:
        requirement = Requirement(line)
        applies = requirement.marker is None or requirement.marker.evaluate(environment)
        yield line, requirement, applies


# --- requirements.txt の要件を解決した版が満たすかを確かめ、要件の数を返す（README.md「Python」）---
def _check_requirements(requirements, pins, python_environment, items=None, *, requested=None):
    failed = []
    active = set()
    rows = []
    for line, requirement, applies in _target_requirements(requirements, python_environment):
        name = canonicalize_name(requirement.name)
        version = pins.get(name)
        expected = str(requirement.specifier) + (
            f"; {requirement.marker}" if requirement.marker else ""
        )
        if not applies:
            rows.append((requirement.name, expected, version, "PASS"))
            continue
        active.add(name)
        if version is None or (
            requirement.specifier and not requirement.specifier.contains(version, prereleases=True)
        ):
            failed.append(line)
        rows.append((requirement.name, expected, version, "FAIL" if line in failed else "PASS"))
    if items is not None:
        items(rows)
    if failed:
        raise ValueError("requirements.txt と解決した版が一致しません: " + ", ".join(failed))
    if requested is not None and requested != active:
        raise ValueError("requirements.txt と調査結果の対象が一致しません。")
    return len(requirements)


# --- 第 1 段階: 調査の環境で、requirements.txt の Python の依存と Ubuntu の依存を 1 つの工程で調べる（README.md「第 1 段階の中身」）---
def resolve_python_dependencies(
    project=DEFAULT_PROJECT, platform="linux/arm64", *, apt_snapshot=None, run_id=None
):
    _, paths = target_inputs(project)
    apt_snapshot = apt_snapshot or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    environment = _lock_environment(paths, platform, apt_snapshot)
    platform = environment["platform"]
    packages = read_package_list(paths.get("ubuntu-packages.txt"))
    requirements = read_requirements(paths["requirements.txt"])
    if not requirements:
        raise ValueError("調査対象の Python の要件がありません。")
    if any(re.match(r"[A-Za-z0-9._-]+\s*\[", item) for item in requirements):
        raise ValueError("extras 付きの要件は、依存の確定で扱えません。")

    def discover(directory, record):
        log = directory / "discovery.log"
        log.write_text(
            f"Base image {environment['base_image']}\nPlatform {platform}\nAPT snapshot {apt_snapshot}\n",
            encoding="utf-8",
        )
        (directory / "apt-snapshot.txt").write_text(f"{apt_snapshot}\n", encoding="utf-8")
        print(f"調査記録 {directory}", file=sys.stderr)
        run_docker(["pull", "--platform", platform, environment["base_image"]], log)
        record["image_id"] = container.local_image_id(environment["base_image"], platform)
        resolution = directory / "resolution"
        _run_inspect_build(
            environment["base_image"],
            platform,
            resolution,
            log,
            {
                "requirements.txt": paths["requirements.txt"].read_text(encoding="utf-8"),
                "ubuntu-packages.txt": _ubuntu_text(paths),
            },
            apt_snapshot=apt_snapshot,
        )
        check = operation_checks(directory, "discover")
        _stop_on_failed_inspection(check, resolution, log)
        native_file = resolution / "native-plan.json"
        native = (
            json.loads(native_file.read_text(encoding="utf-8")) if native_file.is_file() else {}
        )
        plan = _installed_plan(
            (resolution / "base-ubuntu.tsv").read_text(encoding="utf-8").splitlines(),
            (resolution / "installed-ubuntu.tsv").read_text(encoding="utf-8").splitlines(),
            packages,
            native,
        )
        _write_base_inventory(
            directory / "base-ubuntu.tsv",
            environment["base_image"],
            platform,
            (resolution / "base-ubuntu.tsv").read_text(encoding="utf-8").splitlines(),
        )
        status = int((resolution / "resolve-status").read_text(encoding="utf-8"))
        review = ""
        native_review = _native_review(native)
        if native_review:
            review = review_text(item["item"] for item in native_review)
        if status:
            _no_wheel(resolution / "uv-resolve.log")
            review = (
                "要確認: wheel（対象のプラットフォームに合う wheel が無い要件があります。"
                "uv の解決のログ（uv-resolve.log）を見てください）"
            )
        _discovery_checks(check, status, plan, native_review, resolution / "pylock.toml")
        items = operation_check_items(directory, "discover")
        requested = set()
        if not status:
            resolved = {
                re.sub(r"[-_.]+", "-", str(item.get("name", ""))).lower(): str(
                    item.get("version", "")
                )
                for item in tomllib.loads(
                    (resolution / "pylock.toml").read_text(encoding="utf-8")
                ).get("packages", [])
            }
            python_environment = json.loads(
                (resolution / "python-environment.json").read_text(encoding="utf-8")
            )
            requested = {
                canonicalize_name(requirement.name)
                for _, requirement, applies in _target_requirements(
                    requirements, python_environment
                )
                if applies
            }
            check(
                "Python requirements",
                lambda: _check_requirements(
                    requirements,
                    resolved,
                    python_environment,
                    lambda rows: items("Python requirements", rows),
                ),
                detail=f"要件 {len(requirements)}",
            )
        if (resolution / "native-libraries.tsv").is_file():
            shutil.copyfile(resolution / "native-libraries.tsv", directory / "native-libraries.tsv")
        _write_discovery_tables(
            directory,
            plan,
            [
                *native_review,
                *([{"item": "wheel", "detail": _NO_WHEEL}] if status else []),
            ],
            None if status else resolution / "pylock.toml",
            requested,
        )

        def unchanged():
            current = _lock_environment(paths, platform, apt_snapshot)
            items(
                "Build inputs unchanged", hash_item_rows(environment["inputs"], current["inputs"])
            )
            if current != environment:
                raise RuntimeError(
                    "調査の間にプロジェクトの request/ の入力が変わりました。"
                    "調査をやり直してください。"
                )
            return f"入力 {len(current['inputs'])}"

        check("Build inputs unchanged", unchanged, detail=str)
        important = [
            directory / "checks.tsv",
            directory / "apt-snapshot.txt",
            directory / "ubuntu-packages.tsv",
            directory / "base-ubuntu.tsv",
            directory / "python-packages.tsv",
            resolution / "python-environment.json",
        ]
        if not status:
            important.extend([resolution / "pylock.toml", native_file])
        contract.write_build_environment(
            {
                **environment,
                "review": review,
                "evidence": {
                    p.relative_to(directory).as_posix(): file_sha256(p) for p in important
                },
            },
            directory / "build-environment.json",
        )
        return {
            "status": "REVIEW" if review else "PASS",
            "review": review,
            "directory": str(directory),
            "log": str(log),
        }

    return record(
        "resolve-python",
        {
            "image": environment["base_image"],
            "project": project,
            "platform": platform,
            "apt_snapshot": apt_snapshot,
        },
        [*paths.values(), INSPECT_SCRIPT, INSPECT_DOCKERFILE],
        discover,
        run_id=run_id,
    )


# --- 確定記録の版の一覧 python-pins.tsv（package・version・sha256）を書く（README.md「Python」）---
def _write_pins(path, pylock):
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(("package", "version", "sha256"))
        for item in tomllib.loads(pylock).get("packages", []):
            name = re.sub(r"[-_.]+", "-", str(item["name"])).lower()
            hashes = "; ".join(wheel["hashes"]["sha256"] for wheel in item.get("wheels", []))
            writer.writerow((name, item["version"], hashes))


# --- uv の pylock.toml から、配布物の版を読む（wheel と SHA-256 が無いもの・重複は止める）（README.md「Python」）---
def _pylock_pins(text):
    pins = {}
    for item in tomllib.loads(text).get("packages", []):
        name = re.sub(r"[-_.]+", "-", str(item.get("name", ""))).lower()
        version = str(item.get("version", ""))
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
            or not re.fullmatch(r"[A-Za-z0-9!+._-]+", version)
            or name in pins
        ):
            raise ValueError(
                f"調査記録の Python パッケージの名前か版が不正か、名前が重なっています（{name}）"
            )
        wheels = item.get("wheels") or []
        if (
            "sdist" in item
            or not wheels
            or any(
                not urlsplit(str(wheel.get("url", ""))).path.endswith(".whl")
                or not re.fullmatch(r"[a-f0-9]{64}", str(wheel.get("hashes", {}).get("sha256", "")))
                for wheel in wheels
            )
        ):
            raise ValueError(f"SHA-256 を確認できる wheel がありません（{name}）")
        pins[name] = version
    return pins


# --- 調査の記録と今の値（固定ベース・入力と調査の記録のハッシュ）を照合した行を返す（README.md「Python」）---
def _discovery_item_rows(saved, environment, current):
    return [
        ("base_image", saved.get("base_image"), environment.get("base_image")),
        *hash_item_rows(saved.get("inputs", {}), environment.get("inputs", {}), "input:"),
        *hash_item_rows(saved.get("evidence", {}), current, "evidence:"),
    ]


# --- pylock.toml の配布物ごとに、wheel の SHA-256 と名前・版の形を照合した行を返す（README.md「Python」）---
def _pylock_item_rows(text):
    rows = []
    seen = set()
    for item in tomllib.loads(text).get("packages", []):
        name = re.sub(r"[-_.]+", "-", str(item.get("name", ""))).lower()
        version = str(item.get("version", ""))
        wheels = item.get("wheels") or []
        hashes = [str(wheel.get("hashes", {}).get("sha256", "")) for wheel in wheels]
        valid = (
            re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
            and re.fullmatch(r"[A-Za-z0-9!+._-]+", version)
            and name not in seen
            and "sdist" not in item
            and wheels
            and all(urlsplit(str(wheel.get("url", ""))).path.endswith(".whl") for wheel in wheels)
            and all(re.fullmatch(r"[a-f0-9]{64}", digest) for digest in hashes)
        )
        seen.add(name)
        rows.append((f"{name} {version}", "", "; ".join(hashes), "PASS" if valid else "FAIL"))
    return rows


# --- 要確認項目が確認根拠で確定したかを、照合の記録に残す（README.md「結果の共通形式」）---
def _resolved_check(check, findings, unresolved):
    check(
        "Review items resolved",
        detail=f"要確認 {len(findings)}、残り {len(unresolved)}",
        status="REVIEW" if unresolved else "PASS",
    )


# --- 古い調査記録の wheel の理由も同じ項目として照合する（README.md「Python」）---
def _review_key(item, detail):
    if item == "wheel":
        detail = re.sub(
            r"^No verifiable wheel for (.*)$",
            r"SHA-256 を確認できる wheel がありません（\1）",
            detail,
        )
        detail = re.sub(
            r"^Invalid or duplicate resolved Python package: (.*)$",
            r"調査記録の Python パッケージの名前か版が不正か、名前が重なっています（\1）",
            detail,
        )
    return item, detail


# --- 確認根拠の TSV（item・detail・reason）を要確認項目に当てる（README.md「Python」）---
def _apply_review_notes(findings, review_notes):
    with Path(review_notes).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream, delimiter="\t"))
    header, notes = (rows[0], rows[1:]) if rows else ([], [])
    keys = [_review_key(row[0], row[1]) for row in notes if len(row) == 3]
    known = {_review_key(item["item"], item["detail"]) for item in findings}
    if (
        header != ["item", "detail", "reason"]
        or len(keys) != len(notes)
        or len(set(keys)) != len(keys)
        or not set(keys) <= known
        or any(not row[2].strip() for row in notes)
    ):
        raise ValueError("確認根拠の TSV が調査の要確認項目と一致しません。")
    reasons = {_review_key(row[0], row[1]): row[2] for row in notes}
    for item in findings:
        item["decision"] = reasons.get(_review_key(item["item"], item["detail"]), item["decision"])
    if any(item["item"] == "wheel" and item["decision"] for item in findings):
        raise ValueError("wheel の要確認項目は、確認根拠では確定できません。要件を直してください。")


# --- 要確認項目を dependency-review.tsv に書く（列は item・detail・decision）（CHECKS.md 3 節）---
def _write_review(path, findings):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(("item", "detail", "decision"))
        writer.writerows((item["item"], item["detail"], item["decision"]) for item in findings)


# --- 調査の記録のプラットフォームを返す（build-environment.json から読み、読めなければ None。R はベース一覧の 1 行目から読む inventory_platform）（README.md「Python」）---
def _discovery_platform(discovery):
    try:
        saved = (discovery / "build-environment.json").read_text(encoding="utf-8-sig")
        return json.loads(saved).get("platform")
    except (OSError, ValueError, AttributeError):
        return None


# --- 確定するプラットフォームが、調査の記録と同じかを確かめる（README.md「Python」）---
def _same_platform(saved, platform):
    if saved.get("platform") != platform:
        raise ValueError(
            f"調査の記録のプラットフォームが一致しません: {saved.get('platform')} / {platform}。"
            "--platform を付けずに確定するか、調査をやり直してください。"
        )


# --- 第 1 段階: 調査の記録を照合して採用する版を確定し、第 2・第 3 段階で使う確定記録を作る（README.md「第 1 段階の中身」）---
def confirm_python_dependencies(
    discovery,
    project=DEFAULT_PROJECT,
    platform=None,
    *,
    review_notes=None,
    run_id=None,
):
    if isinstance(discovery, dict):
        discovery = discovery.get("directory")
    if not isinstance(discovery, (str, Path)) or not discovery or not Path(discovery).is_dir():
        raise ValueError("調査の記録先（discover の directory）を指定してください。")
    discovery = Path(discovery)
    platform = container.validate_platform(platform or _discovery_platform(discovery))
    _, paths = target_inputs(project)
    saved_file = discovery / "build-environment.json"
    native_file = discovery / "resolution/native-plan.json"
    recorded = [
        discovery / name
        for name in (
            "checks.tsv",
            "apt-snapshot.txt",
            "ubuntu-packages.tsv",
            "base-ubuntu.tsv",
            "python-packages.tsv",
            "resolution/python-environment.json",
            "resolution/pylock.toml",
            "resolution/native-plan.json",
        )
        if (discovery / name).is_file()
    ]

    def confirm(directory, record):
        check = operation_checks(directory, "confirm")
        items = operation_check_items(directory, "confirm")
        saved = contract.read_build_environment(saved_file)
        check("Discovery platform", lambda: _same_platform(saved, platform), platform)
        environment = _lock_environment(paths, platform, saved["apt_snapshot"])
        evidence = {
            "checks.tsv",
            "apt-snapshot.txt",
            "ubuntu-packages.tsv",
            "base-ubuntu.tsv",
            "python-packages.tsv",
            "resolution/python-environment.json",
        }
        status_file = discovery / "resolution/resolve-status"
        if status_file.is_file() and not int(status_file.read_text(encoding="utf-8")):
            evidence.update({"resolution/pylock.toml", "resolution/native-plan.json"})

        def unchanged():
            current = {
                name: file_sha256(discovery / name)
                for name in evidence | set(saved["evidence"])
                if (discovery / name).is_file()
            }
            items("Discovery unchanged", _discovery_item_rows(saved, environment, current))
            if (
                any(saved.get(key) != value for key, value in environment.items())
                or set(saved["evidence"]) != evidence
                or current != saved["evidence"]
            ):
                raise ValueError(
                    "調査のあとで入力か調査の記録が変わっています。調査をやり直してください。"
                )
            with (discovery / "checks.tsv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE))
            for name in ("Python dependency resolution", "Ubuntu dependency plan"):
                if sum(row["check"] == name for row in rows) != 1:
                    raise ValueError("完了済みの調査記録を指定してください。")
            if any(row["status"] not in ("PASS", "REVIEW") for row in rows):
                raise ValueError("完了済みの調査記録を指定してください。")
            snapshot = (discovery / "apt-snapshot.txt").read_text(encoding="utf-8").splitlines()
            if snapshot != [saved["apt_snapshot"]]:
                raise ValueError("調査の記録の APT スナップショットが一致しません。")
            read_base_inventory(discovery / "base-ubuntu.tsv", environment["base_image"], platform)
            return f"入力 {len(saved['inputs'])}、調査の記録 {len(evidence)}"

        check("Discovery unchanged", unchanged, detail=str)
        plan = read_table_tsv(discovery / "ubuntu-packages.tsv")
        native = (
            json.loads(native_file.read_text(encoding="utf-8")) if native_file.is_file() else {}
        )
        findings = [
            {"item": item["item"], "detail": item["detail"], "decision": ""}
            for item in _native_review(native)
        ]
        findings.extend(
            {
                "item": row["package"],
                "detail": "固定ベースのパッケージを更新する候補",
                "decision": "",
            }
            for row in plan
            if row["action"] == "update"
        )
        pins = {}
        python_environment = json.loads(
            (discovery / "resolution/python-environment.json").read_text(encoding="utf-8")
        )
        machine = {"linux/amd64": "x86_64", "linux/arm64": "aarch64"}[platform]

        def architecture():
            if python_environment.get("platform_machine") != machine:
                raise ValueError("uv の解決を、対象と違うアーキテクチャで行っています。")

        check("Resolution architecture", architecture, machine)
        pylock = (
            (discovery / "resolution/pylock.toml").read_text(encoding="utf-8")
            if ("resolution/pylock.toml" in evidence)
            else None
        )
        if pylock is None:
            findings.append({"item": "wheel", "detail": saved["review"], "decision": ""})
            check("Pinned package hashes", detail=saved["review"], status="REVIEW")
        else:
            items("Pinned package hashes", _pylock_item_rows(pylock))
            try:
                pins = _pylock_pins(pylock)
            except ValueError as error:
                findings.append({"item": "wheel", "detail": str(error), "decision": ""})
                check("Pinned package hashes", detail=str(error), status="REVIEW")
            else:
                check("Pinned package hashes", detail=f"配布物 {len(pins)}")
                requirements = read_requirements(paths["requirements.txt"])
                requested = {
                    canonicalize_name(row["package"])
                    for row in read_table_tsv(discovery / "python-packages.tsv")
                    if row["requested"] == "TRUE"
                }
                check(
                    "Python requirements",
                    lambda: _check_requirements(
                        requirements,
                        pins,
                        python_environment,
                        lambda rows: items("Python requirements", rows),
                        requested=requested,
                    ),
                    detail=f"要件 {len(requirements)}",
                )
        if review_notes:
            check(
                "Review notes",
                lambda: _apply_review_notes(findings, review_notes),
                shown_path(review_notes),
            )
        unresolved = [item for item in findings if not item["decision"]]
        _resolved_check(check, findings, unresolved)
        checks = {
            "status": "REVIEW" if unresolved else "PASS",
            "language_packages": len(pins),
            "ubuntu_packages": len(plan),
            "unresolved": len(unresolved),
        }
        _write_review(directory / "dependency-review.tsv", findings)
        if unresolved:
            return {
                **checks,
                "lock": None,
                "review": review_text(item["item"] for item in unresolved),
            }
        ubuntu_pins = []
        for item in plan:
            name, arch, version = (item[key] for key in ("package", "architecture", "version"))
            if (
                not re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", name)
                or not re.fullmatch(r"[a-z0-9]+", arch)
                or not re.fullmatch(r"[A-Za-z0-9.:+~\-]+", version)
            ):
                raise ValueError(f"Ubuntu パッケージの名前か版が不正です: {item}")
            if item["action"] in ("install", "update"):
                ubuntu_pins.append(f"{name}={version}")
        contract.write_build_environment(
            {**environment, "python_version": python_environment["python_full_version"]},
            directory / "build-environment.json",
        )
        shutil.copyfile(paths["requirements.txt"], directory / "requirements.txt")
        if file_sha256(directory / "requirements.txt") != saved["inputs"]["requirements.txt"]:
            raise ValueError(
                "確定の間に requirements.txt が変わりました。調査をやり直してください。"
            )
        for name in ("ubuntu-packages.tsv", "base-ubuntu.tsv", "python-packages.tsv"):
            shutil.copyfile(discovery / name, directory / name)
        (directory / "pylock.toml").write_text(pylock, encoding="utf-8")
        _write_pins(directory / "python-pins.tsv", pylock)
        (directory / "ubuntu-pins.txt").write_text(
            "".join(pin + "\n" for pin in ubuntu_pins), encoding="utf-8"
        )
        lock = directory / "dependency-lock.tsv"
        with lock.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            writer.writerow(("path", "sha256"))
            writer.writerows((name, file_sha256(directory / name)) for name in LOCK_FILES)
        return {**checks, "lock": str(lock), "review": ""}

    return record(
        "confirm-python",
        {
            "image": read_base_file(paths["base.txt"])["BASE_IMAGE"],
            "project": project,
            "platform": platform,
        },
        [*paths.values(), saved_file, *recorded, *([Path(review_notes)] if review_notes else [])],
        confirm,
        run_id=run_id,
    )


# --- Dockerfile の生成に使う値の表（dockerfile-inputs。列は item・value）を確定記録から作る（無ければ空）（README.md「第 1 段階の中身」）---
def dockerfile_input_rows(lock, project=DEFAULT_PROJECT):
    if lock is None:
        return []
    lock = (container.ROOT / lock).resolve(strict=True)
    if not lock.is_file():
        raise ValueError(f"確定記録のファイルを指定してください: {lock}")
    environment = contract.read_build_environment(lock.parent / "build-environment.json")
    return [
        {"item": "project", "value": project_name(project)},
        {"item": "base_image", "value": environment["base_image"]},
        {"item": "platform", "value": environment["platform"]},
        {"item": "apt_snapshot", "value": environment["apt_snapshot"]},
        {"item": "python_version", "value": environment.get("python_version")},
        {"item": "dependency_lock", "value": shown_path(lock)},
        {"item": "ubuntu_pins", "value": shown_path(lock.parent / "ubuntu-pins.txt")},
        {"item": "language_pins", "value": shown_path(lock.parent / "python-pins.tsv")},
        {"item": "pylock", "value": shown_path(lock.parent / "pylock.toml")},
    ]
