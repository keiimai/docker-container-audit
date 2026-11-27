# --- 保存した監査結果の比較のテスト（Docker なし。R は tests/testthat/test-dependencies-compare.R）（README.md「監査結果の比較」）---

import csv
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

#: 子の根（このファイルは <子の根>/tests/ にある）。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import dependencies_compare  # noqa: E402
from dependencies_compare import CHECK_COLUMNS, PACKAGE_COLUMNS, compare_dependencies  # noqa: E402


# --- 保存した表の比較を確かめる（README.md「監査結果の比較」）---
class ComparisonTests(unittest.TestCase):
    # --- 子の根の _tmp/ の中に、比べる表を置く一時フォルダを作る（CHECKS.md 1 節）---
    def setUp(self):
        scratch = ROOT / "_tmp"
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="python-comparison-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.tables = Path(temporary.name)

    # --- 1 つの報告書の、監査の文脈と表を書く（None の表は書かない）（README.md「監査結果の比較」）---
    def write_run(self, run_id, packages, checks, platform="linux/amd64"):
        directory = self.tables / run_id
        directory.mkdir(exist_ok=True)
        context = dict(
            language="python", platform=platform, input_hashes={"requirements.txt": run_id}
        )
        (directory / "audit-context.json").write_text(json.dumps(context), encoding="utf-8")
        for name, columns, rows in (
            ("installed-packages", PACKAGE_COLUMNS, packages),
            ("audit-checks", CHECK_COLUMNS, checks),
        ):
            if rows is not None:
                with (directory / f"{name}.csv").open(
                    "w", encoding="utf-8-sig", newline=""
                ) as stream:
                    writer = csv.writer(stream)
                    writer.writerow(columns)
                    writer.writerows(rows)

    # --- 版の変化・判定の変化・片方に無い一覧を比べる（Python のパッケージ名は書き方をそろえて比べる）（README.md「監査結果の比較」）---
    def test_versions_checks_and_missing_inventory(self):
        self.write_run(
            "baseline",
            [
                ("python", "My_Pkg", "", "1"),
                ("python", "old", "", "1"),
                ("ubuntu", "lib", "amd64", "1"),
            ],
            [("load", "PASS", "")],
        )
        self.write_run(
            "current",
            [("python", "my-pkg", "", "2"), ("python", "new", "", "1")],
            [("load", "FAIL", "broken"), ("apt", "PASS", "")],
        )
        result = compare_dependencies("baseline", "current", self.tables)
        self.assertEqual(
            [(r["package"], r["change"]) for r in result["comparison-packages"]],
            [("my-pkg", "changed"), ("new", "added"), ("old", "removed")],
        )
        self.assertEqual(
            [(r["check"], r["change"]) for r in result["comparison-checks"]],
            [("apt", "added"), ("load", "changed")],
        )
        self.assertIn(
            {"item": "inventory:ubuntu", "baseline": "available", "current": "unavailable"},
            result["comparison-context"],
        )
        self.assertIn(
            {"item": "requirements.txt", "baseline": "baseline", "current": "current"},
            result["comparison-context"],
        )

    # --- プラットフォームの違う報告書は比べない（README.md「監査結果の比較」）---
    def test_incompatible_platform_is_rejected(self):
        self.write_run("baseline", None, None)
        self.write_run("current", None, None, "linux/arm64")
        with self.assertRaisesRegex(ValueError, "同じプラットフォームの監査を指定してください"):
            compare_dependencies("baseline", "current", self.tables)

    # --- 同じ ID・形の不正な ID・audit-context.json の無い報告書は比べない（README.md「監査結果の比較」）---
    def test_invalid_run_ids_are_rejected(self):
        self.write_run("baseline", None, None)
        cases = (
            (("baseline", "baseline"), "異なる 2 つの報告書の実行 ID を指定してください。"),
            (("../baseline", "current"), "報告書の実行 ID の形式が不正です。"),
            (
                ("baseline", "absent"),
                "監査コンテキストがありません。報告書を再生成してください: absent",
            ),
        )
        for run_ids, message in cases:
            with (
                self.subTest(run_ids=run_ids),
                self.assertRaisesRegex(ValueError, re.escape(message)),
            ):
                compare_dependencies(*run_ids, self.tables)

    # --- 結果の表の列が contracts/tables.tsv と違えば止める（README.md「監査結果の比較」）---
    def test_comparison_columns_must_match_contract(self):
        self.write_run("baseline", None, None)
        self.write_run("current", None, None)
        for table in ("comparison-context", "comparison-packages", "comparison-checks"):
            with (
                self.subTest(table=table),
                mock.patch.dict(dependencies_compare.COMPARISON_COLUMNS, {table: ("bad",)}),
                self.assertRaisesRegex(ValueError, "contracts/tables.tsv と一致しません"),
            ):
                compare_dependencies("baseline", "current", self.tables)

    # --- 長い説明のある表を読み、呼び出し元の CSV の欄長設定を保つ（README.md「監査結果の比較」）---
    def test_long_fields_are_read_without_changing_the_callers_limit(self):
        limit = csv.field_size_limit()
        self.write_run("baseline", None, [("load", "PASS", "")])
        self.write_run("current", None, [("load", "FAIL", "x" * (limit + 1))])
        result = compare_dependencies("baseline", "current", self.tables)
        self.assertEqual(result["comparison-checks"][0]["change"], "changed")
        self.assertEqual(csv.field_size_limit(), limit)

    # --- 欄の数が見出しと違う行・中身の空の表は止め、引用の中の改行は通す（README.md「監査結果の比較」）---
    def test_rows_with_wrong_field_counts_are_rejected(self):
        self.write_run("baseline", None, None)
        self.write_run("current", None, None)
        path = self.tables / "current" / "audit-checks.csv"
        for text in (
            "check,status,detail\nload,PASS\n",
            "check,status,detail\nload,PASS,,extra\n",
            "",
        ):
            path.write_text(text, encoding="utf-8")
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(ValueError, "比較用の表の形式が不正です"),
            ):
                compare_dependencies("baseline", "current", self.tables)
        (self.tables / "baseline" / "audit-checks.csv").write_text(
            "check,status,detail\nload,FAIL,\n", encoding="utf-8"
        )
        path.write_text('check,status,detail\nload,PASS,"two\nlines"\n', encoding="utf-8")
        result = compare_dependencies("baseline", "current", self.tables)
        self.assertEqual([row["change"] for row in result["comparison-checks"]], ["changed"])

    # --- 監査コンテキストの形や値の型が違えば止める（名前の一部が合うだけの項目は使わない）（README.md「監査結果の比較」）---
    def test_malformed_context_is_rejected(self):
        self.write_run("baseline", None, None)
        self.write_run("current", None, None)
        path = self.tables / "current" / "audit-context.json"
        for text in (
            '"python"',
            '{"language": "python", "platform": "linux/amd64", "status": true}',
            '{"language": "python", "platform": "linux/amd64", "input_hashes": ["x"]}',
            '{"language": "python", "platform": "linux/amd64", "input_hashes": {"a": 1}}',
            '{"language_x": "python", "platform": "linux/amd64"}',
        ):
            path.write_text(text, encoding="utf-8")
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(
                    ValueError, re.escape("Python の監査コンテキストとして比較できません: current")
                ),
            ):
                compare_dependencies("baseline", "current", self.tables)

    # --- 2 つの実行 ID の形を、監査コンテキストより先に確かめる（文字でない ID も同じ文で止める）（README.md「監査結果の比較」）---
    def test_run_id_forms_are_checked_first(self):
        for run_ids in (("absent", "../current"), ("absent", 1)):
            with (
                self.subTest(run_ids=run_ids),
                self.assertRaisesRegex(ValueError, re.escape("報告書の実行 ID の形式が不正です。")),
            ):
                compare_dependencies(*run_ids, self.tables)

    # --- 入力ハッシュのない報告書と、名前が空の入力ハッシュを比べる（README.md「監査結果の比較」）---
    def test_missing_and_empty_named_input_hashes(self):
        context = {"language": "python", "platform": "linux/amd64"}
        for run_id in ("baseline", "current"):
            self.write_run(run_id, None, None)
            path = self.tables / run_id / "audit-context.json"
            path.write_text(json.dumps(context), encoding="utf-8")
        rows = compare_dependencies("baseline", "current", self.tables)["comparison-context"]
        self.assertNotIn("", [row["item"] for row in rows])
        for run_id in ("baseline", "current"):
            path = self.tables / run_id / "audit-context.json"
            path.write_text(json.dumps({**context, "input_hashes": {"": run_id}}), encoding="utf-8")
        rows = compare_dependencies("baseline", "current", self.tables)["comparison-context"]
        self.assertIn({"item": "", "baseline": "baseline", "current": "current"}, rows)

    # --- タブを含む別々のパッケージ・CPU の組を順序どおり比較する（README.md「監査結果の比較」）---
    def test_tabs_in_separate_key_columns(self):
        for run_id in ("baseline", "current"):
            packages = [("ubuntu", "a\tb", "c", run_id), ("ubuntu", "a", "b\tc", run_id)]
            self.write_run(run_id, packages, None)
        rows = compare_dependencies("baseline", "current", self.tables)["comparison-packages"]
        self.assertEqual(
            [(row["package"], row["architecture"], row["change"]) for row in rows],
            [("a", "b\tc", "changed"), ("a\tb", "c", "changed")],
        )

    # --- 引用していない #、引用内の改行、先頭に BOM のある監査コンテキストを比較する（README.md「監査結果の比較」）---
    def test_hash_and_multiline_check_names_with_bom_context(self):
        for run_id, status, name in (
            ("baseline", "PASS", "Os\r\nload"),
            ("current", "FAIL", "Os\nload"),
        ):
            self.write_run(run_id, None, None)
            directory = self.tables / run_id
            path = directory / "audit-checks.csv"
            path.write_bytes(f'check,status,detail\na#b,{status},\n"{name}",{status},\n'.encode())
        context = self.tables / "current/audit-context.json"
        context.write_text(context.read_text(encoding="utf-8"), encoding="utf-8-sig")
        changes = compare_dependencies("baseline", "current", self.tables)["comparison-checks"]
        self.assertEqual(
            [(row["check"], row["change"]) for row in changes],
            [("Os\nload", "changed"), ("a#b", "changed")],
        )

    # --- 取得していない表と、入力先がディレクトリの場合を区別する（README.md「監査結果の比較」）---
    def test_directory_at_input_path_is_rejected(self):
        self.write_run("baseline", None, None)
        self.write_run("current", None, [])
        for name in ("audit-checks.csv", "audit-context.json"):
            path = self.tables / "current" / name
            path.unlink()
            path.mkdir()
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                compare_dependencies("baseline", "current", self.tables)
            self.assertEqual(
                str(caught.exception), f"比較用の入力が通常ファイルではありません: {path}"
            )
            path.rmdir()


if __name__ == "__main__":
    unittest.main()
