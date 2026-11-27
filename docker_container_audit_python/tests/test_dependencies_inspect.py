#!/usr/bin/env python3
# --- コンテナの中の依存の監査と、共有ライブラリの調べ方のテスト（R は tests/testthat/test-dependencies-inspect.R）（CHECKS.md 1 節）---

import contextlib
import csv
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from packaging.markers import default_environment

#: リポジトリのルート（このファイルは <root>/tests/ にある）。
ROOT = Path(__file__).resolve().parent.parent
#: src/dependencies_inspect.py を、モジュール inspector として読み込む。
spec = importlib.util.spec_from_file_location(
    "dependencies_inspect", ROOT / "src/dependencies_inspect.py"
)
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)


# --- 機械の種類（e_machine）を持つ、ELF の見出しの見本（リトルエンディアン）を返す（README.md「Python」）---
def elf_header(machine):
    return b"\x7fELF\x02\x01\x01" + b"\x00" * 11 + machine.to_bytes(2, "little")


# --- 導入済みのパッケージの代わり（名前と版と、top_level.txt かファイルの一覧を持つ）（README.md「Python」）---
class Dist:
    # --- 名前と版、最上位のモジュールの手がかりを持たせる（README.md「Python」）---
    def __init__(self, name, version, top_level=None, files=()):
        self.metadata = {"Name": name}
        self.version = version
        self.top_level = top_level
        self.files = [PurePosixPath(path) for path in files]

    # --- top_level.txt だけを返す（README.md「Python」）---
    def read_text(self, name):
        return self.top_level if name == "top_level.txt" else None


#: Quarto が動いたときの監査の結果（監査のテストで使う。手元の Quarto には頼らない）。
QUARTO_FOUND = {
    "status": "PASS",
    "detail": "quarto 1.9.36 (/usr/local/bin/quarto)",
    "found": "1.9.36",
}


# --- コンテナ内の監査と解決の結果を確かめる（README.md「Python」）---
class InspectAuditTests(unittest.TestCase):
    # --- 結果を書く一時フォルダを作る（CHECKS.md 1 節）---
    def setUp(self):
        (ROOT / "_tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="python-inspect-test-", dir=ROOT / "_tmp")
        self.addCleanup(self.temp.cleanup)
        self.scratch = Path(self.temp.name)

    # --- 要件のマーカーは調査対象の Python の全項目を保存する（README.md「Python」）---
    def test_native_records_target_marker_environment(self):
        request = self.scratch / "ubuntu-packages.txt"
        request.touch()
        with (
            mock.patch.object(inspector.metadata, "distributions", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(inspector.inspect_native_main(request, self.scratch), 0)
        self.assertEqual(
            json.loads((self.scratch / "python-environment.json").read_text()),
            default_environment(),
        )

    # --- 第1段階で ldd の失敗・未解決名・複数の所有元を記録する（README.md「Python」）---
    def test_native_records_inspection_failures_and_multiple_owners(self):
        request = self.scratch / "ubuntu-packages.txt"
        request.touch()
        outputs = {
            "broken.so": {"status": 1, "output": "read error"},
            "missing.so": {"status": 1, "output": "libabsent.so => not found"},
            "found.so": {"status": 0, "output": "libfound.so => /usr/lib/libfound.so (0x1)"},
        }
        for name in outputs:
            (self.scratch / name).write_bytes(elf_header(183))
        (self.scratch / "foreign.so").write_bytes(elf_header(62))
        package = Dist("fixture", "1", files=[*outputs, "foreign.so"])
        package.locate_file = lambda name: self.scratch / name

        def execute(args):
            if args[0] == "ldd":
                return outputs[Path(args[1]).name]
            self.assertEqual(Path(args[0]).name, "dpkg-query")
            return {"status": 0, "output": "libone:arm64, libtwo:arm64: /usr/lib/libfound.so"}

        with (
            mock.patch.object(inspector.metadata, "distributions", return_value=[package]),
            mock.patch.object(inspector.platform, "machine", return_value="aarch64"),
            mock.patch.object(
                inspector.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"
            ),
            mock.patch.object(inspector, "command", side_effect=execute),
            mock.patch.object(
                inspector, "contents_providers", return_value={"libabsent.so": ["libabsent"]}
            ) as providers,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(inspector.inspect_native_main(request, self.scratch), 0)
        providers.assert_called_once_with(["libabsent.so"], self.scratch)
        plan = json.loads((self.scratch / "native-plan.json").read_text())
        self.assertEqual(plan["missing"], {"libabsent.so": ["fixture"]})
        self.assertEqual(plan["used_by"], {"libone": ["fixture"], "libtwo": ["fixture"]})
        self.assertEqual(len(plan["inspection_review"]), 1)
        self.assertIn("broken.so: read error", plan["inspection_review"][0])

    # --- コマンドの実行を差し替えて監査し、終了コードと python-audit.json の中身を返す（README.md「Python」）---
    def run_fake_audit(
        self,
        config,
        uv_status=0,
        bad_import=False,
        unmet=False,
        distributions=None,
        pylock=None,
        *,
        ubuntu_inventory=None,
        quarto_error=None,
        stdout=None,
    ):
        uv = self.scratch / "uv"
        uv.touch()
        config = {"uv": str(uv), **config}
        prefix = self.scratch / "prefix"
        prefix.mkdir(exist_ok=True)
        (prefix / "audit-pylock.toml").unlink(missing_ok=True)
        if pylock is not None:
            (prefix / "audit-pylock.toml").write_text(pylock, encoding="utf-8")

        def execute(args):
            if args[0] == "dpkg-query" and ubuntu_inventory is not None:
                return ubuntu_inventory
            if "-c" in args:
                return {"status": int(bad_import), "output": "failure" if bad_import else "ok"}
            if "check" in args:
                return {"status": uv_status, "output": "broken" if uv_status else "compatible"}
            if "--dry-run" in args:
                return {
                    "status": 0,
                    "output": "Would install 1 package" if unmet else "Would make no changes",
                }
            return {"status": 0, "output": ""}

        with (
            mock.patch.object(
                inspector.metadata,
                "distributions",
                return_value=distributions or [Dist("Example_Pkg", "1.2")],
            ),
            mock.patch.object(inspector, "command", side_effect=execute),
            mock.patch.object(
                inspector, "inspect_shared_libraries", return_value={"status": "PASS"}
            ),
            mock.patch.object(
                inspector, "audit_quarto", return_value=QUARTO_FOUND, side_effect=quarto_error
            ),
            mock.patch.object(
                inspector.platform,
                "freedesktop_os_release",
                return_value={"ID": "ubuntu", "PRETTY_NAME": "Ubuntu Linux"},
            ),
            mock.patch.object(inspector.sys, "prefix", str(prefix)),
            contextlib.redirect_stdout(io.StringIO() if stdout is None else stdout),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status = inspector.inspect_audit_main(config, self.scratch)
        return status, json.loads((self.scratch / "python-audit.json").read_text())

    # --- audit-items.tsv の行を、(check, item, expected, found, status) の組で返す（README.md「Python」）---
    def item_rows(self):
        with (self.scratch / "audit-items.tsv").open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, dialect="excel-tab")
            rows = [tuple(row.values()) for row in reader]
        self.assertEqual(reader.fieldnames, ["check", "item", "expected", "found", "status"])
        return rows

    # --- audit-checks.tsv を、項目ごとの (判定, 説明) で返す（README.md「Python」）---
    def checks(self):
        with (self.scratch / "audit-checks.tsv").open(encoding="utf-8", newline="") as stream:
            return {
                row["check"]: (row["status"], row["detail"])
                for row in csv.DictReader(stream, dialect="excel-tab")
            }

    # --- 確定記録の設定（版・Python の版・pylock.toml の SHA-256）を作る（ほかの欄は名前を付けて足すか、上書きする）（README.md「Python」）---
    def confirmed(self, packages, **fields):
        return {
            "confirmed": {
                "packages": packages,
                "python_version": inspector.platform.python_version(),
                "pylock_sha256": "0" * 64,
                **fields,
            }
        }

    # --- 名前の書き方が違っても版照合に合い、import も合格する（README.md「Python」）---
    def test_success_name_normalization_and_import(self):
        status, report = self.run_fake_audit(
            {"requirements": ["example-pkg==1.2"]},
            distributions=[Dist("Example_Pkg", "1.2", top_level="example_pkg\n")],
        )
        self.assertEqual(status, 0)
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["requirements"]["pass"] and report["imports"][0]["pass"])
        self.assertIn("Python module import", [check["check"] for check in report["checks"]])
        self.assertEqual(
            (self.scratch / "native-libraries.tsv").read_text().splitlines(),
            ['"language_package"\t"file"\t"soname"\t"path"\t"owners"'],
        )
        self.assertEqual(
            self.item_rows(),
            [
                ("Python requirements.txt", "example-pkg", "==1.2", "1.2", "PASS"),
                ("Python module import", "example_pkg", "", "loaded", "PASS"),
                ("Quarto CLI", "quarto", "", "1.9.36", "PASS"),
            ],
        )

    # --- 要件を満たさない・import の失敗・uv pip check の失敗は FAIL にする（README.md「Python」）---
    def test_bad_version_import_and_uv_check_fail(self):
        status, report = self.run_fake_audit(
            {"requirements": ["example-pkg==9"]},
            uv_status=1,
            bad_import=True,
            unmet=True,
            distributions=[Dist("Example_Pkg", "1.2", top_level="missing\n")],
        )
        self.assertEqual(status, 1)
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["requirements"]["pass"] or report["imports"][0]["pass"])
        self.assertEqual(report["uv_check"]["status"], 1)
        checks = self.checks()
        self.assertEqual(checks["uv pip check"][0], "FAIL")
        self.assertEqual(checks["Python requirements.txt"][0], "FAIL")
        self.assertEqual(checks["Python module import"][0], "FAIL")
        self.assertEqual(
            self.item_rows(),
            [
                ("Python requirements.txt", "example-pkg", "==9", "1.2", "FAIL"),
                ("Python module import", "missing", "", "failure", "FAIL"),
                ("Quarto CLI", "quarto", "", "1.9.36", "PASS"),
            ],
        )

    # --- 要件の行は、入っていないものと uv が入れ替えるものだけを FAIL にする（README.md「Python」）---
    def test_requirement_items_mark_only_changed_or_missing(self):
        failed = {"pass": False, "output": "Would install 1 package\n + beta==2\n"}
        rows = inspector.requirement_items(
            ["alpha>=1", "Beta==2", "gamma"], {"alpha": ["1"], "beta": ["1"]}, failed
        )
        self.assertEqual(
            [(row["item"], row["expected"], row["found"], row["status"]) for row in rows],
            [
                ("alpha", ">=1", "1", "PASS"),
                ("Beta", "==2", "1", "FAIL"),
                ("gamma", "", "", "FAIL"),
            ],
        )
        passed = inspector.requirement_items(["alpha"], {}, {"pass": True, "output": ""})
        self.assertEqual(passed[0]["status"], "PASS")

    # --- 導入済みの全配布物の最上位のモジュールを読み込む（pip などの道具は除く。Template Python version の照合は無い）（README.md「Python」）---
    def test_top_level_modules_of_all_distributions_are_imported(self):
        distributions = [
            Dist("alpha", "1", top_level="alpha\n_alpha_private\n"),
            Dist(
                "beta-pkg",
                "1",
                files=["beta/__init__.py", "beta_pkg-1.dist-info/RECORD", "../../bin/x"],
            ),
            Dist("gamma", "1", files=["gamma.cpython-312-aarch64-linux-gnu.so"]),
            Dist("pip", "24", top_level="pip\n"),
        ]
        status, report = self.run_fake_audit({}, distributions=distributions)
        self.assertEqual(status, 0)
        self.assertEqual([item["module"] for item in report["imports"]], ["alpha", "beta", "gamma"])
        self.assertNotIn("Template Python version", self.checks())

    # --- 同じ名前のパッケージが 2 つ入っていれば、確定記録の版の照合を合格にしない（R と同じ）（README.md「Python」）---
    def test_duplicate_distribution_is_not_silent_success(self):
        status, report = self.run_fake_audit(
            self.confirmed({"example-pkg": "1.2"}),
            distributions=[Dist("example-pkg", "1.2"), Dist("example_pkg", "2")],
        )
        self.assertEqual(status, 1)
        self.assertEqual(
            self.checks()["Confirmed Python versions"],
            ("FAIL", "example-pkg expected 1.2, found 1.2, 2"),
        )
        self.assertIn(
            ("Confirmed Python versions", "example-pkg", "1.2", "1.2, 2", "FAIL"), self.item_rows()
        )
        expected = [("example-pkg", "1.2"), ("example_pkg", "2")]
        self.assertEqual([(row["name"], row["version"]) for row in report["installed"]], expected)
        with (self.scratch / "installed-packages.tsv").open(encoding="utf-8", newline="") as stream:
            installed = csv.DictReader(stream, dialect="excel-tab")
            self.assertEqual([(row["package"], row["version"]) for row in installed], expected)

    # --- 照合した項目の行は、status を省けば expected と found で決め、found が無ければ空にする（README.md「Python」）---
    def test_audit_item_decides_status_from_found(self):
        self.assertEqual(inspector.audit_item("x", "a", "1", "1")["status"], "PASS")
        missing = inspector.audit_item("x", "b", "2", None)
        self.assertEqual((missing["status"], missing["found"]), ("FAIL", ""))

    # --- 監査の途中の例外でも、それまでの照合・一覧を保存して元の例外を返す（README.md「Python」）---
    def test_partial_result_keeps_checks_and_inventory(self):
        error = RuntimeError("監査が中断されました")
        with self.assertRaises(RuntimeError) as caught:
            self.run_fake_audit({"requirements": ["example-pkg==1.2"]}, quarto_error=error)
        self.assertIs(caught.exception, error)
        report = json.loads((self.scratch / "python-audit.json").read_text())
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(self.checks()["Python requirements.txt"][0], "PASS")
        self.assertIn(
            ("Python requirements.txt", "example-pkg", "==1.2", "1.2", "PASS"), self.item_rows()
        )
        self.assertIn("Example_Pkg", (self.scratch / "installed-packages.tsv").read_text())

    # --- 確定した版との違いは FAIL にし、実際のインストール一覧は残す（README.md「Python」）---
    def test_confirmed_package_drift_fails_and_keeps_actual_inventory(self):
        status, report = self.run_fake_audit(self.confirmed({"example-pkg": "9"}))
        self.assertEqual((status, report["status"]), (1, "FAIL"))
        with (self.scratch / "installed-packages.tsv").open(encoding="utf-8", newline="") as stream:
            installed = list(csv.DictReader(stream, dialect="excel-tab"))
        self.assertEqual(
            installed,
            [
                {
                    "ecosystem": "python",
                    "package": "Example_Pkg",
                    "architecture": "",
                    "version": "1.2",
                }
            ],
        )

    # --- 確定記録と比べて、足りない・版が違うものは FAIL、確定記録に無い追加は REVIEW にする（README.md「Python」）---
    def test_missing_or_different_fail_and_unlisted_additions_are_review(self):
        status, _ = self.run_fake_audit(self.confirmed({"example-pkg": "9"}))
        drift = self.checks()
        self.assertEqual(status, 1)
        self.assertEqual(drift["Confirmed Python versions"][0], "FAIL")
        self.assertEqual(drift["Unlisted Python additions"], ("PASS", ""))
        self.assertIn(
            ("Confirmed Python versions", "example-pkg", "9", "1.2", "FAIL"), self.item_rows()
        )
        self.assertIn("Confirmed Python runtime version", drift)
        status, _ = self.run_fake_audit(self.confirmed({"example-pkg": "1.2", "absent": "1"}))
        self.assertEqual(status, 1)
        self.assertEqual(
            self.checks()["Confirmed Python versions"], ("FAIL", "absent expected 1, found ")
        )
        self.assertIn(("Confirmed Python versions", "absent", "1", "", "FAIL"), self.item_rows())
        status, report = self.run_fake_audit(self.confirmed({}))
        unlisted = self.checks()
        self.assertEqual((status, report["status"]), (0, "REVIEW"))
        self.assertEqual(unlisted["Confirmed Python versions"], ("PASS", ""))
        self.assertEqual(unlisted["Unlisted Python additions"], ("REVIEW", "example-pkg 1.2"))
        self.assertIn(
            ("Unlisted Python additions", "example-pkg", "", "1.2", "REVIEW"), self.item_rows()
        )

    # --- 未登録パッケージと版を、画面と表に同じ並びで残す（README.md「Python」）---
    def test_unlisted_packages_have_the_same_order_on_screen_and_in_table(self):
        output = io.StringIO()
        status, report = self.run_fake_audit(
            self.confirmed({}),
            distributions=[Dist("extratwo", "5.0"), Dist("extraone", "4.0")],
            stdout=output,
        )
        self.assertEqual((status, report["status"]), (0, "REVIEW"))
        detail = "extraone 4.0; extratwo 5.0"
        self.assertEqual(self.checks()["Unlisted Python additions"], ("REVIEW", detail))
        self.assertIn(f"   {detail}", output.getvalue().splitlines())

    # --- イメージに pylock.toml が無ければ wheel の SHA-256 は REVIEW、あれば照合して違えば FAIL（確定記録に無い配布物の行も FAIL）（README.md「Python」）---
    def test_wheel_hashes_need_the_pylock_left_in_the_image(self):
        used = (
            'lock-version = "1.0"\n\n[[packages]]\nname = "Example_Pkg"\nversion = "1.2"\n'
            f'wheels = [{{ url = "https://example.invalid/a.whl", hashes = {{ sha256 = "{"a" * 64}" }} }}]\n'
        )
        digest = inspector.hashlib.sha256(used.encode()).hexdigest()

        def config(pylock_sha256, wheel_hash="a" * 64):
            return self.confirmed(
                {"example-pkg": "1.2"},
                hashes={"example-pkg": "1.2 " + wheel_hash},
                pylock_sha256=pylock_sha256,
            )

        status, report = self.run_fake_audit(config(digest))
        self.assertEqual((status, report["status"]), (0, "REVIEW"))
        wheel = self.checks()["Confirmed Python wheel hashes"]
        self.assertEqual(wheel, ("REVIEW", inspector._INSTALL_REPORT_MISSING))
        self.assertEqual(
            [row for row in self.item_rows() if row[0] == "Confirmed Python wheel hashes"],
            [
                ("Confirmed Python wheel hashes", "pylock.toml", digest, "", "REVIEW"),
                ("Confirmed Python wheel hashes", "example-pkg", "1.2 " + "a" * 64, "", "REVIEW"),
            ],
        )
        status, report = self.run_fake_audit(config(digest), pylock=used)
        self.assertEqual((status, report["status"]), (0, "PASS"))
        self.assertIn(
            (
                "Confirmed Python wheel hashes",
                "example-pkg",
                "1.2 " + "a" * 64,
                "1.2 " + "a" * 64,
                "PASS",
            ),
            self.item_rows(),
        )
        status, report = self.run_fake_audit(config(digest, wheel_hash="b" * 64), pylock=used)
        self.assertEqual((status, report["status"]), (1, "FAIL"))
        self.assertEqual(self.checks()["Confirmed Python wheel hashes"][0], "FAIL")
        self.assertIn(
            (
                "Confirmed Python wheel hashes",
                "example-pkg",
                "1.2 " + "b" * 64,
                "1.2 " + "a" * 64,
                "FAIL",
            ),
            self.item_rows(),
        )
        status, _ = self.run_fake_audit(config("0" * 64), pylock=used)
        self.assertEqual(status, 1)
        self.assertEqual(self.checks()["Confirmed Python wheel hashes"][0], "FAIL")
        extra = (
            used + '\n[[packages]]\nname = "Other_Pkg"\nversion = "3"\n'
            f'wheels = [{{ url = "https://example.invalid/c.whl", hashes = {{ sha256 = "{"c" * 64}" }} }}]\n'
        )
        status, report = self.run_fake_audit(
            config(inspector.hashlib.sha256(extra.encode()).hexdigest()), pylock=extra
        )
        self.assertEqual((status, report["status"]), (1, "FAIL"))
        self.assertEqual(self.checks()["Confirmed Python wheel hashes"][0], "FAIL")
        self.assertIn(
            ("Confirmed Python wheel hashes", "other-pkg", "", "3 " + "c" * 64, "FAIL"),
            self.item_rows(),
        )

    # --- ビルドで使った pylock.toml から、配布物ごとの「版 SHA-256」を読む（python-pins.tsv と同じ形）（README.md「Python」）---
    def test_used_pylock_hashes(self):
        used = self.scratch / "audit-pylock.toml"
        used.write_text(
            'lock-version = "1.0"\n\n[[packages]]\nname = "Example_Pkg"\nversion = "1.2"\n'
            f'wheels = [{{ url = "https://example.invalid/a.whl", hashes = {{ sha256 = "{"a" * 64}" }} }},'
            f' {{ url = "https://example.invalid/b.whl", hashes = {{ sha256 = "{"b" * 64}" }} }}]\n',
            encoding="utf-8",
        )
        self.assertEqual(
            inspector.used_pylock_hashes(used), {"example-pkg": f"1.2 {'a' * 64}; {'b' * 64}"}
        )
        self.assertEqual(inspector.used_pylock_hashes(self.scratch / "none.toml"), {})

    # --- 読めない共有ライブラリがあれば FAIL にする（README.md「Python」）---
    def test_missing_native_library_fails(self):
        file = self.scratch / "sample.so"
        file.write_bytes(elf_header(0xB7))
        dist = Dist("example", "1")
        dist.files = [file.name]
        dist.locate_file = lambda value: self.scratch / value

        def execute(args):
            return {"status": 1, "output": "libmissing.so => not found\n"}

        with (
            mock.patch.object(inspector.shutil, "which", return_value="/usr/bin/ldd"),
            mock.patch.object(inspector, "command", side_effect=execute),
            mock.patch.object(inspector.platform, "machine", return_value="aarch64"),
        ):
            report = inspector.inspect_shared_libraries([dist])
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["missing"][0]["soname"], "libmissing.so")
        inspector.write_native_libraries(report, self.scratch / "native-libraries.tsv")
        with (self.scratch / "native-libraries.tsv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, dialect="excel-tab"))
        self.assertEqual(
            rows,
            [
                {
                    "language_package": "example",
                    "file": str(file),
                    "soname": "libmissing.so",
                    "path": "not found",
                    "owners": "",
                }
            ],
        )

    # --- イメージと違う機械用のファイルは、ldd で調べずに外したことを残す（README.md「Python」）---
    def test_files_for_another_machine_are_skipped(self):
        file = self.scratch / "attach_linux_amd64.so"
        file.write_bytes(elf_header(0x3E))
        dist = Dist("debugpy", "1")
        dist.files = [file.name]
        dist.locate_file = lambda value: self.scratch / value
        self.assertEqual(inspector.elf_machine(elf_header(0x3E)), 0x3E)
        self.assertIsNone(inspector.elf_machine(b"\x7fELFshort"))
        with (
            mock.patch.object(inspector.shutil, "which", return_value="/usr/bin/ldd"),
            mock.patch.object(inspector, "command") as command,
            mock.patch.object(inspector.platform, "machine", return_value="aarch64"),
        ):
            report = inspector.inspect_shared_libraries([dist])
        command.assert_not_called()
        self.assertEqual((report["status"], report["foreign"]), ("PASS", [str(file)]))

    # --- Quarto の CLI は、無ければ REVIEW、動かなければ FAIL、動けば版を残す（README.md「Python」）---
    def test_quarto_cli_is_checked(self):
        self.assertEqual(inspector.audit_quarto(None)["status"], "REVIEW")
        with mock.patch.object(inspector, "command", return_value={"status": 0, "output": ""}):
            empty = inspector.audit_quarto("/usr/local/bin/quarto")
        self.assertEqual((empty["status"], empty["found"]), ("PASS", ""))
        with mock.patch.object(
            inspector, "command", return_value={"status": 0, "output": "1.9.36\n"}
        ):
            found = inspector.audit_quarto("/usr/local/bin/quarto")
        self.assertEqual(
            found,
            {
                "status": "PASS",
                "detail": "quarto 1.9.36 (/usr/local/bin/quarto)",
                "found": "1.9.36",
            },
        )
        with mock.patch.object(
            inspector, "command", return_value={"status": 1, "output": "boom\n"}
        ):
            broken = inspector.audit_quarto("/usr/local/bin/quarto")
        self.assertEqual(
            (broken["status"], broken["detail"], broken["found"]), ("FAIL", "boom", "boom")
        )

    # --- ldd が解決したパスを、dpkg-query -S で持ち主のパッケージに対応させる（README.md「Python」）---
    def test_resolved_library_is_mapped_to_its_owner(self):
        file = self.scratch / "found.so"
        file.write_bytes(elf_header(0xB7))
        dist = Dist("example", "1")
        dist.files = [file.name]
        dist.locate_file = lambda value: self.scratch / value

        def execute(args):
            if args[0] == "ldd":
                return {"status": 0, "output": "\tlibfound.so.1 => /usr/lib/libfound.so.1 (0x1)\n"}
            return {"status": 0, "output": "libfound1:arm64: /usr/lib/libfound.so.1\n"}

        with (
            mock.patch.object(
                inspector.shutil, "which", side_effect=lambda name: "/usr/bin/" + name
            ),
            mock.patch.object(inspector, "command", side_effect=execute),
            mock.patch.object(inspector.platform, "machine", return_value="aarch64"),
        ):
            report = inspector.inspect_shared_libraries([dist])
        self.assertEqual(report["status"], "PASS")
        inspector.write_native_libraries(report, self.scratch / "native-libraries.tsv")
        with (self.scratch / "native-libraries.tsv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, dialect="excel-tab"))
        self.assertEqual(
            [(row["soname"], row["path"], row["owners"]) for row in rows],
            [("libfound.so.1", "/usr/lib/libfound.so.1", "libfound1:arm64")],
        )

    # --- ELF でない見出しは None にし、機械の名前の対応は e_machine の値を持つ（README.md「Python」）---
    def test_elf_machine_rejects_other_files(self):
        self.assertIsNone(inspector.elf_machine(b"not elf" + b"\x00" * 20))
        self.assertIsNone(inspector.elf_machine(b"\x7fELF\x02\x01\x01"))
        self.assertEqual(inspector.elf_machine(elf_header(62)), 62)
        self.assertEqual(inspector._ELF_MACHINES["aarch64"], 0xB7)

    # --- ldd が無ければ REVIEW にする（README.md「Python」）---
    def test_missing_ldd_is_review(self):
        with mock.patch.object(inspector.shutil, "which", return_value=None):
            report = inspector.inspect_shared_libraries([])
        self.assertEqual(report["status"], "REVIEW")

    # --- タブ区切りの Contents を読み、途中まで展開した索引は使わない（README.md「Python」）---
    def test_contents_reads_tabs_and_requires_a_complete_index(self):
        index = self.scratch / "Contents"
        index.touch()
        for status in (0, 1):
            process = mock.MagicMock()
            reader = process.__enter__.return_value
            reader.stdout = iter(["  usr/lib/libtest.so\tlibs/pkg\n"])
            reader.wait.return_value = status
            with (
                self.subTest(status=status),
                mock.patch.object(inspector.subprocess, "Popen", return_value=process),
                mock.patch.object(
                    inspector,
                    "command",
                    side_effect=[{"status": 0, "output": ""}, {"status": 0, "output": str(index)}],
                ) as command,
            ):
                if status:
                    with self.assertRaisesRegex(
                        RuntimeError, "^Ubuntu の Contents 索引を展開できません: "
                    ):
                        inspector.contents_providers(["libtest.so"], self.scratch)
                else:
                    self.assertEqual(
                        inspector.contents_providers(["libtest.so"], self.scratch),
                        {"libtest.so": ["pkg"]},
                    )
                self.assertIn("Acquire::https::Timeout=20", command.call_args_list[0].args[0])

    # --- Ubuntu の一覧を取得できなくても、言語の確定版は照合する（README.md「Python」）---
    def test_unavailable_inventory_skips_dependent_checks(self):
        config = self.confirmed({"example-pkg": "1.2"})
        config["ubuntu"] = []
        config["baseline"] = [{"package": "bash", "architecture": "arm64", "version": "5.2"}]
        status, report = self.run_fake_audit(
            config, ubuntu_inventory={"status": 1, "output": "一覧を取得できません"}
        )
        self.assertEqual((status, report["status"]), (1, "FAIL"))
        checks = self.checks()
        self.assertEqual(checks["Ubuntu package inventory"][0], "FAIL")
        self.assertEqual(checks["Confirmed Python versions"][0], "PASS")
        self.assertNotIn("Base OS packages removed or changed", checks)
        self.assertNotIn("Confirmed Ubuntu versions", checks)

    # --- Debian・Ubuntu の名前を残し、対象外の OS には不合格の理由を書く（README.md「Python」）---
    def test_ubuntu_audit_checks_os_and_reports_its_name(self):
        for os_id, name, outcome in (
            ("debian", "Debian 12", ("PASS", "Debian 12")),
            ("ubuntu", 'My "OS"', ("PASS", 'My "OS"')),
            ("alpine", "Alpine Linux", ("FAIL", "Not Debian/Ubuntu: Alpine Linux")),
        ):
            with (
                self.subTest(os_id=os_id),
                mock.patch.object(inspector, "command", return_value={"status": 0, "output": ""}),
            ):
                result = inspector.audit_ubuntu([], {"ID": os_id, "PRETTY_NAME": name})
            os_check = next(row for row in result["checks"] if row["check"] == "OS (Debian/Ubuntu)")
            self.assertEqual((os_check["status"], os_check["detail"]), outcome)

    # --- 確定記録の Ubuntu の版の違い・計画に無い追加・足すと決めたものの欠けを、別の項目で見つける（README.md「Python」）---
    def test_ubuntu_audit_detects_changed_versions_and_unplanned_packages(self):
        inventory = "libgomp1\tamd64\t2\tinstalled\nextra\tall\t1\tinstalled\n"
        plan = [
            {"package": "libgomp1", "architecture": "amd64", "version": "1", "action": "install"},
            {"package": "libfoo1", "architecture": "amd64", "version": "3", "action": "install"},
        ]
        with mock.patch.object(
            inspector,
            "command",
            side_effect=[
                {"status": 0, "output": ""},
                {"status": 0, "output": ""},
                {"status": 0, "output": inventory},
            ],
        ):
            result = inspector.audit_ubuntu(plan, {"ID": "ubuntu"})
        checks = {check["check"]: check for check in result["checks"]}
        self.assertIn(
            "libgomp1:amd64 expected 1, found 2", checks["Confirmed Ubuntu versions"]["detail"]
        )
        self.assertEqual(checks["Unlisted Ubuntu additions"]["detail"], "extra:all")
        self.assertEqual(
            checks["Confirmed Ubuntu additions missing or ambiguous"]["detail"], "libfoo1:amd64"
        )
        self.assertEqual(
            {
                name: check["status"]
                for name, check in checks.items()
                if name.startswith(("Confirmed", "Unlisted"))
            },
            {
                "Confirmed Ubuntu versions": "FAIL",
                "Unlisted Ubuntu additions": "REVIEW",
                "Confirmed Ubuntu additions missing or ambiguous": "FAIL",
            },
        )
        self.assertEqual(
            [tuple(row.values()) for row in result["items"]],
            [
                ("Confirmed Ubuntu versions", "libfoo1:amd64", "3", "", "FAIL"),
                ("Confirmed Ubuntu versions", "libgomp1:amd64", "1", "2", "FAIL"),
                ("Unlisted Ubuntu additions", "extra:all", "", "1", "REVIEW"),
                (
                    "Confirmed Ubuntu additions missing or ambiguous",
                    "libfoo1:amd64",
                    "3",
                    "",
                    "FAIL",
                ),
            ],
        )

    # --- 固定ベースに残す keep の行は版を照らさず、アーキテクチャだけ違えば計画に無い追加と欠けの両方に出す（R と同じ）（README.md「Python」）---
    def test_ubuntu_audit_keeps_base_rows_and_matches_architecture(self):
        inventory = "libgomp1\tamd64\t2\tinstalled\nextra\tall\t1\tinstalled\n"
        plan = [
            {"package": "libgomp1", "architecture": "amd64", "version": "2", "action": "install"},
            {
                "package": "extra",
                "architecture": "all",
                "version": "1",
                "base_version": "1",
                "action": "keep",
            },
        ]
        names = (
            "Confirmed Ubuntu versions",
            "Unlisted Ubuntu additions",
            "Confirmed Ubuntu additions missing or ambiguous",
        )
        responses = [{"status": 0, "output": ""}] * 2 + [{"status": 0, "output": inventory}]
        with mock.patch.object(inspector, "command", side_effect=responses):
            result = inspector.audit_ubuntu(plan, {"ID": "ubuntu"})
        checks = {check["check"]: check for check in result["checks"]}
        self.assertEqual(
            [(checks[name]["status"], checks[name]["detail"]) for name in names], [("PASS", "")] * 3
        )
        plan[0]["architecture"] = "arm64"
        with mock.patch.object(inspector, "command", side_effect=responses):
            result = inspector.audit_ubuntu(plan, {"ID": "ubuntu"})
        checks = {check["check"]: check for check in result["checks"]}
        self.assertEqual(checks["Unlisted Ubuntu additions"]["detail"], "libgomp1:amd64")
        self.assertEqual(
            checks["Confirmed Ubuntu additions missing or ambiguous"]["detail"], "libgomp1:arm64"
        )
        self.assertEqual(
            [tuple(row.values()) for row in result["items"]],
            [
                ("Confirmed Ubuntu versions", "libgomp1:arm64", "2", "", "FAIL"),
                ("Unlisted Ubuntu additions", "libgomp1:amd64", "", "2", "REVIEW"),
                (
                    "Confirmed Ubuntu additions missing or ambiguous",
                    "libgomp1:arm64",
                    "2",
                    "",
                    "FAIL",
                ),
            ],
        )

    # --- 固定ベースのパッケージの変化を見つけ、確定した更新は除く（確定した更新でも消えていれば FAIL）（README.md「Python」）---
    def test_ubuntu_audit_compares_with_the_fixed_base(self):
        inventory = "libgomp1\tamd64\t2\tinstalled\nbash\tamd64\t5.2-1\tinstalled\n"
        plan = [
            {"package": "libgomp1", "architecture": "amd64", "version": "2", "action": "update"},
            {"package": "bash", "architecture": "amd64", "version": "5.2-1", "action": "keep"},
        ]
        baseline = [
            {"package": "libgomp1", "architecture": "amd64", "version": "1"},
            {"package": "bash", "architecture": "amd64", "version": "5.2"},
            {"package": "gone", "architecture": "all", "version": "1"},
        ]
        responses = [{"status": 0, "output": ""}] * 2 + [{"status": 0, "output": inventory}]
        with mock.patch.object(inspector, "command", side_effect=responses):
            result = inspector.audit_ubuntu(plan, {"ID": "ubuntu"}, baseline)
        base = {check["check"]: check for check in result["checks"]}[
            "Base OS packages removed or changed"
        ]
        self.assertEqual((base["status"], base["detail"]), ("FAIL", "bash:amd64; gone:all"))
        self.assertEqual(
            [
                (row["item"], row["found"], row["status"])
                for row in result["items"]
                if row["check"] == "Base OS packages removed or changed"
            ],
            [
                ("libgomp1:amd64", "2", "PASS"),
                ("bash:amd64", "5.2-1", "FAIL"),
                ("gone:all", "", "FAIL"),
            ],
        )
        for approved, detail in (
            ([], "libgomp1:amd64; bash:amd64; gone:all"),
            (
                plan
                + [{"package": "gone", "architecture": "all", "version": "1", "action": "update"}],
                "bash:amd64; gone:all",
            ),
        ):
            with mock.patch.object(inspector, "command", side_effect=responses):
                result = inspector.audit_ubuntu(approved, {"ID": "ubuntu"}, baseline)
            base = {check["check"]: check for check in result["checks"]}[
                "Base OS packages removed or changed"
            ]
            self.assertEqual((base["status"], base["detail"]), ("FAIL", detail))


# --- 調査の環境の APT スナップショットへの書き換えは、取得元（stanza）の区切りの空行を残す（R と同じ）（README.md「第 1 段階の中身」）---
class InspectRecipeTests(unittest.TestCase):
    # --- Dockerfile.inspect の sed の書き換えを、取得元の見本に当てて確かめる（README.md「第 1 段階の中身」）---
    def test_snapshot_rewrite_keeps_the_stanza_breaks(self):
        recipe = (ROOT / "Dockerfile.inspect").read_text(encoding="utf-8").splitlines()
        lines = [line for line in recipe if 'sed "s|^URIs:' in line]
        self.assertEqual(len(lines), 1)
        script = lines[0].split('"')[1].replace("$APT_SNAPSHOT", "20261101T000000Z")
        ports = "URIs: http://ports.ubuntu.com/ubuntu-ports/"
        sources = "\n".join(
            ["Types: deb", ports, "Suites: noble noble-updates", "", "Types: deb", ports]
            + ["Suites: noble-security", ""]
        )
        result = subprocess.run(
            ["sed", script], input=sources, capture_output=True, text=True, check=True
        )
        uri = "URIs: https://snapshot.ubuntu.com/ubuntu/20261101T000000Z/"
        self.assertEqual(
            result.stdout.splitlines(),
            ["Types: deb", uri, "Check-Valid-Until: no", "Suites: noble noble-updates", ""]
            + ["Types: deb", uri, "Check-Valid-Until: no", "Suites: noble-security"],
        )


if __name__ == "__main__":
    unittest.main()
