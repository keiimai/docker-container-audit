# --- contracts/ の共通の決まりを読み、Python の出力を照合する（R は R/contract.R）（CHECKS.md 3 節）---

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

#: 共通の決まりの置き場所（親の contracts/。子の根の 1 つ上）。
CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts"
#: 照合で扱う JSON Schema の語（contracts/ で使うものだけ。ほかの語があれば止める）。
_KNOWN_KEYWORDS = {
    "$schema",
    "$id",
    "title",
    "description",
    "type",
    "enum",
    "const",
    "pattern",
    "required",
    "properties",
    "additionalProperties",
    "items",
}


# --- contracts/ の中のファイルのパスを返す（contracts/ が無ければ止める。R の contract_dir と同じ確かめ）（CHECKS.md 3 節）---
def _contract_file(name):
    if not (CONTRACT_DIR / "tables.tsv").is_file():
        raise ValueError("contracts/ が見つかりません。")
    return CONTRACT_DIR / name


# --- contracts/ の TSV を行の辞書のリストで読む（CHECKS.md 3 節）---
def read_tsv(name):
    with _contract_file(name).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE))


# --- その言語の操作名と工程名の行を返す（contracts/operations.tsv）（CHECKS.md 3 節）---
def operations(language):
    return [row for row in read_tsv("operations.tsv") if row["language"] == language]


# --- その言語の報告書の工程名を返す（contracts/operations.tsv）（CHECKS.md 3 節）---
def step_names(language):
    return list(dict.fromkeys(row["step"] for row in operations(language)))


# --- 報告書の表の列の順を返す（contracts/tables.tsv の common か、その言語の表。無ければ止める）（CHECKS.md 3 節）---
def table_columns(table, language):
    rows = [
        row
        for row in read_tsv("tables.tsv")
        if row["table"] == table and row["language"] in ("common", language)
    ]
    if len(rows) != 1:
        raise ValueError(f"contracts/tables.tsv に表がありません: {table}")
    return rows[0]["columns"].split(",")


# --- 行の列を tables.tsv の順にそろえる（余分な列は除き、足りない列は空欄にする）（R の contract_select_table と同じ）（CHECKS.md 3 節）---
def select_table(table, language, rows):
    columns = table_columns(table, language)
    return [{column: row.get(column, "") for column in columns} for row in rows]


# --- その言語の監査の検査名を返す（contracts/audit-checks.tsv の common とその言語）（CHECKS.md 3 節）---
def audit_checks(language):
    return [
        row["check"]
        for row in read_tsv("audit-checks.tsv")
        if row["language"] in ("common", language)
    ]


# --- その言語のその工程の照合の検査名を返す（contracts/operation-checks.tsv の common とその言語）（CHECKS.md 3 節）---
def operation_checks(language, operation):
    return [
        row["check"]
        for row in read_tsv("operation-checks.tsv")
        if row["operation"] == operation and row["language"] in ("common", language)
    ]


# --- 値が当てはまる JSON Schema の型を返す（整数の値は integer と number の両方）（CHECKS.md 3 節）---
def _json_types(value):
    if value is None:
        return {"null"}
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, int):
        return {"integer", "number"}
    if isinstance(value, float):
        return {"integer", "number"} if value.is_integer() else {"number"}
    if isinstance(value, str):
        return {"string"}
    if isinstance(value, list):
        return {"array"}
    if isinstance(value, dict):
        return {"object"}
    return {"unknown"}


# --- 値を JSON Schema と照合し、合わない点の一覧を返す（知らない語があれば止める）（CHECKS.md 3 節）---
def _check(value, schema, path):
    unknown = set(schema) - _KNOWN_KEYWORDS
    if unknown:
        raise ValueError(f"未対応のスキーマの語です: {', '.join(sorted(unknown))}")
    types = _json_types(value)
    expected = schema.get("type")
    if expected is not None:
        expected = [expected] if isinstance(expected, str) else list(expected)
        if not types & set(expected):
            return [f"{path}: {'/'.join(expected)} ではありません"]
    errors = []
    if "const" in schema and not (
        value == schema["const"] and type(value) is type(schema["const"])
    ):
        errors.append(f"{path}: 値が決まりと異なります")
    if "enum" in schema and not any(
        value == item and type(value) is type(item) for item in schema["enum"]
    ):
        errors.append(f"{path}: 許されない値です")
    if "pattern" in schema and types == {"string"} and not re.search(schema["pattern"], value):
        errors.append(f"{path}: 形式が決まりと異なります")
    if "object" in types:
        missing = [name for name in schema.get("required", []) if name not in value]
        if missing:
            errors.append(f"{path}: 必要な項目がありません: {', '.join(missing)}")
        for name, item in value.items():
            child = schema.get("properties", {}).get(name, schema.get("additionalProperties"))
            if child is False:
                errors.append(f"{path}: 決まりにない項目です: {name}")
            elif isinstance(child, dict):
                errors.extend(_check(item, child, f"{path}.{name}"))
    if "array" in types and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value, start=1):
            errors.extend(_check(item, schema["items"], f"{path}[{index}]"))
    return errors


# --- データが contracts/<schema_file> に合わなければ止める（R は JSON の文字列を受け取る contract_validate_json）（CHECKS.md 3 節）---
def validate(data, schema_file, label):
    schema = json.loads(_contract_file(schema_file).read_text(encoding="utf-8"))
    errors = _check(data, schema, label)
    if errors:
        raise ValueError(f"contracts/{schema_file} に合いません。\n" + "\n".join(errors))
    return data


# --- result-summary.json の形と、操作名が contracts/operations.tsv にあるかを確かめる（README.md「結果の共通形式」）---
def validate_summary(summary):
    try:
        validate(summary, "result-summary.schema.json", "result-summary")
    except (ValueError, OSError) as error:
        raise ValueError("結果の共通形式に合いません。\n" + str(error)) from error
    if not any(row["operation"] == summary["operation"] for row in operations(summary["language"])):
        raise ValueError(
            "結果の共通形式に合いません。\n"
            "result-summary.operation: contracts/operations.tsv にない操作です: "
            f"{summary['language']} {summary['operation']}"
        )
    return summary


# --- その言語の確定記録（dependency-lock.tsv）に載せるファイルを返す（contracts/lock-files.tsv）（CHECKS.md 3 節）---
def lock_files(language):
    return [
        row["file"] for row in read_tsv("lock-files.tsv") if row["language"] in ("common", language)
    ]


# --- build-environment.json を照合してから書き出す（R も同じ名前）（CHECKS.md 3 節）---
def write_build_environment(environment, path):
    validate(environment, "build-environment.schema.json", "build-environment")
    path.write_text(json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# --- build-environment.json を読み、照合して返す（R も同じ名前）（CHECKS.md 3 節）---
def read_build_environment(path):
    return validate(
        json.loads(path.read_text(encoding="utf-8-sig")),
        "build-environment.schema.json",
        "build-environment",
    )


# --- そのプラットフォームの SIF を作るイメージを返す（contracts/apptainer-images.tsv）（README.md「Apptainer の SIF」）---
def apptainer_image(platform):
    rows = [row for row in read_tsv("apptainer-images.tsv") if row["platform"] == platform]
    if len(rows) != 1:
        raise ValueError(f"contracts/apptainer-images.tsv に {platform} がありません。")
    return rows[0]["image"]


# --- SIF の中身を取り出すスクリプトを返す（contracts/sif-extract.sh）（README.md「Apptainer の SIF」）---
def sif_extract_script():
    return _contract_file("sif-extract.sh").read_text(encoding="utf-8").removesuffix("\n")
