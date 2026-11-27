# --- 工程の証跡、照合の行、道具の版と報告書の表をまとめる（R は R/evidence.R）（README.md「構成」）---

from __future__ import annotations

import contextlib
import csv
import json
import os
import platform as platform_module
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import container
import contract
from container import file_sha256, shown_path

#: 入口が読み込む .env（プロジェクトの根）。
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


# --- 根の .env の値を環境変数に読み込む（既にある変数は上書きしない）（REPRODUCIBILITY.md 4 節）---
def load_env_file(path=ENV_FILE):
    if not path.is_file():
        return []
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
        if match:
            value = re.sub(r"\s+#.*$", "", match.group(2)).strip()
            values[match.group(1)] = re.sub(r"^[\"']|[\"']$", "", value)
    loaded = [name for name in values if not os.environ.get(name)]
    for name in loaded:
        os.environ[name] = values[name]
    return loaded


# --- .env を、数値計算のライブラリより先に環境変数にする（R は audit_setup で読む）（REPRODUCIBILITY.md 4 節）---
load_env_file()

#: 工程の証跡を置くフォルダ（根から。その下に <実行 ID>/<工程>/）。
EVIDENCE_DIR = "outputs/evidence"


# --- 操作名に対応する、報告書の工程の名前を返す（contracts/operations.tsv の step）（README.md「結果の共通形式」）---
def operation_step(operation):
    rows = [row for row in contract.operations("python") if row["operation"] == operation]
    if len(rows) != 1:
        raise ValueError(f"contracts/operations.tsv にない操作です: python {operation}")
    return rows[0]["step"]


# --- その実行 ID とその工程の、証跡のフォルダを返す（outputs/evidence/<実行 ID>/<工程>）（README.md「結果の共通形式」）---
def evidence_directory(run_id, step):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
        raise ValueError(
            "報告書の実行 ID（run_id）を渡してください（報告書の記録の state.run_id）。"
        )
    return container.ROOT / EVIDENCE_DIR / run_id / step


# --- 証跡のフォルダの中のファイルを並べて返す（README.md「結果の共通形式」）---
def _operation_files(directory):
    return [path for path in sorted(directory.rglob("*")) if path.is_file()]


# --- 工程が読んだファイルのハッシュを inputs.tsv に書く（無いファイルは空）（README.md「結果の共通形式」）---
def _write_inputs(directory, inputs):
    with (directory / "inputs.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(("path", "sha256"))
        for path in dict.fromkeys(Path(item) for item in inputs):
            writer.writerow(
                (
                    shown_path(path),
                    file_sha256(path) if path.is_file() else "",
                )
            )


# --- 共通形式の result-summary.json を、照合してから書く（README.md「結果の共通形式」）---
def _write_result_summary(directory, operation, parameters, details, status, message=None):
    summary_path = directory / "result-summary.json"
    report_path = directory / "python-audit.json"
    os_info = {}
    if report_path.is_file():
        with contextlib.suppress(OSError, ValueError, AttributeError):
            os_info = json.loads(report_path.read_text(encoding="utf-8")).get("os_release") or {}
    image_id = details.get("image_id")
    log = directory / "python-audit.log"
    if image_id is None and log.is_file():
        with contextlib.suppress(OSError), log.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                match = re.fullmatch(r"Docker image ID: (sha256:[a-f0-9]{64})", line.rstrip())
                if match:
                    image_id = match[1]
                    break
    summary = {
        "schema_version": "1.0",
        "language": "python",
        "operation": operation,
        "status": status,
        "image": parameters.get("image"),
        "platform": parameters.get("platform"),
        "image_id": image_id,
        "os_id": os_info.get("ID"),
        "os_version": os_info.get("VERSION_ID"),
        "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message": message,
        "artifacts": [
            p.relative_to(directory).as_posix()
            for p in _operation_files(directory)
            if p != summary_path
        ],
    }
    if operation in ("audit-python", "audit-python-archive", "audit-python-sif"):
        for key in ("base_image", "input_hashes"):
            if parameters.get(key) is not None:
                summary[key] = parameters[key]
    contract.validate_summary(summary)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# --- 証跡の checks.tsv の判定を並べて返す（無ければ空）（README.md「結果の共通形式」）---
def _check_statuses(directory):
    path = Path(directory) / "checks.tsv"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        return [
            row["status"] for row in csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
        ]


# --- 工程を証跡の枠で包み、証跡と result-summary.json を outputs/evidence/<実行 ID>/<工程>/ に書く（README.md「結果の共通形式」）---
def run_operation(operation, parameters, inputs, action, *, run_id=None):
    directory = evidence_directory(run_id, operation_step(operation))
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.exists():
        raise FileExistsError(f"この実行 ID の工程の証跡が既にあります: {directory}")
    directory.mkdir(mode=0o700)
    details = {}
    try:
        write_tool_versions(directory)
        (directory / "parameters.json").write_text(
            json.dumps(parameters, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        _write_inputs(directory, inputs)
        value = action(directory, details)
        status = worst_status([value["status"], *_check_statuses(directory)])
        message = value.get("review") or None if status == "REVIEW" else None
        _write_result_summary(directory, operation, parameters, details, status, message)
        value = {**value, "status": status}
    except (Exception, KeyboardInterrupt) as error:
        error.evidence = directory
        with contextlib.suppress(Exception):
            _write_result_summary(directory, operation, parameters, details, "FAIL", str(error))
        raise
    return {**value, "run_id": run_id, "evidence": str(directory)}


# --- 工程の照合を 1 つずつ走らせ、checks.tsv に足す関数を返す（止まれば FAIL を書いて止める）（README.md「結果の共通形式」）---
def operation_checks(directory, operation):
    known = contract.operation_checks("python", operation)
    path = Path(directory) / "checks.tsv"

    def add(check, status, detail):
        if check not in known:
            raise ValueError(f"contracts/operation-checks.tsv にない検査です: {operation} {check}")
        detail = re.sub(r"[\t\r\n]+", " ", str(detail))
        with path.open("a", encoding="utf-8", newline="") as stream:
            if stream.tell() == 0:
                stream.write("check\tstatus\tdetail\n")
            stream.write(f"{check}\t{status}\t{detail}\n")

    def run_check(check, action=None, detail="", status="PASS"):
        value = None
        if action is not None:
            try:
                value = action()
            except (Exception, KeyboardInterrupt) as error:
                add(check, "FAIL", error)
                raise
        add(
            check,
            status(value) if callable(status) else status,
            detail(value) if callable(detail) else detail,
        )
        return value

    return run_check


# --- 照合した項目を 1 つずつの行にする（status を省けば expected と found の一致で PASS・FAIL）（README.md「結果の共通形式」）---
def _check_item_rows(check, rows):
    result = []
    for item, expected, found, *given in rows:
        if given:
            status = given[0]
        else:
            status = "PASS" if found is not None and str(found) == str(expected or "") else "FAIL"
        values = [item, expected, found]
        result.append(
            [
                check,
                *(re.sub(r"[\t\r\n]+", " ", "" if v is None else str(v)) for v in values),
                status,
            ]
        )
    return result


# --- 1 つずつ確かめた項目を check-items.tsv に足す関数を返す（契約に無い照合名は止める）（README.md「結果の共通形式」）---
def operation_check_items(directory, operation):
    known = contract.operation_checks("python", operation)
    path = Path(directory) / "check-items.tsv"

    def items(check, rows):
        if check not in known:
            raise ValueError(f"contracts/operation-checks.tsv にない検査です: {operation} {check}")
        rows = _check_item_rows(check, rows)
        with path.open("a", encoding="utf-8", newline="") as stream:
            if stream.tell() == 0:
                stream.write("check\titem\texpected\tfound\tstatus\n")
            stream.writelines("\t".join(row) + "\n" for row in rows)
        return rows

    return items


# --- 記録したハッシュと今のハッシュを名前ごとに照合した行にする（片方だけの名前も行にする。名前に prefix を付けられる）（README.md「結果の共通形式」）---
def hash_item_rows(expected, current, prefix=""):
    return [
        (prefix + name, expected.get(name), current.get(name))
        for name in sorted(expected.keys() | current.keys())
    ]


# --- 確定記録の見出しと2列のデータを読む（README.md「結果の共通形式」）---
def read_lock_entries(lock):
    with Path(lock).open(encoding="utf-8-sig", newline="") as stream:
        rows = [row for row in csv.reader(stream, delimiter="\t") if row]
    if not rows or rows[0] != ["path", "sha256"] or any(len(row) != 2 for row in rows[1:]):
        raise ValueError("確定記録の形式が不正です。")
    return rows[1:]


# --- 確定記録（dependency-lock.tsv）のファイルを 1 つずつ照合した行を返す（読めなければ空）（REPRODUCIBILITY.md 3 節）---
def lock_item_rows(lock):
    try:
        rows = read_lock_entries(lock)
    except (OSError, ValueError, csv.Error):
        return []
    folder = Path(lock).parent
    recorded = dict(rows)
    current = {name: file_sha256(folder / name) for name in recorded if (folder / name).is_file()}
    return hash_item_rows(recorded, current)


# --- 判定のうち最も悪いものを返す（FAIL・REVIEW・PASS の順）（README.md「結果の共通形式」）---
def worst_status(statuses):
    return next((status for status in ("FAIL", "REVIEW") if status in statuses), "PASS")


# --- 判定ごとの項目の数の説明（README.md「結果の共通形式」）---
def status_detail(statuses):
    return "、".join(
        f"{status} {list(statuses).count(status)}" for status in ("PASS", "REVIEW", "FAIL")
    )


# --- 検査用のイメージ（tools/runner.sh が渡す名前・ID・定義のハッシュ）の行（検査用の外では空）（README.md「本番の手順」）---
def _runner_rows():
    if not os.getenv("AUDIT_RUNNER"):
        return []
    return [
        (tool, os.getenv(name) or "unavailable")
        for tool, name in (
            ("runner image", "AUDIT_RUNNER_IMAGE"),
            ("runner image id", "AUDIT_RUNNER_IMAGE_ID"),
            ("runner definition", "AUDIT_RUNNER_DEFINITION"),
        )
    ]


# --- 道具の版を tool-versions.tsv に足す（言語・Docker・buildx。extra は足す行）（README.md「結果の共通形式」）---
def write_tool_versions(directory, extra=()):
    path = Path(directory) / "tool-versions.tsv"
    rows = []
    if not path.is_file():

        def version(args):
            try:
                found = subprocess.run(
                    ["docker", *args], capture_output=True, text=True, check=False
                )
            except OSError:
                return "unavailable"
            line = (found.stdout.strip().splitlines() or [""])[0].strip()
            return line if found.returncode == 0 and line else "unavailable"

        rows = [
            ("Python", platform_module.python_version()),
            ("docker", version(["version", "--format", "{{.Client.Version}}"])),
            ("docker engine", version(["version", "--format", "{{.Server.Version}}"])),
            ("docker buildx", version(["buildx", "version"])),
            *_runner_rows(),
        ]
    with path.open("a", encoding="utf-8", newline="") as stream:
        if stream.tell() == 0:
            stream.write("tool\tversion\n")
        stream.writelines(f"{tool}\t{value}\n" for tool, value in [*rows, *extra])


#: 証跡の枠を持つ関数の差し替え（テストだけが使う。None なら run_operation）。
_RECORDER = None


# --- 1 つの操作を証跡の枠（run_operation。テストでは _RECORDER）の中で実行する（R は run_operation を直に差し替える）（REPRODUCIBILITY.md 2 節）---
def record(operation, parameters, inputs, action, *, run_id=None):
    recorder = _RECORDER
    if recorder is None:
        recorder = run_operation
    return recorder(operation, parameters, inputs, action, run_id=run_id)


# --- 第 1・第 3 段階の報告書の表・工程・証跡をまとめる（README.md「記録」）---
def report_session(root, display, markdown):
    steps = []

    def _quarto_version():
        quarto = Path(os.getenv("QUARTO_BIN_PATH", "")) / "quarto"
        if quarto.is_file() and os.access(quarto, os.X_OK):
            with contextlib.suppress(OSError):
                found = subprocess.run(
                    [str(quarto.resolve()), "--version"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if found.returncode == 0 and found.stdout.strip():
                    return found.stdout.strip().splitlines()[0]
        return "unavailable"

    quarto_version = _quarto_version()
    operation_check_rows = []
    operation_check_item_rows = []
    tool_version_rows = [{"step": "render", "tool": "quarto", "version": quarto_version}]

    def begin(report_id):
        session.run_id = report_id
        session.table_dir = root / "outputs" / "tables" / report_id
        session.table_dir.mkdir(parents=True, exist_ok=True)

    def show_table(name, rows):
        columns = contract.table_columns(name, "python")
        rows = contract.select_table(name, "python", rows)
        path = session.table_dir / f"{name}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=columns,
                quoting=csv.QUOTE_STRINGS,
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(
                [
                    {
                        key: ("TRUE" if value else "FALSE")
                        if isinstance(value, bool)
                        else ""
                        if value is None
                        else value
                        for key, value in row.items()
                    }
                    for row in rows
                ]
            )

        def cell(value):
            return str("" if value is None else value).replace("|", "&#124;").replace("\n", " ")

        if rows:
            header = "| " + " | ".join(columns) + " |"
            separator = "| " + " | ".join("---" for _ in columns) + " |"
            lines = [
                "| " + " | ".join(cell(row.get(column)) for column in columns) + " |"
                for row in rows
            ]
            display(markdown("\n".join([header, separator, *lines])))
        display(markdown(f"保存先 `{path.relative_to(root)}`"))

    def run_step(name, action=None, detail=""):
        if name not in contract.step_names("python"):
            raise ValueError(f"contracts/operations.tsv にない工程です: {name}")
        result = None
        status, evidence = "NOT_RUN", None
        if action is not None:
            try:
                result = action(session.run_id)
                status, evidence = result["status"], Path(result["evidence"])
                detail = result.get("review", "")
            except Exception as error:
                status, detail = "FAIL", str(error)
                evidence = getattr(error, "evidence", None)
                if evidence and (Path(evidence) / "python-audit.json").is_file():
                    result = {"report": Path(evidence) / "python-audit.json"}
        summary = Path(evidence) / "result-summary.json" if evidence else None
        if summary and summary.is_file():
            with contextlib.suppress(OSError, ValueError, KeyError):
                status = json.loads(summary.read_text(encoding="utf-8"))["status"]
        steps.append(
            dict(step=name, status=status, run_id=session.run_id if evidence else "", detail=detail)
        )
        for table, collected in (
            ("tool-versions.tsv", tool_version_rows),
            ("checks.tsv", operation_check_rows),
            ("check-items.tsv", operation_check_item_rows),
        ):
            checked = Path(evidence) / table if evidence else None
            if checked and checked.is_file():
                with checked.open(encoding="utf-8", newline="") as file:
                    collected.extend(
                        {"step": name, **row}
                        for row in csv.DictReader(file, delimiter="\t", quoting=csv.QUOTE_NONE)
                    )
        display(
            markdown(
                f"**判定** {status}"
                + (f"  \n証跡 `{Path(evidence).relative_to(root)}`" if evidence else "")
            )
        )
        if detail:
            print(detail)
        return result

    def read_tsv(path):
        with path.open(encoding="utf-8", newline="") as file:
            return list(csv.DictReader(file, dialect="excel-tab"))

    def status(name):
        found = [step["status"] for step in steps if step["step"] == name]
        return found[-1] if found else "NOT_RUN"

    def artifacts():
        folders = [
            root / "outputs" / kind / session.run_id for kind in ("tables", "evidence", "images")
        ]
        return [
            path
            for folder in folders
            if folder.is_dir()
            for path in sorted(folder.rglob("*"))
            if path.is_file()
        ]

    def ledger(**fields):
        statuses = [step["status"] for step in steps if step["status"] != "NOT_RUN"]
        overall = next((s for s in ("FAIL", "REVIEW", "PASS") if s in statuses), "NOT_RUN")
        return {
            **fields,
            **{f"step_{step['step'].replace('-', '_')}": step["status"] for step in steps},
            "result": overall,
        }

    def show_operation_checks():
        show_table("operation-checks", operation_check_rows)
        meanings = {}
        for row in contract.read_tsv("operation-checks.tsv"):
            if row["language"] in ("common", "python"):
                meanings.setdefault(row["check"], row["meaning"])
        seen = list(dict.fromkeys(row["check"] for row in operation_check_rows))
        if seen:
            display(
                markdown(
                    "各照合の意味\n\n"
                    + "\n".join(f"- `{check}`：{meanings.get(check, '')}" for check in seen)
                )
            )
        show_table("operation-check-items", operation_check_item_rows)

    session = SimpleNamespace(
        steps=steps,
        begin=begin,
        tool_versions=tool_version_rows,
        show_table=show_table,
        run_step=run_step,
        read_tsv=read_tsv,
        status=status,
        artifacts=artifacts,
        ledger=ledger,
        show_operation_checks=show_operation_checks,
    )
    return session
