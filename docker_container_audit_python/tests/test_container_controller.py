# --- 操作側の各部品のテスト（Docker を使わない。R は tests/testthat/test-container-controller.R）（CHECKS.md 1 節）---

import csv
import hashlib
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import archive as archive_ops  # noqa: E402
import container  # noqa: E402
import contract  # noqa: E402
import discovery as discovery_ops  # noqa: E402
import evidence as evidence_ops  # noqa: E402
import generate_dockerfile as generator  # noqa: E402
import image  # noqa: E402
import project  # noqa: E402
import repro  # noqa: E402

#: リポジトリのルート（このファイルは <root>/tests/ にある）。
ROOT = Path(__file__).resolve().parents[1]
#: 調査の段階で記録する、仮の環境の Python。
PYTHON_ENVIRONMENT = {"python_full_version": "3.12.3", "platform_machine": "x86_64"}
#: テストの base.txt に書く固定ベース（ダイジェスト付き）。
BASE = "ubuntu:24.04@sha256:" + "9" * 64
#: テストの APT スナップショットの時点。
SNAPSHOT = "20261122T000000Z"
#: テストのプロジェクト名（一時フォルダの projects/example）。
PROJECT = "example"


# --- uv の解決結果（pylock.toml）の見本を返す（README.md「Python」）---
def pylock(url="https://example.invalid/example-1.0-py3-none-any.whl"):
    return (
        'lock-version = "1.0"\ncreated-by = "uv"\n\n[[packages]]\nname = "Example"\n'
        f'version = "1.0"\nwheels = [{{ url = "{url}", hashes = {{ sha256 = "{"a" * 64}" }} }}]\n'
    )


# --- Ubuntu の計画の TSV（ubuntu-packages の列）を文字で返す（README.md「Python」）---
def ubuntu_tsv(rows):
    columns = [
        "package",
        "architecture",
        "version",
        "relation",
        "base_version",
        "action",
        "language_packages",
    ]
    lines = ["\t".join(f'"{c}"' for c in columns)]
    lines += ["\t".join(f'"{row.get(c, "") or ""}"' for c in columns) for row in rows]
    return "\n".join(lines) + "\n"


# --- 確定が照合する調査の表（Ubuntu の計画・固定ベース・言語のパッケージ）を返す（README.md「Python」）---
def discovery_tables(environment, rows):
    return {
        "apt-snapshot.txt": environment["apt_snapshot"] + "\n",
        "checks.tsv": "check\tstatus\tdetail\nPython dependency resolution\tPASS\t\nUbuntu dependency plan\tPASS\t\n",
        "ubuntu-packages.tsv": ubuntu_tsv(rows),
        "base-ubuntu.tsv": (
            f"# {environment['base_image']} {environment['platform']}\n"
            "Package\tArchitecture\tVersion\tStatus\n"
            "bash\tamd64\t5.2\tinstalled\n"
        ),
        "python-packages.tsv": "package\tversion\trequested\tsha256\n"
        + f"example\t1.0\tTRUE\t{'a' * 64}\n",
    }


# --- 辞書を JSON として書き出す（テストの見本を作る）（README.md「結果の共通形式」）---
def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --- 調査の証跡を書く（JSON か文字列）。uv の解決は成功したことにする（README.md「Python」）---
def write_evidence(discovery, evidence):
    for name, value in evidence.items():
        path = discovery / name
        if isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            write_json(path, value)
    (discovery / "resolution/resolve-status").write_text("0\n", encoding="utf-8")


# --- 工程の記録先の checks.tsv を、照合の名前と判定の組で返す（README.md「結果の共通形式」）---
def read_checks(directory):
    with (Path(directory) / "checks.tsv").open(encoding="utf-8", newline="") as stream:
        return [
            (row["check"], row["status"])
            for row in csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
        ]


# --- 工程の証跡の parameters.json（工程の設定）を返す（README.md「結果の共通形式」）---
def recorded_parameters(directory):
    return json.loads((Path(directory) / "parameters.json").read_text(encoding="utf-8"))


# --- 工程の証跡の inputs.tsv（読んだファイルと SHA-256）を、パスから SHA-256 への辞書で返す（README.md「結果の共通形式」）---
def recorded_inputs(directory):
    with (Path(directory) / "inputs.tsv").open(encoding="utf-8", newline="") as stream:
        return {row["path"]: row["sha256"] for row in csv.DictReader(stream, delimiter="\t")}


# --- 操作側の記録・入力の照合・保存を、Docker を使わずに確かめる（CHECKS.md 1 節）---
class ControllerTests(unittest.TestCase):
    # --- _tmp/ の中の一時フォルダを操作側の ROOT にし、実行 ID を省いた工程には毎回新しい ID を渡す（CHECKS.md 1 節）---
    def setUp(self):
        scratch = ROOT / "_tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="python-controller-test-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src/repro.py").touch()
        env_patch = mock.patch.dict(os.environ, {"REPRO_ROOT": str(self.root), "REPRO_RUN_ID": ""})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        patch = mock.patch.object(container, "ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        numbers = itertools.count(1)

        def recorder(operation, params, inputs, action, *, run_id=None):
            run_id = run_id or f"test_{next(numbers):04d}"
            return evidence_ops.run_operation(operation, params, inputs, action, run_id=run_id)

        recording = mock.patch.object(evidence_ops, "_RECORDER", recorder)
        recording.start()
        self.addCleanup(recording.stop)

    # --- 一時フォルダに projects/example を作り、request のフォルダを返す（README.md「検査対象のフォルダ」）---
    def example_context(self, ubuntu="libgomp1\n"):
        context = self.root / "projects" / PROJECT / "request"
        if not context.is_dir():
            context.mkdir(parents=True)
            (context / "requirements.txt").write_text("example>=1\n", encoding="utf-8")
            (context / "base.txt").write_text(
                f"# 固定ベース\nBASE_IMAGE={BASE}  # 注釈\n", encoding="utf-8"
            )
            if ubuntu is not None:
                (context / "ubuntu-packages.txt").write_text(ubuntu, encoding="utf-8")
        return context

    # --- 調査の記録先の代わりに、確定が照合する調査の記録を書く（README.md「Python」）---
    def example_discovery(self, context, rows=(), native=None, review=""):
        _, paths = project.target_inputs(PROJECT)
        environment = discovery_ops._lock_environment(paths, "linux/amd64", SNAPSHOT)
        discovery = self.root / "discovery"
        (discovery / "resolution").mkdir(parents=True, exist_ok=True)
        evidence = {
            **discovery_tables(environment, list(rows)),
            "resolution/python-environment.json": PYTHON_ENVIRONMENT,
            "resolution/pylock.toml": pylock(),
            "resolution/native-plan.json": native or {"missing": {}, "providers": {}, "review": []},
        }
        write_evidence(discovery, evidence)
        self.rewrite_discovery(discovery, environment, evidence, review)
        return discovery, environment, evidence

    # --- 調査の記録（build-environment.json）を、今の証跡のハッシュで書き直す（README.md「Python」）---
    @staticmethod
    def rewrite_discovery(discovery, environment, evidence, review=""):
        write_json(
            discovery / "build-environment.json",
            {
                **environment,
                "review": review,
                "evidence": {name: container.file_sha256(discovery / name) for name in evidence},
            },
        )

    # --- 第 1 段階の確定記録を作り、dependency-lock.tsv のパスを返す（README.md「Python」）---
    def example_lock(self):
        context = self.example_context()
        discovery, _, _ = self.example_discovery(
            context,
            [
                {
                    "package": "libgomp1",
                    "architecture": "amd64",
                    "version": "1:2",
                    "action": "install",
                }
            ],
        )
        return Path(discovery_ops.confirm_python_dependencies(discovery, PROJECT)["lock"])

    # --- 確定記録から Dockerfile を生成し、置き換えの許可と入力の照合を確かめる（README.md「第 2 段階の中身」）---
    def test_generate_dockerfile_checks_confirmed_inputs_and_preserves_existing_files(self):
        lock = self.example_lock()
        context = self.example_context()
        (context.parent / "config.yaml").write_text(
            f"experiment_name: {PROJECT}\nseed: 1\nproject: {PROJECT}\nplatform: linux/amd64\n"
            f"discovery:\n  apt_snapshot: ''\nimage:\n  dependency_lock: '{lock}'\n",
            encoding="utf-8",
        )
        destination = generator.generate_python_dockerfile(PROJECT)
        self.assertEqual(
            {path.name for path in destination.iterdir()},
            {"Dockerfile", "ubuntu-pins.txt", "pylock.toml"},
        )
        dockerfile = destination / "Dockerfile"
        original = dockerfile.read_text(encoding="utf-8")
        self.assertIn(f"ARG BASE_IMAGE={BASE}", original.splitlines())
        self.assertIn(f"ARG APT_SNAPSHOT={SNAPSHOT}", original.splitlines())
        self.assertEqual(
            (destination / "pylock.toml").read_bytes(), (lock.parent / "pylock.toml").read_bytes()
        )
        self.assertEqual(generator.generate_python_dockerfile(PROJECT), destination)

        dockerfile.write_text("手書き\n", encoding="utf-8")
        extra = destination / "extra.txt"
        extra.write_text("追加ファイル\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "内容が異なる既存ファイル"):
            generator.generate_python_dockerfile(PROJECT)
        self.assertEqual(dockerfile.read_text(encoding="utf-8"), "手書き\n")
        generator.generate_python_dockerfile(PROJECT, replace=True)
        self.assertEqual(dockerfile.read_text(encoding="utf-8"), original)
        self.assertEqual(extra.read_text(encoding="utf-8"), "追加ファイル\n")

        (context / "requirements.txt").write_text("other>=9\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "確定記録と request/ が一致しません"):
            generator.generate_python_dockerfile(PROJECT, replace=True)
        self.assertEqual(dockerfile.read_text(encoding="utf-8"), original)

    # --- config.yaml は記録層の読み方で読み、project がフォルダ名と違えば止める（README.md「検査対象のフォルダ」）---
    def test_project_config_is_read_and_its_project_must_match(self):
        self.example_context()
        config = self.root / "projects" / PROJECT / "config.yaml"
        with self.assertRaisesRegex(ValueError, "設定ファイルがありません"):
            project.load_project_config(PROJECT)
        config.write_text(
            f"experiment_name: {PROJECT}\nseed: 1\nproject: {PROJECT}\nplatform: linux/amd64\n"
            "discovery:\n  apt_snapshot: ''\nimage:\n  build: true\n  extra: kept\n",
            encoding="utf-8",
        )
        cfg = project.load_project_config(PROJECT)
        self.assertEqual(
            (cfg["project"], cfg["platform"], cfg["seed"]), (PROJECT, "linux/amd64", 1)
        )
        self.assertEqual(cfg["image"], {"build": True, "extra": "kept"})
        self.assertEqual(project.load_project_config(PROJECT, stage="discovery"), cfg)
        with self.assertRaisesRegex(ValueError, "image.audit"):
            project.load_project_config(PROJECT, stage="image")
        config.write_text(
            config.read_text().replace("build: true", 'build: "true"'), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "image.build"):
            project.load_project_config(PROJECT, stage="image")
        config.write_text(
            config.read_text().replace("linux/amd64", "linux/s390x"), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "platform"):
            project.load_project_config(PROJECT)
        config.write_text("experiment_name: x\nseed: 1\nproject: other\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "プロジェクトのフォルダの名前"):
            project.load_project_config(PROJECT)
        config.write_text(
            f"experiment_name: x\nseed: 1\nproject: {PROJECT}\nflag: yes\n", encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            project.load_project_config(PROJECT)
        contracts = [
            f"../contracts/{path.name}"
            for path in sorted(contract.CONTRACT_DIR.glob("*"))
            if path.is_file()
        ]
        self.assertTrue(contracts)
        self.assertEqual(
            project.report_inputs(PROJECT, "discovery"),
            [
                f"projects/{PROJECT}/request/requirements.txt",
                f"projects/{PROJECT}/request/base.txt",
                f"projects/{PROJECT}/request/ubuntu-packages.txt",
                f"projects/{PROJECT}/config.yaml",
                container.shown_path(project.INSPECT_DOCKERFILE),
                *contracts,
            ],
        )
        with self.assertRaisesRegex(ValueError, "Dockerfile のフォルダがありません"):
            project.report_inputs(PROJECT, "image")
        dockerfile = self.root / "projects" / PROJECT / "dockerfile"
        dockerfile.mkdir()
        (dockerfile / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "image.dependency_lock に書いてください"):
            project.report_inputs(PROJECT, "image")
        with self.assertRaisesRegex(ValueError, "段階は discovery か image"):
            project.report_inputs(PROJECT, "audit")
        lock = "outputs/evidence/x/confirm/dependency-lock.tsv"
        self.assertEqual(
            project.report_inputs(PROJECT, "image", lock),
            [
                f"projects/{PROJECT}/dockerfile/Dockerfile",
                f"projects/{PROJECT}/config.yaml",
                lock,
                *contracts,
            ],
        )

    # --- 工程の処理は、記録層の流れの関数（run・finish・fail）を呼ばない（REPRODUCIBILITY.md 2 節）---
    def test_step_code_does_not_call_the_record_flow(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import precommit_checks

        for path in sorted((ROOT / "src").glob("*.py")):
            if path.name == "repro.py":
                continue
            with self.subTest(path=path.name):
                self.assertEqual(precommit_checks.flow_calls(path.read_text(encoding="utf-8")), [])

    # --- R と Python の記録が同じ台帳にそろう（REPRODUCIBILITY.md 3 節）---
    def test_r_and_python_records_are_gathered_in_one_ledger(self):
        for record in (
            {
                "run_id": "r-test",
                "status": "success",
                "experiment_name": "r-audit",
                "started_at": "2026-01-01T00:00:00Z",
            },
            {
                "run_id": "python-test",
                "status": "success",
                "experiment_name": "audit-python",
                "started_at": "2026-01-02T00:00:00+0900",
                "finished_at": "2026-01-02T00:01:00+0900",
            },
        ):
            directory = self.root / "outputs/repro" / record["run_id"]
            directory.mkdir(parents=True)
            write_json(directory / f"{record['run_id']}_run.json", record)
        ledger = repro.runs()
        self.assertEqual(len(ledger), 2)
        self.assertEqual({row["run_id"] for row in ledger}, {"r-test", "python-test"})
        experiments = {row["run_id"]: row["experiment"] for row in ledger}
        self.assertEqual(experiments["python-test"], "audit-python")

    # --- プロジェクト名は小文字の形だけ、言語は request の中身で決め、R のものは止める（README.md「検査対象のフォルダ」）---
    def test_projects_are_named_and_their_language_comes_from_the_request(self):
        self.example_context()
        self.assertEqual(project.project_folder(PROJECT), self.root / "projects" / PROJECT)
        self.assertEqual(project.project_language(PROJECT), "python")
        self.assertEqual(project.default_image(PROJECT), "example:local")
        for module in (container, evidence_ops, project, discovery_ops, image, archive_ops):
            for name in ("audit_report_id", "report_prefix"):
                self.assertFalse(hasattr(module, name), name)
        for name in ("Example", "-x", "a/b", "", "x y"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "プロジェクト名は小文字の英数字"),
            ):
                project.project_folder(name)
        with self.assertRaisesRegex(ValueError, "プロジェクトの request/ がありません"):
            project.project_language("absent")
        r_request = self.root / "projects/example-r/request"
        r_request.mkdir(parents=True)
        (r_request / "DESCRIPTION").write_text("Package: x\n", encoding="utf-8")
        (r_request / "base.txt").write_text("BASE_IMAGE=x\n", encoding="utf-8")
        self.assertEqual(project.project_language("example-r"), "r")
        with self.assertRaisesRegex(ValueError, "は R のプロジェクトです"):
            discovery_ops.resolve_python_dependencies(
                "example-r", "linux/arm64", apt_snapshot=SNAPSHOT
            )
        (r_request / "requirements.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "どちらか 1 つを置いて"):
            project.project_language("example-r")
        (r_request / "DESCRIPTION").unlink()
        (r_request / "requirements.txt").unlink()
        with self.assertRaisesRegex(ValueError, "どちらか 1 つを置いて"):
            project.project_language("example-r")

    # --- base.txt は KEY=VALUE で読み、知らないキー・重なり・欠け・ダイジェスト無しは止める（README.md「検査対象のフォルダ」）---
    def test_base_file_is_read_strictly(self):
        path = self.root / "base.txt"
        path.write_text(f"# 注釈\n\nBASE_IMAGE={BASE}   # 行末の注釈\n", encoding="utf-8")
        self.assertEqual(project.read_base_file(path), {"BASE_IMAGE": BASE})
        path.write_text(f"BASE_IMAGE = {BASE}\n", encoding="utf-8")
        self.assertEqual(project.read_base_file(path), {"BASE_IMAGE": BASE})
        for text, message in (
            (f"BASE_IMAGE={BASE}\nR_REPOSITORY=https://p3m.dev/x\n", "知らないキーがあります"),
            (f"BASE_IMAGE={BASE}\nBASE_IMAGE={BASE}\n", "キーが重なっています"),
            ("# 何も書かない\n", "BASE_IMAGE がありません"),
            ("BASE_IMAGE=ubuntu:24.04\n", "sha256"),
            ("BASE_IMAGE=python:3.12@sha256:" + "9" * 64 + "\n", "ubuntu"),
            (f"BASE_IMAGE {BASE}\n", "KEY=VALUE"),
        ):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    project.read_base_file(path)

    # --- requirements.txt と base.txt は必須、ubuntu-packages.txt は任意（README.md「検査対象のフォルダ」）---
    def test_target_folder_needs_requirements_and_base(self):
        context = self.example_context(ubuntu=None)
        _, paths = project.target_inputs(PROJECT)
        self.assertEqual(list(paths), ["requirements.txt", "base.txt"])
        self.assertEqual(discovery_ops._ubuntu_text(paths), "")
        self.assertEqual(project.read_package_list(paths.get("ubuntu-packages.txt")), [])
        environment = discovery_ops._lock_environment(paths, "linux/arm64", SNAPSHOT)
        self.assertEqual(environment["base_image"], BASE)
        self.assertEqual(
            set(environment["inputs"]),
            {"requirements.txt", "base.txt", "Dockerfile.inspect"},
        )
        self.assertNotIn("kind", environment)
        (context / "base.txt").unlink()
        with self.assertRaisesRegex(ValueError, "base.txt がありません"):
            project.target_inputs(PROJECT)

    # --- 確定記録が合わないときも FAIL の記録を残し、記録の確定記録は相対パスにする（README.md「結果の共通形式」）---
    def test_lock_mismatch_is_recorded_as_failure_with_relative_path(self):
        lock = self.root / "bad" / "dependency-lock.tsv"
        lock.parent.mkdir()
        lock.write_text("not a lock\n", encoding="utf-8")
        with (
            mock.patch.object(archive_ops, "run_python_image_audit") as run,
            self.assertRaises(ValueError) as caught,
        ):
            archive_ops.audit_python_image("example:test", lock=lock)
        run.assert_not_called()
        evidence = caught.exception.evidence
        summary = json.loads((evidence / "result-summary.json").read_text())
        self.assertEqual(summary["status"], "FAIL")
        self.assertNotIn("base_image", summary)
        self.assertNotIn("input_hashes", summary)
        parameters = recorded_parameters(evidence)
        self.assertEqual(parameters["dependency_lock"], "bad/dependency-lock.tsv")
        self.assertNotIn("kind", parameters)

    # --- 第 3 段階のビルドと監査に確定記録が必要なことを確かめる（README.md「第 3 段階の中身」）---
    def test_third_stage_requires_a_dependency_lock(self):
        with self.assertRaisesRegex(ValueError, "image.dependency_lock に書いてください"):
            archive_ops.audit_python_image("example:test")
        with self.assertRaisesRegex(ValueError, "image.dependency_lock に書いてください"):
            image.build_python_image("example:test", lock="")

    # --- 確定記録は requirements.txt の写しを持ち、第 3 段階の照合は確定記録の中だけで閉じる（README.md「Python」）---
    def test_confirmed_lock_holds_requirements_and_is_verified_on_its_own(self):
        lock = self.example_lock()
        context = self.example_context()
        self.assertEqual((lock.parent / "requirements.txt").read_text(), "example>=1\n")
        listed = [line.split("\t")[0] for line in lock.read_text().splitlines()[1:]]
        self.assertEqual(listed, list(project.LOCK_FILES))
        self.assertIn("requirements.txt", listed)
        environment = json.loads((lock.parent / "build-environment.json").read_text())
        self.assertNotIn("kind", environment)
        (context / "requirements.txt").write_text("other>=9\n", encoding="utf-8")
        (context / "base.txt").unlink()
        verified, saved = image._verify_python_dependency_lock(lock, "linux/amd64")
        self.assertEqual((verified, saved["python_version"]), (lock, "3.12.3"))
        with self.assertRaisesRegex(ValueError, "確定記録のプラットフォームは linux/amd64 で"):
            image._verify_python_dependency_lock(lock, "linux/arm64")
        (lock.parent / "requirements.txt").write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "確定後に記録が変更されました: requirements.txt"):
            image._verify_python_dependency_lock(lock, "linux/amd64")
        lock.write_text(
            "path\tsha256\n"
            + "".join(
                f"{name}\t{container.file_sha256(lock.parent / name)}\n"
                for name in project.LOCK_FILES
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "第 1 段階の入力と一致しません"):
            image._verify_python_dependency_lock(lock, "linux/amd64")

    # --- 確定記録の名前・ハッシュの形・ベース・APT の時点・一覧の見出しを照合する（README.md「第 3 段階の中身」）---
    def test_build_rejects_inconsistent_lock_metadata(self):
        with self.assertRaisesRegex(ValueError, "dependency-lock.tsv を指定"):
            image._verify_python_dependency_lock(self.root / "other.tsv", "linux/amd64")
        lock = self.example_lock()
        records = lock.read_text(encoding="utf-8").splitlines()
        lock.write_text("\n".join([records[0], "", *records[1:]]) + "\n", encoding="utf-8-sig")
        self.assertEqual(image._verify_python_dependency_lock(lock, "linux/amd64")[0], lock)
        self.assertEqual(len(evidence_ops.lock_item_rows(lock)), len(records) - 1)
        lock.write_text(
            "\n".join([records[0], records[1].split("\t")[0] + "\tshort", *records[2:]])
        )
        with self.assertRaisesRegex(ValueError, "形式が不正"):
            image._verify_python_dependency_lock(lock, "linux/amd64")
        environment = lock.parent / "build-environment.json"
        baseline = lock.parent / "base-ubuntu.tsv"
        original = environment.read_text(encoding="utf-8")
        for path, changed, message in (
            (
                environment,
                json.dumps({**json.loads(original), "apt_snapshot": None}),
                "APT スナップショット",
            ),
            (
                environment,
                json.dumps({**json.loads(original), "base_image": "ubuntu:latest"}),
                "BASE_IMAGE",
            ),
            (baseline, baseline.read_text().replace("linux/amd64", "linux/arm64", 1), "ベース一覧"),
        ):
            with self.subTest(message=message):
                environment.write_text(original, encoding="utf-8")
                path.write_text(changed, encoding="utf-8")
                lock.write_text(
                    "path\tsha256\n"
                    + "".join(
                        f"{name}\t{container.file_sha256(lock.parent / name)}\n"
                        for name in project.LOCK_FILES
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, message):
                    image._verify_python_dependency_lock(lock, "linux/amd64")

    # --- 監査は確定記録の中のファイルだけで条件を作り、照合を 3 行に残す（uv は外から渡す）（README.md「Python」）---
    def test_audit_uses_only_the_dependency_lock(self):
        lock = self.example_lock()
        (self.example_context() / "requirements.txt").write_text("other\n", encoding="utf-8")
        captured = {}

        def run(image, platform, directory, config, record):
            captured.update(config)
            (directory / "python-audit.json").write_text('{"status": "PASS"}', encoding="utf-8")
            return {"report": str(directory / "python-audit.json"), "status": "PASS"}

        with mock.patch.object(archive_ops, "run_python_image_audit", side_effect=run):
            result = archive_ops.audit_python_image("example:test", "linux/amd64", lock=lock)
        self.assertEqual(captured["requirements"], ["example>=1"])
        self.assertNotIn("uv", captured)
        self.assertEqual(
            captured["baseline"], [{"package": "bash", "architecture": "amd64", "version": "5.2"}]
        )
        self.assertEqual(captured["confirmed"]["packages"], {"example": "1.0"})
        self.assertEqual(captured["confirmed"]["hashes"], {"example": f"1.0 {'a' * 64}"})
        self.assertEqual([row["package"] for row in captured["ubuntu"]], ["libgomp1"])
        directory = Path(result["evidence"])
        self.assertEqual(
            (directory / "baseline/base-ubuntu.tsv").read_text(encoding="utf-8").splitlines(),
            [
                f"# {BASE} linux/amd64",
                "Package\tArchitecture\tVersion\tStatus",
                "bash\tamd64\t5.2\tinstalled",
            ],
        )
        self.assertEqual(
            read_checks(directory),
            [("Dependency lock", "PASS"), ("Base inventory", "PASS"), ("Audit checks", "PASS")],
        )
        summary = json.loads((directory / "result-summary.json").read_text())
        environment = json.loads((lock.parent / "build-environment.json").read_text())
        self.assertEqual(summary["base_image"], BASE)
        self.assertEqual(summary["input_hashes"], environment["inputs"])
        parameters = recorded_parameters(directory)
        self.assertEqual(
            set(parameters), {"image", "platform", "dependency_lock", "base_image", "input_hashes"}
        )

    # --- 工程は outputs/evidence/<実行 ID>/<工程>/ に証跡を書き、REVIEW も結果に残す（README.md「結果の共通形式」）---
    def test_review_is_recorded_with_common_summary(self):
        lock = self.example_lock()

        def run(image, platform, directory, config, record):
            report = directory / "python-audit.json"
            report.write_text('{"status":"REVIEW"}', encoding="utf-8")
            return {"report": str(report), "status": "REVIEW", "review": "要確認: x"}

        with mock.patch.object(archive_ops, "run_python_image_audit", side_effect=run):
            result = archive_ops.audit_python_image(
                "example:test", "linux/amd64", lock=lock, run_id="report_0001"
            )
        directory = Path(result["evidence"])
        self.assertEqual(directory, self.root / "outputs/evidence/report_0001/audit")
        self.assertEqual(result["run_id"], "report_0001")
        summary = json.loads((directory / "result-summary.json").read_text())
        self.assertEqual((summary["status"], summary["message"]), ("REVIEW", "要確認: x"))
        self.assertEqual((summary["language"], summary["operation"]), ("python", "audit-python"))
        self.assertTrue(
            {
                "python-audit.json",
                "checks.tsv",
                "tool-versions.tsv",
                "parameters.json",
                "inputs.tsv",
            }
            <= set(summary["artifacts"])
        )
        self.assertFalse((self.root / "outputs/repro").exists())
        self.assertEqual(evidence_ops.operation_step("audit-python-archive"), "audit-archive")
        self.assertEqual(evidence_ops.operation_step("audit-python-sif"), "audit-sif")
        self.assertEqual(evidence_ops.operation_step("resolve-python"), "discover")
        self.assertEqual(evidence_ops.operation_step("save-python-sif"), "sif")
        with self.assertRaises(ValueError):
            evidence_ops.operation_step("discover-python")
        for run_id in (None, "", "a/b"):
            with self.subTest(run_id=run_id), self.assertRaisesRegex(ValueError, "実行 ID"):
                evidence_ops.run_operation(
                    "audit-python", {}, [], lambda directory, details: {}, run_id=run_id
                )

    # --- 失敗した操作でも FAIL の結果を残し、記録は failed にする（README.md「結果の共通形式」）---
    def test_failed_operation_retains_failure_summary(self):
        lock = self.example_lock()
        with (
            mock.patch.object(
                archive_ops, "run_python_image_audit", side_effect=RuntimeError("test failure")
            ),
            self.assertRaisesRegex(RuntimeError, "test failure") as caught,
        ):
            archive_ops.audit_python_image("example:test", "linux/amd64", lock=lock)
        summary_file = caught.exception.evidence / "result-summary.json"
        summary = json.loads(summary_file.read_text())
        self.assertEqual(summary["status"], "FAIL")
        self.assertEqual(summary["message"], "test failure")
        self.assertFalse(list(self.root.rglob("*_run.json")))

    # --- 報告書の実行 ID の証跡のフォルダを使い、同じ工程の証跡はもう一度書けない（REPRODUCIBILITY.md 2 節）---
    def test_supplied_report_step_id_is_kept_and_cannot_be_reused(self):
        lock = self.example_lock()
        run_id = "20260101_000000_python-image-build_0001"
        with mock.patch.object(
            archive_ops, "run_python_image_audit", return_value={"status": "PASS"}
        ) as worker:
            result = archive_ops.audit_python_image(
                "example:test", "linux/amd64", lock=lock, run_id=run_id
            )
            self.assertEqual(Path(result["evidence"]).parent.name, run_id)
            self.assertEqual(result["run_id"], run_id)
            with self.assertRaises(FileExistsError):
                archive_ops.audit_python_image(
                    "example:test", "linux/amd64", lock=lock, run_id=run_id
                )
            worker.assert_called_once()

    # --- 第 3 段階のビルドは dockerfile/ をコンテキストにし、--build-arg を渡さず、確定記録を前後で照合する（README.md「第 3 段階の中身」）---
    def test_build_uses_the_dockerfile_folder_as_its_context(self):
        lock = self.example_lock()
        folder = self.root / "projects" / PROJECT / "dockerfile"
        (folder / "sub").mkdir(parents=True)
        (folder / ".git").mkdir()
        (folder / "Dockerfile").write_text("FROM example\n", encoding="utf-8")
        (folder / "pylock.toml").write_text(pylock(), encoding="utf-8")
        (folder / ".dockerignore").write_text("*.md\n", encoding="utf-8")
        (folder / "sub/notes.txt").write_text("note\n", encoding="utf-8")
        (folder / ".git/config").write_text("[core]\n", encoding="utf-8")
        sent = []
        with (
            mock.patch.object(image, "run_docker", side_effect=lambda args, log: sent.append(args)),
            mock.patch.object(image.container, "local_image_id", return_value="sha256:" + "c" * 64),
        ):
            result = image.build_python_image("example:test", PROJECT, "linux/amd64", lock=lock)
        self.assertEqual(result["image_id"], "sha256:" + "c" * 64)
        self.assertEqual(
            sent[0],
            [
                "buildx",
                "build",
                "--load",
                "--platform",
                "linux/amd64",
                "--tag",
                "example:test",
                "--file",
                str(folder / "Dockerfile"),
                str(folder),
            ],
        )
        directory = Path(result["evidence"])
        self.assertEqual(
            read_checks(directory),
            [
                ("Dependency lock", "PASS"),
                ("Image build", "PASS"),
                ("Dependency lock after build", "PASS"),
                ("Image platform", "PASS"),
            ],
        )
        recorded = recorded_inputs(directory)
        for path in (
            folder / "Dockerfile",
            folder / ".dockerignore",
            folder / "sub/notes.txt",
            lock,
            lock.parent / "requirements.txt",
        ):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(recorded[path.relative_to(self.root).as_posix()], digest)
        self.assertNotIn("projects/example/dockerfile/.git/config", recorded)
        parameters = recorded_parameters(directory)
        self.assertEqual(
            parameters,
            {
                "image": "example:test",
                "project": PROJECT,
                "platform": "linux/amd64",
                "dependency_lock": lock.relative_to(self.root).as_posix(),
            },
        )
        (folder / "Dockerfile").unlink()
        with self.assertRaisesRegex(ValueError, "Dockerfile のフォルダに Dockerfile がありません"):
            image.build_python_image("example:test", PROJECT, "linux/amd64", lock=lock)

    # --- 確定記録が別のプラットフォームのものなら、docker を呼ばずに FAIL を残して止める（README.md「Python」）---
    def test_build_stops_before_building_with_a_lock_for_another_platform(self):
        lock = self.example_lock()
        folder = self.root / "projects" / PROJECT / "dockerfile"
        folder.mkdir(parents=True)
        (folder / "Dockerfile").write_text("FROM example\n", encoding="utf-8")
        with (
            mock.patch.object(image, "run_docker") as docker,
            mock.patch.object(image.container, "local_image_id") as inspect_image,
            self.assertRaisesRegex(ValueError, "linux/arm64 ではありません") as caught,
        ):
            image.build_python_image("example:test", PROJECT, "linux/arm64", lock=lock)
        docker.assert_not_called()
        inspect_image.assert_not_called()
        self.assertEqual(
            read_checks(caught.exception.evidence),
            [("Dependency lock", "FAIL")],
        )
        summary = json.loads((caught.exception.evidence / "result-summary.json").read_text())
        self.assertEqual(summary["status"], "FAIL")

    # --- 名前を省いたビルドは <プロジェクト名>:local にし、確定記録をビルドの前後に同じプラットフォームで照合する（README.md「第 3 段階の中身」）---
    def test_build_defaults_to_the_project_image_and_verifies_the_lock_twice(self):
        lock = self.example_lock()
        folder = self.root / "projects" / PROJECT / "dockerfile"
        folder.mkdir(parents=True)
        (folder / "Dockerfile").write_text("FROM example\n", encoding="utf-8")
        verified = []
        sent = []

        def verify(path, platform):
            verified.append(platform)
            return path, {}

        with (
            mock.patch.object(image, "_verify_python_dependency_lock", side_effect=verify),
            mock.patch.object(image, "run_docker", side_effect=lambda args, log: sent.append(args)),
            mock.patch.object(image.container, "local_image_id", return_value="sha256:" + "c" * 64),
        ):
            result = image.build_python_image(None, PROJECT, lock=lock)
            with self.assertRaises(ValueError):
                image.build_python_image(None, "missing", lock=lock)
        self.assertEqual(result["image"], f"{PROJECT}:local")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][sent[0].index("--tag") + 1], f"{PROJECT}:local")
        self.assertEqual(verified, ["linux/arm64", "linux/arm64"])

    # --- 監査のコンテナはネットワークなし・操作側の uid:gid で動かし、記録先・スクリプト・uv だけをつなぐ（README.md「Python」）---
    def test_image_audit_runs_without_network_as_the_operator(self):
        directory = self.root / "audit"
        directory.mkdir()
        uv = self.root / "uv"
        uv.write_text("uv\n", encoding="utf-8")
        config = {"requirements": ["example>=1"], "platform": "linux/amd64"}
        image_id = "sha256:" + "d" * 64
        record = {}
        sent = []

        def docker(args, log):
            sent.append(args)
            write_json(directory / "python-audit.json", {"status": "PASS"})

        with (
            mock.patch.object(image, "run_docker", side_effect=docker),
            mock.patch.object(image.container, "local_image_id", return_value=image_id),
            mock.patch.object(image, "_uv_binary", return_value=uv),
            mock.patch.object(image, "wait_for_bind_sources") as wait,
        ):
            result = image.run_python_image_audit(
                "example:test", "linux/amd64", directory, config, record
            )
        self.assertEqual(len(sent), 1)
        args = sent[0]
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertEqual(args[args.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        mounts = [args[index + 1] for index, arg in enumerate(args) if arg == "--mount"]
        self.assertEqual(
            mounts,
            [
                container.bind_mount(directory, "/report"),
                container.bind_mount(image.INSPECT_SCRIPT, "/audit/script", True),
                container.bind_mount(uv, "/audit/uv", True),
            ],
        )
        self.assertIn(image_id, args)
        wait.assert_called_once()
        self.assertEqual(wait.call_args.args[0], [directory, image.INSPECT_SCRIPT, uv])
        self.assertEqual(
            json.loads((directory / "python-input.json").read_text(encoding="utf-8")), config
        )
        self.assertEqual(record["image_id"], image_id)
        self.assertEqual((result["status"], result["review"]), ("PASS", ""))

    # --- 調査は requirements.txt の要件を解決した版と照合し、共有ライブラリの表を記録先の直下に置く（README.md「Python」）---
    def test_discovery_checks_requirements_and_keeps_native_libraries_at_the_top(self):
        context = self.example_context(ubuntu=None)
        (context / "requirements.txt").write_text(
            "example>=1\n"
            "transitive; platform_release != 'audit-target' or implementation_version != '9.5.0'\n",
            encoding="utf-8",
        )
        sent = []

        def stage(base, platform, destination, log, files, *, apt_snapshot):
            sent.append((files["ubuntu-packages.txt"], apt_snapshot))
            destination.mkdir(parents=True)
            lock = pylock()
            extra = lock[lock.index("[[packages]]") :].replace("Example", "transitive")
            (destination / "pylock.toml").write_text(
                lock + extra.replace("example-1.0", "transitive-1.0"), encoding="utf-8"
            )
            for name in ("resolve-status", "python-status", "ubuntu-status"):
                (destination / name).write_text("0\n", encoding="utf-8")
            (destination / "base-ubuntu.tsv").write_text(
                "bash\tarm64\t5.2\tinstalled\n", encoding="utf-8"
            )
            (destination / "installed-ubuntu.tsv").write_text(
                "bash\tarm64\t5.2\tinstalled\n", encoding="utf-8"
            )
            write_json(
                destination / "python-environment.json",
                {
                    **PYTHON_ENVIRONMENT,
                    "platform_machine": "aarch64",
                    "platform_release": "audit-target",
                    "implementation_version": "9.5.0",
                },
            )
            write_json(
                destination / "native-plan.json",
                {"missing": {}, "providers": {}, "used_by": {}, "review": []},
            )
            (destination / "native-libraries.tsv").write_text(
                '"language_package"\t"file"\t"soname"\t"path"\n', encoding="utf-8"
            )

        with (
            mock.patch.object(discovery_ops, "_run_inspect_build", side_effect=stage),
            mock.patch.object(discovery_ops, "run_docker") as pull,
            mock.patch.object(
                discovery_ops.container, "local_image_id", return_value="sha256:" + "b" * 64
            ) as inspect_image,
        ):
            result = discovery_ops.resolve_python_dependencies(
                PROJECT, "linux/arm64", apt_snapshot=SNAPSHOT
            )
        pull.assert_called_once()
        inspect_image.assert_called_once()
        self.assertEqual(sent, [("", SNAPSHOT)])
        directory = Path(result["directory"])
        self.assertEqual(
            (directory / "discovery.log").read_text().splitlines()[:3],
            [f"Base image {BASE}", "Platform linux/arm64", f"APT snapshot {SNAPSHOT}"],
        )
        self.assertEqual(
            (directory / "apt-snapshot.txt").read_text(encoding="utf-8"), SNAPSHOT + "\n"
        )
        self.assertEqual(
            read_checks(directory),
            [
                ("Python dependency resolution", "PASS"),
                ("Ubuntu dependency plan", "PASS"),
                ("Python requirements", "PASS"),
                ("Build inputs unchanged", "PASS"),
            ],
        )
        with (directory / "check-items.tsv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE))
        self.assertIn(
            {
                "check": "Python requirements",
                "item": "example",
                "expected": ">=1",
                "found": "1.0",
                "status": "PASS",
            },
            rows,
        )
        self.assertIn("base.txt", [row["item"] for row in rows])
        self.assertTrue((directory / "native-libraries.tsv").is_file())
        environment = json.loads((directory / "build-environment.json").read_text())
        self.assertEqual((environment["base_image"], environment["apt_snapshot"]), (BASE, SNAPSHOT))
        self.assertNotIn("kind", environment)
        packages = discovery_ops.read_table_tsv(directory / "python-packages.tsv")
        self.assertEqual(
            {row["package"]: row["requested"] for row in packages},
            {"Example": "TRUE", "transitive": "FALSE"},
        )
        confirmed = discovery_ops.confirm_python_dependencies(result, PROJECT, "linux/arm64")
        self.assertEqual(confirmed["status"], "PASS")
        (context / "requirements.txt").write_text("example>=2\n", encoding="utf-8")
        with (
            mock.patch.object(discovery_ops, "_run_inspect_build", side_effect=stage),
            mock.patch.object(discovery_ops, "run_docker") as pull,
            mock.patch.object(
                discovery_ops.container, "local_image_id", return_value="sha256:" + "b" * 64
            ) as inspect_image,
            self.assertRaisesRegex(ValueError, "解決した版が一致しません") as caught,
        ):
            discovery_ops.resolve_python_dependencies(PROJECT, "linux/arm64")
        pull.assert_called_once()
        inspect_image.assert_called_once()
        failed = caught.exception.evidence
        self.assertIn(("Python requirements", "FAIL"), read_checks(failed))

    # --- BOM付きの記録からCPUを決め、共有ライブラリの調査失敗は根拠を受け取るまで確定しない（README.md「第 1 段階の中身」）---
    def test_native_inspection_failure_requires_review_before_lock(self):
        detail = "ldd failed for /opt/lib/test.so: read error"
        saved, _, _ = self.example_discovery(
            self.example_context(), native={"inspection_review": [detail]}
        )
        environment = saved / "build-environment.json"
        environment.write_bytes(b"\xef\xbb\xbf" + environment.read_bytes())
        pending = discovery_ops.confirm_python_dependencies(saved, PROJECT)
        self.assertEqual(
            (pending["status"], pending["lock"], pending["unresolved"]), ("REVIEW", None, 1)
        )
        notes = self.root / "review-notes.tsv"
        reason = '  checked\t"both"  '
        with notes.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, dialect="excel-tab")
            writer.writerow(["item", "detail", "reason"])
            writer.writerow(["/opt/lib/test.so", detail, reason])
        confirmed = discovery_ops.confirm_python_dependencies(saved, PROJECT, review_notes=notes)
        self.assertEqual(confirmed["status"], "PASS")
        rows = discovery_ops.read_table_tsv(
            Path(confirmed["lock"]).parent / "dependency-review.tsv"
        )
        self.assertEqual(rows, [{"item": "/opt/lib/test.so", "detail": detail, "decision": reason}])

    # --- 調査の環境で失敗した段は、状態のファイルから FAIL の行を書いてから止める（R と同じ）（README.md「第 1 段階の中身」）---
    def test_discovery_records_the_failed_inspection_step(self):
        self.example_context(ubuntu=None)
        cases = [
            ({"python-status": "", "ubuntu-status": "100"}, [("Ubuntu dependency plan", "FAIL")]),
            (
                {"python-status": "1", "ubuntu-status": "100"},
                [("Python dependency resolution", "FAIL")],
            ),
            (
                {"python-status": "1", "ubuntu-status": "0"},
                [("Python dependency resolution", "FAIL")],
            ),
            (
                {"python-status": "0", "ubuntu-status": "100", "resolve-status": "0"},
                [("Python dependency resolution", "PASS"), ("Ubuntu dependency plan", "FAIL")],
            ),
        ]
        for statuses, expected in cases:
            with self.subTest(statuses=statuses):

                def stage(
                    base, platform, destination, log, files, *, apt_snapshot, statuses=statuses
                ):
                    destination.mkdir(parents=True)
                    (destination / "pylock.toml").write_text(pylock(), encoding="utf-8")
                    for name, value in statuses.items():
                        (destination / name).write_text(value + "\n", encoding="utf-8")

                with (
                    mock.patch.object(discovery_ops, "_run_inspect_build", side_effect=stage),
                    mock.patch.object(discovery_ops, "run_docker"),
                    mock.patch.object(
                        discovery_ops.container, "local_image_id", return_value="sha256:" + "b" * 64
                    ),
                    self.assertRaisesRegex(RuntimeError, "の依存の調査に失敗しました") as caught,
                ):
                    discovery_ops.resolve_python_dependencies(
                        PROJECT, "linux/arm64", apt_snapshot=SNAPSHOT
                    )
                self.assertEqual(read_checks(caught.exception.evidence), expected)

    # --- APT スナップショットの形が誤りなら、調査は docker を呼ばずに止める（README.md「第 1 段階の中身」）---
    def test_discovery_stops_on_a_malformed_apt_snapshot(self):
        self.example_context(ubuntu=None)
        with (
            mock.patch.object(discovery_ops, "_run_inspect_build") as build,
            mock.patch.object(discovery_ops, "run_docker") as docker,
            self.assertRaisesRegex(ValueError, "YYYYMMDDTHHMMSSZ"),
        ):
            discovery_ops.resolve_python_dependencies(PROJECT, "linux/arm64", apt_snapshot="bad")
        build.assert_not_called()
        docker.assert_not_called()

    # --- プラットフォームは linux/arm64 か linux/amd64 だけ（README.md「検査対象のフォルダ」）---
    def test_platform_is_arm64_or_amd64_only(self):
        self.assertEqual(container.validate_platform("linux/arm64"), "linux/arm64")
        with self.assertRaises(ValueError):
            container.validate_platform("linux/s390x")

    # --- ベース一覧の 1 行目からプラットフォームを読み、違えば止める（README.md「Python」）---
    def test_base_inventory_platform_must_match_its_first_line(self):
        path = self.root / "base-ubuntu.tsv"
        path.write_text(
            f"# {BASE} linux/arm64\nPackage\tArchitecture\tVersion\tStatus\n"
            "bash\tarm64\t5.2\tinstalled\n",
            encoding="utf-8",
        )
        self.assertEqual(
            discovery_ops.read_base_inventory(path, BASE, "linux/arm64"),
            [{"package": "bash", "architecture": "arm64", "version": "5.2"}],
        )
        with self.assertRaisesRegex(ValueError, "一覧の形式と一致しません"):
            discovery_ops.read_base_inventory(path, BASE, "linux/amd64")
        header = f"# {BASE} linux/arm64\nPackage\tArchitecture\tVersion\tStatus\n"
        path.write_text(header, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "一覧の形式と一致しません"):
            discovery_ops.read_base_inventory(path, BASE, "linux/arm64")
        path.write_text(header + "bash\tarm64\t5.2\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "3行"):
            discovery_ops.read_base_inventory(path, BASE, "linux/arm64")

    # --- requirements.txt の注釈を除き、pip のオプションは止める（README.md「Python」）---
    def test_requirements_and_invalid_options(self):
        path = self.root / "requirements.txt"
        path.write_text('requests>=2; python_version >= "3.8"\n# comment\n', encoding="utf-8")
        self.assertEqual(project.read_requirements(path), ['requests>=2; python_version >= "3.8"'])
        path.write_text("-r other.txt\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            project.read_requirements(path)

    # --- イメージの ID を決めた後は、プラットフォームの確認にも同じ ID を使う（README.md「結果の共通形式」）---
    def test_image_selection_uses_the_same_immutable_reference_for_both_checks(self):
        image_id = "sha256:" + "a" * 64
        results = [
            subprocess.CompletedProcess([], 0, image_id + "\n", ""),
            subprocess.CompletedProcess([], 0, "linux/amd64\n", ""),
        ]
        with mock.patch.object(container.subprocess, "run", side_effect=results) as inspect:
            self.assertEqual(container.local_image_id("example:test", "linux/amd64"), image_id)
        self.assertEqual(inspect.call_args_list[0].args[0][-1], "example:test")
        self.assertEqual(inspect.call_args_list[1].args[0][-1], image_id)

    # --- bind の指定は source の引用符を二重にし、読み取り専用なら readonly を付ける（README.md「構成」）---
    def test_bind_mount_doubles_quotes_and_adds_readonly(self):
        source = self.root / 'a"b'
        self.assertEqual(
            container.bind_mount(source, "/project", True),
            f'type=bind,"source={self.root}/a""b",destination=/project,readonly',
        )
        self.assertEqual(
            container.bind_mount(source, "/project"),
            f'type=bind,"source={self.root}/a""b",destination=/project',
        )

    # --- 記録に書くパスは、根の中なら根からの相対パス、外なら受け取ったまま（README.md「結果の共通形式」）---
    def test_shown_path_is_relative_inside_the_root(self):
        outside = self.root.parent / "outside.txt"
        self.assertEqual(container.shown_path(self.root / "projects/x.txt"), "projects/x.txt")
        self.assertEqual(container.shown_path(outside), str(outside))
        self.assertIsNone(container.shown_path(None))

    # --- Docker の共有確認は、根の外のパスなら docker を呼ばずに止める（README.md「構成」）---
    def test_wait_for_bind_sources_stops_outside_the_root(self):
        outside = self.root.parent
        with mock.patch.object(container, "run_docker") as docker:
            for path in (outside, self.root):
                with self.assertRaisesRegex(ValueError, "プロジェクト内のパス"):
                    container.wait_for_bind_sources(
                        [path], "example:test", "linux/arm64", self.root / "wait.log"
                    )
            with self.assertRaises(FileNotFoundError):
                container.wait_for_bind_sources(
                    ["absent"], "example:test", "linux/arm64", self.root / "wait.log"
                )
            docker.assert_not_called()
            container.wait_for_bind_sources(
                ["src", self.root / "src"], "example:test", "linux/arm64", self.root / "wait.log"
            )
            self.assertEqual(docker.call_args.args[0].count("src"), 1)

    # --- 明示した環境変数を上書きし、ほかの環境変数を引き継ぐ（README.md「構成」）---
    def test_docker_inherits_environment_when_overriding_a_variable(self):
        with (
            mock.patch.dict(os.environ, {"AUDIT_TEST_KEEP": "keep", "AUDIT_TEST_CHANGE": "old"}),
            mock.patch.object(container.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            container.run_docker(
                ["version"], self.root / "run.log", env={"AUDIT_TEST_CHANGE": "new"}
            )
            env = run.call_args.kwargs["env"]
        self.assertEqual((env["AUDIT_TEST_KEEP"], env["AUDIT_TEST_CHANGE"]), ("keep", "new"))

    # --- バイト数と SHA-256 の説明は「<バイト数> バイト、SHA-256 <ハッシュ>」の文（README.md「結果の共通形式」）---
    def test_size_and_sha256_describes_the_file(self):
        path = self.root / "a.txt"
        path.write_bytes(b"abc")
        self.assertEqual(
            container.size_and_sha256(path),
            "3 バイト、SHA-256 ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )

    # --- 日時・照合結果・直接要求したパッケージを確定時に確かめる（README.md「第 1 段階の中身」）---
    def test_confirmation_checks_recorded_discovery_files(self):
        context = self.example_context()
        for name, content, message in (
            ("apt-snapshot.txt", "20000101T000000Z\n", "APT スナップショットが一致しません"),
            ("checks.tsv", "check\tstatus\tdetail\nUbuntu dependency plan\tFAIL\t\n", "完了済み"),
            ("base-ubuntu.tsv", "# 別のイメージ\n", "ベース一覧が固定イメージ"),
            ("python-packages.tsv", "package\tversion\trequested\tsha256\n", "調査結果の対象"),
        ):
            with self.subTest(file=name):
                discovery, environment, evidence = self.example_discovery(context)
                (discovery / name).write_text(content, encoding="utf-8")
                self.rewrite_discovery(discovery, environment, evidence)
                with self.assertRaisesRegex(ValueError, message):
                    discovery_ops.confirm_python_dependencies({"directory": discovery}, PROJECT)

    # --- 確定記録を作り、wheel を確かめられなければ REVIEW にし、調査のあとで入力が変われば止める（README.md「Python」）---
    def test_confirmation_writes_the_lock_and_reviews_unverifiable_wheels(self):
        context = self.example_context()
        discovery, environment, evidence = self.example_discovery(
            context,
            [
                {
                    "package": "libgomp1",
                    "architecture": "amd64",
                    "version": "1:2~ubuntu.1",
                    "action": "install",
                }
            ],
        )
        result = discovery_ops.confirm_python_dependencies(discovery, PROJECT, "linux/amd64")
        lock = Path(result["lock"])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(
            read_checks(Path(result["evidence"])),
            [
                ("Discovery platform", "PASS"),
                ("Discovery unchanged", "PASS"),
                ("Resolution architecture", "PASS"),
                ("Pinned package hashes", "PASS"),
                ("Python requirements", "PASS"),
                ("Review items resolved", "PASS"),
            ],
        )
        self.assertEqual((result["language_packages"], result["ubuntu_packages"]), (1, 1))
        self.assertEqual((lock.parent / "ubuntu-pins.txt").read_text(), "libgomp1=1:2~ubuntu.1\n")
        self.assertEqual((lock.parent / "pylock.toml").read_text(), pylock())
        self.assertEqual(
            (lock.parent / "python-pins.tsv").read_text(),
            f"package\tversion\tsha256\nexample\t1.0\t{'a' * 64}\n",
        )
        self.assertEqual(
            set(project.LOCK_FILES),
            {
                "build-environment.json",
                "dependency-review.tsv",
                "requirements.txt",
                "ubuntu-pins.txt",
                "ubuntu-packages.tsv",
                "base-ubuntu.tsv",
                "python-packages.tsv",
                "python-pins.tsv",
                "pylock.toml",
            },
        )
        self.assertEqual(
            discovery_ops.dockerfile_input_rows(lock, PROJECT),
            [
                {"item": "project", "value": PROJECT},
                {"item": "base_image", "value": BASE},
                {"item": "platform", "value": "linux/amd64"},
                {"item": "apt_snapshot", "value": SNAPSHOT},
                {"item": "python_version", "value": "3.12.3"},
                {"item": "dependency_lock", "value": lock.relative_to(self.root).as_posix()},
                {
                    "item": "ubuntu_pins",
                    "value": (lock.parent / "ubuntu-pins.txt").relative_to(self.root).as_posix(),
                },
                {
                    "item": "language_pins",
                    "value": (lock.parent / "python-pins.tsv").relative_to(self.root).as_posix(),
                },
                {
                    "item": "pylock",
                    "value": (lock.parent / "pylock.toml").relative_to(self.root).as_posix(),
                },
            ],
        )
        (discovery / "resolution/pylock.toml").write_text(
            pylock("https://example.invalid/example-1.0.tar.gz"), encoding="utf-8"
        )
        self.rewrite_discovery(discovery, environment, evidence)
        review = discovery_ops.confirm_python_dependencies(discovery, PROJECT, "linux/amd64")
        self.assertEqual(review["status"], "REVIEW")
        self.assertIsNone(review["lock"])
        checks = read_checks(Path(review["evidence"]))
        self.assertIn(("Pinned package hashes", "REVIEW"), checks)
        self.assertEqual(checks[-1], ("Review items resolved", "REVIEW"))
        (context / "base.txt").write_text(
            "BASE_IMAGE=ubuntu:24.04@sha256:" + "8" * 64 + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "調査の記録が変わっています"):
            discovery_ops.confirm_python_dependencies(discovery, PROJECT, "linux/amd64")

    # --- 解決した版が要件を満たさなければ止め、当てはまらない環境マーカーの要件は見ない（README.md「Python」）---
    def test_requirements_are_checked_against_resolved_versions(self):
        environment = {"python_full_version": "3.12.3", "platform_machine": "aarch64"}
        pins = {"example-pkg": "1.5", "other": "2.0"}
        self.assertEqual(
            discovery_ops._check_requirements(
                ["Example_Pkg>=1,<2", "other", "absent; platform_machine == 'x86_64'"],
                pins,
                environment,
            ),
            3,
        )
        with self.assertRaisesRegex(ValueError, "example-pkg>=2"):
            discovery_ops._check_requirements(["example-pkg>=2"], pins, environment)
        with self.assertRaisesRegex(ValueError, "absent"):
            discovery_ops._check_requirements(["absent"], pins, environment)
        rows = []
        with self.assertRaises(ValueError):
            discovery_ops._check_requirements(
                ["Example_Pkg>=2", "absent; platform_machine == 'x86_64'"],
                pins,
                environment,
                rows.extend,
            )
        self.assertEqual(
            rows,
            [
                ("Example_Pkg", ">=2", "1.5", "FAIL"),
                ("absent", '; platform_machine == "x86_64"', None, "PASS"),
            ],
        )

    # --- 1 つずつ確かめた項目は check-items.tsv に足し、契約に無い名前は止める（README.md「結果の共通形式」）---
    def test_operation_check_items_are_appended_and_names_come_from_the_contract(self):
        items = evidence_ops.operation_check_items(self.root, "confirm")
        items("Pinned package hashes", [("pkg 1.0", "", "x\ty", "FAIL")])
        items(
            "Discovery unchanged",
            evidence_ops.hash_item_rows({"a": "1", "gone": "2"}, {"a": "1", "new": "3"}, "input:"),
        )
        with self.assertRaisesRegex(ValueError, "operation-checks"):
            items("Image build", [("x", "", "")])
        self.assertEqual(
            (self.root / "check-items.tsv").read_text(encoding="utf-8"),
            "check\titem\texpected\tfound\tstatus\n"
            "Pinned package hashes\tpkg 1.0\t\tx y\tFAIL\n"
            "Discovery unchanged\tinput:a\t1\t1\tPASS\n"
            "Discovery unchanged\tinput:gone\t2\t\tFAIL\n"
            "Discovery unchanged\tinput:new\t\t3\tFAIL\n",
        )
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        (self.root / "b.txt").write_text("two\n", encoding="utf-8")
        lock = self.root / "dependency-lock.tsv"
        digest = container.file_sha256(self.root / "a.txt")
        lock.write_text(f"path\tsha256\na.txt\t{digest}\nb.txt\t{digest}\n", encoding="utf-8")
        self.assertEqual(
            [
                (name, expected == current)
                for name, expected, current in evidence_ops.lock_item_rows(lock)
            ],
            [("a.txt", True), ("b.txt", False)],
        )
        self.assertEqual(evidence_ops.lock_item_rows(self.root / "none.tsv"), [])

    # --- 検査用の中では、使った検査用のイメージと定義のハッシュを道具の版に残し、外では残さない（README.md「本番の手順」）---
    def test_runner_rows_name_the_runner_image(self):
        with mock.patch.dict(os.environ, {"AUDIT_RUNNER": ""}):
            self.assertEqual(evidence_ops._runner_rows(), [])
        values = {
            "AUDIT_RUNNER": "python",
            "AUDIT_RUNNER_IMAGE": "container-audit-runner-python:local",
            "AUDIT_RUNNER_IMAGE_ID": "sha256:" + "a" * 64,
            "AUDIT_RUNNER_DEFINITION": "b" * 64,
        }
        with mock.patch.dict(os.environ, values):
            self.assertEqual(
                evidence_ops._runner_rows(),
                [
                    ("runner image", values["AUDIT_RUNNER_IMAGE"]),
                    ("runner image id", values["AUDIT_RUNNER_IMAGE_ID"]),
                    ("runner definition", values["AUDIT_RUNNER_DEFINITION"]),
                ],
            )

    # --- 工程の照合は checks.tsv に 1 行ずつ足し、失敗は FAIL を書いてから止め、契約に無い名前は止める（README.md「結果の共通形式」）---
    def test_operation_checks_are_appended_and_names_come_from_the_contract(self):
        check = evidence_ops.operation_checks(self.root, "build")
        self.assertEqual(check("Image platform", lambda: "sha256:x", detail=str), "sha256:x")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            check("Image build", lambda: (_ for _ in ()).throw(RuntimeError("boom\tagain")))
        with self.assertRaisesRegex(ValueError, "operation-checks"):
            check("Not a contract check")
        self.assertEqual(
            (self.root / "checks.tsv").read_text(encoding="utf-8"),
            "check\tstatus\tdetail\nImage platform\tPASS\tsha256:x\n"
            "Image build\tFAIL\tboom again\n",
        )
        self.assertEqual(evidence_ops.worst_status(["PASS", "REVIEW"]), "REVIEW")
        self.assertEqual(evidence_ops.worst_status(["REVIEW", "FAIL"]), "FAIL")
        self.assertEqual(evidence_ops.worst_status([]), "PASS")
        self.assertEqual(
            evidence_ops.status_detail(["PASS", "PASS", "FAIL"]), "PASS 2、REVIEW 0、FAIL 1"
        )

    # --- .tar を outputs/images/<実行 ID>/ に保存してハッシュを記録し、不正な保存先は止める（README.md「第 3 段階の中身」）---
    def test_save_writes_archive_in_images_and_records_its_hash(self):
        image_id = "sha256:" + "d" * 64

        def docker(args, log):
            Path(args[args.index("--output") + 1]).write_bytes(b"archive")
            self.assertEqual(args[:4], ["image", "save", "--platform", "linux/arm64"])

        readback = mock.Mock(return_value="audit-readback/example_app_test:arm64 sha256:x、層 1")
        with (
            mock.patch.object(archive_ops, "run_docker", side_effect=docker),
            mock.patch.object(archive_ops.container, "local_image_id", return_value=image_id),
            mock.patch.object(archive_ops, "_readback_archive", readback),
        ):
            result = archive_ops.save_python_image(
                "example/app:test", platform="linux/arm64", run_id="report_0001"
            )
            archive = self.root / "outputs/images/report_0001/example_app_test_arm64.tar"
            self.assertEqual(
                recorded_parameters(result["evidence"])["tar"],
                "outputs/images/report_0001/example_app_test_arm64.tar",
            )
            self.assertEqual(Path(result["archive"]), archive)
            self.assertEqual(result["sha256"], hashlib.sha256(b"archive").hexdigest())
            directory = Path(result["evidence"])
            summary = json.loads((directory / "result-summary.json").read_text())
            self.assertEqual(
                (summary["operation"], summary["platform"], summary["image_id"]),
                ("save-python", "linux/arm64", image_id),
            )
            self.assertIn(
                "outputs/images/report_0001/example_app_test_arm64.tar",
                (directory / "archive.tsv").read_text(),
            )
            self.assertEqual(
                read_checks(directory),
                [
                    ("Image platform", "PASS"),
                    ("Image unchanged during save", "PASS"),
                    ("Archive written", "PASS"),
                    ("Archive loads back", "PASS"),
                ],
            )
            self.assertEqual(
                (directory / "check-items.tsv").read_text(encoding="utf-8").splitlines(),
                [
                    "check\titem\texpected\tfound\tstatus",
                    f"Image unchanged during save\texample/app:test\t{image_id}\t{image_id}\tPASS",
                ],
            )
            self.assertEqual(result["readback"], "audit-readback/example_app_test:arm64")
            self.assertEqual(
                readback.call_args.args[1:4], (image_id, result["readback"], "linux/arm64")
            )
            self.assertEqual(
                (directory / "tool-versions.tsv").read_text().splitlines()[0], "tool\tversion"
            )
            with self.assertRaises(FileExistsError) as caught:
                archive_ops.save_python_image(
                    "example/app:test", str(archive), "linux/arm64", run_id="report_0002"
                )
            failed = caught.exception.evidence / "result-summary.json"
            self.assertEqual(json.loads(failed.read_text())["status"], "FAIL")
            self.assertEqual(read_checks(caught.exception.evidence), [("Archive written", "FAIL")])
            nested = archive_ops._artifact_path("outputs/images/new/x.tar", ".tar", "report_0001")
            self.assertEqual(nested, self.root / "outputs/images/new/x.tar")
            self.assertTrue(nested.parent.is_dir())
        with self.assertRaisesRegex(ValueError, "プロジェクト内"):
            archive_ops._artifact_path(str(self.root.parent / "outside.tar"), ".tar", "report_0001")
        with self.assertRaisesRegex(ValueError, ".tar"):
            archive_ops._artifact_path("x.zip", ".tar", "report_0001")
        with self.assertRaisesRegex(ValueError, "実行 ID"):
            archive_ops._artifact_path("x.tar", ".tar", None)

    # --- 保存する .tar の名前にアーキテクチャが入る（README.md「Apptainer の SIF」）---
    def test_image_archive_name(self):
        self.assertEqual(
            archive_ops._image_archive_name("example/app:test", "linux/arm64"),
            "example_app_test_arm64.tar",
        )

    # --- 保存の間にタグが別のイメージに移れば止め、FAIL を残して .tar を残さない（README.md「第 3 段階の中身」）---
    def test_save_stops_when_the_tag_moves(self):
        expected, moved = "sha256:" + "e" * 64, "sha256:" + "f" * 64
        ids = iter([expected, expected, moved])
        with (
            mock.patch.object(
                archive_ops,
                "run_docker",
                side_effect=lambda args, log: Path(args[args.index("--output") + 1]).write_bytes(
                    b"x"
                ),
            ),
            mock.patch.object(
                archive_ops.container, "local_image_id", side_effect=lambda *a: next(ids)
            ),
            self.assertRaisesRegex(RuntimeError, "保存の開始時と一致しません") as caught,
        ):
            archive_ops.save_python_image("example:test", run_id="report_0001")
        self.assertEqual(
            read_checks(caught.exception.evidence),
            [
                ("Image platform", "PASS"),
                ("Image unchanged during save", "FAIL"),
                ("Archive written", "FAIL"),
            ],
        )
        self.assertEqual(
            (caught.exception.evidence / "check-items.tsv")
            .read_text(encoding="utf-8")
            .splitlines(),
            [
                "check\titem\texpected\tfound\tstatus",
                f"Image unchanged during save\texample:test\t{expected}\t{moved}\tFAIL",
            ],
        )
        self.assertFalse((self.root / "outputs/images/report_0001/example_test_arm64.tar").exists())
        with (
            mock.patch.object(archive_ops, "run_docker") as run,
            mock.patch.object(
                archive_ops.container, "local_image_id", side_effect=[expected, moved]
            ),
            self.assertRaisesRegex(RuntimeError, "保存の開始時と一致しません"),
        ):
            archive_ops.save_python_image("example:test", run_id="before_save")
        run.assert_not_called()

    # --- 要確認項目は確認根拠の TSV で確定でき、照合できない根拠と wheel の根拠は止める（README.md「Python」）---
    def test_review_notes_resolve_native_review_items(self):
        context = self.example_context(ubuntu="python3-venv\n")
        detail = "提供パッケージの候補 libfoo1, libfoo1t64"
        native = {
            "missing": {"libfoo.so.1": ["example"]},
            "providers": {},
            "review": [{"item": "libfoo.so.1", "detail": detail}],
        }
        discovery, environment, evidence = self.example_discovery(
            context, native=native, review="libfoo.so.1"
        )
        reviewed = discovery_ops.confirm_python_dependencies(discovery, PROJECT, "linux/amd64")
        self.assertEqual(
            (reviewed["status"], reviewed["lock"], reviewed["unresolved"]), ("REVIEW", None, 1)
        )

        notes = self.root / "review-notes.tsv"
        notes.write_text(
            f"item\tdetail\treason\nlibfoo.so.1\t{detail}\texample は libfoo を使わない\n",
            encoding="utf-8",
        )
        confirmed = discovery_ops.confirm_python_dependencies(
            discovery, PROJECT, "linux/amd64", review_notes=notes
        )
        self.assertEqual((confirmed["status"], confirmed["unresolved"]), ("PASS", 0))
        lock = Path(confirmed["lock"])
        with (lock.parent / "dependency-review.tsv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, dialect="excel-tab"))
        self.assertEqual(
            rows,
            [{"item": "libfoo.so.1", "detail": detail, "decision": "example は libfoo を使わない"}],
        )
        self.assertIn("dependency-review.tsv", (lock.parent / "dependency-lock.tsv").read_text())

        (discovery / "ubuntu-packages.tsv").write_text(
            ubuntu_tsv(
                [
                    {
                        "package": "bash",
                        "architecture": "amd64",
                        "version": "5.2-1",
                        "action": "update",
                    }
                ]
            ),
            encoding="utf-8",
        )
        self.rewrite_discovery(discovery, environment, evidence, "libfoo.so.1")
        updated = discovery_ops.confirm_python_dependencies(
            discovery, PROJECT, "linux/amd64", review_notes=notes
        )
        self.assertEqual((updated["status"], updated["unresolved"]), ("REVIEW", 1))
        update = "固定ベースのパッケージを更新する候補"
        with notes.open("a", encoding="utf-8") as stream:
            stream.write(f"bash\t{update}\tセキュリティ更新を入れる\n")
        updated = discovery_ops.confirm_python_dependencies(
            discovery, PROJECT, "linux/amd64", review_notes=notes
        )
        self.assertEqual(updated["status"], "PASS")
        pins = (Path(updated["lock"]).parent / "ubuntu-pins.txt").read_text(encoding="utf-8")
        self.assertIn("bash=5.2-1", pins.split())

        notes.write_text("item\tdetail\treason\nlibbar.so.2\tx\ty\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "確認根拠の TSV が調査の要確認項目と一致しません"):
            discovery_ops.confirm_python_dependencies(
                discovery, PROJECT, "linux/amd64", review_notes=notes
            )
        notes.write_text(f"item\tdetail\treason\nlibfoo.so.1\t{detail}\t \n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "確認根拠の TSV が調査の要確認項目と一致しません"):
            discovery_ops.confirm_python_dependencies(
                discovery, PROJECT, "linux/amd64", review_notes=notes
            )

    # --- SIF の監査で組み立てる docker のコマンドを確かめる（README.md「Apptainer の SIF」）---
    def test_sif_audit_commands(self):
        self.assertEqual(
            archive_ops.sif_image_name("Example/App:Test", "linux/arm64"),
            "audit-sif/example_app_test:arm64",
        )
        self.assertEqual(
            archive_ops._sif_volume_name("Report_1-audit sif"), "audit-sif-Report_1-audit-sif"
        )
        sif = self.root / "images/app.sif"
        commands = archive_ops._sif_commands(
            sif,
            "audit-sif-x",
            "apptainer@sha256:1",
            "linux/arm64",
            "audit-sif/app:arm64",
            ['ENV PATH="/opt/python/bin:/usr/bin"', "WORKDIR /work", "USER 1000"],
        )
        self.assertEqual(commands["create"], ["volume", "create", "audit-sif-x"])
        self.assertEqual(commands["remove"], ["volume", "rm", "--force", "audit-sif-x"])
        extract = commands["extract"]
        self.assertIn("type=volume,source=audit-sif-x,destination=/work", extract)
        script = contract.sif_extract_script()
        self.assertEqual(
            extract[extract.index("apptainer@sha256:1") - 2 :],
            [
                "--entrypoint",
                "sh",
                "apptainer@sha256:1",
                "-c",
                script,
                "sh",
                "/input/app.sif",
                "arm64",
            ],
        )
        self.assertNotIn("--sandbox", extract)
        self.assertIn("apptainer sif dump", script)
        self.assertIn("unsquashfs -d /work/rootfs", script)
        self.assertIn(container.bind_mount(sif, "/input/app.sif", True), extract)
        self.assertEqual(commands["export"][-5:], ["-C", "/work/rootfs", "-cf", "-", "."])
        self.assertIn(
            "type=volume,source=audit-sif-x,destination=/work,readonly", commands["export"]
        )
        self.assertEqual(
            commands["import"],
            [
                "import",
                "--platform",
                "linux/arm64",
                "--change",
                'ENV PATH="/opt/python/bin:/usr/bin"',
                "--change",
                "WORKDIR /work",
                "--change",
                "USER 1000",
                "-",
                "audit-sif/app:arm64",
            ],
        )
        config = {
            "Env": ["PATH=/opt/python/bin:/usr/bin", 'MSG=a "b"'],
            "WorkingDir": "",
            "User": "",
        }
        with mock.patch.object(
            archive_ops.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(config), ""),
        ):
            self.assertEqual(
                archive_ops._sif_import_changes("example:test", "linux/arm64"),
                ['ENV PATH="/opt/python/bin:/usr/bin"', 'ENV MSG="a \\"b\\""'],
            )

    # --- SIF を照合し、ボリュームに取り出して取り込んだイメージを監査し、ボリュームは失敗しても消す（README.md「Apptainer の SIF」）---
    def test_sif_audit_checks_the_sif_and_removes_the_volume(self):
        lock = self.example_lock()
        sif = self.root / "images/app.sif"
        sif.parent.mkdir()
        sif.write_bytes(b"sif")
        record = self.root / "sif-record"
        record.mkdir()
        (record / "sif.tsv").write_text(
            f"item\tpath\tsha256\nsif\timages/app.sif\t{container.file_sha256(sif)}\n",
            encoding="utf-8",
        )
        docker, removed, imported, audited = [], [], [], []
        real_run = subprocess.run

        def run(args, **kwargs):
            if list(args[:3]) != ["docker", "volume", "rm"]:
                return real_run(args, **kwargs)
            removed.append(list(args))
            return subprocess.CompletedProcess(args, 0)

        def audit(image, platform, directory, config, record):
            audited.append(image)
            (directory / "python-audit.json").write_text('{"status": "PASS"}', encoding="utf-8")
            return {"report": str(directory / "python-audit.json"), "status": "PASS"}

        wait_for_bind = mock.Mock()
        patches = (
            mock.patch.object(
                archive_ops, "run_docker", side_effect=lambda args, log: docker.append(args)
            ),
            mock.patch.object(archive_ops, "wait_for_bind_sources", wait_for_bind),
            mock.patch.object(archive_ops, "_sif_import_changes", return_value=["USER 1000"]),
            mock.patch.object(
                archive_ops,
                "_import_sif_rootfs",
                side_effect=lambda commands, name, platform, log: (
                    imported.append(commands) or "sha256:" + "e" * 64
                ),
            ),
            mock.patch.object(archive_ops, "run_python_image_audit", side_effect=audit),
            mock.patch.object(archive_ops.subprocess, "run", side_effect=run),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        result = archive_ops.audit_python_sif(
            sif, "audit-readback/app:amd64", "linux/amd64", lock=lock, sif_record=record
        )
        wait_for_bind.assert_called_once()
        directory = Path(result["evidence"])
        self.assertEqual(
            read_checks(directory),
            [
                ("SIF unchanged", "PASS"),
                ("SIF file system extracted", "PASS"),
                ("SIF image imported", "PASS"),
                ("Dependency lock", "PASS"),
                ("Base inventory", "PASS"),
                ("Audit checks", "PASS"),
            ],
        )
        volume = archive_ops._sif_volume_name(Path(result["evidence"]).parent.name)
        self.assertEqual([args[:2] for args in docker], [["volume", "create"], ["run", "--rm"]])
        self.assertEqual(imported[0]["import"][-1], "audit-sif/audit-readback_app_amd64:amd64")
        self.assertIn("--change", imported[0]["import"])
        self.assertEqual(audited, ["audit-sif/audit-readback_app_amd64:amd64"])
        self.assertEqual(removed, [["docker", "volume", "rm", "--force", volume]])
        summary = json.loads((directory / "result-summary.json").read_text())
        self.assertEqual(
            (summary["operation"], summary["image"]),
            ("audit-python-sif", "audit-sif/audit-readback_app_amd64:amd64"),
        )
        removed.clear()
        with (
            mock.patch.object(
                archive_ops, "_import_sif_rootfs", side_effect=RuntimeError("import failed")
            ),
            self.assertRaisesRegex(RuntimeError, "import failed"),
        ):
            archive_ops.audit_python_sif(
                sif, "audit-readback/app:amd64", "linux/amd64", lock=lock, sif_record=record
            )
        self.assertEqual(len(removed), 1)
        original = (record / "sif.tsv").read_text()
        entries = [row for row in original.splitlines() if row.startswith("sif\t")]
        (record / "sif.tsv").write_text(original + entries[0] + "\n")
        with self.assertRaisesRegex(ValueError, "sif.tsv）と一致しません"):
            archive_ops.audit_python_sif(sif, "test", "linux/amd64", lock=lock, sif_record=record)
        (record / "sif.tsv").write_text(original)
        sif.write_bytes(b"changed")
        docker.clear()
        with self.assertRaisesRegex(ValueError, "sif.tsv）と一致しません") as caught:
            archive_ops.audit_python_sif(
                sif, "audit-readback/app:amd64", "linux/amd64", lock=lock, sif_record=record
            )
        self.assertEqual(docker, [])
        self.assertEqual(
            read_checks(caught.exception.evidence),
            [("SIF unchanged", "FAIL")],
        )
        with self.assertRaisesRegex(ValueError, "SIF を監査するには"):
            archive_ops.audit_python_image(
                "x", "linux/amd64", lock=lock, operation="audit-python-sif"
            )

    # --- wheel の要確認は、確認根拠を書いても確定しない（README.md「Python」）---
    def test_review_notes_cannot_confirm_wheel_items(self):
        findings = [
            {
                "item": "wheel",
                "detail": "SHA-256 を確認できる wheel がありません（x）",
                "decision": "",
            }
        ]
        notes = self.root / "notes.tsv"
        notes.write_text(
            "item\tdetail\treason\nwheel\tNo verifiable wheel for x\tok\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "確認根拠では確定できません"):
            discovery_ops._apply_review_notes(findings, notes)


if __name__ == "__main__":
    unittest.main()
