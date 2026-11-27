# --- ローカルの Docker イメージを使う結合テスト（R は tests/testthat/test-integration-docker.R）（CHECKS.md 1 節）---

import csv
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

#: 子の根（このファイルは <子の根>/tests/ にある）。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import archive as archive_ops  # noqa: E402
import container  # noqa: E402
import contract  # noqa: E402
import evidence as evidence_ops  # noqa: E402
from archive import audit_python_image  # noqa: E402


# --- AUDIT_TEST_PYTHON_IMAGE のイメージを、AUDIT_TEST_PYTHON_LOCK の確定記録と照合して監査する（CHECKS.md 1 節）---
@unittest.skipUnless(
    os.getenv("AUDIT_TEST_PYTHON_IMAGE") and os.getenv("AUDIT_TEST_PYTHON_LOCK"),
    "AUDIT_TEST_PYTHON_IMAGE と AUDIT_TEST_PYTHON_LOCK が指定されていないので、"
    "Docker 結合テストは飛ばします",
)
class DockerIntegrationTests(unittest.TestCase):
    # --- _tmp/ の一時フォルダの名前を実行 ID にし、終わったら証跡も消す（CHECKS.md 1 節）---
    def setUp(self):
        root_tmp = ROOT / "_tmp"
        root_tmp.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="python-docker-test-", dir=root_tmp)
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name)
        self.run_id = self.scratch.name
        self.enterContext(
            mock.patch.object(evidence_ops, "EVIDENCE_DIR", self.scratch / "evidence")
        )
        self.image = os.environ["AUDIT_TEST_PYTHON_IMAGE"]
        self.lock = (ROOT / os.environ["AUDIT_TEST_PYTHON_LOCK"]).resolve()
        self.platform = os.environ.get("AUDIT_TEST_PLATFORM", "linux/arm64")

    # --- 共通形式の結果・監査の項目・インストール一覧を確かめる（README.md「結果の共通形式」）---
    def check_result(self, run_id, expected, check, check_status=None):
        check_status = check_status or expected
        directory = evidence_ops.evidence_directory(run_id, "audit")
        summary = json.loads((directory / "result-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], expected)
        self.assertEqual(summary["language"], "python")
        with (directory / "audit-checks.tsv").open(encoding="utf-8", newline="") as handle:
            checks = {
                row["check"]: row["status"] for row in csv.DictReader(handle, dialect="excel-tab")
            }
        with (directory / "installed-packages.tsv").open(encoding="utf-8", newline="") as handle:
            ecosystems = {row["ecosystem"] for row in csv.DictReader(handle, dialect="excel-tab")}
        self.assertEqual(ecosystems, {"python", "ubuntu"})
        self.assertEqual(checks[check], check_status)
        if expected == "PASS":
            self.assertEqual(set(checks.values()), {"PASS"})
        with (directory / "audit-items.tsv").open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, dialect="excel-tab")
            items = list(reader)
        self.assertEqual(reader.fieldnames, contract.table_columns("audit-items", "python"))
        self.assertTrue({row["check"] for row in items} <= set(checks))
        self.assertTrue({row["status"] for row in items} <= {"PASS", "REVIEW", "FAIL"})
        if expected == "PASS":
            self.assertTrue(items)
            self.assertEqual({row["status"] for row in items}, {"PASS"})
        self.assertEqual(
            any(row["status"] == "FAIL" for row in items if row["check"] == check),
            check_status == "FAIL",
        )
        return items

    # --- 確定記録のファイルを、一時フォルダの name のフォルダに写す（CHECKS.md 1 節）---
    def copy_lock(self, name):
        directory = self.scratch / name
        directory.mkdir()
        for file in contract.lock_files("python"):
            shutil.copyfile(self.lock.parent / file, directory / file)
        return directory

    # --- 写した確定記録の SHA-256 で、dependency-lock.tsv を書き直す（CHECKS.md 1 節）---
    def rehash_lock(self, directory):
        path = directory / "dependency-lock.tsv"
        path.write_text(
            "path\tsha256\n"
            + "".join(
                f"{file}\t{container.file_sha256(directory / file)}\n"
                for file in contract.lock_files("python")
            ),
            encoding="utf-8",
        )
        return path

    # --- 見本のイメージの監査は、固定ベースとの比較も含めて全項目が PASS になる（CHECKS.md 1 節）---
    def test_installed_requirements_and_imports_pass(self):
        audit_python_image(self.image, self.platform, lock=self.lock, run_id=self.run_id)
        items = self.check_result(self.run_id, "PASS", "Base OS packages removed or changed")
        checks = {row["check"] for row in items}
        self.assertTrue(
            {
                "Python requirements.txt",
                "Python module import",
                "Base OS packages removed or changed",
                "Confirmed Python versions",
                "Confirmed Python wheel hashes",
            }
            <= checks
        )
        self.assertEqual(
            {row["found"] for row in items if row["check"] == "Python module import"}, {"loaded"}
        )

    # --- 確定記録の base-ubuntu.tsv の版を書き換えると、ベースとの差分の監査が FAIL として記録される（CHECKS.md 1 節）---
    def test_changed_base_os_version_is_recorded_as_failure(self):
        modified = self.copy_lock("modified-baseline")
        packages = modified / "base-ubuntu.tsv"
        rows = packages.read_text(encoding="utf-8").splitlines()
        fields = [row.split("\t") for row in rows[2:]]
        installed = [index for index, row in enumerate(fields) if row[3] == "installed"]
        self.assertTrue(installed)
        first = installed[0]
        fields[first][2] = "0.0.0-test"
        rows[first + 2] = "\t".join(fields[first])
        packages.write_text("".join(f"{row}\n" for row in rows), encoding="utf-8")
        lock = self.rehash_lock(modified)
        with self.assertRaisesRegex(RuntimeError, "の実行に失敗しました"):
            audit_python_image(self.image, self.platform, lock=lock, run_id=self.run_id)
        items = self.check_result(self.run_id, "FAIL", "Base OS packages removed or changed")
        self.assertEqual(
            [
                (row["item"], row["expected"])
                for row in items
                if row["check"] == "Base OS packages removed or changed" and row["status"] == "FAIL"
            ],
            [(f"{fields[first][0]}:{fields[first][1]}", "0.0.0-test")],
        )

    # --- 確定記録の requirements.txt の写しを満たさないイメージの監査は、FAIL として記録される（CHECKS.md 1 節）---
    def test_unmet_requirement_is_recorded_as_failure(self):
        invalid = self.copy_lock("invalid-lock")
        (invalid / "requirements.txt").write_text(
            "no-such-audit-package-xyz==0.0.1\n", encoding="utf-8"
        )
        environment = contract.read_build_environment(invalid / "build-environment.json")
        environment["inputs"]["requirements.txt"] = container.file_sha256(
            invalid / "requirements.txt"
        )
        contract.write_build_environment(environment, invalid / "build-environment.json")
        lock = self.rehash_lock(invalid)
        with self.assertRaisesRegex(RuntimeError, "の実行に失敗しました"):
            audit_python_image(self.image, self.platform, lock=lock, run_id=self.run_id)
        items = self.check_result(self.run_id, "FAIL", "Python requirements.txt")
        self.assertEqual(
            [
                (row["item"], row["expected"], row["found"], row["status"])
                for row in items
                if row["check"] == "Python requirements.txt"
            ],
            [("no-such-audit-package-xyz", "==0.0.1", "", "FAIL")],
        )

    # --- SIF と同じ取り込みで、環境変数の引用符と逆斜線が元の値に戻る（CHECKS.md 1 節）---
    def test_import_preserves_quoted_environment(self):
        path = self.scratch / "probe.tar"
        with tarfile.open(path, mode="w") as stream:
            stream.addfile(tarfile.TarInfo("probe"))
        entry = 'AUDIT_QUOTED=a"b\\c'
        values = json.dumps({"Env": [entry]})
        with mock.patch.object(
            archive_ops.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, values)
        ):
            changes = archive_ops._sif_import_changes(self.image, self.platform)
        args = [
            "docker",
            "import",
            "--platform",
            self.platform,
            "--change",
            f"LABEL audit.test={self.run_id}",
        ]
        for change in changes:
            args.extend(("--change", change))
        image_id = subprocess.check_output([*args, str(path)], text=True).strip()
        self.addCleanup(
            subprocess.run, ["docker", "image", "rm", image_id], check=True, capture_output=True
        )
        found = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{json .Config.Env}}", image_id], text=True
        )
        self.assertIn(entry, json.loads(found))


if __name__ == "__main__":
    unittest.main()
