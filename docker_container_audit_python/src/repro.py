# --- 再現性の記録層（REPRODUCIBILITY.md 2 節）---

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import locale as locale_module
import logging
import math
import os
import platform
import random
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import threadpoolctl
import yaml

#: 記録層の版番号。
__version__ = "0.1.0"


#: ルート判定のマーカー。これがあるディレクトリだけをルートとみなす。
_ROOT_MARKERS: tuple[str, ...] = ("src/repro.py",)


# --- プロジェクトルートを返す（REPRO_ROOT が無ければ、src/ の 1 つ上）（REPRODUCIBILITY.md 2 節）---
def find_root() -> Path:
    root = Path(os.getenv("REPRO_ROOT") or Path(__file__).resolve().parents[1]).expanduser()
    if not all((root / marker).exists() for marker in _ROOT_MARKERS):
        raise RuntimeError(
            "プロジェクトルートを特定できませんでした。\n"
            f"  次が全て存在するディレクトリをルートとみなします: {list(_ROOT_MARKERS)}\n"
            f"  見た場所: {root}（環境変数 REPRO_ROOT で変えられます）"
        )
    return root.resolve()


# --- ルートからの相対パスを POSIX 形式の文字列で返す（ルートの外は ../ で上る。上れない別のドライブだけ絶対パス）（REPRODUCIBILITY.md 2 節）---
def _relative(path: str | Path, root: Path) -> str:
    base = root.resolve()
    target = Path(path).resolve()
    try:
        return Path(os.path.relpath(target, base)).as_posix()
    except ValueError:
        return target.as_posix()


#: ハッシュを取るときに一度に読む大きさ（1 MiB。大きなファイルでもメモリを食わない）。
_CHUNK_BYTES = 1 << 20


# --- ファイルの SHA-256 を返す（不在なら None）（REPRODUCIBILITY.md 2 節）---
def _hash_file(path: str | Path) -> str | None:
    target = Path(path)
    if not target.is_file():
        return None
    hasher = hashlib.sha256()
    try:
        with target.open("rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                hasher.update(chunk)
    except OSError:
        return None
    return hasher.hexdigest()


#: 記録の時刻と実行 ID の時刻帯の既定（.env にも環境変数にも TZ が無いときに使う）。
DEFAULT_TZ = "Asia/Tokyo"

#: 記録の時刻の書式（R の記録層と同じ。秒までで、時差にコロンを付けない）。
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


# --- 時刻帯の名前（.env の TZ → 環境変数 TZ → Asia/Tokyo の順。.env はほかの行を読まない）（REPRODUCIBILITY.md 2 節）---
def _run_timezone_name() -> str:
    try:
        env_file: Path | None = find_root() / ".env"
    except RuntimeError:
        env_file = None
    name = ""
    if env_file is not None and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*TZ\s*=(.*)$", line)
            if match:
                name = re.sub(r"^[\"']|[\"']$", "", re.sub(r"\s+#.*$", "", match.group(1)).strip())
    return name or os.environ.get("TZ") or DEFAULT_TZ


# --- 時刻（時刻帯は _run_timezone_name()）（REPRODUCIBILITY.md 2 節）---
def _run_timezone() -> ZoneInfo:
    name = _run_timezone_name()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        msg = (
            f"TZ={name} は時刻帯の名前ではありません。.env の TZ（無ければ環境変数 TZ）に、"
            "Asia/Tokyo のような名前を書いてください（→ docs/REPRODUCIBILITY.md 2 節）"
        )
        raise ValueError(msg) from None


# --- 今の時刻を、記録の時刻帯で書式どおりの文字にする（REPRODUCIBILITY.md 2 節）---
def _timestamp(fmt: str = TIMESTAMP_FORMAT) -> str:
    return datetime.now(_run_timezone()).strftime(fmt)


# --- エポック秒を、記録の時刻帯の時刻の組にする（実行ログの各行の時刻）（REPRODUCIBILITY.md 2 節）---
def _run_timetuple(epoch_seconds: float | None) -> time.struct_time:
    seconds = time.time() if epoch_seconds is None else epoch_seconds
    return datetime.fromtimestamp(seconds, tz=_run_timezone()).timetuple()


#: 実行の記録の置き場所（ルートからの相対パス）。1 回の実行ごとに <run_id>/ を作り、
#: 記録（<run_id>_run.json）・実行ログ（<run_id>.log）・使った設定（<run_id>.config.yaml）を置く。
REPRO_DIR = "outputs/repro"

#: 実行台帳（runs() が作り直す）の場所（ルートからの相対パス）。
RUNS_CSV = "outputs/repro/runs.csv"

#: run() が作る、解析の成果物の置き場所（ルートからの相対パス）。記録を切っていても作る。
OUTPUT_DIRS: tuple[str, ...] = (
    "outputs",
    "outputs/figures",
    "outputs/metrics",
    "outputs/tables",
    "outputs/reports",
)


#: YAML 1.1 が論理値に読む語のうち、true・false 以外（PyYAML が論理値に読むもの）。引用符が無ければ止める。
_YAML_BOOLEAN_WORDS = frozenset({"yes", "no", "on", "off"})

#: R の yaml は論理値に読むが、PyYAML は文字に読む語。引用符が無ければ止める（R と同じ設定を読むため）。
_YAML_SHORT_BOOLEAN_WORDS = frozenset({"y", "Y", "n", "N"})


# --- 引用符の無い論理値の語を止める文（REPRODUCIBILITY.md 4 節）---
def _boolean_word_error(node: yaml.ScalarNode) -> ValueError:
    return ValueError(
        f"{node.start_mark.name} の {node.start_mark.line + 1} 行目: 引用符の無い「{node.value}」は、"
        "YAML が論理値（true・false）として読みます。キーと値に、引用符の無い y・n・yes・no・on・off"
        f'（大文字を含む）を使わないでください。文字として使うなら引用符で囲み（"{node.value}"）、'
        "論理値なら true・false と書いてください（→ docs/REPRODUCIBILITY.md 4 節）"
    )


# --- 引用符の無い yes・no・on・off を、論理値にせず止める（REPRODUCIBILITY.md 4 節）---
def _construct_strict_bool(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> bool:
    if str(node.value).lower() in _YAML_BOOLEAN_WORDS:
        raise _boolean_word_error(node)
    return bool(loader.construct_yaml_bool(node))


#: 指数の形の文字（1e-6・1.0e6 など）
_NUMBER_LIKE = re.compile(r"^[-+]?[0-9][0-9_]*(\.[0-9_]*)?[eE][-+]?[0-9]+$")


# --- 引用符の無い y・n を止め、指数の形の文字は、文字のまま読んで警告する（REPRODUCIBILITY.md 4 節）---
def _construct_number_like_str(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> str:
    value = str(loader.construct_scalar(node))
    if node.style is None and value in _YAML_SHORT_BOOLEAN_WORDS:
        raise _boolean_word_error(node)
    if _NUMBER_LIKE.match(value):
        warnings.warn(
            f"{node.start_mark.name}: {value} は、YAML では数ではなく文字として読みます。数なら小数点と"
            "符号を付けて書いてください（例: 1.0e-6・1.0e+5。→ docs/REPRODUCIBILITY.md 4 節）",
            RuntimeWarning,
            stacklevel=2,
        )
    return value


# --- 論理値の語を止め、指数の形の文字を警告する YAML の読み手（SafeLoader を継ぐ）（REPRODUCIBILITY.md 4 節）---
class _StrictLoader(yaml.SafeLoader):
    pass


_StrictLoader.add_constructor("tag:yaml.org,2002:bool", _construct_strict_bool)
_StrictLoader.add_constructor("tag:yaml.org,2002:str", _construct_number_like_str)


# --- 論理値に化けたキー（true・false）があれば止める（REPRODUCIBILITY.md 4 節）---
def _check_boolean_keys(node: Any, path: Path) -> None:
    if isinstance(node, dict):
        if any(isinstance(key, bool) for key in node):
            raise ValueError(
                f"{path}: キーに true・false（大文字を含む）を使わないでください。YAML が"
                "論理値として読み、名前が True・False に変わります。名前を変えるか、引用符で"
                "囲んでください（→ docs/REPRODUCIBILITY.md 4 節）"
            )
        for value in node.values():
            _check_boolean_keys(value, path)
    elif isinstance(node, list):
        for value in node:
            _check_boolean_keys(value, path)


# --- YAML をロケールに頼らず UTF-8 として読む（REPRODUCIBILITY.md 4 節）---
def read_yaml(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        loaded = yaml.load(handle, Loader=_StrictLoader)
    _check_boolean_keys(loaded, Path(path))
    return loaded


# --- 設定の既定値（R の repro_config_defaults() と同じ）（REPRODUCIBILITY.md 2 節）---
def config_defaults() -> dict[str, Any]:
    return {
        "seed": 42,
        "experiment_name": "default",
        "data": {"mode": "dummy"},
        "repro": {"enabled": True, "quiet": False},
    }


# --- 辞書を階層ごとに重ねる（書いたキーが勝ち、null も残す。R の modifyList(keep.null = TRUE) と同じ）（REPRODUCIBILITY.md 2 節）---
def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


#: seed に使える整数の大きさの上限（R の整数と同じ）。
_SEED_LIMIT = 2147483647


# --- 設定ファイルを既定値に重ねる（知らないキーも残す。seed は整数でなければ止める）（REPRODUCIBILITY.md 2 節）---
def config_load(path: str | Path) -> dict[str, Any]:
    loaded = read_yaml(path)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError(
            f"{path} の一番上は、キーと値の組にしてください（→ docs/REPRODUCIBILITY.md 2 節）"
        )
    cfg = _merge(config_defaults(), loaded)
    seed = cfg.get("seed")
    whole: int | None = None
    if isinstance(seed, int) and not isinstance(seed, bool):
        whole = seed
    elif isinstance(seed, float) and seed.is_integer():
        whole = int(seed)
    if whole is None or abs(whole) > _SEED_LIMIT:
        if isinstance(seed, str):
            shown = f'"{seed}"'
        elif isinstance(seed, list):
            shown = ", ".join(str(item) for item in seed)
        else:
            shown = "null" if seed is None else json.dumps(seed)
        raise ValueError(
            f"{path} の seed は整数にしてください（いまは {shown}。→ docs/REPRODUCIBILITY.md 2 節）"
        )
    cfg["seed"] = whole
    return cfg


# --- 1 回の実行に関する共有状態（成果物は artifacts に、台帳の値は ledger に入れてから finish() を呼ぶ）（REPRODUCIBILITY.md 2 節）---
@dataclass
class RunState:
    run_id: str
    root: Path
    cfg: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    enabled: bool = True
    artifacts: list[str | Path] = field(default_factory=list)
    ledger: dict[str, Any] = field(default_factory=dict)
    git_commit: str | None = None
    git_dirty: bool | None = None
    environment: dict[str, Any] | None = None
    inputs: list[dict[str, Any]] = field(default_factory=list)
    meta_path: Path | None = None
    config_path: str | None = None
    log_path: Path | None = None


#: 進行中の実行状態（run() から finish() まで。実行ログを閉じるために使う）。
_CURRENT: RunState | None = None


# --- 進行中の実行状態を返す（run() の前なら None）（REPRODUCIBILITY.md 2 節）---
def _current() -> RunState | None:
    return _CURRENT


# --- 進行中の実行状態を差し替える（REPRODUCIBILITY.md 2 節）---
def _set_current(state: RunState | None) -> None:
    global _CURRENT
    _CURRENT = state


# --- 実行ログの状態（ハンドラと、差し替える前の警告の出し方）（REPRODUCIBILITY.md 2 節）---
_HANDLER: logging.Handler | None = None
_PREVIOUS_SHOWWARNING = None


# --- 実行ログを開き、ルートロガーに接続する（警告は出したうえでログにも残す）（REPRODUCIBILITY.md 2 節）---
def _open_log(path: Path) -> None:
    global _HANDLER, _PREVIOUS_SHOWWARNING
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    formatter.converter = _run_timetuple
    handler.setFormatter(formatter)
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)
    previous = warnings.showwarning

    # --- 警告を元の出し方で出し、このログが開いている間はログにも書く（書き方は logging.captureWarnings と同じ）（REPRODUCIBILITY.md 2 節）---
    def show_and_log(
        message: Warning | str,
        category: type[Warning],
        filename: str,
        lineno: int,
        file: TextIO | None = None,
        line: str | None = None,
    ) -> None:
        previous(message, category, filename, lineno, file, line)
        if file is None and _HANDLER is handler:
            text = warnings.formatwarning(message, category, filename, lineno, line)
            handler.handle(
                logging.LogRecord(
                    "py.warnings", logging.WARNING, filename, lineno, text, None, None
                )
            )

    _PREVIOUS_SHOWWARNING = previous
    warnings.showwarning = show_and_log
    _HANDLER = handler


# --- 1 行を、ロガーを通さずに実行ログへ書く（実行ログが開いていなければ何もしない）（REPRODUCIBILITY.md 2 節）---
def log(message: str, level: str = "INFO") -> None:
    if _HANDLER is not None:
        record_level = logging.getLevelName(level.upper())
        _HANDLER.handle(logging.LogRecord("repro", record_level, "", 0, message, None, None))


# --- 実行ログのハンドラを外し、警告の出し方を戻す（REPRODUCIBILITY.md 2 節）---
def _close_log() -> None:
    global _HANDLER, _PREVIOUS_SHOWWARNING
    if _HANDLER is not None:
        logging.getLogger().removeHandler(_HANDLER)
        _HANDLER.close()
        _HANDLER = None
    if _PREVIOUS_SHOWWARNING is not None:
        warnings.showwarning = _PREVIOUS_SHOWWARNING
        _PREVIOUS_SHOWWARNING = None


#: 数値の決定性に効くスレッド数の環境変数。記録に残し、1 でなければ run() が警告する。
THREAD_ENV_VARS: tuple[str, ...] = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)

#: 環境の記録に残す、時刻帯とロケールの環境変数。
_LOCALE_ENV_VARS: tuple[str, ...] = ("TZ", "LANG", "LC_ALL")


# --- git コマンドを走らせる（index を書き換えない。英語の表示にする）（REPRODUCIBILITY.md 2 節）---
def _git(
    root: Path, *args: str, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={**os.environ, **(env or {}), "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None


# --- コードの版（git のコミットと、作業ツリーが最後のコミットから変わっているか。持ち主の違うリポジトリも読む。ノートブックの出力だけの違いは数えない）（REPRODUCIBILITY.md 2 節）---
def _git_version(root: Path) -> tuple[str | None, bool | None]:
    with tempfile.NamedTemporaryFile("w", suffix=".gitconfig") as safe:
        safe.write("[safe]\n\tdirectory = *\n")
        safe.flush()
        env = {"GIT_CONFIG_GLOBAL": safe.name}
        head = _git(root, "rev-parse", "HEAD", env=env)
        commit: str | None = None
        if head is not None and head.returncode == 0 and head.stdout.strip():
            commit = head.stdout.strip()
        elif head is not None:
            message = (head.stderr.strip().splitlines() or ["(理由の表示なし)"])[0]
            # git の外（親のフォルダまでか、マウントの境目まで探して無い）は黙る。.git が壊れているときは警告する
            if "not a git repository (or any" not in message:
                warnings.warn(
                    f"git のコミットを取得できません（git_commit は null）: {message}",
                    RuntimeWarning,
                    stacklevel=3,
                )
        status = _git(root, "status", "--porcelain", "-z", env=env)
        if status is None or status.returncode != 0:
            return commit, None
        changed = status.stdout.split("\0")[:-1]
        if (
            commit is None
            or not changed
            or not all(name[:3] == " M " and name.endswith(".ipynb") for name in changed)
        ):
            return commit, bool(changed)
        with tempfile.NamedTemporaryFile("w", suffix=".gitattributes") as attributes:
            attributes.write("*.ipynb filter=nbstripout\n")
            attributes.flush()
            strip = f"filter.nbstripout.clean={shlex.quote(sys.executable)} -m nbstripout"
            config = ("-c", f"core.attributesFile={attributes.name}", "-c", strip)
            stat = ("-c", "core.checkStat=minimal", "-c", "core.trustctime=false")
            diff = _git(root, *config, *stat, "diff", "HEAD", "--name-only", env=env)
    dirty = diff is None or diff.returncode != 0 or bool(diff.stdout.strip())
    return commit, dirty


#: uv.lock の 1 つのパッケージの名前・版・取得元（editable・virtual はこのプロジェクト自身）。
_LOCK_PACKAGE = re.compile(
    r'^\[\[package\]\]\nname = "(?P<name>[^"]+)"\nversion = "(?P<version>[^"]+)"\n'
    r"source = \{ (?P<source>[\w-]+)",
    re.MULTILINE,
)


# --- 読まれるパッケージの版が uv.lock と同じか（名前は PEP 503 の形で比べる）（REPRODUCIBILITY.md 2 節）---
def _check_lock_versions(root: Path) -> None:
    lock_path = root / "uv.lock"
    if not lock_path.is_file():
        return
    locked: dict[str, set[str]] = {}
    for found in _LOCK_PACKAGE.finditer(lock_path.read_text(encoding="utf-8")):
        if found["source"] not in {"editable", "virtual"}:
            name = re.sub(r"[-_.]+", "-", found["name"]).lower()
            locked.setdefault(name, set()).add(found["version"])
    installed: dict[str, metadata.Distribution] = {}
    for dist in metadata.distributions():
        if dist.metadata["Name"]:
            installed.setdefault(re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower(), dist)
    problems = [
        f"{name}: uv.lock は {' / '.join(sorted(versions))}、"
        f"読まれるのは {installed[name].version}（{installed[name].locate_file('')}）"
        for name, versions in sorted(locked.items())
        if name in installed and installed[name].version not in versions
    ]
    if problems:
        raise RuntimeError(
            "uv.lock と違う版のパッケージが読まれるので、実行を止めます:\n  "
            + "\n  ".join(problems)
            + "\n環境が uv.lock より古いときに起きます。"
            "環境を uv.lock に合わせて作り直してから、もう一度実行してください"
            "（→ docs/REPRODUCIBILITY.md 2 節）"
        )


#: イメージの素性（Docker のイメージを作るときに書き込んだもの。無ければ記録しない）。
BUILD_INFO_PATH = Path("/opt/build-info.json")


# --- 実行環境のスナップショット（BLAS は threadpoolctl が見たもの。読めなくても止めない。TZ は記録に使った時刻帯）（REPRODUCIBILITY.md 2 節）---
def _environment_snapshot(root: Path) -> dict[str, Any]:
    try:
        blas: list[dict[str, Any]] | None = threadpoolctl.threadpool_info()
    except Exception:
        blas = None
    return {
        "python_version": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "rng": {"pythonhashseed_env": _PYTHONHASHSEED_AT_STARTUP},
        "locale": list(locale_module.getlocale()),
        "env": {
            **{name: os.getenv(name) for name in (*THREAD_ENV_VARS, *_LOCALE_ENV_VARS)},
            "TZ": _run_timezone_name(),
        },
        "blas": blas,
        "build_info": (
            json.loads(BUILD_INFO_PATH.read_text(encoding="utf-8"))
            if BUILD_INFO_PATH.is_file()
            else None
        ),
        "uv_lock_sha256": _hash_file(root / "uv.lock"),
    }


#: 実行 ID に使う時刻の書式。ファイル名として安全で、辞書順＝時系列順になる。
RUN_ID_TIME_FORMAT = "%Y%m%d_%H%M%S"


# --- run_id を作る（記録があれば作り直す）（REPRODUCIBILITY.md 2 節）---
def _new_run_id(name: str, repro_dir: Path) -> str:
    for _ in range(20):
        stamp = _timestamp(RUN_ID_TIME_FORMAT)
        run_id = f"{stamp}_{name}_{secrets.randbelow(10000):04d}"
        if not (repro_dir / run_id / f"{run_id}_run.json").exists():
            break
    return run_id


# --- 実行 ID を作る（描画の前などに知りたいとき。run(cfg, run_id=...) に渡せる。記録があれば作り直す）（REPRODUCIBILITY.md 2 節）---
def new_run_id(name: str) -> str:
    if not isinstance(name, str) or not name or "/" in name:
        raise ValueError(
            "name は、実行 ID と記録のファイル名の頭に使うので、"
            "/ を含まない 1 つの名前にしてください（→ docs/REPRODUCIBILITY.md 2 節）"
        )
    return _new_run_id(name, find_root() / REPRO_DIR)


# --- 読んだファイルと書き出したファイルの記録（パス・SHA-256・大きさ。無いファイルは null）（REPRODUCIBILITY.md 2 節）---
def _describe_files(paths: Iterable[str | Path], root: Path) -> list[dict[str, Any]]:
    records = []
    for entry in paths:
        target = Path(entry) if Path(entry).is_absolute() else root / entry
        readable = target.is_file()
        records.append(
            {
                "path": _relative(target, root),
                "sha256": _hash_file(target) if readable else None,
                "bytes": target.stat().st_size if readable else None,
            }
        )
    return records


# --- 1 つのパスか、パスの並びを、リストにする（REPRODUCIBILITY.md 2 節）---
def _as_paths(paths: Iterable[str | Path] | str | Path | None) -> list[str | Path]:
    return [paths] if isinstance(paths, (str, Path)) else list(paths or [])


#: 記録（<run_id>_run.json）の形の版。形を変えたら上げる。
RUN_SCHEMA_VERSION = 1


# --- 記録 JSON を書き出す（欄の名前と順は R の記録層と同じ。log は Python だけ）（REPRODUCIBILITY.md 2 節）---
def _write_record(
    state: RunState,
    status: str,
    error: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> None:
    payload = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": state.run_id,
        "status": status,
        "error": error,
        "experiment_name": state.cfg.get("experiment_name"),
        "started_at": state.started_at,
        "finished_at": None if status == "running" else _timestamp(),
        "data_mode": _data_mode(state.cfg),
        "seed": state.cfg.get("seed"),
        "git_commit": state.git_commit,
        "git_dirty": state.git_dirty,
        "inputs": state.inputs,
        "environment": state.environment,
        "config": state.config_path,
        "artifacts": artifacts or [],
        "ledger": state.ledger,
        "log": _relative(state.log_path, state.root) if state.log_path else None,
    }
    if state.meta_path is not None:
        _write_json(payload, state.meta_path)


# --- 設定の data.mode（無ければ None）（REPRODUCIBILITY.md 2 節）---
def _data_mode(cfg: Mapping[str, Any]) -> Any:
    data = cfg.get("data")
    return data.get("mode") if isinstance(data, Mapping) else None


# --- 設定の repro の中の 1 つが true か（REPRODUCIBILITY.md 2 節）---
def _repro_flag(cfg: Mapping[str, Any], name: str) -> bool:
    section = cfg.get("repro")
    return isinstance(section, Mapping) and section.get(name) is True


# --- 記録を始める（設定を読み、記録と設定を書く。実行ログを開き、前の実行のログが開いていれば閉じる）（REPRODUCIBILITY.md 2 節）---
def run(
    cfg: Mapping[str, Any],
    inputs: Iterable[str | Path] | str | Path | None = None,
    run_id: str | None = None,
) -> RunState:
    _close_log()
    root = find_root()
    _check_lock_versions(root)
    name = cfg.get("experiment_name")
    if not isinstance(name, str) or not name or "/" in name:
        raise ValueError(
            "experiment_name は、実行 ID と記録のファイル名の頭に使うので、"
            "/ を含まない 1 つの名前にしてください（→ docs/REPRODUCIBILITY.md 2 節）"
        )
    paths = _as_paths(inputs)
    folders = [str(entry) for entry in paths if (root / entry).is_dir()]
    if folders:
        raise ValueError(
            f"{'・'.join(folders)} はフォルダです。inputs にはファイルを 1 つずつ渡してください"
            "（→ docs/REPRODUCIBILITY.md 2 節）"
        )
    repro_dir = root / REPRO_DIR
    given_by = "run_id" if run_id else "REPRO_RUN_ID"
    run_id = run_id or os.getenv("REPRO_RUN_ID") or ""
    if not run_id:
        run_id = _new_run_id(name, repro_dir)
    elif "/" in run_id:
        raise ValueError(
            f"{given_by} は、記録のファイル名の頭に使うので、/ を含まない名前にしてください"
            "（→ docs/REPRODUCIBILITY.md 2 節）"
        )
    elif (repro_dir / run_id / f"{run_id}_run.json").exists():
        raise RuntimeError(
            f"{given_by}={run_id} の記録が既にあります。1 回の描画で run() を呼ぶのは"
            " 1 回にしてください（→ docs/REPRODUCIBILITY.md 2 節）"
        )
    state = RunState(
        run_id=run_id,
        root=root,
        cfg=dict(cfg),
        started_at=_timestamp(),
        enabled=_repro_flag(cfg, "enabled"),
    )
    for directory in OUTPUT_DIRS:
        (root / directory).mkdir(parents=True, exist_ok=True)
    _set_current(state)
    # --- 途中で失敗したら、開いた実行ログを閉じて進行中の状態を外し、例外を投げ直す（作りかけの記録のフォルダは残す）（REPRODUCIBILITY.md 2 節）---
    try:
        if not state.enabled:
            warnings.warn(
                f"repro.enabled=false のため、この実行は記録されません（run_id={run_id}）",
                RuntimeWarning,
                stacklevel=2,
            )
            return state

        loose = [
            f"{variable}={os.getenv(variable) or '(未設定)'}"
            for variable in THREAD_ENV_VARS
            if os.getenv(variable) != "1"
        ]
        threads_note = (
            f"スレッド数が 1 ではありません（{'・'.join(loose)}）。最終結果を出す実行では 1 に"
            "してください（変えたなら戻す。→ docs/REPRODUCIBILITY.md 4 節）"
            if loose
            else None
        )
        if threads_note:
            warnings.warn(threads_note, RuntimeWarning, stacklevel=2)

        run_dir = repro_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state.log_path = run_dir / f"{run_id}.log"
        _open_log(state.log_path)
        log(
            f"run: run_id={run_id} experiment_name={name} data_mode={_data_mode(cfg)} "
            f"seed={cfg.get('seed')}"
        )
        if threads_note:
            log(threads_note, level="WARNING")

        state.git_commit, state.git_dirty = _git_version(root)
        log(f"code git = {state.git_commit or '(不明)'} dirty={state.git_dirty}")
        if state.git_dirty:
            warnings.warn(
                "作業ツリーにコミットしていない変更があります（git_dirty = true）。"
                "記録の git_commit は、走ったコードと違うことがあります。"
                "結果を出す実行の前にコミットしてください（→ docs/REPRODUCIBILITY.md 2 節）",
                RuntimeWarning,
                stacklevel=2,
            )

        state.environment = _environment_snapshot(root)
        state.inputs = _describe_files(paths, root)
        absent = [record["path"] for record in state.inputs if record["sha256"] is None]
        if absent:
            warnings.warn(
                f"inputs に、無いファイルがあります（SHA-256 は null で記録します）: {'・'.join(absent)}",
                RuntimeWarning,
                stacklevel=2,
            )
        log(f"読むファイル: {len(state.inputs)} 件")
        state.meta_path = run_dir / f"{run_id}_run.json"
        config_file = run_dir / f"{run_id}.config.yaml"
        plain = json.loads(json.dumps(dict(cfg), ensure_ascii=False, default=str))
        config_file.write_text(
            yaml.safe_dump(plain, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        state.config_path = _relative(config_file, root)
        _write_record(state, "running")

        if not _repro_flag(cfg, "quiet"):
            print(f"[repro] run_id={run_id} data_mode={_data_mode(cfg)} seed={cfg.get('seed')}")
        return state
    except BaseException:
        if _current() is state:
            _close_log()
            _set_current(None)
        raise


# --- 記録を終える（state.artifacts のうちファイルでないものは、警告して記録から外す）（REPRODUCIBILITY.md 2 節）---
def finish(state: RunState, status: str = "success", error: str | None = None) -> RunState:
    if not state.enabled:
        if _current() is state:
            _set_current(None)
        return state
    root = state.root
    paths = _as_paths(state.artifacts)
    targets = [Path(entry) if Path(entry).is_absolute() else root / entry for entry in paths]
    files = [target for target in targets if target.is_file()]
    others = [_relative(target, root) for target in targets if not target.is_file()]
    if others:
        warnings.warn(
            f"artifacts に、無いファイルかフォルダがあります（記録に載せません）: {'・'.join(others)}",
            RuntimeWarning,
            stacklevel=2,
        )
    artifacts = _describe_files(files, root)
    unnamed = [
        record["path"]
        for record in artifacts
        if record["path"].startswith("outputs/") and state.run_id not in record["path"]
    ]
    if unnamed:
        warnings.warn(
            "artifacts の outputs/ のファイルに、実行 ID が付いていません（次の実行が上書きし、"
            f"この記録と合わなくなります）: {'・'.join(unnamed)}（→ docs/REPRODUCIBILITY.md 2 節）",
            RuntimeWarning,
            stacklevel=2,
        )
    _write_record(state, status, error, artifacts)
    log(
        f"finish: status={status} artifacts={len(artifacts)}"
        + (f" left_out={len(others)}" if others else "")
        + (f" error={error}" if error else "")
    )
    if _current() is state:
        _close_log()
        _set_current(None)

    if not _repro_flag(state.cfg, "quiet"):
        print(f"\n[repro] run_id      : {state.run_id} ({status})")
        print(f"[repro] data_mode   : {_data_mode(state.cfg)}")
        print(f"[repro] git commit  : {(state.git_commit or '')[:12]}")
        print(f"[repro] 成果物       : {len(artifacts)} 件")
        meta = _relative(state.meta_path, root) if state.meta_path else None
        print(f"[repro] 記録         : {meta}\n")
    return state


# --- 失敗として記録を終える（error には例外の文だけ。型の名前とトレースバックは実行ログに残す）（REPRODUCIBILITY.md 2 節）---
def fail(state: RunState, error: BaseException | str) -> RunState:
    if isinstance(error, BaseException):
        message = str(error) or type(error).__name__
        detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    else:
        message = detail = str(error)
    log(f"実行が失敗しました: {detail}", level="ERROR")
    return finish(state, status="failed", error=message)


#: 実行台帳（runs.csv）の、どの実行にもある列（R の記録層と同じ）。台帳の値（ledger）はこの名前を上書きできない。
RUNS_COLUMNS: tuple[str, ...] = (
    "run_id",
    "status",
    "experiment",
    "data_mode",
    "seed",
    "started_at",
    "finished_at",
    "git_commit",
    "git_dirty",
    "n_inputs",
    "n_artifacts",
    "error",
)


# --- 記録 1 つを、実行台帳の 1 行にする（REPRODUCIBILITY.md 3 節）---
def _runs_row(path: Path) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{path} を読めません（{exc}）。直すか消してください") from exc
    error = payload.get("error")
    row: dict[str, Any] = {
        "run_id": payload.get("run_id"),
        "status": payload.get("status"),
        "experiment": payload.get("experiment_name"),
        "data_mode": payload.get("data_mode"),
        "seed": payload.get("seed"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "git_commit": (payload.get("git_commit") or "")[:12],
        "git_dirty": payload.get("git_dirty"),
        "n_inputs": len(payload.get("inputs") or []),
        "n_artifacts": len(payload.get("artifacts") or []),
        "error": None if error is None else str(error),
    }
    ledger = payload.get("ledger")
    for name, value in ledger.items() if isinstance(ledger, dict) else []:
        if name not in row and isinstance(value, (str, int, float, bool)):
            row[name] = value
    return row


# --- 表を左に寄せて表示する（REPRODUCIBILITY.md 3 節）---
def _print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    cells = [[_shown(row.get(name)) for name in columns] for row in rows]
    widths = [max([len(name)] + [len(line[i]) for line in cells]) for i, name in enumerate(columns)]
    print(" ".join(name.ljust(width) for name, width in zip(columns, widths, strict=True)))
    for line in cells:
        print(" ".join(value.ljust(width) for value, width in zip(line, widths, strict=True)))


# --- 表示する 1 つの値（欠けた値は NA、論理値は TRUE・FALSE）（REPRODUCIBILITY.md 3 節）---
def _shown(value: Any) -> str:
    if value is None:
        return "NA"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


# --- 実行台帳を作り直す（全件を runs.csv に書き、絞った行を返す。show=True なら表示する）（REPRODUCIBILITY.md 3 節）---
def runs(
    experiment: str | None = None, limit: int | str | None = None, show: bool = False
) -> list[dict[str, Any]]:
    experiment = experiment or None
    limit = int(limit) if limit not in (None, "") else None
    root = find_root()
    records = [
        path
        for path in sorted((root / REPRO_DIR).glob("*/*_run.json"))
        if path.parent.name == path.name.removesuffix("_run.json")
    ]
    rows = [_runs_row(path) for path in records]
    rows.sort(key=lambda row: row["started_at"] or "", reverse=True)
    columns = list(RUNS_COLUMNS)
    for row in rows:
        columns += [name for name in row if name not in columns]

    output = root / RUNS_CSV
    output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows, output, columns=columns)
    print(f"[repro] {len(rows)} 件 -> {_relative(output, root)}")

    shown = [row for row in rows if experiment is None or row["experiment"] == experiment]
    shown = shown[:limit] if limit is not None else shown
    if show and not shown:
        print(
            "[repro] 実行記録がありません（repro.smoke() で 1 回走らせてください）。"
            if experiment is None
            else f"[repro] 実験名 {experiment} の実行記録はありません。"
        )
    elif show:
        _print_table(shown, columns)
        running = sum(row["status"] == "running" for row in shown)
        if running:
            print(
                f"[repro] 終わっていない記録（status = running）が {running} 件あります。"
                "途中で止まったか、まだ走っている実行です（→ docs/REPRODUCIBILITY.md 2 節）。"
            )
    return shown


#: 数として書く値の、有効数字の桁数（R の write.table と同じ 15 桁）。
_CSV_DIGITS = 15


# --- 小数を R の write.table と同じ形の文字にする（15 桁に丸め、短い方の書き方。指数は 2 桁以上）（REPRODUCIBILITY.md 4 節）---
def _format_double(value: float) -> str:
    if math.isnan(value):
        return "NA"
    if math.isinf(value):
        return "Inf" if value > 0 else "-Inf"
    if value == 0:
        return "0"
    mantissa, _, exponent_text = f"{value:.{_CSV_DIGITS - 1}e}".partition("e")
    exponent = int(exponent_text)
    sign = "-" if mantissa.startswith("-") else ""
    digits = mantissa.lstrip("-").replace(".", "").rstrip("0") or "0"
    scientific = f"{sign}{digits[0]}{'.' + digits[1:] if len(digits) > 1 else ''}e"
    scientific += f"{'-' if exponent < 0 else '+'}{abs(exponent):02d}"
    if exponent >= 0:
        whole = digits[: exponent + 1].ljust(exponent + 1, "0")
        fraction = digits[exponent + 1 :]
    else:
        whole, fraction = "0", "0" * (-exponent - 1) + digits
    fixed = f"{sign}{whole}{'.' + fraction if fraction else ''}"
    return fixed if len(fixed) <= len(scientific) else scientific


# --- 列の型を、R の unlist と同じ順（論理値 < 整数 < 小数 < 文字）で決める（REPRODUCIBILITY.md 4 節）---
def _column_kind(values: list[Any]) -> str:
    present = [value for value in values if value is not None and value == value]
    if any(not isinstance(value, (bool, int, float)) for value in present):
        return "character"
    if any(isinstance(value, float) for value in present):
        return "double"
    if any(not isinstance(value, bool) for value in present):
        return "integer"
    return "logical"


# --- 1 つの値を、列の型に合わせて CSV の 1 つの欄にする（欠けた値は NA、文字は " で囲む）（REPRODUCIBILITY.md 4 節）---
def _csv_cell(value: Any, kind: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "NA"
    if kind == "character":
        if isinstance(value, bool):
            text = "TRUE" if value else "FALSE"
        elif isinstance(value, float):
            text = _format_double(value)
        else:
            text = str(value)
        return '"' + text.replace('"', '""') + '"'
    if kind == "logical":
        return "TRUE" if value else "FALSE"
    if kind == "double":
        return _format_double(float(value))
    return str(int(value))


# --- NumPy と pandas の値を Python の値にする（欠けた値は None）（REPRODUCIBILITY.md 4 節）---
def _python_value(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    with contextlib.suppress(TypeError, ValueError):
        if value is not None and not isinstance(value, (str, bool, int, float)) and value != value:
            return None
    return value


# --- 表（行の辞書のリストか、pandas の DataFrame）を、BOM 付きの UTF-8 の CSV に書く（R の repro_write_csv() と同じ書き方）（REPRODUCIBILITY.md 4 節）---
def write_csv(table: Any, path: str | Path, *, columns: Sequence[str] | None = None) -> Path:
    if hasattr(table, "to_dict") and hasattr(table, "columns"):
        names = [str(name) for name in table.columns]
        rows = [dict(zip(names, values, strict=True)) for values in table.itertuples(index=False)]
    else:
        rows = [dict(row) for row in table]
        names = []
        for row in rows:
            names += [str(name) for name in row if str(name) not in names]
    names = list(columns) if columns is not None else names
    values = {name: [_python_value(row.get(name)) for row in rows] for name in names}
    kinds = {name: _column_kind(values[name]) for name in names}
    lines = [",".join('"' + name.replace('"', '""') + '"' for name in names)]
    lines += [
        ",".join(_csv_cell(values[name][index], kinds[name]) for name in names)
        for index in range(len(rows))
    ]
    target = Path(path)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write("\n".join(lines) + "\n")
    return target


# --- 記録層の経路を一通り通す（REPRODUCIBILITY.md 2 節）---
def smoke() -> None:
    cfg = config_defaults()
    cfg["experiment_name"] = "smoke"
    cfg["data"]["mode"] = "synthetic"
    set_seed(cfg["seed"], torch_deterministic=False)
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "smoke_input.csv"
        write_csv([{"x": i, "y": random.gauss(0.0, 1.0)} for i in range(1, 6)], source)
        state = run(cfg, inputs=[source])
        try:
            with source.open(encoding="utf-8-sig", newline="") as handle:
                frame = [
                    {"x": int(row["x"]), "y": float(row["y"])} for row in csv.DictReader(handle)
                ]
            output = state.root / "outputs/tables" / f"{state.run_id}_smoke.csv"
            write_csv(frame, output)
        except BaseException as exc:
            fail(state, exc)
            raise
        state.artifacts = [output]
        state.ledger = {"n_rows": 5}
        finish(state)
    print(f"成果物:\n  {_relative(output, state.root)}")


# --- ノートブック（.ipynb・.qmd）を元のフォルダで Quarto に 1 回だけ実行させ、HTML と report.json を outputs/reports/<run_id>/ に出す（params は "名前:値" の並び）（REPRODUCIBILITY.md 2 節）---
def render(notebook: str | Path, params: Sequence[str] = ()) -> Path:
    root = find_root()
    source = Path(notebook).resolve(strict=True)
    run_id = _new_run_id(source.stem, root / REPRO_DIR)
    output_dir = root / "outputs/reports" / run_id
    staging = Path(tempfile.mkdtemp())
    target = source.with_name(f".{run_id}{source.suffix}") if source.suffix == ".ipynb" else source
    executed = output_dir / source.name if target != source else None
    source_sha256 = _hash_file(source)
    if target != source:
        shutil.copy2(source, target)
    work = source.parent / ".quarto"
    if work.is_symlink():
        work.unlink()
    if not work.exists():
        with contextlib.suppress(OSError):
            work.symlink_to(tempfile.mkdtemp())

    print(f"[repro] 実行と描画: {_relative(source, root)}  (run_id={run_id})")
    try:
        completed = subprocess.run(
            [
                "quarto",
                "render",
                target.name,
                "--execute",
                "--execute-dir",
                str(root),
                "--output-dir",
                str(staging),
                "--output",
                f"{source.stem}.html",
                "-M",
                f"date-modified:{datetime.fromtimestamp(source.stat().st_mtime, _run_timezone()):%Y-%m-%d}",
                *[part for param in params for part in ("-P", param)],
            ],
            cwd=source.parent,
            check=False,
            env={**os.environ, "REPRO_RUN_ID": run_id},
        )
        if completed.returncode == 0:
            shutil.copytree(staging, output_dir)
            if executed is not None:
                shutil.copy2(target, executed)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if work.is_symlink():
            work.unlink()
        if target != source:
            target.unlink(missing_ok=True)
            shutil.rmtree(target.with_name(f"{target.stem}_files"), ignore_errors=True)
    if completed.returncode != 0:
        _mark_failed(root, run_id)
        raise RuntimeError(
            f"ノートブックの実行か描画に失敗しました（上の Quarto の出力を見てください）: {source}"
        )

    html = output_dir / f"{source.stem}.html"
    _write_json(
        {
            "run_id": run_id,
            "source_notebook": _relative(source, root),
            "source_sha256": source_sha256,
            "executed_notebook": _relative(executed, root) if executed else None,
            "executed_sha256": _hash_file(executed) if executed else None,
            "html": _relative(html, root),
            "html_sha256": _hash_file(html),
            "rendered_at": _timestamp(),
        },
        output_dir / "report.json",
    )
    print(f"[repro] 完了: {_relative(output_dir, root)}/")
    return output_dir


# --- render() が失敗した実行の記録を failed にし、途中の成果物を記録の下に移す（REPRODUCIBILITY.md 2 節）---
def _mark_failed(root: Path, run_id: str) -> None:
    path = root / REPRO_DIR / run_id / f"{run_id}_run.json"
    record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if record is None:
        return
    moved = _move_partial(root, run_id)
    if record.get("status") == "failed" and not moved:
        return
    if record.get("status") != "failed":
        record.update(status="failed", error="render() が失敗した（HTML は無い）")
        record["finished_at"] = record.get("finished_at") or _timestamp()
    if moved:
        record["error"] = f"{record['error']}。途中の成果物 {moved} 件を partial/ に移した"
    _write_json(record, path)


# --- 失敗した実行の途中の成果物を記録の下に移す（REPRODUCIBILITY.md 2 節）---
def _move_partial(root: Path, run_id: str) -> int:
    outputs = root / "outputs"
    hits = [
        path
        for path in outputs.rglob(f"*{run_id}*")
        if path.relative_to(outputs).parts[0] not in ("repro", "reports")
        and run_id not in str(path.parent.relative_to(outputs))
    ]
    for path in hits:
        destination = root / REPRO_DIR / run_id / "partial" / path.relative_to(outputs)
        destination.parent.mkdir(parents=True, exist_ok=True)
        path.rename(destination)
    return len(hits)


# --- NumPy の数は Python の数に、ほかは文字列にする（REPRODUCIBILITY.md 2 節）---
def _plain(value: Any) -> Any:
    return value.item() if isinstance(value, np.generic) else str(value)


# --- 辞書を JSON として書き出す（REPRODUCIBILITY.md 2 節）---
def _write_json(payload: dict[str, Any], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=_plain)


#: 乱数の固定が警告を出すロガー。
logger = logging.getLogger(__name__)

#: 起動時に効いた PYTHONHASHSEED（import した時点の値。-E・-I で起動すると環境変数は読まれない）。
_PYTHONHASHSEED_AT_STARTUP: str | None = (
    None if sys.flags.ignore_environment else os.environ.get("PYTHONHASHSEED")
)


# --- 乱数種の固定（REPRODUCIBILITY.md 2 節）---
def set_seed(seed: int = 42, *, torch_deterministic: bool = True) -> None:
    if _PYTHONHASHSEED_AT_STARTUP in (None, "", "random"):
        warnings.warn(
            "PYTHONHASHSEED が起動時に固定されていないので、文字列の hash() と set の順が"
            "実行ごとに変わります。この変数は Python の起動時にしか読まれません。"
            "`PYTHONHASHSEED=42` を付けて起動してください（値は seed と同じでなくてよい）。",
            RuntimeWarning,
            stacklevel=2,
        )
        os.environ["PYTHONHASHSEED"] = str(seed)
    random.seed(seed)
    np.random.seed(seed)  # noqa: NPY002

    _seed_torch(seed, deterministic=torch_deterministic)


# --- PyTorch が入っていれば固定する（無ければ何もしない）（REPRODUCIBILITY.md 2 節）---
def _seed_torch(seed: int, *, deterministic: bool) -> None:
    try:
        import torch
    except ImportError:
        return

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    if not deterministic:
        return

    if os.environ.get("CUBLAS_WORKSPACE_CONFIG") not in {":4096:8", ":16:8"}:
        warnings.warn(
            "CUBLAS_WORKSPACE_CONFIG が未設定です。GPU で学習する場合、"
            "cuBLAS の演算が決定的になりません。環境変数 "
            "CUBLAS_WORKSPACE_CONFIG=:4096:8 を設定してください。",
            RuntimeWarning,
            stacklevel=3,
        )
    try:
        torch.use_deterministic_algorithms(True, warn_only=False)
    except (RuntimeError, AttributeError) as exc:
        logger.warning("torch の決定的アルゴリズムを有効にできませんでした: %s", exc)

    if hasattr(torch.backends, "cudnn"):
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
