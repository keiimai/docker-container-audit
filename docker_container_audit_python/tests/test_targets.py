# --- プロジェクト・入力・確定記録・SIF・.env のテスト（Docker なし。R は tests/testthat/test-targets.R）（README.md「検査とテスト」）---

import csv
import importlib.util
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

#: この子（docker_container_audit_python）の根（このファイルは <根>/tests/ にある）。
REAL_ROOT = Path(__file__).resolve().parents[1]
#: もう片方の子（R）の根。無ければ、R と比べるテストだけを飛ばす。
R_ROOT = REAL_ROOT.parent / "docker_container_audit_r"
#: arm64 のイメージの、目次（index.json）の項目。
IMAGE = {
    "digest": "sha256:" + "1" * 64,
    "platform": {"architecture": "arm64", "os": "linux", "variant": "v8"},
}
#: Docker がイメージの横に足す、証明書の類の目次の項目。
ATTESTATION = {
    "digest": "sha256:" + "2" * 64,
    "annotations": {"io.containerd.manifest.subject": IMAGE["digest"]},
}
#: amd64 のイメージの、目次の項目。
AMD64 = {"digest": "sha256:" + "3" * 64, "platform": {"architecture": "amd64", "os": "linux"}}
sys.path.insert(0, str(REAL_ROOT / "src"))
import archive as archive_ops  # noqa: E402
import container  # noqa: E402
import contract  # noqa: E402
import discovery as discovery_ops  # noqa: E402
import evidence as evidence_ops  # noqa: E402
import image  # noqa: E402
import project  # noqa: E402

#: コンテナ内で動く src/dependencies_inspect.py を、モジュールとして読み込む。
spec = importlib.util.spec_from_file_location(
    "inspector", REAL_ROOT / "src/dependencies_inspect.py"
)
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)


# --- ファイルを文字で読む（root を省けばこの子の根から。R の子のファイルは root=R_ROOT）（README.md「検査対象のフォルダ」）---
def read(path, root=REAL_ROOT):
    return (root / path).read_text(encoding="utf-8")


# --- Dockerfile の ARG NAME=VALUE を辞書で返す（README.md「本番の手順」）---
def dockerfile_args(text):
    return dict(re.findall(r"^ARG ([A-Z_]+)=(\S+)$", text, re.M))


# --- 見本のプロジェクトの request/base.txt を KEY=VALUE の辞書で読む（README.md「検査対象のフォルダ」）---
def base_values(language, root=REAL_ROOT):
    values = {}
    for raw in read(f"projects/example-{language}/request/base.txt", root).splitlines():
        line = raw.partition("#")[0].strip()
        if line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


# --- 検査対象のフォルダと、見本の Dockerfile・調査の環境の定義、uv の渡し方と pylock.toml の固定を確かめる（README.md「検査対象のフォルダ」）---
class TargetSetup(unittest.TestCase):
    # --- 一時フォルダを記録層のルートにし、操作側の ROOT も差し替える（CHECKS.md 1 節）---
    def setUp(self):
        (REAL_ROOT / "_tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(
            prefix="python-targets-test-", dir=REAL_ROOT / "_tmp"
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src/repro.py").touch()
        env = mock.patch.dict(os.environ, {"REPRO_ROOT": str(self.root), "REPRO_RUN_ID": ""})
        env.start()
        self.addCleanup(env.stop)
        root = mock.patch.object(container, "ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)


# --- プロジェクトと確定記録の入力を確かめる（README.md「テスト」）---
class TargetTests(TargetSetup):
    # --- BOM が付いた固定ベースと Ubuntu パッケージ名を読む（README.md「検査対象のフォルダ」）---
    def test_base_and_ubuntu_packages_accept_utf8_bom(self):
        base_file = self.root / "base.txt"
        image_name = "ubuntu:24.04@sha256:" + "d" * 64
        base_file.write_text(f"BASE_IMAGE={image_name}\n", encoding="utf-8-sig")
        packages = self.root / "ubuntu-packages.txt"
        packages.write_text("bash\n", encoding="utf-8-sig")
        self.assertEqual(project.read_base_file(base_file), {"BASE_IMAGE": image_name})
        self.assertEqual(project.read_package_list(packages), ["bash"])

    # --- request/ には第 1 段階の入力だけを置き、Dockerfile を置かない（targets/・dockerfiles/ は無い）（README.md「検査対象のフォルダ」）---
    def test_first_stage_folder_holds_only_the_lists_and_the_base(self):
        folder = REAL_ROOT / "projects/example-python/request"
        self.assertFalse((folder / "Dockerfile").exists())
        self.assertFalse((REAL_ROOT / "targets/python").exists())
        self.assertFalse((REAL_ROOT / "dockerfiles/python").exists())
        for name in ("base.txt", "requirements.txt", "ubuntu-packages.txt"):
            self.assertTrue((folder / name).is_file(), name)
        base = project.read_base_file(folder / "base.txt")["BASE_IMAGE"]
        self.assertRegex(base, r"^ubuntu:24\.04@sha256:[a-f0-9]{64}$")
        self.assertIn("optuna", project.read_requirements(folder / "requirements.txt"))
        self.assertEqual(
            dockerfile_args(read("projects/example-python/dockerfile/Dockerfile"))["BASE_IMAGE"],
            base,
        )

    # --- 見本の Dockerfile は確定した取得元の時点と固定ファイルを使い、wheel の SHA-256 を照合して入れる（README.md「第 2 段階の中身」）---
    def test_sample_dockerfile_uses_confirmed_values_and_pins(self):
        recipe = read("projects/example-python/dockerfile/Dockerfile")
        self.assertNotIn("audit_lock", recipe)
        self.assertIn("ARG APT_SNAPSHOT=", recipe)
        self.assertEqual(len(re.findall(r"^ARG APT_SNAPSHOT=[0-9]{8}T[0-9]{6}Z$", recipe, re.M)), 1)
        self.assertIn("source=pylock.toml", recipe)
        self.assertIn("source=ubuntu-pins.txt", recipe)
        self.assertIn("uv pip sync --python /opt/python/bin/python --require-hashes", recipe)
        self.assertIn("/opt/python/audit-pylock.toml", recipe)
        self.assertNotIn(" pip install", recipe.replace("uv pip install", ""))

    # --- 自動では分からない Ubuntu パッケージは、注釈と重複を除き、名前でない行で止める（README.md「第 1 段階の中身」）---
    def test_ubuntu_package_list_drops_comments_and_duplicates(self):
        listed = self.root / "packages.txt"
        listed.write_text(
            "# 注釈\nlibmagick++-dev   # magick\n\nlibmagick++-dev\n", encoding="utf-8"
        )
        self.assertEqual(project.read_package_list(listed), ["libmagick++-dev"])
        listed.write_text("Bad Name\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            project.read_package_list(listed)
        example = REAL_ROOT / "projects/example-python/request/ubuntu-packages.txt"
        self.assertEqual(project.read_package_list(example), ["ca-certificates", "python3"])

    # --- 操作側・調査の環境・見本の Dockerfile は同じ uv のイメージ（版とハッシュで固定）を使う（README.md「Python」）---
    def test_uv_image_is_pinned_and_matches_the_dockerfiles(self):
        for path in ("projects/example-python/dockerfile/Dockerfile", "Dockerfile.inspect"):
            with self.subTest(path=path):
                self.assertIn(f"ARG UV_IMAGE={image.UV_IMAGE}\n", read(path))
        self.assertIn(
            "COPY --from=uv /uv /usr/local/bin/uv\n",
            read("projects/example-python/dockerfile/Dockerfile"),
        )
        self.assertRegex(image.UV_IMAGE, r"^ghcr\.io/astral-sh/uv:[0-9.]+@sha256:[a-f0-9]{64}$")

    # --- 調査の環境は R と同じ段の名前（uv のほか）を持ち、環境テンプレートの扱い（MODE）は持たない（README.md「第 1 段階の中身」）---
    def test_inspect_dockerfile_has_the_shared_stages_only(self):
        recipe = read("Dockerfile.inspect")
        self.assertEqual(
            re.findall(r"^FROM \S+ AS (\S+)$", recipe, re.MULTILINE),
            ["audit_tools", "uv", "discovery", "dependency_report"],
        )
        self.assertNotIn("MODE", recipe)
        self.assertNotIn("template", recipe)

    # --- Python の検査用のイメージは、固定ベースを見本の base.txt とそろえ、道具を固定し、Python の版の一覧だけを送る（README.md「本番の手順」）---
    def test_python_runner_is_pinned(self):
        py_runner = dockerfile_args(read("Dockerfile.runner"))
        self.assertEqual(py_runner["BASE_IMAGE"], base_values("python")["BASE_IMAGE"])
        self.assertEqual(py_runner["UV_IMAGE"], image.UV_IMAGE)
        for name, value in py_runner.items():
            if name.endswith("_IMAGE"):
                self.assertRegex(value, r"@sha256:[a-f0-9]{64}$")
        quarto = re.findall(
            r"ADD --checksum=\S+ \\\n\s+\S+quarto-1\.9\.36\S+",
            read("projects/example-python/dockerfile/Dockerfile"),
        )
        self.assertEqual(len(quarto), 2)
        for line in quarto:
            self.assertIn(line, read("Dockerfile.runner"))
        self.assertNotIn("renv", read("Dockerfile.runner"))
        self.assertIn('requires-python = "==3.12.*"', read("pyproject.toml"))
        self.assertEqual(read(".python-version").strip(), "3.12")
        ignore = read("Dockerfile.runner.dockerignore").splitlines()
        self.assertEqual(
            [line for line in ignore if line and not line.startswith("#")],
            ["*", "!pyproject.toml", "!uv.lock", "!.python-version"],
        )
        runner = read("tools/runner.sh")
        self.assertIn(
            "definition_files=(Dockerfile.runner pyproject.toml uv.lock .python-version)", runner
        )
        self.assertIn("container-audit-runner}-python:local", runner)
        self.assertNotIn("r|python", runner)

    # --- R の子の検査用とは別に作り、言語によらない道具は同じ版にそろえる（R の子が無ければ飛ばす）（README.md「本番の手順」）---
    @unittest.skipUnless(
        (R_ROOT / "Dockerfile.runner").is_file(),
        "../docker_container_audit_r が無いので、R の子との比較は飛ばします",
    )
    def test_runners_share_only_common_tools_with_the_r_child(self):
        r_runner = dockerfile_args(read("Dockerfile.runner", R_ROOT))
        py_runner = dockerfile_args(read("Dockerfile.runner"))
        self.assertEqual(r_runner["BASE_IMAGE"], base_values("r", R_ROOT)["BASE_IMAGE"])
        self.assertTrue(r_runner["BASE_IMAGE"].startswith("rocker/rstudio:4.5.3@"))
        for name in ("DOCKER_CLI_IMAGE", "BUILDX_IMAGE", "APT_SNAPSHOT"):
            self.assertEqual(r_runner[name], py_runner[name])
        for name, value in r_runner.items():
            if name.endswith("_IMAGE"):
                self.assertRegex(value, r"@sha256:[a-f0-9]{64}$")
        self.assertNotIn("uv sync", read("Dockerfile.runner", R_ROOT))
        ignore = read("Dockerfile.runner.dockerignore", R_ROOT).splitlines()
        self.assertEqual(
            [line for line in ignore if line and not line.startswith("#")],
            ["*", "!renv-pins.tsv", "!tools/install-r-packages.R"],
        )

    # --- 渡す uv は、コンテナの中から読めるようになるまで待ち、読めなければ止める（Dropbox の同期の間）（README.md「Python」）---
    def test_uv_binary_waits_until_a_container_can_read_it(self):
        uv = self.root / "uv"
        uv.write_bytes(b"uv")
        log = self.root / "uv.log"
        results = iter(
            [
                subprocess.CompletedProcess([], 1, "", "Input/output error"),
                subprocess.CompletedProcess([], 0, "uv 0.13.0\n", ""),
            ]
        )
        with (
            mock.patch.object(image.subprocess, "run", side_effect=lambda *a, **k: next(results)),
            mock.patch.object(image.time, "sleep") as sleep,
        ):
            self.assertEqual(image._wait_for_uv(uv, "linux/arm64", log), uv)
        sleep.assert_called_once()
        self.assertIn("attempt 2", log.read_text(encoding="utf-8"))
        failing = subprocess.CompletedProcess([], 1, "", "Input/output error")
        with (
            mock.patch.object(image.subprocess, "run", return_value=failing),
            mock.patch.object(image.time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "Docker から uv を読めません"),
        ):
            image._wait_for_uv(uv, "linux/arm64", log, attempts=3)
        sleep.assert_called()

    # --- pylock.toml は、wheel と SHA-256 がそろった配布物だけを受け付ける（README.md「Python」）---
    def test_pylock_pins_need_wheels_with_hashes(self):
        wheel = '{ url = "https://x.invalid/a-1-py3-none-any.whl", hashes = { sha256 = "%s" } }'
        good = f'[[packages]]\nname = "A_b"\nversion = "1"\nwheels = [{wheel % ("a" * 64)}]\n'
        self.assertEqual(discovery_ops._pylock_pins(good), {"a-b": "1"})
        for bad in (
            good.replace("a" * 64, "short"),
            '[[packages]]\nname = "a"\nversion = "1"\nsdist = { url = "https://x.invalid/a.tar.gz" }\n',
            good + good,
        ):
            with self.assertRaises(ValueError):
                discovery_ops._pylock_pins(bad)
        self.assertEqual(discovery_ops._pylock_item_rows(good), [("a-b 1", "", "a" * 64, "PASS")])
        self.assertEqual(
            [row[3] for row in discovery_ops._pylock_item_rows(good + good)], ["PASS", "FAIL"]
        )
        self.assertEqual(
            discovery_ops._pylock_item_rows(good.replace("a" * 64, "short"))[0][3], "FAIL"
        )


# --- wheel の共有ライブラリに要る Ubuntu パッケージを、ビルドの前に割り出す（README.md「Python」）---
class NativeDependencyTests(unittest.TestCase):
    # --- 共有ライブラリの提供元は、1 つならそれ、ubuntu-packages.txt に書いた候補を優先し、決まらなければ要確認にする（README.md「第 1 段階の中身」）---
    def test_providers_are_chosen_with_listed_packages_winning(self):
        missing = {"libgomp.so.1": {"lightgbm"}, "libfoo.so.1": {"x"}, "libbar.so.2": {"y"}}
        providers = {
            "libgomp.so.1": ["libgomp1"],
            "libfoo.so.1": ["libfoo1", "libfoo1t64"],
            "libbar.so.2": ["libbar2", "libbar2-alt"],
        }
        chosen, review = inspector.choose_providers(missing, providers, {"libbar2"})
        self.assertEqual(chosen, {"libgomp.so.1": "libgomp1", "libbar.so.2": "libbar2"})
        self.assertEqual([item["item"] for item in review], ["libfoo.so.1"])
        self.assertEqual(review[0]["detail"], "提供パッケージの候補 libfoo1, libfoo1t64")
        chosen, review = inspector.choose_providers({"libnone.so": {"z"}}, {}, set())
        self.assertEqual(chosen, {})
        self.assertEqual([row["item"] for row in review], ["libnone.so"])
        self.assertIn("見つかりません", review[0]["detail"])

    # --- 入れる前と後の一覧から計画を作り、提供元を direct にして、それを使う wheel を書く（README.md「第 1 段階の中身」）---
    def test_plan_from_inventories_marks_providers_and_their_wheels(self):
        base = ["bash\tarm64\t5.2\tinstalled", "libc6\tarm64\t2.39\tinstalled"]
        after = base[:1] + [
            "libc6\tarm64\t2.40\tinstalled",
            "python3-venv\tarm64\t3.12\tinstalled",
            "libgomp1\tarm64\t14\tinstalled",
            "gcc-14-base\tarm64\t14\tinstalled",
        ]
        native = {
            "missing": {"libgomp.so.1": ["lightgbm", "xgboost"]},
            "providers": {"libgomp.so.1": "libgomp1"},
            "used_by": {"libc6": ["numpy", "xgboost"]},
        }
        plan = {
            row["package"]: row
            for row in discovery_ops._installed_plan(base, after, ["python3-venv"], native)
        }
        self.assertEqual(
            plan["libgomp1"],
            {
                "package": "libgomp1",
                "architecture": "arm64",
                "version": "14",
                "relation": "direct",
                "base_version": None,
                "action": "install",
                "language_packages": "lightgbm, xgboost",
            },
        )
        self.assertEqual(
            (plan["libgomp1"]["relation"], plan["libgomp1"]["action"]), ("direct", "install")
        )
        self.assertEqual(plan["libgomp1"]["language_packages"], "lightgbm, xgboost")
        self.assertEqual(
            (plan["gcc-14-base"]["relation"], plan["gcc-14-base"]["action"]),
            ("indirect", "install"),
        )
        self.assertEqual(
            (plan["libc6"]["action"], plan["libc6"]["base_version"]), ("update", "2.39")
        )
        self.assertEqual((plan["bash"]["relation"], plan["bash"]["action"]), ("base", "keep"))
        self.assertEqual(plan["libc6"]["language_packages"], "numpy, xgboost")
        self.assertEqual(plan["bash"]["language_packages"], "")
        self.assertEqual(
            inspector.owner_packages(
                ["libc6:arm64: /usr/lib/aarch64-linux-gnu/libc.so.6", "a:arm64, b: /x"]
            ),
            {"libc6", "a", "b"},
        )
        self.assertEqual(plan["python3-venv"]["relation"], "direct")
        with self.assertRaisesRegex(RuntimeError, "削除しました: libc6"):
            discovery_ops._installed_plan(base, after[:1], [], {})


# --- 与えた目次の項目を持つ、小さな OCI の docker-archive を作る（README.md「Apptainer の SIF」）---
def oci_archive(path, manifests, blobs=None):
    source = path.parent / (path.stem + "-src")
    source.mkdir()
    (source / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}', encoding="utf-8")
    for digest, value in (blobs or {}).items():
        blob = source / "blobs" / digest.replace(":", "/", 1)
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_text(json.dumps(value), encoding="utf-8")
    (source / "index.json").write_text(
        json.dumps({"schemaVersion": 2, "manifests": manifests}), encoding="utf-8"
    )
    with tarfile.open(path, "w") as stream:
        for item in sorted(source.iterdir()):
            stream.add(item, arcname=item.name)


# --- 保存した .tar の 1 つのイメージから SIF を作る（README.md「Apptainer の SIF」）---
class SifTests(TargetSetup):
    # --- 目次をそのプラットフォームのイメージ 1 つに絞り、1 つでなければ止める（README.md「Apptainer の SIF」）---
    def test_layout_keeps_only_the_platform_image(self):
        archive = self.root / "image.tar"
        oci_archive(archive, [IMAGE, ATTESTATION, AMD64])
        work = self.root / "work"
        work.mkdir()
        captured = []

        def items(check, rows):
            captured.extend(evidence_ops._check_item_rows(check, rows))

        self.assertEqual(
            archive_ops._sif_layout(archive, work, "linux/arm64", items=items), IMAGE["digest"]
        )
        with self.assertRaises(FileExistsError):
            archive_ops._sif_layout(archive, work, "linux/arm64", items=items)
        self.assertEqual(
            sorted(row[1] for row in captured if row[0] == "Archive entries safe"),
            ["index.json", "oci-layout"],
        )
        self.assertTrue(all(row[4] == "PASS" for row in captured))
        self.assertIn(
            ["Archive platform image", IMAGE["digest"], "linux/arm64", "linux/arm64", "PASS"],
            captured,
        )
        index = json.loads((work / "layout/index.json").read_text())
        self.assertEqual([item["digest"] for item in index["manifests"]], [IMAGE["digest"]])
        for name, manifests in (
            ("none.tar", [ATTESTATION]),
            ("two.tar", [IMAGE, dict(IMAGE, digest="sha256:" + "4" * 64)]),
        ):
            oci_archive(self.root / name, manifests)
            other = self.root / ("work-" + name)
            other.mkdir()
            captured.clear()
            with self.assertRaisesRegex(ValueError, "1 つだけ入っていません"):
                archive_ops._sif_layout(self.root / name, other, "linux/arm64", items=items)
            chosen = [row for row in captured if row[0] == "Archive platform image"]
            self.assertEqual([row[4] for row in chosen], ["FAIL"] * len(chosen))
            self.assertEqual(len(chosen), 0 if name == "none.tar" else 2)
        plain = {"digest": "sha256:" + "5" * 64}
        config = "sha256:" + "6" * 64
        blobs = {
            plain["digest"]: {"config": {"digest": config}},
            config: {"architecture": "arm64", "os": "linux"},
        }
        oci_archive(self.root / "plain.tar", [plain], blobs)
        (self.root / "work-plain").mkdir()
        self.assertEqual(
            archive_ops._sif_layout(
                self.root / "plain.tar", self.root / "work-plain", "linux/arm64"
            ),
            plain["digest"],
        )
        (self.root / "work-plain-amd64").mkdir()
        oci_archive(self.root / "null.tar", [dict(plain, platform=None)], blobs)
        (self.root / "work-null").mkdir()
        self.assertEqual(
            archive_ops._sif_layout(self.root / "null.tar", self.root / "work-null", "linux/arm64"),
            plain["digest"],
        )
        with self.assertRaisesRegex(ValueError, "1 つだけ入っていません"):
            archive_ops._sif_layout(
                self.root / "plain.tar", self.root / "work-plain-amd64", "linux/amd64"
            )

    # --- リンクを含む .tar は展開する前に止め、項目に link の FAIL を残し、目次を書かない（README.md「Apptainer の SIF」）---
    def test_layout_stops_at_a_link(self):
        archive = self.root / "linked.tar"
        oci_archive(archive, [IMAGE])
        link = tarfile.TarInfo("link.json")
        link.type = tarfile.SYMTYPE
        link.linkname = "index.json"
        with tarfile.open(archive, "a") as stream:
            stream.addfile(link)
        work = self.root / "work"
        work.mkdir()
        captured = []

        def items(check, rows):
            captured.extend(evidence_ops._check_item_rows(check, rows))

        with self.assertRaisesRegex(ValueError, "安全でない中身"):
            archive_ops._sif_layout(archive, work, "linux/arm64", items=items)
        self.assertEqual(
            captured,
            [
                ["Archive entries safe", "index.json", "safe", "safe", "PASS"],
                ["Archive entries safe", "oci-layout", "safe", "safe", "PASS"],
                ["Archive entries safe", "link.json", "safe", "link", "FAIL"],
            ],
        )
        self.assertFalse((work / "layout/index.json").exists())

    # --- 保存した .tar を別の名前で読み戻し、元のイメージと ID と層が同じかを確かめる（README.md「第 3 段階の中身」）---
    def test_archive_is_read_back_under_another_name(self):
        archive = self.root / "images/app_arm64.tar"
        archive.parent.mkdir()
        oci_archive(archive, [IMAGE])
        name = archive_ops.readback_image_name("Example/App:Test", "linux/arm64")
        self.assertEqual(name, "audit-readback/example_app_test:arm64")
        loaded = {}
        real_run = subprocess.run

        def run(args, **kwargs):
            if args[:2] != ["docker", "load"]:
                return real_run(args, **kwargs)
            with tarfile.open(fileobj=kwargs["stdin"], mode="r|") as stream:
                for member in stream:
                    if member.name.lstrip("./") == "index.json":
                        loaded.update(json.load(stream.extractfile(member)))
            return subprocess.CompletedProcess(args, 0)

        same = {"id": IMAGE["digest"], "layers": ["sha256:" + "7" * 64]}
        log = self.root / "save.log"
        captured = []

        def items(check, rows):
            captured.extend(evidence_ops._check_item_rows(check, rows))

        with (
            mock.patch.object(archive_ops.subprocess, "run", side_effect=run),
            mock.patch.object(archive_ops, "_platform_image_layers", return_value=same),
        ):
            detail = archive_ops._readback_archive(
                archive, "sha256:x", name, "linux/arm64", log, items
            )
        self.assertEqual(detail, f"{name} {IMAGE['digest']}、層 1")
        self.assertEqual(
            captured,
            [
                ["Archive loads back", "image ID", IMAGE["digest"], IMAGE["digest"], "PASS"],
                [
                    "Archive loads back",
                    "layer 1",
                    "sha256:" + "7" * 64,
                    "sha256:" + "7" * 64,
                    "PASS",
                ],
            ],
        )
        self.assertEqual(
            evidence_ops._check_item_rows(
                "Archive loads back",
                archive_ops._readback_item_rows(
                    {"id": "sha256:1", "layers": ["sha256:7", "sha256:8"]},
                    {"id": "sha256:1", "layers": ["sha256:7"]},
                ),
            ),
            [
                ["Archive loads back", "image ID", "sha256:1", "sha256:1", "PASS"],
                ["Archive loads back", "layer 1", "sha256:7", "sha256:7", "PASS"],
                ["Archive loads back", "layer 2", "sha256:8", "", "FAIL"],
            ],
        )
        annotations = loaded["manifests"][0]["annotations"]
        self.assertEqual(annotations["io.containerd.image.name"], f"docker.io/{name}")
        self.assertEqual(annotations["org.opencontainers.image.ref.name"], "arm64")
        layers = iter([same, {**same, "layers": []}])
        with (
            mock.patch.object(archive_ops.subprocess, "run", side_effect=run),
            mock.patch.object(
                archive_ops, "_platform_image_layers", side_effect=lambda *a: next(layers)
            ),
            self.assertRaisesRegex(RuntimeError, "一致しません"),
        ):
            archive_ops._readback_archive(archive, "sha256:x", name, "linux/arm64", log)

    # --- SIF を作り、apptainer sif list でアーキテクチャを確かめ、ハッシュを記録する（既にあれば止める）（README.md「Apptainer の SIF」）---
    def test_sif_is_built_listed_and_recorded(self):
        archive = self.root / "images/app_arm64.tar"
        archive.parent.mkdir()
        oci_archive(archive, [IMAGE, ATTESTATION])
        calls = []

        def docker(args, log):
            calls.append(args)
            work = Path(args[args.index("--mount") + 1].split('"source=')[1].split('"')[0])
            if "build" in args:
                (work / "image.sif").write_bytes(b"sif")
            else:
                with Path(log).open("a") as stream:
                    stream.write(
                        "4    |1   |NONE   |36864-29036544   |FS (Squashfs/*System/arm64)\n"
                    )

        with (
            mock.patch.object(archive_ops, "run_docker", side_effect=docker),
            mock.patch.object(archive_ops, "wait_for_bind_sources") as wait_for_bind,
            mock.patch.object(
                archive_ops, "_apptainer_version", return_value="apptainer version 1.5.5"
            ),
        ):
            result = archive_ops.save_python_sif("images/app_arm64.tar", run_id="report_0001")
        wait_for_bind.assert_called_once()
        self.assertEqual(
            Path(result["sif"]), self.root / "outputs/images/report_0001/app_arm64.sif"
        )
        self.assertEqual(calls[0][-3:], ["build", "/work/image.sif", "oci:/work/layout"])
        self.assertEqual(calls[1][-3:], ["sif", "list", "/work/image.sif"])
        self.assertEqual(
            contract.apptainer_image("linux/arm64"),
            "ghcr.io/apptainer/apptainer@sha256:"
            "167bf566808fd902a7722fe7c98c37f5f0f71f5e4f35dbac64a50f13f4352ef3",
        )
        self.assertIn(contract.apptainer_image("linux/arm64"), calls[0])
        directory = Path(result["evidence"])
        self.assertEqual(directory, self.root / "outputs/evidence/report_0001/sif")
        summary = json.loads((directory / "result-summary.json").read_text())
        self.assertEqual(
            (summary["operation"], summary["platform"]), ("save-python-sif", "linux/arm64")
        )
        self.assertIn(
            "outputs/images/report_0001/app_arm64.sif", (directory / "sif.tsv").read_text()
        )
        with (directory / "checks.tsv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE))
        self.assertEqual(
            [row["check"] for row in rows],
            [
                "Archive entries safe",
                "Archive platform image",
                "SIF build",
                "SIF file system architecture",
                "SIF written",
            ],
        )
        self.assertTrue(all(row["status"] == "PASS" for row in rows))
        self.assertEqual(rows[1]["detail"], IMAGE["digest"])
        with (directory / "check-items.tsv").open(encoding="utf-8", newline="") as stream:
            item_rows = list(csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE))
        self.assertEqual(
            [row["check"] for row in item_rows],
            ["Archive entries safe", "Archive entries safe", "Archive platform image"],
        )
        self.assertTrue(all(row["status"] == "PASS" for row in item_rows))
        self.assertIn("Squashfs", rows[3]["detail"])
        self.assertIn(
            "apptainer\tapptainer version 1.5.5", (directory / "tool-versions.tsv").read_text()
        )
        with self.assertRaises(FileExistsError):
            archive_ops.save_python_sif("images/app_arm64.tar", run_id="report_0001")


# --- 入口は .env の値を環境変数に読み込み、既にある変数は上書きしない（REPRODUCIBILITY.md 4 節）---
class EnvFileTests(unittest.TestCase):
    # --- 未設定の変数だけを、注釈と引用符を外して読み込む（最後の行が勝つ）（REPRODUCIBILITY.md 4 節）---
    def test_env_file_fills_only_unset_variables(self):
        (REAL_ROOT / "_tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=REAL_ROOT / "_tmp") as work:
            env_file = Path(work) / ".env"
            env_file.write_text(
                '# 注釈\nAUDIT_TEST_A=1   # 行末の注釈\nAUDIT_TEST_B="x"\nAUDIT_TEST_C=new\nAUDIT_TEST_A=2\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"AUDIT_TEST_C": "old"}, clear=False):
                os.environ.pop("AUDIT_TEST_A", None)
                os.environ.pop("AUDIT_TEST_B", None)
                loaded = evidence_ops.load_env_file(env_file)
                values = [
                    os.environ.get(name)
                    for name in ("AUDIT_TEST_A", "AUDIT_TEST_B", "AUDIT_TEST_C")
                ]
            self.assertEqual(sorted(loaded), ["AUDIT_TEST_A", "AUDIT_TEST_B"])
            self.assertEqual(values, ["2", "x", "old"])
            self.assertEqual(evidence_ops.load_env_file(Path(work) / "missing.env"), [])


# --- コンテナに pip や uv が無いときの代わりの手段を確かめる（README.md「検査対象のフォルダ」）---
class InContainerFallbackTests(unittest.TestCase):
    # --- 依存の確認は、ツールが渡す uv の uv pip check で行い、uv が無ければ status を None にする（README.md「Python」）---
    def test_uv_check_uses_the_given_uv_and_reviews_without_it(self):
        calls = []

        def command(args):
            calls.append(args)
            return {"status": 0, "output": ""}

        (REAL_ROOT / "_tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=REAL_ROOT / "_tmp") as work:
            uv = Path(work) / "uv"
            uv.touch()
            with mock.patch.object(inspector, "command", side_effect=command):
                self.assertEqual(inspector.run_uv_check(str(uv))["status"], 0)
                self.assertIsNone(inspector.run_uv_check(str(uv) + "-missing")["status"])
            output = (
                "Using Python 3.12.3 environment at: /opt/python\n"
                "Checked 128 packages in 19ms\nAll installed packages are compatible"
            )
            with mock.patch.object(
                inspector, "command", return_value={"status": 0, "output": output}
            ):
                self.assertEqual(
                    inspector.run_uv_check(str(uv))["output"],
                    "Checked 128 packages\nAll installed packages are compatible",
                )
        self.assertEqual(calls, [[str(uv), "pip", "check", "--python", inspector.sys.executable]])

    # --- Debian も dpkg の OS として受け入れ、Alpine は不合格にする（README.md「Python」）---
    def test_debian_is_accepted_as_a_dpkg_system(self):
        with mock.patch.object(inspector, "command", return_value={"status": 0, "output": ""}):
            debian = inspector.audit_ubuntu([], {"ID": "debian", "PRETTY_NAME": "Debian 12"})
            alpine = inspector.audit_ubuntu([], {"ID": "alpine"})
        self.assertEqual(
            debian["checks"][0],
            {"check": "OS (Debian/Ubuntu)", "status": "PASS", "detail": "Debian 12"},
        )
        self.assertEqual(alpine["checks"][0]["status"], "FAIL")
        self.assertEqual(
            [check["check"] for check in alpine["checks"]],
            [
                "OS (Debian/Ubuntu)",
                "APT consistency",
                "dpkg audit",
                "Ubuntu package inventory",
                "Confirmed Ubuntu versions",
                "Unlisted Ubuntu additions",
                "Confirmed Ubuntu additions missing or ambiguous",
            ],
        )
        apt = {check["check"]: check for check in debian["checks"]}["APT consistency"]
        self.assertEqual((apt["status"], apt["detail"]), ("PASS", ""))

    # --- apt-get check の出力は失敗のときだけ残す（README.md「Python」）---
    def test_apt_check_output_is_kept_only_for_a_failure(self):
        output = {"status": 100, "output": "Reading package lists...\nE: broken\n"}
        with mock.patch.object(inspector, "command", return_value=output):
            checks = inspector.audit_ubuntu([], {"ID": "ubuntu"})["checks"]
        apt = {check["check"]: check for check in checks}["APT consistency"]
        self.assertEqual(
            (apt["status"], apt["detail"]), ("FAIL", "Reading package lists...; E: broken")
        )


if __name__ == "__main__":
    unittest.main()
