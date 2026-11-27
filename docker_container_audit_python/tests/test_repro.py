# --- 記録層 repro.py のテスト（REPRODUCIBILITY.md 1 節）---

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import time
import warnings
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import repro  # noqa: E402

#: リポジトリのルート（このファイルは <root>/tests/ にある）。
ROOT = Path(__file__).resolve().parents[1]

#: 止めた時計の時刻（日本時間の 9:30 は、UTC の 0:30）。
_FROZEN_AT = datetime(2026, 9, 26, 9, 30, tzinfo=ZoneInfo("Asia/Tokyo"))

#: 記録 JSON の欄の名前と順（R の記録層と同じ。log は Python だけ）。
_RECORD_FIELDS = [
    "schema_version",
    "run_id",
    "status",
    "error",
    "experiment_name",
    "started_at",
    "finished_at",
    "data_mode",
    "seed",
    "git_commit",
    "git_dirty",
    "inputs",
    "environment",
    "config",
    "artifacts",
    "ledger",
    "log",
]

#: 新しいインタプリタで set_seed を呼び、ハッシュの種の扱いを JSON で返すコード。
_HASHSEED_CHILD = """
import json, os, subprocess, sys, warnings
import repro
parent = hash("icu")
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    repro.set_seed(20260924, torch_deterministic=False)
rng = repro._environment_snapshot(repro.find_root())["rng"]
child = subprocess.run(
    [sys.executable, "-c", "print(hash('icu'))"], capture_output=True, text=True, check=True)
print(json.dumps({
    "warnings": sum("PYTHONHASHSEED" in str(w.message) for w in caught),
    "recorded": rng["pythonhashseed_env"],
    "env_after": os.environ.get("PYTHONHASHSEED"),
    "child_same": int(child.stdout) == parent,
}))
"""


# --- テストごとに、記録層の状態・乱数の状態・PYTHONHASHSEED を元に戻す（REPRODUCIBILITY.md 2 節）---
@pytest.fixture(autouse=True)
def _restore_state() -> Iterator[None]:
    hashseed = os.environ.get("PYTHONHASHSEED")
    random_state = random.getstate()
    numpy_state = np.random.get_state()  # noqa: NPY002
    yield
    repro._close_log()
    repro._set_current(None)
    random.setstate(random_state)
    np.random.set_state(numpy_state)  # noqa: NPY002
    if hashseed is None:
        os.environ.pop("PYTHONHASHSEED", None)
    else:
        os.environ["PYTHONHASHSEED"] = hashseed


# --- テスト用の、記録層が動く最小のプロジェクトを一時ディレクトリに作る（.env も環境変数 TZ も無い状態から始め、時刻帯を手元の PC に頼らない）（REPRODUCIBILITY.md 2 節）---
@pytest.fixture
def repro_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for directory in ("src", "data/raw"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "src/repro.py").touch()
    monkeypatch.setenv("REPRO_ROOT", str(tmp_path))
    monkeypatch.delenv("REPRO_RUN_ID", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    return tmp_path


# --- 既定値に重ねた、表示を出さない設定（REPRODUCIBILITY.md 2 節）---
def _cfg(**changes: Any) -> dict[str, Any]:
    cfg = repro.config_defaults()
    cfg["experiment_name"] = "unit"
    cfg["repro"]["quiet"] = True
    cfg.update(changes)
    return cfg


# --- 記録層の時計を _FROZEN_AT で止める（REPRODUCIBILITY.md 2 節）---
class _FrozenDatetime(datetime):
    # --- 止めた時刻を、渡された時間帯に直して返す（REPRODUCIBILITY.md 2 節）---
    @classmethod
    def now(cls, tz=None):
        return _FROZEN_AT.astimezone(tz)


# --- JSON のファイルを読む（REPRODUCIBILITY.md 3 節）---
def _read_json(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


# --- 実行の記録を読む（REPRODUCIBILITY.md 2 節）---
def _record(root: Path, state: repro.RunState) -> dict[str, Any]:
    return _read_json(root / repro.REPRO_DIR / state.run_id / f"{state.run_id}_run.json")


# --- 身元を固定して git を走らせる（手元の git の設定に左右されない）（REPRODUCIBILITY.md 2 節）---
def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )


# --- 新しいインタプリタで set_seed を呼んだ結果を受け取る（REPRODUCIBILITY.md 4 節）---
def _run_hashseed_child(hashseed: str | None, *flags: str) -> dict[str, Any]:
    env = dict(os.environ)
    env.pop("PYTHONHASHSEED", None)
    if hashseed is not None:
        env["PYTHONHASHSEED"] = hashseed
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            *flags,
            "-c",
            f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r})\n{_HASHSEED_CHILD}",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
        env=env,
        timeout=300,
    )
    loaded: dict[str, Any] = json.loads(completed.stdout.strip().splitlines()[-1])
    return loaded


# --- PYTHONHASHSEED: 起動時に固定されていれば警告せず、子も同じハッシュ。固定されていなければ警告し、子のために seed を代入する（REPRODUCIBILITY.md 4 節）---
@pytest.mark.parametrize(
    ("hashseed", "flags", "expected"),
    [
        ("42", (), {"warnings": 0, "recorded": "42", "env_after": "42", "child_same": True}),
        (None, (), {"warnings": 1, "recorded": None, "env_after": "20260924"}),
        ("random", (), {"warnings": 1, "recorded": "random", "env_after": "20260924"}),
        ("42", ("-E",), {"warnings": 1, "recorded": None, "env_after": "20260924"}),
    ],
    ids=["fixed", "unset", "random", "ignore-environment"],
)
def test_hashseed_at_startup(
    hashseed: str | None, flags: tuple[str, ...], expected: dict[str, Any]
) -> None:
    result = _run_hashseed_child(hashseed, *flags)
    assert {key: result[key] for key in expected} == expected


# --- 環境のスナップショットにスレッド数・時間帯・ロケールなどが入る（REPRODUCIBILITY.md 2 節）---
def test_environment_snapshot_records_determinism_settings(repro_project: Path) -> None:
    snapshot = repro._environment_snapshot(repro_project)
    assert set(snapshot["env"]) == {*repro.THREAD_ENV_VARS, "TZ", "LANG", "LC_ALL"}
    assert snapshot["uv_lock_sha256"] is None


# --- 設定は既定値に階層ごとに重なり、知らないキーと null は残り、書かないキーは既定になる（REPRODUCIBILITY.md 2 節）---
def test_config_load_merges_onto_defaults(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "experiment_name: main\ndata:\n  path: data/raw/x.csv\nrepro:\n  quiet: true\n"
        "model: {depth: 3}\nnote: null\nseed: 7.0\n",
        encoding="utf-8",
    )
    cfg = repro.config_load(path)
    assert cfg == {
        "seed": 7,
        "experiment_name": "main",
        "data": {"mode": "dummy", "path": "data/raw/x.csv"},
        "repro": {"enabled": True, "quiet": True},
        "model": {"depth": 3},
        "note": None,
    }
    assert isinstance(cfg["seed"], int)
    path.write_text("", encoding="utf-8")
    assert repro.config_load(path) == repro.config_defaults()
    path.write_text("- 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="キーと値の組"):
        repro.config_load(path)


# --- seed は整数でなければ止める（文字・小数・論理値・null・R の整数を超える数）（REPRODUCIBILITY.md 2 節）---
@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("seed: '7'\n", '"7"'),
        ("seed: 7.5\n", "7.5"),
        ("seed: true\n", "true"),
        ("seed: null\n", "null"),
        ("seed: 2147483648\n", "2147483648"),
    ],
    ids=["text", "fraction", "logical", "null", "too-large"],
)
def test_config_load_stops_on_a_seed_that_is_not_an_integer(
    tmp_path: Path, text: str, shown: str
) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(
        ValueError, match=re.escape(f"の seed は整数にしてください（いまは {shown}。")
    ):
        repro.config_load(path)


# --- 始めた時点で status=running の記録が R と同じ欄の順で残り、終わると上書きされる。無い成果物とフォルダは警告して外す（REPRODUCIBILITY.md 2 節）---
def test_run_leaves_log_and_records(repro_project: Path, recwarn: pytest.WarningsRecorder) -> None:
    state = repro.run(_cfg(seed=7))
    running = _record(repro_project, state)
    assert list(running) == _RECORD_FIELDS
    assert (running["status"], running["finished_at"], running["artifacts"]) == (
        "running",
        None,
        [],
    )
    assert running["environment"]["python_version"].startswith(platform.python_version())
    artifact = repro_project / "outputs/table.csv"
    artifact.write_text("x\n1\n", encoding="utf-8")
    state.artifacts = [artifact, repro_project / "outputs/missing.csv", "outputs/tables"]
    state.ledger = {"n_rows": 1}
    repro.finish(state)
    assert any(
        "記録に載せません" in str(caught.message)
        and "outputs/missing.csv・outputs/tables" in str(caught.message)
        for caught in recwarn
    )
    assert any("実行 ID が付いていません" in str(caught.message) for caught in recwarn)

    run_dir = repro_project / repro.REPRO_DIR / state.run_id
    assert [path.name for path in run_dir.parent.iterdir()] == [state.run_id]
    names = sorted(path.name for path in run_dir.iterdir())
    assert names == [
        f"{state.run_id}.config.yaml",
        f"{state.run_id}.log",
        f"{state.run_id}_run.json",
    ]
    run = _record(repro_project, state)
    assert list(run) == _RECORD_FIELDS
    assert (run["schema_version"], run["status"], run["seed"], run["experiment_name"]) == (
        1,
        "success",
        7,
        "unit",
    )
    assert run["artifacts"] == [
        {"path": "outputs/table.csv", "sha256": hashlib.sha256(b"x\n1\n").hexdigest(), "bytes": 4}
    ]
    assert (run["inputs"], run["ledger"]) == ([], {"n_rows": 1})
    assert run["log"] == f"{repro.REPRO_DIR}/{state.run_id}/{state.run_id}.log"
    log_text = (repro_project / run["log"]).read_text(encoding="utf-8")
    assert "finish: status=success artifacts=1 left_out=2" in log_text
    assert "記録に載せません" in log_text
    assert repro._current() is None


# --- 記録のある run_id は末尾を引き直す。REPRO_RUN_ID はそのまま使い、/ を含むか記録が既にあれば止める。experiment_name は 1 つの名前でなければ止める（REPRODUCIBILITY.md 2 節）---
def test_run_id_and_experiment_name(repro_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repro, "datetime", _FrozenDatetime)
    draws = iter([1111, 1111, 2222])
    monkeypatch.setattr(repro.secrets, "randbelow", lambda _: next(draws))
    run_ids = [repro.finish(repro.run(_cfg())).run_id for _ in range(2)]
    assert run_ids == ["20260926_093000_unit_1111", "20260926_093000_unit_2222"]
    repro_dir = repro_project / repro.REPRO_DIR
    monkeypatch.setenv("REPRO_RUN_ID", "20260926_093000_given_0000")
    assert repro.finish(repro.run(_cfg())).run_id == "20260926_093000_given_0000"
    with pytest.raises(RuntimeError, match="の記録が既にあります"):
        repro.run(_cfg())
    assert repro._current() is None and len(list(repro_dir.glob("*/*_run.json"))) == 3
    monkeypatch.setenv("REPRO_RUN_ID", "a/b")
    with pytest.raises(ValueError, match="REPRO_RUN_ID は、記録のファイル名の頭に使うので"):
        repro.run(_cfg())
    monkeypatch.delenv("REPRO_RUN_ID")
    for name in ("../unit", "", None, ["a", "b"]):
        with pytest.raises(ValueError, match="/ を含まない 1 つの名前"):
            repro.run(_cfg(experiment_name=name))


# --- run_id と記録の時刻は、時刻帯 TZ（無ければ Asia/Tokyo）で、R と同じ書式（秒まで・時差にコロンなし）で付く（REPRODUCIBILITY.md 2 節）---
@pytest.mark.parametrize(
    ("tz", "clock", "offset"), [(None, "09:30", "+0900"), ("UTC", "00:30", "+0000")]
)
def test_times_follow_container_time_zone(
    repro_project: Path, monkeypatch: pytest.MonkeyPatch, tz: str | None, clock: str, offset: str
) -> None:
    if tz is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", tz)
    monkeypatch.setattr(repro, "datetime", _FrozenDatetime)
    state = repro.finish(repro.run(_cfg()))
    assert state.run_id.startswith(f"20260926_{clock.replace(':', '')}00_unit_")
    run = _record(repro_project, state)
    assert run["started_at"] == run["finished_at"] == f"2026-09-26T{clock}:00{offset}"
    stamp = _FROZEN_AT.timestamp()
    assert time.strftime("%Y-%m-%d %H:%M", repro._run_timetuple(stamp)) == f"2026-09-26 {clock}"


# --- 時刻帯の名前でない TZ（POSIX の形など）では、記録を始めずに止める（REPRODUCIBILITY.md 2 節）---
def test_time_zone_that_is_not_a_name_stops(
    repro_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "JST-9")
    with pytest.raises(ValueError, match="は時刻帯の名前ではありません"):
        repro.run(_cfg())
    assert not (repro_project / repro.REPRO_DIR).exists()


# --- 時刻帯は .env の TZ → 環境変数 TZ → Asia/Tokyo の順で、記録の環境にも使った時刻帯が残る（REPRODUCIBILITY.md 2 節）---
def test_time_zone_prefers_env_file(repro_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "UTC")
    assert repro._run_timezone_name() == "UTC"
    env_file = repro_project / ".env"
    env_file.write_text(
        '# 注\nOMP_NUM_THREADS=1\nTZ = "America/New_York"  # 途中で変えない\n', encoding="utf-8"
    )
    assert repro._run_timezone_name() == "America/New_York"
    state = repro.finish(repro.run(_cfg()))
    assert _record(repro_project, state)["environment"]["env"]["TZ"] == "America/New_York"
    env_file.write_text("TZ=\n", encoding="utf-8")
    assert repro._run_timezone_name() == "UTC"
    monkeypatch.delenv("TZ")
    assert repro._run_timezone_name() == "Asia/Tokyo"
    env_file.write_text("TZ=JST-9\n", encoding="utf-8")
    with pytest.raises(ValueError, match="TZ=JST-9 は時刻帯の名前ではありません"):
        repro.run(_cfg())


# --- 記録を取っても、random と NumPy の乱数の並びは変わらない（REPRODUCIBILITY.md 2 節）---
def test_recording_does_not_consume_random_numbers(repro_project: Path) -> None:
    repro.set_seed(42, torch_deterministic=False)
    expected = (random.random(), np.random.random())  # noqa: NPY002
    repro.set_seed(42, torch_deterministic=False)
    repro.finish(repro.run(_cfg()))
    assert (random.random(), np.random.random()) == expected  # noqa: NPY002


# --- run() をもう一度呼ぶと（記録を切っても）、前の実行のログは閉じ、後の行は入らない（セルの再実行）（REPRODUCIBILITY.md 2 節）---
@pytest.mark.parametrize("enabled", [True, False])
def test_run_again_closes_previous_log(repro_project: Path, enabled: bool) -> None:
    first = repro.run(_cfg())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        second = repro.run(_cfg(repro={"enabled": enabled, "quiet": True}))
    repro.finish(second)
    logging.getLogger("unit").warning("後の行")
    assert first.log_path is not None and "後の行" not in first.log_path.read_text(encoding="utf-8")


# --- finish() を catch_warnings の中で呼んでも、次の実行の警告は 1 回だけ出てその実行のログにだけ入り、ロガーの出し先と水準には触れない（REPRODUCIBILITY.md 2 節）---
def test_warning_after_finish_inside_catch_warnings(
    repro_project: Path, recwarn: pytest.WarningsRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    level = logging.getLogger().level
    first = repro.run(_cfg())
    with warnings.catch_warnings():
        repro.finish(first)
    second = repro.run(_cfg())
    warnings.warn("後の警告", UserWarning, stacklevel=1)
    repro.finish(second)
    assert sum("後の警告" in str(caught.message) for caught in recwarn) == 1
    logs = [state.log_path.read_text(encoding="utf-8") for state in (first, second)]
    assert [text.count("UserWarning: 後の警告") for text in logs] == [0, 1]
    assert not caplog.records and logging.getLogger().level == level


# --- 例外で終わった実行は、status=failed と例外の文（空なら型の名前）と台帳の値が残り、トレースバックが実行ログに残る（REPRODUCIBILITY.md 2 節）---
def test_failed_run_is_recorded(repro_project: Path) -> None:
    state = repro.run(_cfg())
    state.ledger = {"n_rows": 0}
    try:
        raise RuntimeError("わざと落とした")
    except RuntimeError as exc:
        repro.fail(state, exc)
    run = _record(repro_project, state)
    assert (run["status"], run["error"], run["ledger"]) == (
        "failed",
        "わざと落とした",
        {"n_rows": 0},
    )
    log_text = (repro_project / run["log"]).read_text(encoding="utf-8")
    assert "finish: status=failed" in log_text and "Traceback" in log_text
    assert "RuntimeError: わざと落とした" in log_text
    empty = repro.run(_cfg())
    repro.fail(empty, KeyboardInterrupt())
    assert _record(repro_project, empty)["error"] == "KeyboardInterrupt"


# --- 記録を止めると何も書かず（fail() でも）、実行 ID と成果物のフォルダは作り、前の実行の状態は消す（REPRODUCIBILITY.md 2 節）---
def test_disabled_run_writes_no_record(repro_project: Path) -> None:
    repro._set_current(repro.RunState(run_id="stale", root=repro_project))
    with pytest.warns(RuntimeWarning, match="repro.enabled=false のため"):
        state = repro.run(_cfg(repro={"enabled": False, "quiet": True}))
    assert repro._current() is state and not state.enabled
    assert re.fullmatch(r"[0-9]{8}_[0-9]{6}_unit_[0-9]{4}", state.run_id)
    repro.fail(state, "落ちた")
    assert repro._current() is None
    assert all((repro_project / directory).is_dir() for directory in repro.OUTPUT_DIRS)
    assert not (repro_project / "outputs/repro").exists()


# --- 読んだファイル・データの別・既定値まで含めた設定が記録に残る（無いファイルは警告して null で残し、フォルダは何も書かずに止める）（REPRODUCIBILITY.md 2 節）---
def test_inputs_data_mode_and_config_are_recorded(repro_project: Path) -> None:
    source = repro_project / "data/raw/input.csv"
    source.write_text("a\n1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="data/raw はフォルダです"):
        repro.run(_cfg(), inputs=[source, "data/raw"])
    assert repro._current() is None and not (repro_project / "outputs").exists()
    cfg = _cfg(名前="値", items=[1, 2])
    cfg["data"]["mode"] = "real"
    with pytest.warns(RuntimeWarning, match="無いファイルがあります（SHA-256 は null"):
        state = repro.run(cfg, inputs=["data/raw/missing.csv", source])
    repro.finish(state)
    run = _record(repro_project, state)
    assert run["inputs"] == [
        {"path": "data/raw/missing.csv", "sha256": None, "bytes": None},
        {"path": "data/raw/input.csv", "sha256": hashlib.sha256(b"a\n1\n").hexdigest(), "bytes": 4},
    ]
    config = f"{repro.REPRO_DIR}/{state.run_id}/{state.run_id}.config.yaml"
    assert (run["data_mode"], run["config"]) == ("real", config)
    assert repro.read_yaml(repro_project / run["config"]) == cfg


# --- ルートの外の入力は、ルートから ../ で上る相対パスで残す（Mac のパスを記録に残さない）（REPRODUCIBILITY.md 2 節）---
def test_inputs_outside_root_go_up_from_root(
    repro_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = repro_project / "child"
    (child / "src").mkdir(parents=True)
    (child / "src/repro.py").touch()
    monkeypatch.setenv("REPRO_ROOT", str(child))
    shared = repro_project / "contracts/rules.tsv"
    shared.parent.mkdir()
    shared.write_text("a\n", encoding="utf-8")
    state = repro.run(_cfg(), inputs=["../contracts/rules.tsv", shared])
    repro.finish(state)
    paths = [entry["path"] for entry in _record(child, state)["inputs"]]
    assert paths == ["../contracts/rules.tsv", "../contracts/rules.tsv"]


# --- uv.lock と違う版が読まれるなら、何も書かずに止まる（無い・入っていない・自分自身・後ろの同じ名前は見ない）（REPRODUCIBILITY.md 2 節）---
def test_lock_versions_are_checked_before_recording(
    repro_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    numpy_version = repro.metadata.version("numpy")
    entry = '[[package]]\nname = "{}"\nversion = "{}"\nsource = {{ {} = "x" }}\n\n'
    lock = repro_project / "uv.lock"
    lock.write_text(
        entry.format("NumPy", numpy_version, "registry")
        + entry.format("numpy", "0.0.1", "registry")
        + entry.format("unit", "0.0.0", "editable")
        + entry.format("not-installed-anywhere", "1.0", "registry"),
        encoding="utf-8",
    )
    state = repro.finish(repro.run(_cfg()))
    assert _record(repro_project, state)["environment"]["uv_lock_sha256"] == repro._hash_file(lock)
    lock.write_text(entry.format("numpy", "0.0.1", "registry"), encoding="utf-8")
    shutil.rmtree(repro_project / "outputs")
    with pytest.raises(RuntimeError, match=r"numpy: uv\.lock は 0\.0\.1、読まれるのは"):
        repro.run(_cfg())
    assert repro._current() is None and not (repro_project / "outputs").exists()
    shadow = repro_project / "shadow/numpy-0.0.1.dist-info"
    shadow.mkdir(parents=True)
    (shadow / "METADATA").write_text("Name: numpy\nVersion: 0.0.1\n", encoding="utf-8")
    lock.write_text(entry.format("numpy", numpy_version, "registry"), encoding="utf-8")
    monkeypatch.setattr(sys, "path", [*sys.path, str(shadow.parent)])
    repro.finish(repro.run(_cfg()))
    monkeypatch.setattr(sys, "path", [str(shadow.parent), *sys.path])
    with pytest.raises(RuntimeError, match=r"読まれるのは 0\.0\.1（.*shadow）"):
        repro.run(_cfg())


# --- 失敗した実行の、その実行 ID の途中の成果物は partial/ に移る（REPRODUCIBILITY.md 2 節）---
def test_partial_outputs_of_a_failed_run_move_under_its_record(repro_project: Path) -> None:
    run_id = "20260101_000000_unit_0001"
    made = [f"tables/{run_id}_t.csv", f"figures/{run_id}/a.png"]
    for name in [*made, "tables/other.csv"]:
        (repro_project / "outputs" / name).parent.mkdir(parents=True, exist_ok=True)
        (repro_project / "outputs" / name).write_text("x", encoding="utf-8")
    assert repro._move_partial(repro_project, run_id) == 2
    partial = repro_project / repro.REPRO_DIR / run_id / "partial"
    assert all((partial / name).exists() for name in made)
    assert (repro_project / "outputs/tables/other.csv").exists()


# --- 実行台帳: R と同じ列と書き方で全件を runs.csv に書き、絞った行を返す。台帳の値は長さ 1 のものだけで、共通の列を上書きできない（REPRODUCIBILITY.md 3 節）---
def test_runs_ledger_lists_every_run(
    repro_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = iter(f"2026010{n}" for n in range(1, 10))
    monkeypatch.setattr(
        repro, "_timestamp", lambda fmt=repro.TIMESTAMP_FORMAT: next(clock) if "%z" in fmt else "t"
    )
    first = repro.run(_cfg(experiment_name="a", seed=1))
    first.ledger = {"n_rows": 5, "status": "上書きしない", "table": [1], "none": None, "rate": 0.5}
    repro.finish(first)
    second = repro.run(_cfg(experiment_name="b"))
    repro.fail(second, '落ちた "理由"')
    third = repro.run(_cfg(experiment_name="b"))
    repro._set_current(None)
    record = repro_project / repro.REPRO_DIR / first.run_id / f"{first.run_id}_run.json"
    (repro_project / repro.REPRO_DIR / "meta").mkdir()
    shutil.copy2(record, repro_project / repro.REPRO_DIR / "meta")

    shown = repro.runs(experiment="a")
    assert [row["run_id"] for row in shown] == [first.run_id]
    assert (shown[0]["status"], shown[0]["n_rows"], shown[0]["rate"]) == ("success", 5, 0.5)
    assert {"table", "none"}.isdisjoint(shown[0])
    raw = (repro_project / repro.RUNS_CSV).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    lines = raw[3:].decode("utf-8").splitlines()
    header = ",".join(f'"{name}"' for name in [*repro.RUNS_COLUMNS, "n_rows", "rate"])
    assert lines[0] == header
    assert lines[1] == f'"{third.run_id}","running","b","dummy",42,"20260105",NA,"",NA,0,0,NA,NA,NA'
    assert lines[2] == (
        f'"{second.run_id}","failed","b","dummy",42,"20260103","20260104","",NA,0,0,'
        '"落ちた ""理由""",NA,NA'
    )
    assert lines[3].endswith(',"",NA,0,0,NA,5,0.5') and len(lines) == 4
    repro.runs(experiment="b", show=True)
    out = capsys.readouterr().out
    assert "status = running）が 1 件" in out and "落ちた" in out
    repro.runs(experiment="z", show=True)
    assert "実験名 z の実行記録はありません" in capsys.readouterr().out
    repro.runs(limit="1")
    assert "run_id" not in capsys.readouterr().out


# --- 台帳は新しい順で、コミットは 12 字、git の外では空の文字。壊れた記録で止める（REPRODUCIBILITY.md 3 節）---
def test_runs_are_newest_first_and_stop_on_a_broken_record(repro_project: Path) -> None:
    old = repro.finish(repro.run(_cfg()))
    new = repro.finish(repro.run(_cfg()))
    path = repro_project / repro.REPRO_DIR / old.run_id / f"{old.run_id}_run.json"
    record = _read_json(path)
    record["started_at"] = "2000-01-01T00:00:00+0900"
    record["git_commit"] = "a" * 40
    path.write_text(json.dumps(record), encoding="utf-8")
    shown = repro.runs()
    assert [row["run_id"] for row in shown] == [new.run_id, old.run_id]
    assert (shown[0]["git_commit"], shown[1]["git_commit"]) == ("", "a" * 12)
    broken = repro_project / repro.REPRO_DIR / "broken"
    broken.mkdir()
    (broken / "broken_run.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="broken_run.json を読めません"):
        repro.runs()


# --- CSV は R の repro_write_csv() と同じ書き方（BOM・LF・文字は " で囲む・欠けた値は NA・論理値は TRUE と FALSE・数は 15 桁で短い方）（REPRODUCIBILITY.md 4 節）---
def test_write_csv_writes_like_r(tmp_path: Path) -> None:
    path = tmp_path / "t.csv"
    rows = [
        {"name": "a,b", "n": 1, "x": 0.1, "flag": True, "mixed": 1},
        {"name": None, "n": None, "x": float("nan"), "flag": False, "mixed": "y"},
        {"name": "c", "n": 3, "x": 100000.0, "flag": None, "mixed": True},
    ]
    repro.write_csv(rows, path)
    assert path.read_bytes().decode("utf-8-sig") == (
        '"name","n","x","flag","mixed"\n'
        '"a,b",1,0.1,TRUE,"1"\n'
        'NA,NA,NA,FALSE,"y"\n'
        '"c",3,1e+05,NA,"TRUE"\n'
    )
    numbers = [1 / 3, 123456.0, 1e-20, 0.0001, 0.001, -0.5, 1e15, 0.0, 2.5]
    repro.write_csv([{"v": value} for value in numbers], path)
    assert path.read_bytes().decode("utf-8-sig").splitlines()[1:] == [
        "0.333333333333333",
        "123456",
        "1e-20",
        "1e-04",
        "0.001",
        "-0.5",
        "1e+15",
        "0",
        "2.5",
    ]
    pandas = pytest.importorskip("pandas")
    frame = pandas.DataFrame({"a": [1, 2], "b": ["x", None], "c": [1.5, float("nan")]})
    repro.write_csv(frame, path)
    assert path.read_bytes().decode("utf-8-sig") == '"a","b","c"\n1,"x",1.5\n2,NA,NA\n'


# --- quiet でなければ、始めと終わりに実行 ID などを出す（REPRODUCIBILITY.md 2 節）---
def test_run_and_finish_print_unless_quiet(
    repro_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _cfg(seed=7)
    cfg["repro"]["quiet"] = False
    state = repro.finish(repro.run(cfg))
    out = capsys.readouterr().out
    assert f"[repro] run_id={state.run_id} data_mode=dummy seed=7" in out
    assert f"[repro] run_id      : {state.run_id} (success)" in out
    assert "[repro] 成果物       : 0 件" in out


# --- イメージの中では、ビルドの記録を取り込む（REPRODUCIBILITY.md 2 節）---
def test_environment_snapshot_includes_build_info(repro_project: Path) -> None:
    if not repro.BUILD_INFO_PATH.is_file():
        pytest.skip("イメージの外（ビルドの記録が無い）")
    expected = json.loads(repro.BUILD_INFO_PATH.read_text(encoding="utf-8"))
    assert repro._environment_snapshot(repro_project)["build_info"] == expected


# --- smoke() は、読むファイル・BOM 付きの成果物・台帳の値を、R と同じ欄の順の 1 つの記録に残す（REPRODUCIBILITY.md 2 節）---
def test_smoke_leaves_one_record(repro_project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repro.smoke()
    (record,) = (repro_project / repro.REPRO_DIR).glob("*/*_run.json")
    run = _read_json(record)
    assert list(run) == _RECORD_FIELDS
    assert (run["status"], run["experiment_name"], run["data_mode"]) == (
        "success",
        "smoke",
        "synthetic",
    )
    assert (len(run["inputs"]), len(run["artifacts"]), run["ledger"]) == (1, 1, {"n_rows": 5})
    (table,) = (repro_project / "outputs/tables").glob("*_smoke.csv")
    assert table.read_bytes()[:3] == b"\xef\xbb\xbf"
    assert f"成果物:\n  outputs/tables/{table.name}" in capsys.readouterr().out


# --- render() は REPRO_RUN_ID を読まずに新しい実行 ID を作って渡し、引数を -P で渡す。success で終えた後の失敗も failed にして途中の成果物を移し、移すものが無ければ変えない（REPRODUCIBILITY.md 2 節）---
@pytest.mark.parametrize("ending", ["finish", "fail", "none"])
def test_failed_render_marks_the_record(
    repro_project: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    notebook = repro_project / "notebooks/n.qmd"
    notebook.parent.mkdir()
    notebook.write_text("x", encoding="utf-8")
    monkeypatch.setenv("REPRO_RUN_ID", "given")
    real_run = subprocess.run
    seen: dict[str, Any] = {}

    # --- quarto の代わりに、ノートブックの中の記録だけを書いて失敗を返す（REPRODUCIBILITY.md 2 節）---
    def fake_run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        if command[0] != "quarto":
            return real_run(command, *args, **kwargs)
        seen["command"] = command
        monkeypatch.setenv("REPRO_RUN_ID", kwargs["env"]["REPRO_RUN_ID"])
        if ending != "none":
            state = repro.run(_cfg())
            if ending == "finish":
                partial = repro_project / "outputs/tables" / f"{state.run_id}_t.csv"
                partial.write_text("x", encoding="utf-8")
                repro.finish(state)
            else:
                repro.fail(state, "落ちた")
            seen["path"] = (
                repro_project / repro.REPRO_DIR / state.run_id / f"{state.run_id}_run.json"
            )
            seen["before"] = _read_json(seen["path"])
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(repro.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="ノートブックの実行か描画に失敗しました"):
        repro.render(notebook, params=["n:10", "label:a b"])
    assert seen["command"][-4:] == ["-P", "n:10", "-P", "label:a b"]
    if ending == "none":
        assert not (repro_project / repro.REPRO_DIR).exists()
        return
    assert re.fullmatch(r"[0-9]{8}_[0-9]{6}_n_[0-9]{4}", seen["path"].parent.name)
    run = _read_json(seen["path"])
    if ending == "fail":
        assert run == seen["before"]
        return
    assert (run["status"], run["finished_at"]) == ("failed", seen["before"]["finished_at"])
    assert run["error"].startswith("render() が失敗した（HTML は無い）")
    assert run["error"].endswith("。途中の成果物 1 件を partial/ に移した")


# --- 論理値になる語（R の yaml が読む y・n も）は、引用符が無ければキーでも値でも止め、ほかは読む（REPRODUCIBILITY.md 4 節）---
@pytest.mark.parametrize(
    "text",
    ["a: yes\n", "on: 1\n", "b:\n  - Off\n", "True: 1\n", "a:\n  false: 2\n", "a: y\n", "N: 1\n"],
    ids=["value", "key", "list", "true-key", "nested-false-key", "short-value", "short-key"],
)
def test_read_yaml_stops_on_boolean_words(tmp_path: Path, text: str) -> None:
    path = tmp_path / "x.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="引用符"):
        repro.read_yaml(path)
    path.write_text(
        "a: 'yes'\nb: true\n'true': 1\n名前: 値\ntol: 1.0e-6\nc: 'y'\nd: \"N\"\ne: yy\n",
        encoding="utf-8",
    )
    assert repro.read_yaml(path) == {
        "a": "yes",
        "b": True,
        "true": 1,
        "名前": "値",
        "tol": 1e-6,
        "c": "y",
        "d": "N",
        "e": "yy",
    }
    path.write_text("a: 1e-6\nb: '2E3'\n", encoding="utf-8")
    with pytest.warns(RuntimeWarning, match="数ではなく文字"):
        assert repro.read_yaml(path) == {"a": "1e-6", "b": "2E3"}


# --- git の外の文（親のフォルダまで・マウントの境目まで）では黙り、.git が壊れている文では警告する（REPRODUCIBILITY.md 2 節）---
@pytest.mark.parametrize(
    ("stderr", "outside"),
    [
        ("fatal: not a git repository (or any of the parent directories): .git\n", True),
        (
            "fatal: not a git repository (or any parent up to mount point /)\n"
            "Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).\n",
            True,
        ),
        ("fatal: not a git repository: /nonexistent/.git/worktrees/a\n", False),
        ("fatal: invalid gitfile format: .git\n", False),
    ],
)
def test_git_version_tells_outside_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str, outside: bool
) -> None:
    failed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr=stderr)
    monkeypatch.setattr(repro, "_git", lambda root, *args, **kwargs: failed)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert repro._git_version(tmp_path) == (None, None)
    assert [w.category for w in caught] == ([] if outside else [RuntimeWarning])


# --- git の外では黙って null。コミットの無いリポジトリは警告してコミットは null でも汚れは読む。コミット直後は git_dirty=false、汚れた作業ツリーは警告を出してログにも残し git_dirty=true（REPRODUCIBILITY.md 2 節）---
def test_git_version_is_recorded(repro_project: Path, recwarn: pytest.WarningsRecorder) -> None:
    if shutil.which("git") is None:
        pytest.skip("git が無い")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert repro._git_version(repro_project) == (None, None)
    (repro_project / ".gitignore").write_text("outputs/\n", encoding="utf-8")
    _git(repro_project, "init", "-q")
    with pytest.warns(RuntimeWarning, match="git のコミットを取得できません"):
        assert repro._git_version(repro_project) == (None, True)
    _git(repro_project, "add", "-A")
    _git(repro_project, "commit", "-q", "-m", "init")
    commit, dirty = repro._git_version(repro_project)
    assert re.fullmatch(r"[0-9a-f]{40}", commit or "") and dirty is False
    (repro_project / "draft.py").write_text("x = 1\n", encoding="utf-8")

    state = repro.finish(repro.run(_cfg()))
    run = _record(repro_project, state)
    assert (run["git_commit"], run["git_dirty"]) == (commit, True)
    log_text = (repro_project / run["log"]).read_text(encoding="utf-8")
    assert "作業ツリーにコミットしていない変更があります" in log_text
    assert any("git_dirty = true" in str(caught.message) for caught in recwarn)


# --- 持ち主の違うリポジトリ（コンテナにマウントしたものなど）でも、コミットと汚れを読む（REPRODUCIBILITY.md 2 節）---
def test_git_version_reads_a_repository_owned_by_someone_else(
    repro_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git が無い")
    _git(repro_project, "init", "-q")
    _git(repro_project, "add", "-A")
    _git(repro_project, "commit", "-q", "-m", "init")
    for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    plain = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repro_project,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},
    )
    if "dubious ownership" not in plain.stderr:
        pytest.skip("この git は、持ち主の違うリポジトリを真似られない")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        commit, dirty = repro._git_version(repro_project)
    assert re.fullmatch(r"[0-9a-f]{40}", commit or "") and dirty is False


# --- 記録の置き場所を作れなければ、解析を回す前の run() で止まる（REPRODUCIBILITY.md 2 節）---
def test_run_stops_when_record_dirs_cannot_be_made(repro_project: Path) -> None:
    (repro_project / "outputs").write_text("フォルダの代わりにファイルがある\n", encoding="utf-8")
    with pytest.raises(OSError):
        repro.run(_cfg())
    assert repro._current() is None


# --- スレッド数が 1 でなければ警告する（ログにも残す）（REPRODUCIBILITY.md 4 節）---
def test_threads_other_than_one_warn(repro_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in repro.THREAD_ENV_VARS:
        monkeypatch.setenv(name, "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        repro.finish(repro.run(_cfg()))

    monkeypatch.setenv("OMP_NUM_THREADS", "4")
    monkeypatch.delenv("MKL_NUM_THREADS")
    with pytest.warns(RuntimeWarning, match=r"OMP_NUM_THREADS=4・MKL_NUM_THREADS=\(未設定\)"):
        state = repro.run(_cfg())
    repro.finish(state)
    assert state.log_path is not None
    assert "スレッド数が 1 ではありません" in state.log_path.read_text(encoding="utf-8")


# --- 実行 ID は引数 run_id で渡せ、環境変数 REPRO_RUN_ID より先に使う。new_run_id() で前もって作れる（REPRODUCIBILITY.md 2 節）---
def test_run_id_argument_and_new_run_id(
    repro_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = repro.new_run_id("unit")
    assert re.fullmatch(r"\d{8}_\d{6}_unit_\d{4}", run_id)
    monkeypatch.setenv("REPRO_RUN_ID", "from_env_0000")
    assert repro.finish(repro.run(_cfg(), run_id=run_id)).run_id == run_id
    with pytest.raises(RuntimeError, match=f"run_id={run_id} の記録が既にあります"):
        repro.run(_cfg(), run_id=run_id)
    with pytest.raises(ValueError, match="run_id は、記録のファイル名の頭に使うので"):
        repro.run(_cfg(), run_id="a/b")
    assert repro.finish(repro.run(_cfg())).run_id == "from_env_0000"
    for name in ("", "a/b"):
        with pytest.raises(ValueError, match="/ を含まない 1 つの名前"):
            repro.new_run_id(name)


# --- run() が途中で失敗しても、開いた実行ログを閉じて進行中の状態を外し、次の run() が始められる（REPRODUCIBILITY.md 2 節）---
def test_failed_start_closes_log(repro_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in repro.THREAD_ENV_VARS:
        monkeypatch.setenv(variable, "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(RuntimeWarning, match="inputs に、無いファイルがあります"):
            repro.run(_cfg(), inputs=["data/raw/none.csv"])
    assert repro._current() is None and repro._HANDLER is None
    assert len(list((repro_project / repro.REPRO_DIR).glob("*/*.log"))) == 1
    state = repro.finish(repro.run(_cfg()))
    assert _record(repro_project, state)["status"] == "success"
