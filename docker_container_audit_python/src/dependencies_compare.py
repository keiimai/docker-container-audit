# --- 保存した監査の表を、Docker を使わずに比べる（R は R/dependencies-compare.R）（README.md「監査結果の比較」）---

import csv
import json
import re
import subprocess
import sys
from pathlib import Path

import contract

#: 実際のインストール一覧（installed-packages.csv）の列（contracts/tables.tsv）。
PACKAGE_COLUMNS = tuple(contract.table_columns("installed-packages", "python"))
#: installed-packages.csv のキーの列（PACKAGE_COLUMNS の最後の列 version を除く）。
PACKAGE_KEYS = PACKAGE_COLUMNS[:-1]
#: 監査結果（audit-checks.csv）の列（contracts/tables.tsv）。
CHECK_COLUMNS = tuple(contract.table_columns("audit-checks", "python"))
#: 比較処理が作る列。0行でも contracts/tables.tsv と照合する。
COMPARISON_COLUMNS = {
    "comparison-context": ("item", "baseline", "current"),
    "comparison-packages": (*PACKAGE_KEYS, "previous_version", "current_version", "change"),
    "comparison-checks": ("check", "previous_status", "current_status", "change"),
}
#: 監査コンテキストの中で比べる項目。
CONTEXT_ITEMS = ("language", "platform", "status", "image_id", "base_image", "os_id", "os_version")


# --- 値が 1 つの文字か null か（README.md「監査結果の比較」）---
def _text_or_null(value):
    return value is None or isinstance(value, str)


# --- 監査コンテキストが、名前の付いた項目の集まりで、比べる値がどれも文字か null か（README.md「監査結果の比較」）---
def _valid_context(context):
    if not isinstance(context, dict):
        return False
    hashes = context.get("input_hashes")
    return all(_text_or_null(context.get(item)) for item in CONTEXT_ITEMS) and (
        hashes is None
        or (isinstance(hashes, dict) and all(_text_or_null(value) for value in hashes.values()))
    )


# --- 保存した CSV の欄の数・形・重複を確かめ、比較に使うキーで行を引けるようにする（README.md「監査結果の比較」）---
def _read_csv_table(path, columns, keys):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != columns:
            raise ValueError(f"比較用の表の形式が不正です: {path}")
        rows = {}
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"比較用の表の形式が不正です: {path}")
            key = tuple(row[column].replace("\r\n", "\n") for column in keys)
            if columns == PACKAGE_COLUMNS and key[0] == "python":
                key = (key[0], re.sub(r"[-_.]+", "-", key[1]).lower(), key[2])
            if key in rows:
                raise ValueError(f"比較用の表に重複があります: {path}: {key}")
            rows[key] = row
        return rows


# --- 保存した表を読み、欄が長いときだけ別のプロセスでそのファイルの大きさまで読む（README.md「監査結果の比較」）---
def _read_table(path, columns, keys):
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError(f"比較用の入力が通常ファイルではありません: {path}")
    try:
        return _read_csv_table(path, columns, keys)
    except csv.Error as error:
        if not str(error).startswith("field larger than field limit"):
            raise ValueError(f"比較用の表の形式が不正です: {path}") from error
    result = subprocess.run(
        [sys.executable, __file__, str(path), json.dumps(columns), json.dumps(keys)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"比較用の表の形式が不正です: {path}")
    return {tuple(key): row for key, row in json.loads(result.stdout)}


# --- 2 つの表の 1 つの欄を比べ、追加・削除・変更の行を返す（README.md「監査結果の比較」）---
def _table_changes(before, after, field, key_columns):
    rows = []
    for key in sorted(before.keys() | after.keys()):
        old = before.get(key, {}).get(field, "")
        new = after.get(key, {}).get(field, "")
        if key in before and key in after and old == new:
            continue
        change = "added" if key not in before else "removed" if key not in after else "changed"
        rows.append(
            {
                **dict(zip(key_columns, key, strict=True)),
                f"previous_{field}": old,
                f"current_{field}": new,
                "change": change,
            }
        )
    return rows


# --- 2 つの Python の報告書の実行 ID を比べ、変化と取得の有無の表を返す（README.md「監査結果の比較」）---
def compare_dependencies(baseline, current, tables="outputs/tables"):
    if baseline == current:
        raise ValueError("異なる 2 つの報告書の実行 ID を指定してください。")
    for run_id in (baseline, current):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_id):
            raise ValueError("報告書の実行 ID の形式が不正です。")
    directories = []
    contexts = []
    for run_id in (baseline, current):
        directory = Path(tables) / run_id
        context_path = directory / "audit-context.json"
        if not context_path.exists():
            raise ValueError(f"監査コンテキストがありません。報告書を再生成してください: {run_id}")
        if not context_path.is_file():
            raise ValueError(f"比較用の入力が通常ファイルではありません: {context_path}")
        context = json.loads(context_path.read_text(encoding="utf-8-sig"))
        if (
            not _valid_context(context)
            or context.get("language") != "python"
            or context.get("platform") not in ("linux/amd64", "linux/arm64")
        ):
            raise ValueError(f"Python の監査コンテキストとして比較できません: {run_id}")
        contexts.append(context)
        directories.append(directory)
    before, after = contexts
    if before["platform"] != after["platform"]:
        raise ValueError("同じプラットフォームの監査を指定してください。")

    packages = [
        _read_table(d / "installed-packages.csv", PACKAGE_COLUMNS, PACKAGE_KEYS)
        for d in directories
    ]
    checks = [_read_table(d / "audit-checks.csv", CHECK_COLUMNS, ("check",)) for d in directories]
    context_rows = [{"item": "report_id", "baseline": baseline, "current": current}]
    for item in CONTEXT_ITEMS:
        context_rows.append(
            {
                "item": item,
                "baseline": "" if before.get(item) is None else before.get(item),
                "current": "" if after.get(item) is None else after.get(item),
            }
        )
    before_hashes, after_hashes = before.get("input_hashes") or {}, after.get("input_hashes") or {}
    for item in sorted(before_hashes.keys() | after_hashes.keys()):
        context_rows.append(
            {
                "item": item,
                "baseline": "" if before_hashes.get(item) is None else before_hashes[item],
                "current": "" if after_hashes.get(item) is None else after_hashes[item],
            }
        )
    package_changes = []
    for ecosystem in ("python", "ubuntu"):
        inventories = [
            {key: row for key, row in inventory.items() if key[0] == ecosystem}
            for inventory in packages
        ]
        context_rows.append(
            {
                "item": f"inventory:{ecosystem}",
                "baseline": "available" if inventories[0] else "unavailable",
                "current": "available" if inventories[1] else "unavailable",
            }
        )
        if all(inventories):
            package_changes.extend(_table_changes(*inventories, "version", PACKAGE_KEYS))
    context_rows.append(
        {
            "item": "checks",
            "baseline": "available" if checks[0] else "unavailable",
            "current": "available" if checks[1] else "unavailable",
        }
    )
    results = {
        "comparison-context": context_rows,
        "comparison-packages": package_changes,
        "comparison-checks": _table_changes(*checks, "status", ("check",)) if all(checks) else [],
    }
    for name, rows in results.items():
        expected = tuple(contract.table_columns(name, "python"))
        if COMPARISON_COLUMNS[name] != expected or any(tuple(row) != expected for row in rows):
            raise ValueError(f"比較の表の列が contracts/tables.tsv と一致しません: {name}")
    return results


# --- 長い欄がある CSV を読み直して行を呼び出し元へ返す（README.md「監査結果の比較」）---
def _read_long_fields(path, columns, keys):
    csv.field_size_limit(max(csv.field_size_limit(), path.stat().st_size))
    rows = _read_csv_table(path, columns, keys)
    json.dump(list(rows.items()), sys.stdout)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("比較する CSV と列名とキーを指定してください。")
    try:
        _read_long_fields(
            Path(sys.argv[1]), tuple(json.loads(sys.argv[2])), tuple(json.loads(sys.argv[3]))
        )
    except csv.Error:
        sys.exit(f"比較用の表の形式が不正です: {sys.argv[1]}")
    except (OSError, ValueError) as error:
        sys.exit(str(error))
