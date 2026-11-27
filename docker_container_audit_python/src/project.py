# --- プロジェクトの場所と設定を読み、報告書の入力を集める（R は R/project.R）（README.md「検査対象のフォルダ」）---

from __future__ import annotations

import re
from pathlib import Path

import container
import contract
from container import shown_path

#: 対象コンテナの中で依存を調べるスクリプト（読み取り専用でマウントして動かす）。
INSPECT_SCRIPT = container.ROOT / "src/dependencies_inspect.py"


# --- 記録層 repro を読み込む（無ければ uv sync を促して止める）（README.md「操作側と検査対象」）---
def _repro_module():
    try:
        import repro
    except ImportError as error:
        raise RuntimeError(
            "uv sync で Python の環境を整えてください（notebooks/README.md）。"
        ) from error
    return repro


#: 入力の取りまとめに使う報告書（.qmd）の場所（第 1 段階の依存調査、第 3 段階のイメージの作成と監査）。
REPORTS = {
    "discovery": "notebooks/python-dependency-discovery.qmd",
    "image": "notebooks/python-image-build.qmd",
}


# --- プロジェクトの config.yaml を記録層で読む（名前違いと、もう一方の言語のプロジェクトは止める）（README.md「検査対象のフォルダ」）---
def load_project_config(project, *, stage=None):
    folder = _python_project(project)
    path = folder / "config.yaml"
    if not path.is_file():
        raise ValueError(f"設定ファイルがありません: {path}")
    cfg = _repro_module().config_load(path)
    if cfg.get("project") != project:
        raise ValueError(
            f"設定ファイルの project（{cfg.get('project')}）が、"
            f"プロジェクトのフォルダの名前（{project}）と違います: {path}"
        )
    container.validate_platform(cfg.get("platform"))
    for section in ("discovery", "image"):
        if not isinstance(cfg.get(section), dict):
            raise ValueError(f"設定ファイルに {section}: の節がありません: {path}")
    if stage is not None:
        if stage not in REPORTS:
            raise ValueError("段階は discovery か image です。")
        text_keys = (
            ("apt_snapshot", "review_notes")
            if stage == "discovery"
            else ("image", "dependency_lock", "compare_to")
        )
        for name in text_keys:
            value = cfg[stage].get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"設定ファイルの {stage}.{name} は文字にしてください。")
        if stage == "image":
            for name in ("build", "audit", "save", "sif"):
                if not isinstance(cfg[stage].get(name), bool):
                    raise ValueError(
                        f"設定ファイルの image.{name} は true か false にしてください。"
                    )
    return cfg


# --- 第 1・第 3 段階の報告書の記録（repro.run）に渡す入力ファイルを返す（README.md「記録」）---
def report_inputs(project, stage, lock=None):
    if stage not in REPORTS:
        raise ValueError("段階は discovery か image です。")
    config = _python_project(project) / "config.yaml"
    if stage == "discovery":
        files = [*target_inputs(project)[1].values(), config, INSPECT_DOCKERFILE]
    else:
        dockerfile = dockerfile_files(dockerfile_folder(project))
        files = [*dockerfile, config, *lock_inputs(required_lock(lock))]
    contracts = sorted(path.name for path in contract.CONTRACT_DIR.glob("*") if path.is_file())
    return list(
        dict.fromkeys(
            [*(shown_path(path) for path in files), *(f"../contracts/{name}" for name in contracts)]
        )
    )


# --- requirements.txt を 1 行 1 要件として読む（pip のオプション・URL・別ファイルは止める）（README.md「Python」）---
def read_requirements(path):
    if not path:
        return []
    lines = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = re.sub(r"\s+#.*$", "", raw).strip()
        if not line or line.startswith("#"):
            continue
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", line) or any(
            c in line for c in ("@", "\\")
        ):
            raise ValueError(
                "requirements.txt にはパッケージの要件だけを書いてください"
                "（pip のオプション・URL・別ファイルは使えません）。"
            )
        lines.append(line)
    return lines


#: Python の調査の環境の定義（Dockerfile.inspect）。
INSPECT_DOCKERFILE = container.ROOT / "Dockerfile.inspect"


#: base.txt で使えるキー（Python は BASE_IMAGE だけ）。どれも必須。
_BASE_KEYS = ("BASE_IMAGE",)


# --- base.txt を読み、キーと値を返す（知らないキー・重なり・欠けは止める）（README.md「検査対象のフォルダ」）---
def read_base_file(path):
    values = {}
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.partition("#")[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            raise ValueError(
                f"base.txt は 1 行に KEY=VALUE の形で書いてください: {path}"
                f"（{number} 行目: {raw.strip()}）"
            )
        key, value = match.groups()
        if key not in _BASE_KEYS:
            raise ValueError(f"base.txt に知らないキーがあります: {key}（{path}）")
        if key in values:
            raise ValueError(f"base.txt でキーが重なっています: {key}（{path}）")
        values[key] = value
    missing = [key for key in _BASE_KEYS if not values.get(key)]
    if missing:
        raise ValueError(f"base.txt に {', '.join(missing)} がありません: {path}")
    validate_base_image(values["BASE_IMAGE"])
    return values


# --- Ubuntu のベースイメージが版と SHA-256 で固定されているか確かめる（README.md「検査対象のフォルダ」）---
def validate_base_image(image):
    if not re.fullmatch(r"ubuntu:[0-9][0-9.]*@sha256:[a-f0-9]{64}", image):
        raise ValueError(
            f"BASE_IMAGE は ubuntu:<版>@sha256:<64 桁のダイジェスト> にしてください: {image}"
        )


#: 既定のプロジェクト（projects/example-python）。
DEFAULT_PROJECT = "example-python"


#: 第 1 段階の入力（projects/<名前>/request の中。requirements.txt と base.txt は必須、ubuntu-packages.txt は任意）。
_TARGET_FILES = ("requirements.txt", "base.txt", "ubuntu-packages.txt")


#: request の中で言語を決めるファイル（DESCRIPTION なら R、requirements.txt なら Python）。
_LANGUAGE_FILES = {"DESCRIPTION": "r", "requirements.txt": "python"}


# --- プロジェクト名の形を確かめて返す（Docker のイメージ名にも使えるよう小文字）（README.md「検査対象のフォルダ」）---
def project_name(project):
    if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise ValueError(
            "プロジェクト名は小文字の英数字・ハイフン・下線（^[a-z0-9][a-z0-9_-]*$）にしてください: "
            f"{project}"
        )
    return project


# --- プロジェクトのフォルダ（ROOT の projects/<名前>）を返す（README.md「検査対象のフォルダ」）---
def project_folder(project):
    return container.ROOT / "projects" / project_name(project)


# --- プロジェクトの言語を request の中身で決める（両方・どちらも無しは止める）（README.md「検査対象のフォルダ」）---
def project_language(project):
    request = project_folder(project) / "request"
    if not request.is_dir():
        raise ValueError(
            f"プロジェクトの request/ がありません: {request}（projects/{project}/request を作ってください）"
        )
    found = sorted(
        language for name, language in _LANGUAGE_FILES.items() if (request / name).is_file()
    )
    if len(found) != 1:
        raise ValueError(
            "request/ には DESCRIPTION（R）か requirements.txt（Python）のどちらか 1 つを置いて"
            f"ください: projects/{project}/request"
        )
    return found[0]


# --- Python のプロジェクトかを確かめる（R なら、何をすればよいかを書いて止める）（README.md「検査対象のフォルダ」）---
def _python_project(project):
    if project_language(project) != "python":
        raise ValueError(
            f"プロジェクト {project} は R のプロジェクトです（request/DESCRIPTION）。"
            "R の子（../docker_container_audit_r）の報告書を使ってください。"
        )
    return project_folder(project)


# --- 第 3 段階のイメージの既定の名前（<プロジェクト名>:local）（README.md「第 3 段階の中身」）---
def default_image(project=DEFAULT_PROJECT):
    return f"{project_name(project)}:local"


# --- プロジェクトの request と、第 1 段階の入力のファイルを返す（README.md「検査対象のフォルダ」）---
def target_inputs(project):
    request = (_python_project(project) / "request").resolve(strict=True)
    missing = [name for name in _TARGET_FILES[:2] if not (request / name).is_file()]
    if missing:
        raise ValueError(f"対象のフォルダに {'、'.join(missing)} がありません: {request}")
    paths = {name: request / name for name in _TARGET_FILES if (request / name).is_file()}
    return request, paths


#: 確定記録（dependency-lock.tsv）に載せるファイル（contracts/lock-files.tsv）。
LOCK_FILES = tuple(contract.lock_files("python"))


# --- 第 2・第 3 段階に渡す確定記録のパスを返す（無ければ設定方法を示して止める）（README.md「検査対象のフォルダ」）---
def required_lock(lock):
    if not lock:
        raise ValueError(
            "第 1 段階の確定記録（dependency-lock.tsv）を、設定ファイル projects/<名前>/config.yaml の "
            "image.dependency_lock に書いてください。第 1 段階の報告書 "
            "notebooks/python-dependency-discovery.qmd で確定すると、表 dockerfile-inputs の "
            "dependency_lock にパスが出ます。"
        )
    return (container.ROOT / lock).resolve()


# --- 記録の入力にする、確定記録と、そこに載るファイルのうちあるものを返す（REPRODUCIBILITY.md 2 節）---
def lock_inputs(lock):
    return [lock, *(p for p in (lock.parent / name for name in LOCK_FILES) if p.is_file())]


# --- ビルドに使う Dockerfile のフォルダ（projects/<名前>/dockerfile）を返す（README.md「検査対象のフォルダ」）---
def dockerfile_folder(project):
    folder = _python_project(project) / "dockerfile"
    if not folder.is_dir():
        raise ValueError(
            f"Dockerfile のフォルダがありません: {folder}（projects/{project}/dockerfile を作ってください）"
        )
    folder = folder.resolve(strict=True)
    if not (folder / "Dockerfile").is_file():
        raise ValueError(f"Dockerfile のフォルダに Dockerfile がありません: {folder}")
    return folder


# --- Dockerfile のフォルダのすべてのファイルを返す（隠しファイルを含み、.git は除く）（README.md「検査対象のフォルダ」）---
def dockerfile_files(folder):
    return [
        path
        for path in sorted(folder.rglob("*"), key=lambda item: item.relative_to(folder).as_posix())
        if path.is_file() and ".git" not in path.relative_to(folder).parts
    ]


# --- ubuntu-packages.txt を読む（1 行に 1 つ。# の後ろは注釈。無ければ空）（README.md「検査対象のフォルダ」）---
def read_package_list(package_file):
    if package_file is None:
        return []
    packages = [
        line.partition("#")[0].strip()
        for line in Path(package_file).read_text(encoding="utf-8-sig").splitlines()
    ]
    packages = list(dict.fromkeys(item for item in packages if item))
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9.+-]+", item) for item in packages):
        raise ValueError(
            f"Ubuntu パッケージは APT のパッケージ名を 1 行に 1 つ書いてください: {package_file}"
        )
    return packages
