# --- contracts/ の共通の決まりと Python の実装が一致するかのテスト（R は tests/testthat/test-contract.R）（CHECKS.md 3 節）---

import csv
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

#: 子の根（このファイルは <子の根>/tests/ にある）。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import container  # noqa: E402
import contract  # noqa: E402
import evidence  # noqa: E402
import image  # noqa: E402

#: 両言語のテストが同じ判定を確かめる結果の見本（親の contracts/result-summary-examples.tsv）。
EXAMPLES = contract.read_tsv("result-summary-examples.tsv")


# --- 操作側の全ファイルを読み、契約と照らすためのソースを返す（CHECKS.md 3 節）---
def controller_source():
    modules = ("container", "evidence", "project", "discovery", "image", "archive")
    return "\n".join((ROOT / "src" / f"{name}.py").read_text(encoding="utf-8") for name in modules)


# --- 共通の決まりと、それを読む実装・ノートブックを照合する（CHECKS.md 3 節）---
class ContractTests(unittest.TestCase):
    # --- 一時フォルダを根にし、docker を呼ばずに証跡の枠を走らせる用意をする（README.md「結果の共通形式」）---
    def scratch_root(self):
        scratch = ROOT / "_tmp"
        scratch.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix="python-contract-test-", dir=scratch)
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        for patch in (
            mock.patch.object(container, "ROOT", root),
            mock.patch.object(evidence.subprocess, "run", side_effect=OSError),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        return root

    # --- BOM の付いた工程表と確定記録を、元のバイトを保って読む（CHECKS.md 3 節）---
    def test_tables_and_build_environment_accept_utf8_bom(self):
        root = self.scratch_root()
        (root / "tables.tsv").write_text("見出し\n", encoding="utf-8")
        path = root / "operations.tsv"
        path.write_text(
            "language\toperation\tstep\npython\tbuild-python\tbuild\n", encoding="utf-8-sig"
        )
        with mock.patch.object(contract, "CONTRACT_DIR", root):
            self.assertEqual(contract.operations("python")[0]["step"], "build")
        environment = {
            "schema_version": "1.0",
            "language": "python",
            "platform": "linux/arm64",
            "base_image": "x",
            "apt_snapshot": None,
            "inputs": {},
        }
        path = root / "build-environment.json"
        path.write_text(json.dumps(environment), encoding="utf-8-sig")
        self.assertEqual(contract.read_build_environment(path), environment)
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        environment["inputs"] = {"": "a" * 64}
        path.write_text(json.dumps(environment), encoding="utf-8")
        self.assertEqual(contract.read_build_environment(path)["inputs"], environment["inputs"])

    # --- 監査ログだけにあるイメージ ID と、取得できた空の入力一覧を要約に残す（README.md「結果の共通形式」）---
    def test_summary_recovers_image_id_and_keeps_known_empty_inputs(self):
        root = self.scratch_root()
        image_id = "sha256:" + "a" * 64

        def action(directory, details):
            (directory / "python-audit.log").write_text(f"Docker image ID: {image_id}\n")
            return {"status": "PASS"}

        result = evidence.run_operation(
            "audit-python", {"input_hashes": {}}, [], action, run_id="log-id"
        )
        directory = Path(result["evidence"])
        self.assertTrue(directory.is_relative_to(root))
        summary = json.loads((directory / "result-summary.json").read_text())
        self.assertEqual(summary["image_id"], image_id)
        self.assertEqual(summary["input_hashes"], {})
        self.assertNotIn("base_image", summary)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    # --- 後から照合を追記しても引用符を保ち、照合の REVIEW を工程要約に残す ---
    def test_review_in_checks_is_recorded_in_summary(self):
        self.scratch_root()

        def action(directory, details):
            check = evidence.operation_checks(directory, "build")
            check("Image build", detail='値は "例" です', status="REVIEW")
            check("Image platform", detail="linux/arm64")
            return {"status": "PASS", "review": "要確認: Image build"}

        result = evidence.run_operation("build-python", {}, [], action, run_id="contract-checks")
        directory = Path(result["evidence"])
        summary = json.loads((directory / "result-summary.json").read_text(encoding="utf-8"))
        self.assertEqual((summary["status"], summary["message"]), ("REVIEW", "要確認: Image build"))
        with (directory / "checks.tsv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE))
        self.assertEqual([row["detail"] for row in rows], ['値は "例" です', "linux/arm64"])

    # --- 利用者による中断を呼び出し元に返し、当該照合と工程の両方に FAIL を残す ---
    def test_interrupted_check_writes_two_failure_records(self):
        root = self.scratch_root()

        def interrupt():
            raise KeyboardInterrupt("中断")

        def action(directory, details):
            evidence.operation_checks(directory, "build")("Image build", interrupt)

        with self.assertRaisesRegex(KeyboardInterrupt, "中断"):
            evidence.run_operation("build-python", {}, [], action, run_id="contract-interrupt")
        directory = root / "outputs/evidence/contract-interrupt/build"
        summary = json.loads((directory / "result-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "FAIL")
        with (directory / "checks.tsv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE))
        self.assertEqual([(row["check"], row["status"]) for row in rows], [("Image build", "FAIL")])

    # --- 共通の見本を、R と同じく verdict が valid なら受け入れ、invalid なら拒否する（CHECKS.md 3 節）---
    def test_shared_examples_have_the_same_verdict_as_r(self):
        self.assertEqual(contract.CONTRACT_DIR, ROOT.parent / "contracts")
        self.assertTrue(EXAMPLES)
        self.assertEqual(list(EXAMPLES[0]), ["example", "verdict", "schema_verdict", "json"])
        self.assertEqual(len({row["example"] for row in EXAMPLES}), len(EXAMPLES))
        for row in EXAMPLES:
            summary = json.loads(row["json"])
            with self.subTest(row["example"]):
                self.assertIn(row["verdict"], ("valid", "invalid"))
                self.assertIn(row["schema_verdict"], ("valid", "invalid"))
                self.assertFalse(row["verdict"] == "valid" and row["schema_verdict"] == "invalid")
                if row["verdict"] == "valid":
                    contract.validate_summary(summary)
                else:
                    with self.assertRaises(ValueError):
                        contract.validate_summary(summary)

    # --- スキーマは標準の JSON Schema で、jsonschema の判定と一致する（CHECKS.md 3 節）---
    def test_schema_is_standard_json_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema が入っていないため飛ばします")
        schema = json.loads(
            (contract.CONTRACT_DIR / "result-summary.schema.json").read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(schema)
        for row in EXAMPLES:
            summary = json.loads(row["json"])
            with self.subTest(row["example"]):
                self.assertEqual(validator.is_valid(summary), row["schema_verdict"] == "valid")

    # --- 操作側が使う操作名が contracts/operations.tsv のその言語の分と一致する（CHECKS.md 3 節）---
    def test_operations_match_the_controller(self):
        code = controller_source()
        recorded = re.findall(r'record\(\s*"([a-z-]+)"', code)
        self.assertEqual(len(recorded), len(set(recorded)))
        used = set(recorded)
        used |= set(re.findall(r'"(audit-python(?:-archive|-sif)?)"', code))
        self.assertEqual(used, {row["operation"] for row in contract.operations("python")})

    # --- 第 1・第 3 段階の報告書の工程名と表が contracts/ に定義され、段階ごとの工程がそろう（CHECKS.md 3 節）---
    def test_notebook_steps_and_tables_are_defined(self):
        steps = {}
        shared = (ROOT / "src/evidence.py").read_text(encoding="utf-8")
        for name in ("python-dependency-discovery", "python-image-build"):
            qmd = (ROOT / f"notebooks/{name}.qmd").read_text(encoding="utf-8")
            steps[name] = set(re.findall(r'run_step\("([a-z-]+)"', qmd))
            for table in set(re.findall(r'show_table\("([a-z-]+)"', qmd + shared)):
                contract.table_columns(table, "python")
        self.assertEqual(steps["python-dependency-discovery"], {"discover", "confirm"})
        self.assertEqual(set.union(*steps.values()), set(contract.step_names("python")))
        self.assertEqual(
            steps["python-image-build"],
            set(contract.step_names("python")) - {"discover", "confirm"},
        )
        self.assertFalse((ROOT / "notebooks/python-container-audit.qmd").exists())
        for name in ("python-dependency-discovery", "python-image-build"):
            qmd = (ROOT / f"notebooks/{name}.qmd").read_text(encoding="utf-8")
            with self.subTest(report=name):
                parameters = re.search(r"#\| tags: \[parameters\]\n(.*?)```", qmd, re.S).group(1)
                self.assertEqual(parameters.strip(), 'project = "example-python"')
                labels = re.findall(r"^#\| label: ([a-z-]+)$", qmd, re.M)
                self.assertEqual((labels[0], labels[1], labels[-1]), ("setup", "begin", "finish"))
                self.assertEqual(qmd.count("repro.run("), 1)
                self.assertEqual(qmd.count("repro.finish(state)"), 1)
                self.assertIn("repro.fail(state, error)", qmd)
                self.assertNotIn("AUDIT_REPORT_ID", qmd)
        with self.assertRaises(ValueError):
            contract.table_columns("r-packages", "python")

    # --- コンテナ内の監査が出す検査名は contracts/audit-checks.tsv の Python の分と同じ集合（CHECKS.md 3 節）---
    def test_audit_check_names_match_the_contract(self):
        inspect_source = (ROOT / "src/dependencies_inspect.py").read_text(encoding="utf-8")
        used = set(re.findall(r'(?:add_check|record)\(\s*"([^"]+)"', inspect_source))
        used |= set(re.findall(r'"check": "([^"]+)"', inspect_source))
        self.assertEqual(used, set(contract.audit_checks("python")))
        names = [row["check"] for row in contract.read_tsv("audit-checks.tsv")]
        self.assertEqual(len(names), len(set(names)))

    # --- operation-checks.tsv の照合の名前は重ならず、操作側が使う名前とそろう（CHECKS.md 3 節）---
    def test_operation_check_names_match_the_controller(self):
        operation_check_rows = contract.read_tsv("operation-checks.tsv")
        self.assertTrue(
            {row["operation"] for row in operation_check_rows}
            <= {"discover", "confirm", "build", "audit", "save", "sif"}
        )
        self.assertTrue(
            {row["language"] for row in operation_check_rows} <= {"common", "r", "python"}
        )
        keys = [(row["operation"], row["check"]) for row in operation_check_rows]
        self.assertEqual(len(keys), len(set(keys)))
        source = controller_source()
        used = set(re.findall(r'check\(\s*"([^"]+)"', source))
        expected = {
            row["check"] for row in operation_check_rows if row["language"] in ("common", "python")
        }
        self.assertEqual(used, expected)

    # --- 照合が扱わない JSON Schema の語は止める（CHECKS.md 3 節）---
    def test_unsupported_schema_keywords_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "未対応のスキーマの語です"):
            contract._check({}, {"oneOf": []}, "x")

    # --- 知らない語は文字の順に並べて知らせ、const は null と型まで比べる（CHECKS.md 3 節）---
    def test_unknown_keywords_are_sorted_and_const_compares_types(self):
        with self.assertRaisesRegex(ValueError, "未対応のスキーマの語です: oneOf, zeta"):
            contract._check({}, {"zeta": 1, "oneOf": []}, "x")
        self.assertEqual(len(contract._check("a", {"const": None}, "x")), 1)
        self.assertEqual(contract._check(None, {"const": None}, "x"), [])
        self.assertEqual(len(contract._check(True, {"const": 1}, "x")), 1)

    # --- contracts/ が無いとき・決まりが読めないときの止め方と、sif-extract.sh の最後の改行（CHECKS.md 3 節）---
    def test_missing_contract_files_and_sif_script_newline(self):
        root = self.scratch_root()
        directory = root / "contracts"
        with mock.patch.object(contract, "CONTRACT_DIR", directory):
            with self.assertRaisesRegex(ValueError, "contracts/ が見つかりません。"):
                contract.read_tsv("tables.tsv")
            directory.mkdir()
            (directory / "tables.tsv").write_text("table\tlanguage\tcolumns\n", encoding="utf-8")
            (directory / "sif-extract.sh").write_text("a\n\n", encoding="utf-8")
            self.assertEqual(contract.sif_extract_script(), "a\n")
            with self.assertRaisesRegex(ValueError, "結果の共通形式に合いません。"):
                contract.validate_summary({})

    # --- 列は契約の順にし、余分な列を除き、足りない列を空欄にする（CHECKS.md 3 節）---
    def test_select_table_follows_the_contract_columns(self):
        selected = contract.select_table(
            "dependency-review", "python", [{"detail": "d", "extra": 1, "item": "i"}]
        )
        self.assertEqual(selected, [{"item": "i", "detail": "d", "decision": ""}])
        self.assertEqual(list(selected[0]), ["item", "detail", "decision"])

    # --- 成功した工程は result-summary.json を 1 つだけ書き、artifacts は証跡のファイルと一致する（README.md「結果の共通形式」）---
    def test_successful_step_writes_one_summary_with_exact_artifacts(self):
        root = self.scratch_root()
        image_id = "sha256:" + "b" * 64

        def action(directory, details):
            (directory / "build.log").write_text("ok\n", encoding="utf-8")
            details["image_id"] = image_id
            return {"status": "PASS", "value": "example:test"}

        evidence.run_operation(
            "build-python",
            {"image": "example:test", "platform": "linux/arm64"},
            [],
            action,
            run_id="contract-pass",
        )
        summaries = sorted((root / "outputs/evidence").rglob("result-summary.json"))
        self.assertEqual(
            summaries, [root / "outputs/evidence/contract-pass/build/result-summary.json"]
        )
        summary = json.loads(summaries[0].read_text(encoding="utf-8"))
        self.assertEqual(
            (summary["operation"], summary["status"], summary["platform"], summary["image_id"]),
            ("build-python", "PASS", "linux/arm64", image_id),
        )
        self.assertEqual(
            summary["artifacts"],
            ["build.log", "inputs.tsv", "parameters.json", "tool-versions.tsv"],
        )
        versions = (summaries[0].parent / "tool-versions.tsv").read_text(encoding="utf-8")
        version_rows = [line.split("\t") for line in versions.splitlines()[1:]]
        runner = (
            ["runner image", "runner image id", "runner definition"]
            if os.getenv("AUDIT_RUNNER")
            else []
        )
        self.assertEqual(
            [row[0] for row in version_rows],
            ["Python", "docker", "docker engine", "docker buildx", *runner],
        )
        self.assertEqual(version_rows[0][1], platform.python_version())
        run_directory = summaries[0].parent.parent
        steps_before = set(run_directory.iterdir())
        with self.assertRaisesRegex(ValueError, "operations.tsv"):
            evidence.run_operation(
                "save-python-unknown",
                {"image": "example:test"},
                [],
                action,
                run_id="contract-pass",
            )
        self.assertEqual(set(run_directory.iterdir()), steps_before)

    # --- contracts/operations.tsv にない操作名は止め、証跡のフォルダも作らない（README.md「結果の共通形式」）---
    def test_unknown_operation_is_rejected(self):
        root = self.scratch_root()
        with self.assertRaisesRegex(ValueError, "operations.tsv"):
            evidence.operation_step("save-python-unknown")
        with self.assertRaisesRegex(ValueError, "operations.tsv"):
            evidence.run_operation(
                "save-python-unknown",
                {"image": "example:test"},
                [],
                lambda directory, details: {"status": "PASS", "value": 1},
                run_id="contract-pass",
            )
        self.assertFalse((root / "outputs/evidence").exists())

    # --- 壊れた確定記録でビルドが止まった理由と、渡したプロジェクト・パスを証跡に残す ---
    def test_malformed_build_lock_is_recorded_as_failure(self):
        root = self.scratch_root()
        project = "example-python"
        shutil.copytree(ROOT / "projects" / project, root / "projects" / project)
        lock = root / "bad" / "dependency-lock.tsv"
        lock.parent.mkdir()
        lock.write_text("not a lock\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "確定記録の形式が不正です"):
            image.build_python_image(
                "x:test", lock=lock, project=project, platform="linux/arm64", run_id="contract-lock"
            )
        directory = root / "outputs/evidence/contract-lock/build"
        summary = json.loads((directory / "result-summary.json").read_text(encoding="utf-8"))
        parameters = json.loads((directory / "parameters.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "FAIL")
        self.assertEqual(parameters["project"], project)
        self.assertEqual(parameters["dependency_lock"], "bad/dependency-lock.tsv")
        self.assertFalse((root / "outputs/repro").exists())


if __name__ == "__main__":
    unittest.main()
