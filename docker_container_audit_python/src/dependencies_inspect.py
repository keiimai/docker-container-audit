#!/usr/bin/env python3
# --- 対象コンテナの中で Python の環境を監査するか、wheel の共有ライブラリに要る Ubuntu パッケージを割り出す（R は R/dependencies-inspect.R）（README.md「Python」）---

import csv
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path


# --- コマンドを走らせ、終了コードと出力を返す（起動できないか時間切れなら終了コードは None）（README.md「Python」）---
def command(args):
    try:
        result = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=600,
            check=False,
        )
        return {"status": result.returncode, "output": result.stdout}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": None, "output": str(exc)}


# --- パッケージ名を PEP 503 の形にそろえる（README.md「Python」）---
def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


#: コンテナに読み取り専用で渡す uv の実行ファイル（操作側が固定した公式イメージから取り出す）。
UV = "/audit/uv"


# --- 要件を一時ファイルに書き、そのパスを返す（uv は要件をファイルで受け取る）（README.md「Python」）---
def requirement_file(lines, name):
    path = Path(os.environ.get("TMPDIR", "/tmp")) / name
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


#: 機械の名前（platform.machine()）と、ELF の見出しの機械の種類（e_machine）の対応。
_ELF_MACHINES = {"x86_64": 0x3E, "amd64": 0x3E, "aarch64": 0xB7, "arm64": 0xB7}


# --- ELF の見出しから機械の種類（e_machine）を返す（ELF でなければ None）（README.md「Python」）---
def elf_machine(header):
    if len(header) < 20 or header[:4] != b"\x7fELF":
        return None
    return int.from_bytes(header[18:20], "big" if header[5] == 2 else "little")


# --- 配布物が記録する ELF ファイルを ldd で調べ、解決したパスを dpkg の持ち主に対応させる（README.md「Python」）---
def inspect_shared_libraries(distributions):
    result = {
        "status": "PASS",
        "files": [],
        "review": [],
        "missing": [],
        "foreign": [],
        "scope": "ELF files listed by installed distribution metadata; dynamic dlopen, executables, build and unrecorded files are not covered.",
    }
    if not shutil.which("ldd"):
        result["status"] = "REVIEW"
        result["review"].append("ldd is unavailable; shared libraries were not inspected")
        return result
    dpkg = shutil.which("dpkg-query")
    for dist in distributions:
        name = dist.metadata.get("Name", "")
        if dist.files is None:
            result["review"].append("No file inventory for " + name)
            continue
        for rel in dist.files:
            if not re.search(r"\.so(?:\.[0-9]+)*$", str(rel)):
                continue
            path = Path(dist.locate_file(rel))
            try:
                with path.open("rb") as handle:
                    machine = elf_machine(handle.read(20))
            except OSError as exc:
                result["review"].append(str(path) + ": " + str(exc))
                continue
            if machine is None:
                continue
            own = _ELF_MACHINES.get(platform.machine())
            if own is not None and machine != own:
                result["foreign"].append(str(path))
                continue
            linked = command(["ldd", str(path)])
            record = {
                "distribution": name,
                "file": str(path),
                "relative": str(rel),
                "libraries": [],
                "ldd_status": linked["status"],
            }
            for line in linked["output"].splitlines():
                match = re.match(r"\s*(\S+)\s+=>\s+(not found|/\S+)", line)
                if not match:
                    continue
                soname, resolved = match.groups()
                entry = {"soname": soname, "path": resolved, "owners": []}
                if resolved == "not found":
                    result["missing"].append({"file": str(path), "soname": soname})
                elif dpkg:
                    candidates = list(dict.fromkeys([resolved, os.path.realpath(resolved)]))
                    for candidate in candidates:
                        owners = command([dpkg, "-S", candidate])
                        if owners["status"] == 0:
                            entry["owners"] = owners["output"].strip().splitlines()
                            break
                record["libraries"].append(entry)
            if linked["status"] != 0 and not any(
                x["path"] == "not found" for x in record["libraries"]
            ):
                result["review"].append(
                    "ldd failed for " + str(path) + ": " + linked["output"].strip()
                )
            result["files"].append(record)
    if not dpkg:
        result["review"].append("dpkg-query is unavailable; OS package ownership was not mapped")
    if result["missing"]:
        result["status"] = "FAIL"
    elif result["review"]:
        result["status"] = "REVIEW"
    return result


# --- 共有ライブラリごとの場所と持ち主を native-libraries.tsv に書く（README.md「Quarto 報告書を作る」）---
def write_native_libraries(native, path, owners=True, relative=False):
    fields = ["language_package", "file", "soname", "path", *(["owners"] if owners else [])]
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=fields,
            dialect="excel-tab",
            quoting=csv.QUOTE_ALL,
            lineterminator="\n",
        )
        writer.writeheader()
        for record in native.get("files", []):
            for library in record["libraries"]:
                row = {
                    "language_package": record["distribution"],
                    "file": record.get("relative", record["file"]) if relative else record["file"],
                    "soname": library["soname"],
                    "path": library["path"],
                }
                if owners:
                    row["owners"] = "; ".join(
                        owner.rsplit(": ", 1)[0] for owner in library["owners"]
                    )
                writer.writerow(row)


# --- uv pip check で配布物どうしの依存を確かめる（uv が無ければ status は None）（README.md「Python」）---
def run_uv_check(uv):
    if not Path(uv).is_file():
        return {
            "status": None,
            "output": "uv is unavailable; dependency consistency was not checked",
        }
    result = command([uv, "pip", "check", "--python", sys.executable])
    lines = result["output"].splitlines()
    result["output"] = "\n".join(
        re.sub(r"^(Checked \d+ packages?) in \S+$", r"\1", line)
        for line in lines
        if not line.startswith("Using Python")
    )
    return result


# --- 要件を満たすかを、uv のネットワークを使わない試算で確かめる（README.md「Python」）---
def check_requirements(lines, uv):
    if not Path(uv).is_file():
        return {"count": len(lines), "pass": False, "output": "uv is unavailable"}
    result = command(
        [
            uv,
            "pip",
            "install",
            "--dry-run",
            "--offline",
            "--python",
            sys.executable,
            "-r",
            str(requirement_file(lines, "requirements.txt")),
        ]
    )
    satisfied = result["status"] == 0 and "Would make no changes" in result["output"]
    return {"count": len(lines), "pass": satisfied, "output": result["output"].strip()}


# --- Quarto の CLI を確かめ、判定・説明・版を返す（無ければ REVIEW、動かなければ FAIL）（README.md「Python」）---
def audit_quarto(executable):
    if not executable:
        return {"status": "REVIEW", "detail": "quarto is unavailable", "found": ""}
    result = command([executable, "--version"])
    if result["status"] != 0:
        detail = _one_line(result["output"]) or "command failed"
        return {"status": "FAIL", "detail": detail, "found": detail}
    version = (result["output"].strip().splitlines() or [""])[0].strip()
    return {"status": "PASS", "detail": f"quarto {version} ({executable})", "found": version}


# --- 照合した項目を audit-items.tsv の行にする（status を省けば expected と found で決める）（1 行。R は audit_items）（README.md「Python」）---
def audit_item(check, item, expected="", found="", status=None):
    if status is None:
        status = "PASS" if found is not None and str(found) == str(expected or "") else "FAIL"

    def text(value):
        return re.sub(r"[\t\r\n]+", " ", "" if value is None else str(value))

    return {
        "check": check,
        "item": text(item),
        "expected": text(expected),
        "found": text(found),
        "status": status,
    }


# --- requirements.txt の要件を 1 つずつの行にする（README.md「Python」）---
def requirement_items(lines, versions, requirements):
    changed = {
        canonical(match.group(1))
        for line in requirements["output"].splitlines()
        if (match := re.match(r"^\s*[-+~]\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line))
    }
    rows = []
    for line in lines:
        name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line).group(0)
        found = versions.get(canonical(name), [])
        failed = not requirements["pass"] and (not found or canonical(name) in changed)
        rows.append([name, line[len(name) :].strip(), ", ".join(found), failed])
    if not requirements["pass"] and not any(row[3] for row in rows):
        for row in rows:
            row[3] = True
    return [
        audit_item("Python requirements.txt", name, expected, found, "FAIL" if failed else "PASS")
        for name, expected, found, failed in rows
    ]


#: 監査の項目の並び（R の監査と同じ順）。
_CHECK_ORDER = (
    "Python requirements.txt",
    "uv pip check",
    "Python module import",
    "Shared libraries",
    "Quarto CLI",
    "OS (Debian/Ubuntu)",
    "APT consistency",
    "dpkg audit",
    "Ubuntu package inventory",
    "Base OS packages removed or changed",
    "Confirmed Python versions",
    "Confirmed Ubuntu versions",
    "Unlisted Python additions",
    "Unlisted Ubuntu additions",
    "Confirmed Ubuntu additions missing or ambiguous",
    "Confirmed Python wheel hashes",
    "Confirmed Python runtime version",
)

#: 自動の import 確認から外す、環境を作るための道具の配布物（正規化した名前）。
_BOOTSTRAP = {"pip", "setuptools", "wheel"}

#: ビルドの導入記録が無いときの説明（R は INSTALL_REPORT_MISSING）。
_INSTALL_REPORT_MISSING = "ビルドの導入記録が無いので、SHA-256 は照合していない"


# --- ビルドで使った pylock.toml から、配布物ごとの「版 SHA-256」を返す（読めなければ空）（README.md「Python」）---
def used_pylock_hashes(path):
    try:
        import tomllib

        packages = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("packages", [])
    except (ImportError, OSError, ValueError):
        return {}
    return {
        canonical(str(item.get("name", ""))): f"{item.get('version', '')} "
        + "; ".join(
            str(wheel.get("hashes", {}).get("sha256", "")) for wheel in item.get("wheels") or []
        )
        for item in packages
    }


# --- ビルドの導入記録を、ファイル全体と配布物ごとの版・SHA-256 で照合する（README.md「第 3 段階の中身」）---
def _audit_wheel_hashes(confirmed, used):
    summary = {
        "check": "Confirmed Python wheel hashes",
        "status": "REVIEW",
        "detail": _INSTALL_REPORT_MISSING,
    }
    check = summary["check"]
    expected_digest = confirmed["pylock_sha256"]
    expected_hashes = confirmed.get("hashes", {})
    if not used.is_file():
        rows = [audit_item(check, "pylock.toml", expected_digest, "", "REVIEW")]
        rows.extend(
            audit_item(check, name, value, "", "REVIEW")
            for name, value in sorted(expected_hashes.items())
        )
        return summary, rows

    digest = hashlib.sha256(used.read_bytes()).hexdigest()
    file_row = audit_item(check, "pylock.toml", expected_digest, digest)
    package_rows = []
    if "hashes" in confirmed:
        built = used_pylock_hashes(used)
        package_rows = [
            audit_item(check, name, expected_hashes.get(name), built.get(name))
            for name in sorted(expected_hashes.keys() | built.keys())
        ]
    mismatches = [row for row in package_rows if row["status"] == "FAIL"]
    details = sorted(
        f"{row['item']} {row[field]}"
        for row in mismatches
        for field in ("expected", "found")
        if row[field]
    )
    if file_row["status"] == "FAIL" or not details:
        details.insert(0, f"Expected pylock.toml {expected_digest}, found {digest}")
    summary["status"] = "FAIL" if file_row["status"] == "FAIL" or mismatches else "PASS"
    summary["detail"] = "; ".join(details)
    return summary, [file_row, *package_rows]


# --- 配布物の最上位のモジュール名を返す（top_level.txt か、ファイルの一覧から）（README.md「Python」）---
def top_level_modules(distributions):
    modules = set()
    for dist in distributions:
        if canonical(dist.metadata.get("Name", "")) in _BOOTSTRAP:
            continue
        read_text = getattr(dist, "read_text", None)
        text = read_text("top_level.txt") if read_text else None
        names = text.split() if text else []
        if not names:
            for path in getattr(dist, "files", None) or []:
                parts = path.parts
                if (
                    not parts
                    or parts[0] in ("..", "__pycache__")
                    or parts[0].endswith((".dist-info", ".data", ".egg-info"))
                ):
                    continue
                if len(parts) > 1 and path.suffix == ".py":
                    names.append(parts[0])
                elif len(parts) == 1 and path.suffix in (".py", ".so"):
                    names.append(parts[0].split(".")[0])
        modules.update(name for name in names if name.isidentifier() and not name.startswith("_"))
    return modules


# --- 環境を監査し、python-audit.json と、installed-packages など 4 つの TSV を書く（README.md「Python」）---
def inspect_audit_main(config, output):
    report = {"status": "FAIL", "python": sys.version, "machine": platform.machine()}
    checks = []
    items = []
    try:
        _collect_audit(config, report, checks, items)
    except (Exception, KeyboardInterrupt) as error:
        try:
            _write_audit(output, report, checks, items)
        except Exception as write_error:
            error.add_note(str(write_error))
        raise
    statuses = {check["status"] for check in checks}
    report["status"] = next((status for status in ("FAIL", "REVIEW") if status in statuses), "PASS")
    _write_audit(output, report, checks, items)
    passed = report["status"] != "FAIL"
    print_audit_log(
        report, checks, report["installed"], report.get("ubuntu"), config.get("baseline"), passed
    )
    return 0 if passed else 1


# --- 調べ終えた項目から監査の記録に加える（README.md「Python」）---
def _collect_audit(config, report, checks, items):
    def record(name, checked, detail):
        checks.append({"check": name, "status": "PASS" if checked else "FAIL", "detail": detail})

    report["executable"] = sys.executable
    report["scope"] = (
        "Installed distribution metadata, required versions and imports; not application behavior."
    )

    distributions = list(metadata.distributions())
    installed = sorted(
        (
            {"name": dist.metadata.get("Name", ""), "version": dist.version}
            for dist in distributions
        ),
        key=lambda x: (canonical(x["name"]), x["version"]),
    )
    report["installed"] = installed
    versions = {}
    for item in installed:
        versions.setdefault(canonical(item["name"]), []).append(item["version"])
    imports = report["imports"] = []
    for module in sorted(top_level_modules(distributions)):
        result = command(
            [
                sys.executable,
                "-c",
                "import importlib,sys; importlib.import_module(sys.argv[1])",
                module,
            ]
        )
        imports.append(
            {
                "module": module,
                "pass": result["status"] == 0,
                "status": result["status"],
                "output": result["output"],
            }
        )
    items.extend(
        audit_item(
            "Python module import",
            item["module"],
            "",
            "loaded" if item["pass"] else (item["output"].strip().splitlines() or [""])[-1],
            "PASS" if item["pass"] else "FAIL",
        )
        for item in imports
    )
    failed_imports = [
        f"{item['module']}: {(item['output'].strip().splitlines() or [''])[-1]}"
        for item in imports
        if not item["pass"]
    ]
    record(
        "Python module import",
        not failed_imports,
        "; ".join(failed_imports) if failed_imports else f"{len(imports)} 件",
    )
    uv = config.get("uv", UV)
    uv_check = report["uv_check"] = run_uv_check(uv)
    uv_unavailable = uv_check["status"] is None
    checks.append(
        {
            "check": "uv pip check",
            "status": "REVIEW" if uv_unavailable else "PASS" if uv_check["status"] == 0 else "FAIL",
            "detail": _one_line(uv_check["output"]),
        }
    )
    requirements = report["requirements"] = (
        check_requirements(config["requirements"], uv) if config.get("requirements") else None
    )
    if requirements is not None:
        items.extend(requirement_items(config["requirements"], versions, requirements))
        record(
            "Python requirements.txt",
            requirements["pass"],
            f"{requirements['count']} 件"
            if requirements["pass"]
            else "; ".join(requirements["output"].splitlines()[-3:]),
        )
    native = report["native"] = inspect_shared_libraries(distributions)
    native_details = native.get("review", []) + [
        f"{item['soname']}: {item['file']}" for item in native.get("missing", [])
    ]
    if native.get("foreign"):
        native_details.append("他の機械用のため調べない: " + ", ".join(native["foreign"]))
    checks.append(
        {
            "check": "Shared libraries",
            "status": native["status"],
            "detail": "; ".join(native_details),
        }
    )
    os_release = platform.freedesktop_os_release() if Path("/etc/os-release").is_file() else {}
    report["os_release"] = os_release
    if config.get("ubuntu") is not None:
        ubuntu = report["ubuntu"] = {"packages": [], "checks": [], "items": []}
        try:
            audit_ubuntu(config["ubuntu"], os_release, config.get("baseline"), result=ubuntu)
        finally:
            items.extend(ubuntu["items"])
            checks.extend(ubuntu["checks"])
    quarto = audit_quarto(shutil.which("quarto"))
    checks.append({"check": "Quarto CLI", "status": quarto["status"], "detail": quarto["detail"]})
    items.append(audit_item("Quarto CLI", "quarto", "", quarto["found"], quarto["status"]))
    report["quarto"] = quarto
    if confirmed := config.get("confirmed"):
        expected = confirmed["packages"]
        different = [
            f"{name} expected {expected[name]}, found {', '.join(versions.get(name, []))}"
            for name in sorted(expected)
            if versions.get(name, []) != [expected[name]]
        ]
        record("Confirmed Python versions", not different, "; ".join(different))
        items.extend(
            audit_item(
                "Confirmed Python versions",
                name,
                expected[name],
                ", ".join(versions.get(name, [])) or None,
                "PASS" if versions.get(name, []) == [expected[name]] else "FAIL",
            )
            for name in sorted(expected)
        )
        unlisted = [
            f"{name} {', '.join(versions[name])}"
            for name in sorted(versions.keys() - expected.keys())
        ]
        checks.append(
            {
                "check": "Unlisted Python additions",
                "status": "REVIEW" if unlisted else "PASS",
                "detail": "; ".join(unlisted),
            }
        )
        items.extend(
            audit_item("Unlisted Python additions", name, "", ", ".join(versions[name]), "REVIEW")
            for name in sorted(versions.keys() - expected.keys())
        )
        actual_python = platform.python_version()
        record(
            "Confirmed Python runtime version",
            actual_python == confirmed["python_version"],
            f"Expected {confirmed['python_version']}, found {actual_python}",
        )
        items.append(
            audit_item(
                "Confirmed Python runtime version",
                "Python",
                confirmed["python_version"],
                actual_python,
            )
        )
        hash_check, hash_items = _audit_wheel_hashes(
            confirmed, Path(sys.prefix, "audit-pylock.toml")
        )
        checks.append(hash_check)
        items.extend(hash_items)


# --- 監査の JSON と TSV を書く（途中で失敗した場合も調査済みの行を残す）（README.md「Python」）---
def _write_audit(output, report, checks, items):
    checks.sort(key=lambda item: _CHECK_ORDER.index(item["check"]))
    items.sort(key=lambda item: _CHECK_ORDER.index(item["check"]))
    report["checks"] = checks
    (output / "python-audit.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    )
    inventory = (
        {
            "ecosystem": "python",
            "package": row["name"],
            "architecture": "",
            "version": row["version"],
        }
        for row in report.get("installed", [])
    )
    ubuntu = report.get("ubuntu")
    with (output / "installed-packages.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["ecosystem", "package", "architecture", "version"],
            dialect="excel-tab",
            quoting=csv.QUOTE_ALL,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(inventory)
        if ubuntu:
            writer.writerows({"ecosystem": "ubuntu", **row} for row in ubuntu["packages"])
    if "native" in report:
        write_native_libraries(report["native"], output / "native-libraries.tsv")
    with (output / "audit-checks.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["check", "status", "detail"],
            dialect="excel-tab",
            quoting=csv.QUOTE_ALL,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(checks)
    with (output / "audit-items.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["check", "item", "expected", "found", "status"],
            dialect="excel-tab",
            quoting=csv.QUOTE_ALL,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(items)


# --- 監査のログを R と同じ形で書く（README.md「Python」）---
def print_audit_log(report, checks, installed, ubuntu, baseline, passed):
    for field in ("ID", "VERSION_ID"):
        if report["os_release"].get(field):
            print(f"AUDIT_OS_{field}={report['os_release'][field]}")
    for check in checks:
        print(f"{check['check']}: {check['status']}")
        if check["status"] != "PASS" and check["detail"]:
            print(f"   {check['detail']}")
    if baseline is not None:
        print("Additional Python packages: " + ", ".join(item["name"] for item in installed))
        base = {(row["package"], row["architecture"]) for row in baseline}
        added = [
            f"{row['package']}:{row['architecture']}"
            for row in (ubuntu or {}).get("packages", [])
            if (row["package"], row["architecture"]) not in base
        ]
        print("Additional OS packages: " + ", ".join(added))
    if passed:
        print("DEPENDENCY AUDIT PASSED")
    else:
        failed = [check["check"] for check in checks if check["status"] == "FAIL"]
        print("Dependency audit failed: " + ", ".join(failed), file=sys.stderr)


# --- コマンドの出力を、空でない行を「; 」でつないだ 1 行にする（R の command_failure の 1 行にする部分）（README.md「Python」）---
def _one_line(output):
    return "; ".join(line.strip() for line in str(output).splitlines() if line.strip())


# --- OS・APT と dpkg の整合性・Ubuntu のパッケージの版・固定ベースとの差分を確かめる（R の audit_os_release・audit_confirmed_ubuntu・audit_base_items・audit_base_changes に当たる）（README.md「Python」）---
def audit_ubuntu(plan, os_release, baseline=None, *, result=None):
    if result is None:
        result = {"packages": [], "checks": [], "items": []}
    checks = result["checks"]
    items = result["items"]

    def record(name, passed, detail=""):
        checks.append({"check": name, "status": "PASS" if passed else "FAIL", "detail": detail})

    is_dpkg_os = os_release.get("ID") in ("ubuntu", "debian")
    record(
        "OS (Debian/Ubuntu)",
        is_dpkg_os,
        os_release.get("PRETTY_NAME", "")
        if is_dpkg_os
        else f"Not Debian/Ubuntu: {os_release.get('PRETTY_NAME', '')}",
    )
    apt = command(
        [
            "apt-get",
            "-o",
            "Debug::NoLocking=1",
            "-o",
            "Dir::Cache::pkgcache=",
            "-o",
            "Dir::Cache::srcpkgcache=",
            "check",
        ]
    )
    record(
        "APT consistency",
        apt["status"] == 0,
        "" if apt["status"] == 0 else _one_line(apt["output"]) or "command failed",
    )
    dpkg = command(["dpkg", "--audit"])
    record(
        "dpkg audit",
        dpkg["status"] == 0 and not dpkg["output"].strip(),
        _one_line(dpkg["output"]) or ("" if dpkg["status"] == 0 else "command failed"),
    )
    inventory = command(
        ["dpkg-query", "-W", "-f=${Package}\t${Architecture}\t${Version}\t${db:Status-Status}\n"]
    )
    packages = []
    if inventory["status"] == 0:
        packages = [
            {"package": fields[0], "architecture": fields[1], "version": fields[2]}
            for line in inventory["output"].splitlines()
            if len(fields := line.split("\t")) == 4 and fields[3] == "installed"
        ]
    result["packages"] = packages
    record(
        "Ubuntu package inventory",
        bool(packages),
        "" if packages else inventory["output"].strip() or "No installed OS packages",
    )
    if plan is not None and inventory["status"] == 0:
        actual = {(p["package"], p["architecture"]): p["version"] for p in packages}
        planned = {(p["package"], p["architecture"]): p for p in plan}
        different = [
            f"{name}:{arch} expected {row['version']}, found {actual.get((name, arch), '')}"
            for (name, arch), row in sorted(planned.items())
            if row.get("action") in ("install", "update")
            and actual.get((name, arch)) != row["version"]
        ]
        record("Confirmed Ubuntu versions", not different, "; ".join(different))
        items.extend(
            audit_item(
                "Confirmed Ubuntu versions",
                f"{name}:{arch}",
                row["version"],
                actual.get((name, arch)),
            )
            for (name, arch), row in sorted(planned.items())
            if row.get("action") in ("install", "update")
        )
        unlisted = [f"{name}:{arch}" for name, arch in sorted(actual.keys() - planned.keys())]
        checks.append(
            {
                "check": "Unlisted Ubuntu additions",
                "status": "REVIEW" if unlisted else "PASS",
                "detail": "; ".join(unlisted),
            }
        )
        items.extend(
            audit_item(
                "Unlisted Ubuntu additions", f"{name}:{arch}", "", actual[name, arch], "REVIEW"
            )
            for name, arch in sorted(actual.keys() - planned.keys())
        )
        base = {key for key, row in planned.items() if row.get("base_version")}
        additions = [name for name, arch in actual if (name, arch) not in base]
        missing = [
            f"{name}:{arch}"
            for (name, arch), row in sorted(planned.items())
            if row.get("action") == "install" and (name, arch) not in actual
        ]
        ambiguous = sorted({name for name in additions if additions.count(name) > 1})
        record(
            "Confirmed Ubuntu additions missing or ambiguous",
            not (missing or ambiguous),
            "; ".join(missing + ambiguous),
        )
        items.extend(
            audit_item(
                "Confirmed Ubuntu additions missing or ambiguous",
                key,
                planned[tuple(key.split(":", 1))]["version"],
                "",
                "FAIL",
            )
            for key in missing
        )
        items.extend(
            audit_item(
                "Confirmed Ubuntu additions missing or ambiguous",
                name,
                "",
                ", ".join(
                    sorted(f"{n}:{a}" for n, a in actual if n == name and (n, a) not in base)
                ),
                "FAIL",
            )
            for name in ambiguous
        )
    if baseline is not None and inventory["status"] == 0:
        actual = {(p["package"], p["architecture"]): p["version"] for p in packages}
        approved = {
            (p["package"], p["architecture"]) for p in plan or [] if p.get("action") == "update"
        }
        changed = [
            f"{row['package']}:{row['architecture']}"
            for row in baseline
            if actual.get((row["package"], row["architecture"])) != row["version"]
            and not (
                (row["package"], row["architecture"]) in approved
                and (row["package"], row["architecture"]) in actual
            )
        ]
        record("Base OS packages removed or changed", not changed, "; ".join(changed))
        items.extend(
            audit_item(
                "Base OS packages removed or changed",
                f"{row['package']}:{row['architecture']}",
                row["version"],
                actual.get((row["package"], row["architecture"])),
                "PASS"
                if actual.get((row["package"], row["architecture"])) == row["version"]
                or (
                    (row["package"], row["architecture"]) in approved
                    and (row["package"], row["architecture"]) in actual
                )
                else "FAIL",
            )
            for row in baseline
        )
    return result


#: Contents の索引で、共有ライブラリを置くパス。
_LIBRARY_PATH = re.compile(r"^(?:usr/)?lib(?:/[a-z0-9_]+-linux-gnu|64)?/[^/]+$")

#: APT に Contents の索引を取らせる設定。
_CONTENTS_CONF = """Acquire::IndexTargets::deb::Contents-deb {
    MetaKey "$(COMPONENT)/Contents-$(ARCHITECTURE)";
    ShortDescription "Contents-$(ARCHITECTURE)";
    Description "$(RELEASE)/$(COMPONENT) $(ARCHITECTURE) Contents";
    KeepCompressed "true";
    Optional "true";
};
Acquire::IndexTargets::deb::Contents-deb-legacy {
    MetaKey "Contents-$(ARCHITECTURE)";
    ShortDescription "Contents-$(ARCHITECTURE)";
    Description "$(RELEASE) $(ARCHITECTURE) Contents";
    KeepCompressed "true";
    Optional "true";
    Fallback-Of "Contents-deb";
    Identifier "Contents-deb";
};
"""


# --- 署名付きの APT の Contents の索引で、共有ライブラリの名前を Debian・Ubuntu のパッケージに対応させる（README.md「検査対象のフォルダ」）---
def contents_providers(sonames, workdir):
    wanted = set(sonames)
    if not wanted:
        return {}
    conf = Path(workdir) / "contents.conf"
    conf.write_text(_CONTENTS_CONF, encoding="utf-8")
    update = command(
        [
            "apt-get",
            "-c",
            str(conf),
            "-o",
            "Acquire::Retries=2",
            "-o",
            "Acquire::http::Timeout=20",
            "-o",
            "Acquire::https::Timeout=20",
            "-o",
            "APT::Update::Error-Mode=any",
            "update",
        ]
    )
    if update["status"] != 0:
        raise RuntimeError("Ubuntu の APT 索引の更新に失敗しました。\n" + update["output"])
    targets = command(
        [
            "apt-get",
            "-c",
            str(conf),
            "indextargets",
            "--format",
            "$(FILENAME)",
            "Identifier: Contents-deb",
        ]
    )
    indices = [line for line in targets["output"].splitlines() if Path(line).is_file()]
    if targets["status"] != 0 or not indices:
        raise RuntimeError("Ubuntu の Contents 索引を取得できません。")
    providers = {}
    for index in indices:
        with subprocess.Popen(
            ["/usr/lib/apt/apt-helper", "cat-file", index], text=True, stdout=subprocess.PIPE
        ) as reader:
            for line in reader.stdout:
                fields = line.rsplit(None, 1)
                if len(fields) != 2:
                    continue
                path, owners = fields
                path = path.strip()
                if not _LIBRARY_PATH.match(path) or path.rsplit("/", 1)[-1] not in wanted:
                    continue
                for owner in owners.split(","):
                    providers.setdefault(path.rsplit("/", 1)[-1], set()).add(
                        owner.rsplit("/", 1)[-1]
                    )
            if reader.wait() != 0:
                raise RuntimeError(f"Ubuntu の Contents 索引を展開できません: {index}")
    return {name: sorted(packages) for name, packages in providers.items()}


# --- 足りない共有ライブラリごとに提供元を 1 つ選ぶ（ubuntu-packages.txt の候補が勝つ）（README.md「検査対象のフォルダ」）---
def choose_providers(missing, providers, listed):
    chosen, review = {}, []
    for soname in sorted(missing):
        options = providers.get(soname, [])
        explicit = [option for option in options if option in listed]
        if len(explicit) == 1:
            options = explicit
        if len(options) == 1:
            chosen[soname] = options[0]
        else:
            detail = (
                "提供パッケージの候補 " + ", ".join(options)
                if options
                else "Ubuntu の索引に提供パッケージが見つかりません。"
            )
            review.append({"item": soname, "detail": detail})
    return chosen, review


# --- dpkg -S の出力から、パッケージ名だけを返す（「pkg:arch, pkg2:arch: /path」の形）（README.md「Python」）---
def owner_packages(lines):
    packages = set()
    for line in lines:
        owners = line.rsplit(": ", 1)[0]
        packages.update(owner.strip().split(":")[0] for owner in owners.split(",") if owner.strip())
    return packages


# --- 要件の環境マーカーに使う値を、調査対象の Python から取得する（README.md「Python」）---
def _marker_environment():
    version = sys.implementation.version
    implementation_version = f"{version.major}.{version.minor}.{version.micro}"
    if version.releaselevel != "final":
        implementation_version += f"{version.releaselevel[0]}{version.serial}"
    return {
        "implementation_name": sys.implementation.name,
        "implementation_version": implementation_version,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "platform_python_implementation": platform.python_implementation(),
        "python_full_version": platform.python_version(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "sys_platform": sys.platform,
    }


# --- wheel が読めない共有ライブラリと、その提供元の Ubuntu パッケージを割り出す（README.md「Python」）---
def inspect_native_main(requested_file, output):
    (output / "python-environment.json").write_text(
        json.dumps(_marker_environment(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    requested = set()
    for raw in Path(requested_file).read_text(encoding="utf-8").splitlines():
        item = raw.partition("#")[0].strip()
        if item:
            requested.add(item)
    native = inspect_shared_libraries(list(metadata.distributions()))
    missing = {}
    used_by = {}
    for record in native["files"]:
        for library in record["libraries"]:
            if library["path"] == "not found":
                missing.setdefault(library["soname"], set()).add(record["distribution"])
            for package in owner_packages(library.get("owners", [])):
                used_by.setdefault(package, set()).add(record["distribution"])
    providers = contents_providers(sorted(missing), output) if missing else {}
    chosen, review = choose_providers(missing, providers, requested)
    report = {
        "missing": {soname: sorted(names) for soname, names in sorted(missing.items())},
        "providers": chosen,
        "used_by": {package: sorted(names) for package, names in sorted(used_by.items())},
        "review": review,
        "inspection_review": native["review"],
    }
    (output / "native-plan.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    )
    write_native_libraries(native, output / "native-libraries.tsv", owners=False, relative=True)
    (output / "native-providers.txt").write_text(
        "".join(package + "\n" for package in sorted(set(chosen.values())))
    )
    (output / "contents.conf").unlink(missing_ok=True)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


# --- コマンドライン（audit・native）を受けて、対応する処理を走らせる（README.md「Python」）---
def main():
    if len(sys.argv) == 4 and sys.argv[1] == "native":
        return inspect_native_main(Path(sys.argv[2]), Path(sys.argv[3]))
    if len(sys.argv) != 4 or sys.argv[1] != "audit":
        sys.exit(
            "使い方: dependencies_inspect.py audit INPUT.json OUTPUT_DIR "
            "| native UBUNTU_PACKAGES.txt OUTPUT_DIR"
        )
    config = json.loads(Path(sys.argv[2]).read_text())
    return inspect_audit_main(config, Path(sys.argv[3]))


if __name__ == "__main__":
    sys.exit(main())
